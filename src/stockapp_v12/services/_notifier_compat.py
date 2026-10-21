"""
Internal compat shim for the legacy `services.notification_service` module.

P2-boundaries (council 2026-07-09): the real notification
implementations live in ``stockapp_v12.notifiers/`` (LogNotifier,
WebhookNotifier, etc.). The legacy ``services/notification_service``
file was a thin re-export shim whose location in ``services/`` was
misleading — the module map says B7 (NotificationService) lives in
``notifiers/``, not ``services/``.

The new canonical home for the shim is this file
(``services/_notifier_compat.py``). The leading underscore signals
"internal/legacy compat" so reviewers and new code prefer
``stockapp_v12.notifiers`` directly.

The old path ``stockapp_v12.services.notification_service`` still
works (one-line re-export) for any code that hasn't migrated.
"""

import logging

from stockapp_v12.contracts.notifier import Notifier
from stockapp_v12.notifiers.log_notifier import LogNotifier as _LogNotifierImpl

logger = logging.getLogger(__name__)


# B5: _Notifier is now a thin alias for LogNotifier (same 8-method surface,
# same log-prefix format, same enabled flag). Kept for backward-compat with
# all imports of ``from stockapp_v12.services.notification_service import _Notifier``.
_Notifier = _LogNotifierImpl


notifier = _Notifier()

__all__ = ["notifier", "_Notifier", "Notifier"]
