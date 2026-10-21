"""RedisPubSubNotifier — Redis pubsub Notifier backend (B5).

Publishes every ``notify_*`` call to a Redis pubsub channel as a JSON
payload. Subscribers (downstream alert dashboards, audit consumers,
slack-bridge workers) attach via ``SUBSCRIBE`` and receive a stream
of structured events without coupling to the V12 process.

Pattern (mirror ChronosBackend + LogNotifier): passive Notifier
Protocol implementation, idempotent (same call → same payload),
non-blocking (``publish`` is fire-and-forget over a short-lived
connection), failure-isolated (any Redis error is caught and
logged via the standard logger — never propagates to trading logic).

Channel layout:
  - Default: ``v12:notify`` (configurable via ``channel`` arg)
  - Payload: JSON ``{"method": "notify_order_intent", "args": {...},
    "ts": "<iso8601-utc>"}`` — ``args`` is the kwargs dict for the
    specific notify_* call (caller-supplied).

Env-var selection:
  - ``V12_NOTIFIER_BACKEND=redis`` → this backend (see
    ``v12/notifiers/make_notifier_from_env``)
  - ``V12_NOTIFIER_REDIS_URL`` (optional) → connection URL override
    (default ``redis://localhost:6379/0``)
  - ``V12_NOTIFIER_REDIS_CHANNEL`` (optional) → channel override
    (default ``v12:notify``)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class RedisPubSubNotifier:
    """Redis-pubsub-backed Notifier (B5 Redis backend).

    :param redis_client: A ``redis.Redis`` instance (injected for
        testability; production wires ``redis.Redis.from_url(...)``).
    :param channel: Pubsub channel name (default ``v12:notify``).
    :param enabled: Master switch — when ``False``, all calls are
        no-ops (mirrors LogNotifier.enabled for substitutability).
    """

    def __init__(
        self,
        redis_client: Any,
        channel: str = "v12:notify",
        enabled: bool = True,
    ) -> None:
        self.redis = redis_client
        self.channel = channel
        self.enabled = enabled

    def _publish(self, method_name: str, payload: dict) -> None:
        """Fire-and-forget publish; failures are logged, never raised."""
        if not self.enabled:
            return
        envelope = {
            "method": method_name,
            "args": payload,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.redis.publish(self.channel, json.dumps(envelope, default=str))
        except Exception as e:
            # Failure-isolated: never propagate to trading logic.
            logger.warning(f"[V12-NOTIFY-REDIS] publish failed on {self.channel}: {e}")

    def notify_stock_pool_updated(
        self,
        tier: str = "",
        symbols: Optional[List[str]] = None,
        source: str = "",
    ) -> None:
        self._publish(
            "notify_stock_pool_updated",
            {"tier": tier, "symbols": list(symbols or []), "source": source},
        )

    def notify_order_intent(self, **kwargs) -> None:
        self._publish("notify_order_intent", kwargs)

    def notify_order_submitted(self, **kwargs) -> None:
        self._publish("notify_order_submitted", kwargs)

    def notify_order_filled(self, **kwargs) -> None:
        self._publish("notify_order_filled", kwargs)

    def notify_order_rejected(self, **kwargs) -> None:
        self._publish("notify_order_rejected", kwargs)

    def notify_daily_review(self, symbol: str = "", action: str = "", **kwargs) -> None:
        self._publish("notify_daily_review", {"symbol": symbol, "action": action, **kwargs})

    def notify_position_state_change(
        self, symbol: str = "", old: str = "", new: str = "", **kwargs,
    ) -> None:
        self._publish(
            "notify_position_state_change",
            {"symbol": symbol, "old": old, "new": new, **kwargs},
        )

    def notify(self, *args, **kwargs) -> None:
        # Generic fallback: not part of the typed stream; log a debug
        # line so operators can see ad-hoc messages in journal.
        if not self.enabled:
            return
        logger.debug(f"[V12-NOTIFY-REDIS] ad-hoc notify args={args} kwargs={kwargs}")


__all__ = ["RedisPubSubNotifier"]