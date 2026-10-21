"""LogNotifier — log-only Notifier implementation (B5 default backend).

This is the in-tree default backend that preserves the original
behavior of ``services.notification_service._Notifier`` (pre-B5):
every ``notify_*`` call writes a structured log line at INFO
(``[V12-NOTIFY] ...``) or WARNING (rejection only). No external
I/O, no side effects beyond the Python logging module.

Kept here (under ``v12/notifiers/``) alongside B5's Redis pubsub
and webhook backends so all delivery surfaces live in one package.
Selection happens via ``v12/notifiers/make_notifier_from_env`` which
returns this class when ``V12_NOTIFIER_BACKEND=log`` (the default).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# C3: Secrets redaction — keys matching this pattern have their values
# replaced with "***REDACTED***" before being logged.
_SENSITIVE_KEY_RE = re.compile(
    r"(pass|token|secret|api[_-]?key|credential|auth|private[_-]?key)",
    re.IGNORECASE,
)
_REDACTED_VALUE = "***REDACTED***"


def _redact_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of kwargs with sensitive values masked."""
    return {
        k: (_REDACTED_VALUE if _SENSITIVE_KEY_RE.search(k) else v)
        for k, v in kwargs.items()
    }


class LogNotifier:
    """Logging-only Notifier (default B5 backend).

    Mirrors the pre-B5 ``services.notification_service._Notifier``
    public API byte-for-byte so callers see no behavioral diff when
    the env-var falls back to ``log``.
    """

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def notify_stock_pool_updated(
        self,
        tier: str = "",
        symbols: Optional[List[str]] = None,
        source: str = "",
    ) -> None:
        if not self.enabled:
            return
        logger.info(f"[V12-NOTIFY] stock_pool tier={tier} n={len(symbols or [])} src={source}")

    def notify_order_intent(self, **kwargs) -> None:
        if not self.enabled:
            return
        logger.info(f"[V12-NOTIFY] order_intent {_redact_kwargs(kwargs)}")

    def notify_order_submitted(self, **kwargs) -> None:
        if not self.enabled:
            return
        logger.info(f"[V12-NOTIFY] order_submitted {_redact_kwargs(kwargs)}")

    def notify_order_filled(self, **kwargs) -> None:
        if not self.enabled:
            return
        logger.info(f"[V12-NOTIFY] order_filled {_redact_kwargs(kwargs)}")

    def notify_order_rejected(self, **kwargs) -> None:
        if not self.enabled:
            return
        logger.warning(f"[V12-NOTIFY] order_rejected {_redact_kwargs(kwargs)}")

    def notify_daily_review(self, symbol: str = "", action: str = "", **kwargs) -> None:
        if not self.enabled:
            return
        logger.info(f"[V12-NOTIFY] daily_review {symbol} -> {action}")

    def notify_position_state_change(
        self, symbol: str = "", old: str = "", new: str = "", **kwargs,
    ) -> None:
        if not self.enabled:
            return
        logger.info(f"[V12-NOTIFY] position {symbol}: {old} -> {new}")

    def notify(self, *args, **kwargs) -> None:
        # Generic fallback: silent no-op (mirrors pre-B5 _Notifier.notify).
        return


__all__ = ["LogNotifier"]