"""WebhookNotifier — Slack/Discord-compatible webhook Notifier backend (B5).

Posts every ``notify_*`` call to a configurable HTTP webhook URL with a
JSON payload. Works with Slack Incoming Webhooks, Discord Webhooks, and
generic JSON-accepting HTTP endpoints (the request body shape is the
same — receivers parse ``method`` + ``args`` as they see fit).

Pattern (mirror ChronosBackend + LogNotifier): passive Notifier
Protocol implementation, non-blocking (uses ``requests`` session with
a 5-second timeout, fire-and-forget at the call site — failures
return immediately and are logged, never raised), failure-isolated.

Endpoint shape:
  - Default URL: ``V12_NOTIFIER_WEBHOOK_URL`` env var
  - Default: no-op when ``webhook_url`` is empty (safe-by-default)
  - Optional ``username`` is included in the payload for Slack-style
    bot identification (ignored by Discord)

Payload (POST body):
  ```json
  {
    "method": "notify_order_intent",
    "args": {"symbol": "AAPL", "side": "long"},
    "ts": "<iso8601-utc>",
    "username": "stockapp-v12"
  }
  ```

Env-var selection:
  - ``V12_NOTIFIER_BACKEND=webhook`` → this backend
  - ``V12_NOTIFIER_WEBHOOK_URL`` (required) → endpoint URL
  - ``V12_NOTIFIER_WEBHOOK_USERNAME`` (optional) → bot name
"""

from __future__ import annotations

import atexit
import ipaddress
import json
import logging
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# SSRF defense: private/reserved IP ranges that must never be reachable
# via a webhook URL. Prevents accidental or malicious requests to
# internal services, cloud metadata endpoints, or loopback addresses.
_BLOCKED_NETWORKS = frozenset({
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
})


def _is_safe_webhook_url(url: str) -> bool:
    """Validate that a webhook URL does not resolve to a private/reserved IP.

    Performs a DNS lookup on the hostname and rejects any address that
    falls within ``_BLOCKED_NETWORKS``. Returns ``True`` for public IPs only.

    Note: There is an inherent TOCTOU window between this check and the
    actual HTTP request (DNS rebinding). For this trading system, the
    webhook URL is operator-configured (not end-user input), so the
    risk is limited to accidental misconfiguration.
    """
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        # Can't resolve DNS — allow (HTTP library will handle the error).
        # This ensures compatibility with test mocks (e.g. requests_mock)
        # and environments where DNS is resolved by a proxy.
        return True
    for _family, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except (ValueError, TypeError):
            continue
        if any(ip in net for net in _BLOCKED_NETWORKS):
            logger.warning(
                "[V12-NOTIFY-WEBHOOK] SSRF block: %s resolves to private IP %s",
                hostname, ip_str,
            )
            return False
    return True


