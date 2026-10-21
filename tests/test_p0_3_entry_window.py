"""Regression tests for P0-3 (council 2026-07-09): entry_window module.

Confirms:
  - tier2 ZSET is read (NOT actionable:today LIST)
  - market_regime:current is read from Redis (default NEUTRAL on miss)
  - orchestrator absent → log + skip (NOT silently 0 entries)
  - tier2-empty case → exit cleanly with operator-visible log
  - the production script + cron registration exist
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional


REPO_ROOT = Path(__file__).resolve().parents[1]

from stockapp_v12.pipeline.entry_window import (
    DEFAULT_REGIME,
    MARKET_REGIME_KEY,
    TIER2_ZSET_KEY,
    read_market_regime,
    read_tier2_symbols,
    run_entry_window,
)


# ─── Fakes ────────────────────────────────────────────────────────────────


class _FakeRedis:
    """Tiny in-memory fake that mimics the small ZRANGE/GET/PING surface."""

    def __init__(
        self,
        *,
        zset_members: Optional[dict] = None,
        string_keys: Optional[dict] = None,
        fail_zrange: bool = False,
        fail_get: bool = False,
    ):
        self.zset_members = zset_members or {}
        self.string_keys = string_keys or {}
        self.fail_zrange = fail_zrange
        self.fail_get = fail_get

    def ping(self) -> bool:
        return True

    def zrange(self, key, start, stop):
        if self.fail_zrange:
            raise ConnectionError("simulated redis zrange failure")
        members = self.zset_members.get(key, [])
        return list(members)[start:stop + 1 if stop != -1 else None]

    def get(self, key):
        if self.fail_get:
            raise ConnectionError("simulated redis get failure")
        return self.string_keys.get(key)


# ─── Static invariants ────────────────────────────────────────────────────


def test_tier2_key_constant():
    """P0-3 must target tier2 (NOT actionable:today — removed in hygiene-I)."""
    assert TIER2_ZSET_KEY == "watchlist:tier2"


def test_market_regime_key_constant():
    """P0-3 must read market_regime:current (NOT hardcoded BULL_LONG)."""
    assert MARKET_REGIME_KEY == "market_regime:current"


def test_default_regime_is_neutral():
    """P0-3: missing regime defaults to NEUTRAL — not BULL_LONG.

    Pre-fix the orchestrator saw ``market_regime='BULL_LONG'`` even on a
    missing Redis read, which wrongly biased it toward entries.
    """
    assert DEFAULT_REGIME == "NEUTRAL"


# ─── read_tier2_symbols ───────────────────────────────────────────────────


def test_read_tier2_symbols_returns_python_list():
    """ZRANGE result must come back as a Python list of strings."""
    fake = _FakeRedis(zset_members={TIER2_ZSET_KEY: ["AAPL", "MSFT", "NVDA"]})
    syms = read_tier2_symbols(fake)
    assert syms == ["AAPL", "MSFT", "NVDA"]


def test_read_tier2_symbols_handles_empty():
    """Empty tier2 must return [] (not raise)."""
    fake = _FakeRedis(zset_members={TIER2_ZSET_KEY: []})
    assert read_tier2_symbols(fake) == []


def test_read_tier2_symbols_log_warning_on_redis_error(caplog):
    """P0-3: a Redis ZRANGE failure must log + return [], not crash."""
    import logging
    caplog.set_level(logging.WARNING, logger="stockapp_v12.pipeline.entry_window")
    fake = _FakeRedis(fail_zrange=True)
    out = read_tier2_symbols(fake)
    assert out == []
    assert any("ZRANGE" in r.message for r in caplog.records)


# ─── read_market_regime ───────────────────────────────────────────────────


def test_read_market_regime_returns_redis_value():
    fake = _FakeRedis(string_keys={MARKET_REGIME_KEY: "BEAR_HEDGE"})
    assert read_market_regime(fake) == "BEAR_HEDGE"


def test_read_market_regime_default_when_missing(caplog):
    """Missing key → default NEUTRAL, with INFO log (not WARNING)."""
    import logging
    caplog.set_level(logging.INFO, logger="stockapp_v12.pipeline.entry_window")
    fake = _FakeRedis(string_keys={})  # empty — no key
    assert read_market_regime(fake) == "NEUTRAL"
    assert any("unset" in r.message for r in caplog.records)


def test_read_market_regime_default_when_empty_string(caplog):
    """Empty value → NEUTRAL."""
    import logging
    caplog.set_level(logging.WARNING, logger="stockapp_v12.pipeline.entry_window")
    fake = _FakeRedis(string_keys={MARKET_REGIME_KEY: "   "})
    assert read_market_regime(fake) == "NEUTRAL"


def test_read_market_regime_default_when_redis_fails(caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="stockapp_v12.pipeline.entry_window")
    fake = _FakeRedis(fail_get=True)
    assert read_market_regime(fake) == "NEUTRAL"


# ─── run_entry_window ─────────────────────────────────────────────────────


def test_run_entry_window_with_empty_tier2_logs_and_exits_cleanly(caplog):
    """If tier2 is empty the run must log + return result with 0 evaluated.

    Pre-fix, the missing-key path silently produced '0 entries per day' with
    no operator signal. Post-fix we still get 0 entries, but at least the
    noop reason is in the log.
    """
    import logging
    caplog.set_level(logging.INFO, logger="stockapp_v12.pipeline.entry_window")
    fake = _FakeRedis(
        zset_members={TIER2_ZSET_KEY: []},
        string_keys={MARKET_REGIME_KEY: "BULL_LONG"},
    )
    res = run_entry_window(fake, mode="paper_ibkr")
    assert res.tier2_size == 0
    assert res.evaluated == 0
    assert res.buys == 0
    assert res.denies == 0
    assert res.skipped_no_orchestrator == 0
    assert res.skipped_no_bars == 0
    assert res.regime == "BULL_LONG"
    assert any("tier2 is empty" in r.message for r in caplog.records)


def test_run_entry_window_without_orchestrator_logs_only(caplog):
    """P0-3: orchestrator=None must NOT silently 0 — must log + count skip."""
    import logging
    caplog.set_level(logging.INFO, logger="stockapp_v12.pipeline.entry_window")
    fake = _FakeRedis(
        zset_members={TIER2_ZSET_KEY: ["AAPL", "MSFT"]},
        string_keys={MARKET_REGIME_KEY: "BULL_LONG"},
    )
    res = run_entry_window(fake, mode="paper_ibkr", orch_factory=None)
    assert res.tier2_size == 2
    assert res.evaluated == 0  # no orchestrator → no eval
    assert res.skipped_no_orchestrator == 2  # both candidates counted as skip
    assert any("candidate AAPL" in r.message for r in caplog.records)
    assert any("candidate MSFT" in r.message for r in caplog.records)


def test_run_entry_window_no_bars_skips_dispatch(caplog):
    """With orchestrator + no bars, the symbol is skipped but the count
    is preserved so the operator sees \"skipped_no_bars>0\" in the log.
    """
    import logging
    caplog.set_level(logging.INFO, logger="stockapp_v12.pipeline.entry_window")
    fake = _FakeRedis(
        zset_members={TIER2_ZSET_KEY: ["AAPL"]},
        string_keys={MARKET_REGIME_KEY: "BULL_LONG"},
    )

    class _O:
        def evaluate_and_maybe_enter(self, ctx):
            raise AssertionError("should not be called without bars")

    res = run_entry_window(
        fake,
        mode="paper_ibkr",
        bars_provider=lambda s: None,
        orch_factory=lambda **kw: _O(),
    )
    assert res.tier2_size == 1
    assert res.evaluated == 0
    assert res.skipped_no_bars == 1


def test_run_entry_window_dispatches_per_symbol():
    """orchestrator present + bars present → evaluate_and_maybe_enter fires."""
    fake = _FakeRedis(
        zset_members={TIER2_ZSET_KEY: ["AAPL", "MSFT"]},
        string_keys={MARKET_REGIME_KEY: "BULL_LONG"},
    )

    calls = []

    class _O:
        def evaluate_and_maybe_enter(self, ctx):
            calls.append(ctx.symbol)
            from stockapp_v12.services.entry_orchestrator import EntryResult
            return EntryResult(decision="BUY", reason_code="OK")

    # Fake-bars: a tiny DataFrame. Importing pandas only here, in this test.
    import pandas as pd
    bars = pd.DataFrame({"close": [100.0]}, index=pd.to_datetime(["2026-07-09"]))
    res = run_entry_window(
        fake,
        mode="paper_ibkr",
        bars_provider=lambda s: bars,
        orch_factory=lambda **kw: _O(),
    )
    assert sorted(calls) == ["AAPL", "MSFT"]
    assert res.evaluated == 2
    assert res.buys == 2
    assert res.evaluated_symbols == ["AAPL", "MSFT"]


def test_run_entry_window_swallows_orchestrator_exception(caplog):
    """If the orchestrator raises for one symbol, the run continues.
    """
    import logging
    caplog.set_level(logging.WARNING, logger="stockapp_v12.pipeline.entry_window")
    fake = _FakeRedis(
        zset_members={TIER2_ZSET_KEY: ["AAPL", "MSFT"]},
        string_keys={MARKET_REGIME_KEY: "BULL_LONG"},
    )

    class _O:
        def evaluate_and_maybe_enter(self, ctx):
            if ctx.symbol == "AAPL":
                raise RuntimeError("transient orchestrator error")
            from stockapp_v12.services.entry_orchestrator import EntryResult
            return EntryResult(decision="PASS", reason_code="OK")

    import pandas as pd
    bars = pd.DataFrame({"close": [100.0]}, index=pd.to_datetime(["2026-07-09"]))
    res = run_entry_window(
        fake,
        mode="paper_ibkr",
        bars_provider=lambda s: bars,
        orch_factory=lambda **kw: _O(),
    )
    assert res.evaluated == 1  # MSFT only — AAPL raised
    assert res.passes == 1
    assert res.buys == 0
    assert any("orchestrator raised for AAPL" in r.message for r in caplog.records)


def test_run_entry_window_orch_factory_failure_path(caplog):
    """orch_factory raises → log + skip dispatch, not crash."""
    import logging
    caplog.set_level(logging.WARNING, logger="stockapp_v12.pipeline.entry_window")
    fake = _FakeRedis(
        zset_members={TIER2_ZSET_KEY: ["AAPL"]},
        string_keys={MARKET_REGIME_KEY: "BULL_LONG"},
    )

    def boom(**kw):
        raise RuntimeError("wiring blew up")

    res = run_entry_window(fake, mode="paper_ibkr", orch_factory=boom)
    assert res.tier2_size == 1
    assert res.evaluated == 0
    assert res.skipped_no_orchestrator == 1


def test_run_entry_window_regime_override():
    """regime_override bypasses Redis (test-only escape hatch)."""
    fake = _FakeRedis(
        zset_members={TIER2_ZSET_KEY: []},
        string_keys={MARKET_REGIME_KEY: "BULL_LONG"},
    )
    res = run_entry_window(fake, mode="paper_ibkr", regime_override="TEST_REGIME_X")
    assert res.regime == "TEST_REGIME_X"


# ─── Production script + cron registration ────────────────────────────────


SCRIPT = REPO_ROOT / "scripts" / "run_entry_window.sh"
CRON_INSTALLER = REPO_ROOT / "scripts" / "install_pipeline_cron.sh"


def test_entry_window_shell_script_exists_and_executable():
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"{SCRIPT} is not +x"


def test_entry_window_shell_script_invokes_python_module():
    """Cron shell invokes app.entry_cron (composition root; not pipeline CLI)."""
    src = SCRIPT.read_text()
    assert "stockapp_v12.app.entry_cron" in src


def test_entry_window_shell_script_runs_preflight():
    """The script must still pass preflight (kill switch + redis + venv)."""
    src = SCRIPT.read_text()
    assert "_preflight.sh" in src


def test_entry_window_shell_script_writes_log():
    """Pre-fix cron scripts wrote nothing — the script must produce a log file."""
    src = SCRIPT.read_text()
    assert "LOG_FILE=" in src
    assert "entry_window_" in src


def test_install_pipeline_cron_registers_entry_window_at_0935():
    """P0-3: entry_window must be wired into install_pipeline_cron.sh at 09:35.
    """
    src = CRON_INSTALLER.read_text()
    assert "run_entry_window.sh" in src
    # 09:35 ET must be one of the times.
    assert "35 9" in src, (
        "P0-3 regression: install_pipeline_cron.sh no longer schedules "
        "entry_window at 09:35 ET"
    )


def test_install_pipeline_cron_runs_during_market_hours():
    """P0-3: entry_window must also run repeatedly during the trading day."""
    src = CRON_INSTALLER.read_text()
    # The every-5-min slot covers 10-15.
    assert "*/5 10-15" in src


# ─── CLI smoke test (--dry-run) ───────────────────────────────────────────


def test_cli_dry_run_smoke(tmp_path, monkeypatch):
    """`python -m stockapp_v12.pipeline.entry_window --dry-run` must exit 0
    even when there's no Redis at all (it should report the Redis error
    as a one-line human message and exit 1; with a fake Redis on
    127.0.0.1 it would exit 0).
    """
    # We can't easily stand up a real Redis here; assert that running the
    # CLI without Redis exits non-zero and prints the expected one-liner.
    env = os.environ.copy()
    env["REDIS_HOST"] = "127.0.0.1"
    env["REDIS_PORT"] = "1"  # nothing listens on port 1
    env["PYTHONPATH"] = f"{REPO_ROOT / 'src'}:{env.get('PYTHONPATH', '')}"
    result = subprocess.run(
        [sys.executable, "-m", "stockapp_v12.pipeline.entry_window",
         "--dry-run"],
        env=env, capture_output=True, text=True, timeout=20,
    )
    # We expect either an exit-1 with a friendly error line OR an exit-0
    # (in case a stray Redis happens to answer 127.0.0.1:1, which it won't).
    assert result.returncode in (0, 1)
    if result.returncode == 1:
        assert "cannot reach Redis" in result.stdout or "cannot reach Redis" in result.stderr
