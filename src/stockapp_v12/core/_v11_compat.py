"""V11 archive compatibility — explicit bridge layer.

Post-migration (2026-07-09), V11 source lives under the dated archive
``archive/v11-legacy-2026-07-09/`` (and older snapshots under ``archive/V11/``,
``archive/src/``). These are **not** on sys.path by default.

V12 still depends on a small number of V11 modules by design (documented
bridge architecture; see ``src/stockapp_v12/bridge/gatekeeper.py``):

    V11 ``HardRiskGatekeeper`` (src.core.risk_controller) — V12 risk front door
    V11 ``lifecycle_scheduler``    (src.services.lifecycle_scheduler) — scheduler
    V11 ``LongbridgeDataService``  (src.services.longbridge_data_service) — broker API

Rather than scattering ``sys.path.insert`` calls, callers that need V11
modules call :func:`ensure_v11_archive_on_path` first. The helper is
idempotent.

Example::

    from stockapp_v12.core._v11_compat import ensure_v11_archive_on_path
    ensure_v11_archive_on_path()
    from src.core.risk_controller import HardRiskGatekeeper  # V11 archived

Search order (first that contains ``src/`` wins):
    1. archive/v11-legacy-2026-07-09   (canonical 2026-07-09 migration archive)
    2. archive/V11                     (older complete snapshot with core/)
    3. archive/src                     (partial older snapshot)

We inject the chosen **archive root** (not its ``src/``) so that the
``from src.X import Y`` form works (``src/`` resolves as a package thanks to
its subdir ``__init__.py`` files).

The path is computed relative to this file (repo root = parents[3]).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Post-migration candidates. We prefer the dated canonical archive.
_V11_CANDIDATES = [
    _REPO_ROOT / "archive" / "v11-legacy-2026-07-09",
    _REPO_ROOT / "archive" / "V11",
    _REPO_ROOT / "archive" / "src",
]

V11_ARCHIVE_ROOT: Path | None = None

# Phase 4: set STOCKAPP_DISABLE_V11_ARCHIVE=1 to refuse archive path injection
# (CI / NUC dry-run without archive). Callers must soft-fail.
_DISABLE_ENV = "STOCKAPP_DISABLE_V11_ARCHIVE"


def v11_archive_disabled() -> bool:
    """True when env forbids loading the V11 archive onto sys.path."""
    return os.getenv(_DISABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _has_useful_v11_content(c: Path) -> bool:
    """True if this archive root looks like it can satisfy V11 imports."""
    src = c / "src"
    if not src.exists():
        return False
    # Either the scheduler/longbridge (services) or the risk gatekeeper (core)
    return (
        (src / "services" / "lifecycle_scheduler.py").exists()
        or (src / "services" / "longbridge_data_service.py").exists()
        or (src / "core" / "risk_controller.py").exists()
    )


def ensure_v11_archive_on_path() -> bool:
    """Ensure usable V11 archive root(s) are on sys.path. Idempotent.

    We may add more than one root (e.g. v11-legacy has services/,
    V11 has core/risk_controller). Later insertions win for name resolution
    if there is overlap, but we insert richer/complete archives first.

    Phase 4: if ``STOCKAPP_DISABLE_V11_ARCHIVE`` is truthy, returns False
    without mutating ``sys.path`` (production path toward archive-free NUC).

    Returns
    -------
    bool
        True if at least one useful archive root is now on sys.path.
    """
    global V11_ARCHIVE_ROOT

    if v11_archive_disabled():
        V11_ARCHIVE_ROOT = None
        return False

    useful = [c for c in _V11_CANDIDATES if _has_useful_v11_content(c)]

    if not useful:
        return False

    # Insert in reverse so the first in the list ends up earliest in sys.path
    for c in reversed(useful):
        p = str(c)
        if p not in sys.path:
            sys.path.insert(0, p)

    # Record the preferred (first) one for introspection
    V11_ARCHIVE_ROOT = useful[0]
    return True
