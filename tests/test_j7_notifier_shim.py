"""Regression tests for P2-boundaries (council 2026-07-09):
services.notification_service shim must be renamed to _notifier_compat
to make the boundary explicit.

Pre-fix: services/notification_service.py was a re-export shim
sitting next to real service classes (PositionManager, DailyReview).
Misleading — module map says B7 lives in notifiers/.

Post-fix: _notifier_compat.py holds the canonical shim. The old
path notification_service.py remains as a one-line re-export.
"""

from __future__ import annotations

from pathlib import Path



def test_canonical_shim_file_exists():
    """P2: _notifier_compat.py must exist as the canonical shim home."""
    path = Path("src/stockapp_v12/services/_notifier_compat.py")
    assert path.exists(), f"P2: canonical shim file {path} missing"


def test_legacy_notification_service_is_reexport():
    """P2: services/notification_service.py must be a thin re-export."""
    path = Path("src/stockapp_v12/services/notification_service.py")
    src = path.read_text()
    # Must re-export from _notifier_compat
    assert "from stockapp_v12.services._notifier_compat import" in src, (
        "P2: notification_service.py must re-export from _notifier_compat"
    )


def test_both_import_paths_resolve_to_same_objects():
    """P2: legacy path and canonical path must yield the same notifier."""
    from stockapp_v12.services.notification_service import notifier as n1, _Notifier as c1
    from stockapp_v12.services._notifier_compat import notifier as n2, _Notifier as c2
    assert n1 is n2, "P2: notifier must be identical object via both paths"
    assert c1 is c2, "P2: _Notifier must be identical class via both paths"


def test_legacy_path_emits_deprecation_warning():
    """K-cleanup: importing services.notification_service must warn.

    The shim is kept for backward compatibility, but the warning
    makes the migration visible in test output and production logs.
    The canonical home is services/_notifier_compat.py; explicit
    backend selection lives in stockapp_v12.notifiers.
    """
    import importlib
    import sys
    import warnings

    # Force a fresh import — the module may already be cached in sys.modules
    sys.modules.pop("stockapp_v12.services.notification_service", None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module("stockapp_v12.services.notification_service")

    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert deprecations, (
        "K-cleanup: legacy notification_service import must emit a "
        "DeprecationWarning so the migration is visible to stragglers"
    )
    # Warning must point to the deprecated path explicitly
    msg = str(deprecations[0].message)
    assert "notification_service" in msg
    assert "_notifier_compat" in msg, (
        f"DeprecationWarning should point callers to _notifier_compat, got: {msg!r}"
    )
