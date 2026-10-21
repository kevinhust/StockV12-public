"""Regression: cron-style invocation needs repo root on PYTHONPATH for scripts.*.

Day-1 NUC incident (2026-07-27): PYTHONPATH=src:scripts only →
  ModuleNotFoundError: No module named 'scripts'
when running `python scripts/shadow_trade.py` (sys.path[0] = scripts/).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _run_import(pythonpath: str) -> subprocess.CompletedProcess[str]:
    """Mimic `python scripts/foo.py`: path[0] ends up as scripts/."""
    env = os.environ.copy()
    env["PYTHONPATH"] = pythonpath
    # Don't inherit pytest's polluted path via PYTHONPATH only — clear PYTHONSAFEPATH noise
    # Strip '' / '.' so cwd does not accidentally provide repo root
    # (python -c inserts '' at path[0]; cron `python scripts/x.py` does not).
    code = (
        "import sys\n"
        "sys.path = [p for p in sys.path if p not in ('', '.')]\n"
        f"sys.path.insert(0, {str(REPO / 'scripts')!r})\n"
        "from scripts.eval_chronos_h2h_v3 import load_model\n"
        "print('ok')\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_scripts_package_fails_without_repo_root() -> None:
    """src:scripts alone must NOT resolve package scripts.* under cron path0."""
    pp = f"{REPO / 'src'}{os.pathsep}{REPO / 'scripts'}"
    result = _run_import(pp)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "scripts" in (result.stderr + result.stdout).lower()


def test_scripts_package_ok_with_repo_root() -> None:
    """Production contract: src + scripts + repo root."""
    pp = f"{REPO / 'src'}{os.pathsep}{REPO / 'scripts'}{os.pathsep}{REPO}"
    result = _run_import(pp)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok" in result.stdout


def test_source_archive_free_env_includes_repo_root() -> None:
    text = (REPO / "scripts" / "source_archive_free_env.sh").read_text(encoding="utf-8")
    assert "STOCKAPP_ROOT}/src" in text or "${STOCKAPP_ROOT}/src" in text
    assert "${STOCKAPP_ROOT}/scripts:${STOCKAPP_ROOT}" in text or (
        "${STOCKAPP_ROOT}/scripts:${STOCKAPP_ROOT}" in text.replace(" ", "")
    )
    # must not force-only src:scripts without root
    assert 'PYTHONPATH="${STOCKAPP_ROOT}/src:${STOCKAPP_ROOT}/scripts"' not in text


def test_entry_window_sources_archive_free_env() -> None:
    text = (REPO / "scripts" / "run_entry_window.sh").read_text(encoding="utf-8")
    assert "source_archive_free_env.sh" in text
    assert "/home/app/Stockapp/src" not in text  # no host-hardcoded PYTHONPATH


def test_check_cron_env_contract_script_exists_and_documents_root() -> None:
    path = REPO / "scripts" / "check_cron_env_contract.sh"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "source_archive_free_env.sh" in text
    assert "from scripts." in text or "scripts.eval_chronos" in text
    assert "--with-redis" in text
