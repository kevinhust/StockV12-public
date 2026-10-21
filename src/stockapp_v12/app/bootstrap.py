"""Path bootstrap for CLI scripts — single place for PYTHONPATH folklore.

Phase 5: scripts should prefer ``pip install -e .`` or env PYTHONPATH.
When neither is set, call :func:`ensure_src_on_path` once at process start.
Never adds ``archive/``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # .../Stockapp
_SRC = _REPO_ROOT / "src"
_SCRIPTS = _REPO_ROOT / "scripts"


def repo_root() -> Path:
    return _REPO_ROOT


def ensure_src_on_path() -> Path:
    """Put ``src/`` (and ``scripts/``) on ``sys.path`` if missing. Idempotent.

    Returns the repo root path.
    """
    src = str(_SRC)
    scripts = str(_SCRIPTS)
    # Prefer src first for stockapp_v12 package
    if src not in sys.path:
        sys.path.insert(0, src)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    # Refuse accidental archive contamination when bootstrap runs
    cleaned = [p for p in sys.path if "archive/v11" not in p and "/archive/" not in p]
    if len(cleaned) != len(sys.path):
        sys.path[:] = cleaned
    return _REPO_ROOT


__all__ = ["ensure_src_on_path", "repo_root"]