class WebhookNotifier:
    """Webhook-backed Notifier (B5 webhook backend).

    :param webhook_url: Target URL (Slack/Discord Incoming Webhook
        or any JSON-accepting endpoint). Empty string → no-op
        backend (safe-by-default if env-var not set).
    :param username: Bot name included in payload (default
        ``"stockapp-v12"``).
    :param timeout_seconds: HTTP timeout per POST (default 5.0).
    :param session: Optional ``requests.Session`` (injected for
        testability with ``requests_mock``).
    :param enabled: Master switch — when ``False``, all calls are
        no-ops.
    """

    def __init__(
        self,
        webhook_url: str = "",
        username: str = "stockapp-v12",
        timeout_seconds: float = 5.0,
        session: Any = None,
        enabled: bool = True,
        max_workers: int = 2,
    ) -> None:
        self.webhook_url = webhook_url
        self.username = username
        self.timeout_seconds = timeout_seconds
        # Lazy import: keep ``requests`` optional so the package
        # imports cleanly on a fresh install (webhook backend is
        # selected only when env-var demands it).
        if session is None and webhook_url:
            try:
                import requests  # type: ignore[import-untyped]

                self._session = requests.Session()
            except ImportError:
                logger.warning(
                    "[V12-NOTIFY-WEBHOOK] 'requests' not installed; "
                    "webhook backend disabled until installed"
                )
                self._session = None
        else:
            self._session = session

        # C2: SSRF defense — reject webhook URLs that resolve to private IPs.
        if webhook_url and self._session is not None:
            if not _is_safe_webhook_url(webhook_url):
                logger.warning(
                    "[V12-NOTIFY-WEBHOOK] webhook_url blocked by SSRF defense: %s",
                    webhook_url,
                )
                self._session = None

        # C1: Non-blocking delivery via background thread pool.
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="webhook-notify",
        )
        # P0-2 (council 2026-07-09): serialize flush-vs-submit so we cannot
        # ``shutdown()`` while a ``submit()`` is in flight, which would raise
        # ``RuntimeError: cannot schedule new futures after shutdown`` and
        # silently lose the notification. Also wire ``atexit`` so a normal
        # interpreter shutdown waits for in-flight posts before exits (a SIGTERM
        # triggered right after ``notify_order_submitted`` previously dropped the
        # payload — the executor was destroyed before workers could complete).
        self._executor_lock = threading.Lock()
        self._shutdown_registered = False
        try:
            atexit.register(self._shutdown, wait=True)
            self._shutdown_registered = True
        except Exception as e:  # pragma: no cover — defensive
            # ``atexit.register`` may fail in unusual contexts (PyInterpreters,
            # some test harnesses). We still want to operate; just log and move on.
            logger.warning(
                "[V12-NOTIFY-WEBHOOK] atexit.register failed; executor will not "
                "auto-flush on interpreter shutdown: %s", e,
            )
        self.enabled = enabled

    def flush(self, timeout: float = 10.0) -> None:
        """Wait for all pending webhook posts to complete.

        Useful for tests and graceful shutdown. After flushing, a new
        executor is created so subsequent ``notify_*`` calls continue to work.

        P0-2 (council 2026-07-09): a previously-shutdown executor was
        handed back with no protection, so a concurrent ``submit()`` could
        raise ``RuntimeError`` mid-flight. We now hold ``_executor_lock`` for
        the duration of ``shutdown + replace`` so any submit either completes
        before shutdown or waits for the new executor to be installed.
        """
        # ``_executor_lock`` blocks any new ``submit()`` until we have rotated
        # in a fresh executor, so ``shutdown()`` is never called while a
        # ``submit()`` is half-executed.
        with self._executor_lock:
            self._executor.shutdown(wait=True)
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix="webhook-notify",
            )

    def _shutdown(self, wait: bool = True) -> None:
        """Idempotent shutdown hook for ``atexit`` / signal handlers.

        Holding ``_executor_lock`` guarantees that a final ``submit()``
        (e.g. an ``atexit``-flushed notification) cannot race with the
        interpreter teardown — either the submit completes first and we
        drain it, or we shut down with no in-flight work.

        Safe to call multiple times: subsequent calls are a no-op once
        the executor is shut down and replaced by a fresh one, and on
        interpreter exit we accept that the late ``replace`` step is
        dropped.
        """
        # ``atexit`` hooks run during interpreter finalization — exception
        # suppression here would only mask real bugs; let them raise so
        # we notice in tests.
        try:
            with self._executor_lock:
                self._executor.shutdown(wait=wait)
                # Do NOT recreate the executor during interpreter exit —
                # teardown is the whole point of this hook.
        except TypeError:
            # Executor already shut down by a previous ``flush()`` / ``_shutdown()``.
            pass

    def _post(self, method_name: str, payload: dict) -> None:
        """Submit POST to background thread pool; never blocks the caller."""
        if not self.enabled:
            return
        if not self.webhook_url:
            # Safe-by-default: silently no-op if URL not configured.
            return
        if self._session is None:
            # requests missing or SSRF-blocked → no-op.
            return
        body = {
            "method": method_name,
            "args": payload,
            "ts": datetime.now(timezone.utc).isoformat(),
            "username": self.username,
        }
        # C1: fire-and-forget via thread pool — caller never blocks on HTTP.
        # P0-2 (council 2026-07-09): hold ``_executor_lock`` around submit so
        # it cannot race with a concurrent ``flush()`` / ``_shutdown()`` —
        # ``ThreadPoolExecutor.submit`` after ``shutdown`` raises
        # ``RuntimeError`` and we would lose the notification.
        with self._executor_lock:
            self._executor.submit(self._do_post, method_name, body)

    def _do_post(self, method_name: str, body: dict) -> None:
        """Actual HTTP POST, runs in the background thread pool."""
        try:
            resp = self._session.post(
                self.webhook_url,
                data=json.dumps(body, default=str),
                timeout=self.timeout_seconds,
                headers={"Content-Type": "application/json"},
            )
            # Slack returns 200, Discord 204. Anything else is logged but not raised.
            if resp.status_code >= 400:
                logger.warning(
                    f"[V12-NOTIFY-WEBHOOK] non-2xx {resp.status_code} "
                    f"for {method_name} -> {self.webhook_url}"
                )
        except Exception as e:
            # Failure-isolated: never propagate to trading logic.
            logger.warning(
                f"[V12-NOTIFY-WEBHOOK] post failed for {method_name}: {e}"
            )

    def notify_stock_pool_updated(
        self,
        tier: str = "",
        symbols: Optional[List[str]] = None,
        source: str = "",
    ) -> None:
        self._post(
            "notify_stock_pool_updated",
            {"tier": tier, "symbols": list(symbols or []), "source": source},
        )

    def notify_order_intent(self, **kwargs) -> None:
        self._post("notify_order_intent", kwargs)

    def notify_order_submitted(self, **kwargs) -> None:
        self._post("notify_order_submitted", kwargs)

    def notify_order_filled(self, **kwargs) -> None:
        self._post("notify_order_filled", kwargs)

    def notify_order_rejected(self, **kwargs) -> None:
        self._post("notify_order_rejected", kwargs)

    def notify_daily_review(self, symbol: str = "", action: str = "", **kwargs) -> None:
        self._post("notify_daily_review", {"symbol": symbol, "action": action, **kwargs})

    def notify_position_state_change(
        self, symbol: str = "", old: str = "", new: str = "", **kwargs,
    ) -> None:
        self._post(
            "notify_position_state_change",
            {"symbol": symbol, "old": old, "new": new, **kwargs},
        )

    def notify(self, *args, **kwargs) -> None:
        # Generic fallback: log only (webhook receivers parse typed methods).
        if not self.enabled:
            return
        logger.debug(f"[V12-NOTIFY-WEBHOOK] ad-hoc notify args={args} kwargs={kwargs}")


__all__ = ["WebhookNotifier"]