"""Legacy re-export — DEPRECATED. See ``_notifier_compat.py``.

P2-boundaries (council 2026-07-09): the compat shim moved to
``services/_notifier_compat.py``. This file remains for backward
compatibility with the old import path; new code should import from
``stockapp_v12.services._notifier_compat`` (or, for explicit backend
selection, ``stockapp_v12.notifiers``) directly.

K-cleanup (council 2026-07-09 tail): importing this module now emits a
``DeprecationWarning``. The shim is kept (one-line re-export) so any
straggler imports keep working, but the warning makes the migration
visible in test output and production logs. The shim is scheduled for
removal in a future release (target: next major version bump).
"""
import warnings as _warnings

_warnings.warn(
    "stockapp_v12.services.notification_service is deprecated; import "
    "from stockapp_v12.services._notifier_compat (singleton shim) or "
    "stockapp_v12.notifiers (backend selection) instead. The legacy "
    "shim will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

from stockapp_v12.services._notifier_compat import (  # noqa: E402,F401
    Notifier,
    _Notifier,
    notifier,
)

__all__ = ["notifier", "_Notifier", "Notifier"]
