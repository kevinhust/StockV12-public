"""Regression test for P0-5 (council 2026-07-09).

The catalyst_scorer is currently a stub (no Python module exists yet).
Downstream code (entry_window / run_entry_window.sh) reads
``watchlist:tier2`` (ZSET) as the canonical "candidates that cleared the
catalyst filter" key. Without tier2 the live entry path deadlocks.

P0-5 fix: ``scripts/run_catalyst_scorer.sh`` now writes a passthrough —
``tier2 := tier1`` (same members, score=1.0) — and emits an explicit
``L2_DEGRADED`` marker so operators can grep the logs and see we are not
actually scoring catalysts yet.

This test verifies the invariants of the shell script without invoking
real Redis. It also runs the script end-to-end against a fake
``redis-cli`` to exercise the control flow.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "run_catalyst_scorer.sh"


# ─── Static invariants ────────────────────────────────────────────────────


def test_catalyst_scorer_script_exists():
    """The stub script must still be present at scripts/run_catalyst_scorer.sh."""
    assert SCRIPT.is_file(), f"missing {SCRIPT}"


def test_catalyst_scorer_script_is_executable():
    """Cron calls this without a shell wrapper, so +x is required."""
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, f"{SCRIPT} is not executable (mode={oct(mode)})"


def test_catalyst_scorer_writes_tier2():
    """P0-5 fix: tier2 must be written, not just left empty."""
    src = SCRIPT.read_text()
    assert "ZADD watchlist:tier2" in src, (
        "P0-5 regression: catalyst_scorer no longer writes tier2; downstream "
        "stages (entry_window) will deadlock."
    )


def test_catalyst_scorer_reads_tier1_for_passthrough():
    """tier2 := tier1 — must ZRANGE tier1 first."""
    src = SCRIPT.read_text()
    assert "ZRANGE watchlist:tier1" in src
    # ZRANGE must precede ZADD tier2 to make the passthrough logical.
    assert src.index("ZRANGE watchlist:tier1") < src.index("ZADD watchlist:tier2"), (
        "P0-5 regression: ZRANGE tier1 must come before ZADD tier2 (passthrough "
        "must source from tier1)."
    )


def test_catalyst_scorer_logs_L2_DEGRADED():
    """P0-5 must emit L2_DEGRADED so operators can grep the logs."""
    src = SCRIPT.read_text()
    # at least one L2_DEGRADED line in the script body (not just comments).
    body_lines = [
        ln.strip() for ln in src.splitlines()
        if "L2_DEGRADED" in ln and not ln.strip().startswith("#")
    ]
    assert len(body_lines) >= 1, (
        "P0-5 regression: no executable L2_DEGRADED log line found in the "
        "catalyst_scorer stub."
    )


def test_catalyst_scorer_deletes_stale_tier2_before_rebuild():
    """DEL watchlist:tier2 must precede ZADD watchlist:tier2 to make rebuild
    idempotent across days (otherwise stale symbols linger)."""
    src = SCRIPT.read_text()
    assert "DEL watchlist:tier2" in src
    assert src.index("DEL watchlist:tier2") < src.index("ZADD watchlist:tier2"), (
        "P0-5 regression: tier2 must be DEL'd before being rebuilt, otherwise "
        "stale symbols from a previous run would linger."
    )


def test_catalyst_scorer_exits_zero_on_success():
    """Happy path → exit 0."""
    src = SCRIPT.read_text()
    # At least one clean exit point.
    assert "exit 0" in src


def test_catalyst_scorer_preserves_preflight():
    """Preflight must still gate the script (kill switch + redis + python
    imports). Removing it would let a broken-redis run blast a tier2."""
    src = SCRIPT.read_text()
    assert "_preflight.sh" in src


# ─── End-to-end with fake redis-cli ────────────────────────────────────────


def test_catalyst_scorer_passthrough_end_to_end(tmp_path, monkeypatch):
    """Run the production script against a fake redis. Verify:

      1. exit code 0
      2. log file contains at least one L2_DEGRADED line
      3. log file contains the 'Promoted N symbols from tier1 → tier2' line
      4. log file does NOT contain ERROR (no redis errors)
    """
    # Set up sandbox
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    redis_cli = fake_bin / "redis-cli"
    redis_cli.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        # Skip ``-h HOST -p PORT`` options so we can match the
        # command directly without parsing the connection args.
        while [ "${1:0:2}" = "-h" ] || [ "${1:0:2}" = "-p" ]; do
            [ "${1:0:2}" = "-h" ] && shift
            [ "${1:0:2}" = "-p" ] && shift
            shift
        done
        cmd="$1"; shift || true
        case "$cmd" in
            ZCARD)
                key="$1"
                [ "$key" = "watchlist:tier1" ] && echo "3"
                [ "$key" = "watchlist:tier2" ] && echo "3"
                ;;
            ZRANGE)
                echo "AAPL"; echo "1.5"
                echo "MSFT"; echo "2.0"
                echo "NVDA"; echo "0.9"
                ;;
            ZADD) echo "1" ;;
            DEL)  echo "1" ;;
            *)    exit 2 ;;
        esac
    """))
    redis_cli.chmod(0o755)

    # Override _preflight to a no-op. NB: cannot use `exit 0` because
    # when sourced, `exit` terminates the *calling* shell — the
    # catalyst_scorer.sh — instead of returning to the source call. Use
    # `: ; return 0` instead.
    prod_scripts = REPO_ROOT / "scripts"
    real_preflight = prod_scripts / "_preflight.sh"
    backup_path = tmp_path / "_preflight.sh.bak"
    if real_preflight.exists():
        shutil.copy(real_preflight, backup_path)
    real_preflight.write_text("#!/usr/bin/env bash\n: ; return 0\n")
    real_preflight.chmod(0o755)

    try:
        # Run the actual production script with our PATH prepend so our
        # fake redis-cli wins.
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
        env["LOG_DIR"] = str(tmp_path / "logs")
        env["REDIS_HOST"] = "127.0.0.1"
        env["REDIS_PORT"] = "9999"
        # Don't actually create /home/app/Stockapp/logs/pipeline on macOS;
        # we override LOG_DIR above.
        logs = tmp_path / "logs"
        logs.mkdir()
        env["LOG_DIR"] = str(logs)

        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

        # 1. Exit code 0
        assert result.returncode == 0, (
            f"catalyst_scorer exited {result.returncode}\n"
            f"  stdout={result.stdout!r}\n"
            f"  stderr={result.stderr!r}"
        )

        # Find the log file (named catalyst_scorer_YYYYMMDD.log).
        log_files = list(logs.glob("catalyst_scorer_*.log"))
        assert len(log_files) == 1, f"expected 1 log file, got {log_files}"
        log_text = log_files[0].read_text()

        # 2. L2_DEGRADED marker present
        assert "L2_DEGRADED" in log_text, (
            f"P0-5 regression: L2_DEGRADED marker missing from log\n{log_text}"
        )

        # 3. Promoted N symbols line present (must say tier1 → tier2 passthrough)
        assert "Promoted" in log_text, (
            f"P0-5 regression: passthrough promotion line missing from log\n{log_text}"
        )
        assert "from tier1" in log_text
        assert "tier2" in log_text

        # 4. No ERROR (no redis-down errors)
        assert "ERROR" not in log_text, (
            f"P0-5 regression: ERROR line in log — fake redis should succeed\n{log_text}"
        )
    finally:
        # Restore real _preflight.sh
        if backup_path.exists():
            shutil.copy(backup_path, real_preflight)
