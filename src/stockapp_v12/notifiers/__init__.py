"""V12 notifier backends (B5) — log, Redis pubsub, webhook.

Backends all satisfy the ``stockapp_v12.contracts.Notifier`` Protocol
surface. Selection happens via the ``V12_NOTIFIER_BACKEND`` env var:

  - ``log`` (default) — ``LogNotifier`` (preserves pre-B5 behavior)
  - ``redis`` — ``RedisPubSubNotifier`` (publishes JSON to Redis pubsub)
  - ``webhook`` — ``WebhookNotifier`` (POSTs JSON to Slack/Discord URL)

Caller code (``EntryOrchestrator``, ``DailyReview``, ``PositionManager``)
continues to use ``services.notification_service.notifier`` (the
``_Notifier`` instance) — that module's singleton is now a
``LogNotifier`` but with the same public API as before, so existing
imports remain compatible.

For explicit backend selection (test/dev):
  >>> from stockapp_v12.notifiers import make_notifier_from_env
  >>> n = make_notifier_from_env(backend="redis", redis_client=my_redis)
"""

from __future__ import annotations

import os
from typing import Any, Optional

from stockapp_v12.notifiers.log_notifier import LogNotifier
from stockapp_v12.notifiers.redis_pubsub_notifier import RedisPubSubNotifier
from stockapp_v12.notifiers.webhook_notifier import WebhookNotifier

NOTIFIER_BACKEND_ENV = "V12_NOTIFIER_BACKEND"
NOTIFIER_REDIS_URL_ENV = "V12_NOTIFIER_REDIS_URL"
NOTIFIER_REDIS_CHANNEL_ENV = "V12_NOTIFIER_REDIS_CHANNEL"
NOTIFIER_WEBHOOK_URL_ENV = "V12_NOTIFIER_WEBHOOK_URL"
NOTIFIER_WEBHOOK_USERNAME_ENV = "V12_NOTIFIER_WEBHOOK_USERNAME"

VALID_BACKENDS = ("log", "redis", "webhook")


def make_notifier_from_env(
    backend: Optional[str] = None,
    *,
    redis_client: Any = None,
    webhook_session: Any = None,
) -> Any:
    """Select a Notifier backend via env var (B5 surface).

    :param backend: Explicit backend override (``"log"``, ``"redis"``,
        ``"webhook"``). When ``None`` (the default), reads from
        ``V12_NOTIFIER_BACKEND`` env var, falling back to ``"log"``.
    :param redis_client: Required when ``backend="redis"`` — a
        ``redis.Redis`` instance. If ``None`` and backend is
        ``"redis"``, falls back to ``LogNotifier`` with a warning
        (fail-safe: never blow up the trading pipeline on misconfig).
    :param webhook_session: Optional ``requests.Session`` for the
        webhook backend (test injection).
    :return: A Notifier-Protocol-compliant object. Always returns
        successfully; backend selection failures degrade to
        ``LogNotifier`` + a warning log.
    """
    import logging as _logging

    selected = (backend or os.environ.get(NOTIFIER_BACKEND_ENV) or "log").lower()

    if selected == "log":
        return LogNotifier()

    if selected == "redis":
        if redis_client is None:
            _logging.getLogger(__name__).warning(
                "[V12-NOTIFY] backend=redis requested but no redis_client "
                "passed; degrading to LogNotifier (safe-by-default)"
            )
            return LogNotifier()
        channel = os.environ.get(NOTIFIER_REDIS_CHANNEL_ENV, "v12:notify")
        return RedisPubSubNotifier(redis_client=redis_client, channel=channel)

    if selected == "webhook":
        url = os.environ.get(NOTIFIER_WEBHOOK_URL_ENV, "")
        username = os.environ.get(NOTIFIER_WEBHOOK_USERNAME_ENV, "stockapp-v12")
        if not url:
            _logging.getLogger(__name__).warning(
                "[V12-NOTIFY] backend=webhook requested but "
                f"{NOTIFIER_WEBHOOK_URL_ENV} not set; degrading to "
                "LogNotifier (safe-by-default)"
            )
            return LogNotifier()
        return WebhookNotifier(
            webhook_url=url,
            username=username,
            session=webhook_session,
        )

    _logging.getLogger(__name__).warning(
        f"[V12-NOTIFY] unknown backend={selected!r} (valid: {VALID_BACKENDS}); "
        "degrading to LogNotifier (safe-by-default)"
    )
    return LogNotifier()


__all__ = [
    "LogNotifier",
    "RedisPubSubNotifier",
    "WebhookNotifier",
    "make_notifier_from_env",
    "NOTIFIER_BACKEND_ENV",
    "NOTIFIER_REDIS_URL_ENV",
    "NOTIFIER_REDIS_CHANNEL_ENV",
    "NOTIFIER_WEBHOOK_URL_ENV",
    "NOTIFIER_WEBHOOK_USERNAME_ENV",
    "VALID_BACKENDS",
]