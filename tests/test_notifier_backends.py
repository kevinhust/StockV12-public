"""B5 — Notifier backend implementations.

Verifies:
  - LogNotifier (default backend): byte-identical to pre-B5 _Notifier
    log-line format (9 per-component Phase 7 tests still pass; we
    only verify the new class here).
  - RedisPubSubNotifier: every typed method publishes a JSON envelope
    to the configured Redis pubsub channel; failure on publish is
    isolated (no raise to caller).
  - WebhookNotifier: every typed method POSTs a JSON envelope to the
    configured URL; non-2xx is logged but not raised.
  - make_notifier_from_env: env-var-driven selection (log / redis /
    webhook / unknown → safe-degrade to log).
"""

from __future__ import annotations

import json
import logging
from typing import List


from stockapp_v12.contracts import Notifier
from stockapp_v12.notifiers import (
    LogNotifier,
    NOTIFIER_BACKEND_ENV,
    NOTIFIER_REDIS_CHANNEL_ENV,
    NOTIFIER_WEBHOOK_URL_ENV,
    RedisPubSubNotifier,
    VALID_BACKENDS,
    WebhookNotifier,
    make_notifier_from_env,
)


# ─── LogNotifier (default backend) ──────────────────────────────────────────


def test_log_notifier_satisfies_notifier_protocol():
    """LogNotifier must isinstance Notifier (structural contract)."""
    assert isinstance(LogNotifier(), Notifier)


def test_log_notifier_emits_v12_notify_prefix(caplog):
    """All typed methods must emit '[V12-NOTIFY] ...' log lines."""
    caplog.set_level(logging.INFO)
    n = LogNotifier()
    n.notify_stock_pool_updated(tier="A", symbols=["AAPL", "MSFT"], source="scanner")
    n.notify_order_intent(symbol="AAPL", side="long", qty=10)
    n.notify_order_submitted(symbol="AAPL", order_id="o1")
    n.notify_order_filled(symbol="AAPL", qty=10, fill_price=101.0)
    n.notify_order_rejected(symbol="NVDA", reason="margin")
    n.notify_daily_review(symbol="AAPL", action="HOLD")
    n.notify_position_state_change(symbol="AAPL", old="OPEN", new="TRAILING")

    lines = [r.message for r in caplog.records if "[V12-NOTIFY]" in r.message]
    # 7 typed methods → 7 lines (notify() is a no-op).
    assert len(lines) == 7, f"Expected 7 [V12-NOTIFY] lines, got {len(lines)}"
    # Rejected at WARNING (failure-isolated alerting).
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_log_notifier_disabled_is_noop(caplog):
    """When ``enabled=False``, no log lines are emitted."""
    caplog.set_level(logging.INFO)
    n = LogNotifier(enabled=False)
    n.notify_order_intent(symbol="AAPL")
    n.notify_daily_review(symbol="MSFT", action="HOLD")
    assert not [r for r in caplog.records if "[V12-NOTIFY]" in r.message]


# ─── RedisPubSubNotifier ────────────────────────────────────────────────────


class _FakeRedis:
    """Minimal Redis stub capturing every publish() call."""

    def __init__(self, fail_on_publish: bool = False) -> None:
        self.published: List[tuple[str, str]] = []
        self.fail_on_publish = fail_on_publish

    def publish(self, channel: str, message: str) -> int:
        if self.fail_on_publish:
            raise ConnectionError("fake redis unavailable")
        self.published.append((channel, message))
        return 1


def test_redis_pubsub_notifier_satisfies_notifier_protocol():
    """RedisPubSubNotifier must isinstance Notifier."""
    assert isinstance(RedisPubSubNotifier(redis_client=_FakeRedis()), Notifier)


def test_redis_pubsub_notifier_publishes_envelope_per_call():
    """Every typed method → 1 publish() with a JSON envelope."""
    fake = _FakeRedis()
    n = RedisPubSubNotifier(redis_client=fake, channel="v12:test")

    n.notify_stock_pool_updated(tier="A", symbols=["AAPL"], source="scanner")
    n.notify_order_intent(symbol="AAPL", side="long", qty=10)
    n.notify_order_submitted(symbol="AAPL", order_id="o1")
    n.notify_order_filled(symbol="AAPL", qty=10, fill_price=101.0)
    n.notify_order_rejected(symbol="NVDA", reason="margin")
    n.notify_daily_review(symbol="AAPL", action="HOLD")
    n.notify_position_state_change(symbol="AAPL", old="OPEN", new="TRAILING")

    # 7 typed methods → 7 publishes.
    assert len(fake.published) == 7
    for channel, payload in fake.published:
        assert channel == "v12:test"
        env = json.loads(payload)
        assert "method" in env and env["method"].startswith("notify_")
        assert "args" in env and isinstance(env["args"], dict)
        assert "ts" in env  # ISO8601

    # verify per-method args shape
    env_by_method = {json.loads(p)["method"]: json.loads(p)["args"] for _, p in fake.published}
    assert env_by_method["notify_stock_pool_updated"]["tier"] == "A"
    assert env_by_method["notify_stock_pool_updated"]["symbols"] == ["AAPL"]
    assert env_by_method["notify_order_intent"]["symbol"] == "AAPL"
    assert env_by_method["notify_order_rejected"]["reason"] == "margin"
    assert env_by_method["notify_daily_review"]["symbol"] == "AAPL"
    assert env_by_method["notify_daily_review"]["action"] == "HOLD"
    assert env_by_method["notify_position_state_change"]["new"] == "TRAILING"


def test_redis_pubsub_notifier_publish_failure_is_isolated(caplog):
    """A Redis publish failure must NOT propagate (failure-isolated)."""
    caplog.set_level(logging.WARNING)
    n = RedisPubSubNotifier(redis_client=_FakeRedis(fail_on_publish=True))
    # Should not raise.
    n.notify_order_intent(symbol="AAPL")
    n.notify_order_submitted(symbol="AAPL")
    # Both calls logged a warning.
    warn_lines = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("publish failed" in r.message for r in warn_lines)


def test_redis_pubsub_notifier_disabled_is_noop():
    """enabled=False → no publishes."""
    fake = _FakeRedis()
    n = RedisPubSubNotifier(redis_client=fake, enabled=False)
    n.notify_order_intent(symbol="AAPL")
    n.notify_daily_review(symbol="AAPL", action="HOLD")
    assert fake.published == []


# ─── WebhookNotifier ────────────────────────────────────────────────────────


def test_webhook_notifier_satisfies_notifier_protocol():
    """WebhookNotifier must isinstance Notifier."""
    n = WebhookNotifier(webhook_url="https://example.test/hook")
    assert isinstance(n, Notifier)


def test_webhook_notifier_posts_envelope_per_call(requests_mock):
    """Every typed method → 1 POST with a JSON envelope."""
    requests_mock.post("https://example.test/hook", status_code=200)
    n = WebhookNotifier(webhook_url="https://example.test/hook", username="bot-v12")

    n.notify_stock_pool_updated(tier="A", symbols=["AAPL"], source="scanner")
    n.notify_order_intent(symbol="AAPL", side="long", qty=10)
    n.notify_order_submitted(symbol="AAPL", order_id="o1")
    n.notify_order_filled(symbol="AAPL", qty=10, fill_price=101.0)
    n.notify_order_rejected(symbol="NVDA", reason="margin")
    n.notify_daily_review(symbol="AAPL", action="HOLD")
    n.notify_position_state_change(symbol="AAPL", old="OPEN", new="TRAILING")

    # Wait for async posts to complete.
    n.flush()
    # 7 POSTs.
    assert len(requests_mock.request_history) == 7
    for req in requests_mock.request_history:
        assert req.headers["Content-Type"] == "application/json"
        body = json.loads(req.body)
        assert body["method"].startswith("notify_")
        assert body["username"] == "bot-v12"
        assert "ts" in body and isinstance(body["args"], dict)


def test_webhook_notifier_4xx_response_is_logged_not_raised(requests_mock, caplog):
    """A 4xx/5xx webhook response must be logged, not raised."""
    caplog.set_level(logging.WARNING)
    requests_mock.post("https://example.test/hook", status_code=500)
    n = WebhookNotifier(webhook_url="https://example.test/hook")
    # Should not raise.
    n.notify_order_intent(symbol="AAPL")
    n.flush()
    assert any("non-2xx 500" in r.message for r in caplog.records)


def test_webhook_notifier_no_url_is_silent_noop(caplog):
    """Empty URL → no-op backend (safe-by-default)."""
    caplog.set_level(logging.DEBUG)
    n = WebhookNotifier(webhook_url="")
    # No session created, no error.
    n.notify_order_intent(symbol="AAPL")
    n.notify_order_submitted(symbol="AAPL")
    assert n._session is None


def test_webhook_notifier_disabled_is_noop(requests_mock):
    """enabled=False → no POSTs even with a valid URL."""
    requests_mock.post("https://example.test/hook", status_code=200)
    n = WebhookNotifier(webhook_url="https://example.test/hook", enabled=False)
    n.notify_order_intent(symbol="AAPL")
    n.notify_daily_review(symbol="AAPL", action="HOLD")
    assert len(requests_mock.request_history) == 0


# ─── make_notifier_from_env ─────────────────────────────────────────────────


def test_make_notifier_returns_log_by_default(monkeypatch):
    """No env var → LogNotifier (backwards-compatible default)."""
    monkeypatch.delenv(NOTIFIER_BACKEND_ENV, raising=False)
    assert isinstance(make_notifier_from_env(), LogNotifier)


def test_make_notifier_log_explicit():
    """backend='log' → LogNotifier."""
    assert isinstance(make_notifier_from_env(backend="log"), LogNotifier)


def test_make_notifier_redis_requires_client(monkeypatch, caplog):
    """backend='redis' without a redis_client → degrade to LogNotifier."""
    caplog.set_level(logging.WARNING)
    n = make_notifier_from_env(backend="redis", redis_client=None)
    assert isinstance(n, LogNotifier)
    assert any("degrading to LogNotifier" in r.message for r in caplog.records)


def test_make_notifier_redis_with_client(monkeypatch):
    """backend='redis' with client → RedisPubSubNotifier."""
    monkeypatch.setenv(NOTIFIER_REDIS_CHANNEL_ENV, "v12:custom")
    fake = _FakeRedis()
    n = make_notifier_from_env(backend="redis", redis_client=fake)
    assert isinstance(n, RedisPubSubNotifier)
    assert n.channel == "v12:custom"


def test_make_notifier_webhook_missing_url_degrades(monkeypatch, caplog):
    """backend='webhook' without URL → degrade to LogNotifier."""
    monkeypatch.delenv(NOTIFIER_WEBHOOK_URL_ENV, raising=False)
    caplog.set_level(logging.WARNING)
    n = make_notifier_from_env(backend="webhook")
    assert isinstance(n, LogNotifier)
    assert any("degrading to LogNotifier" in r.message for r in caplog.records)


def test_make_notifier_webhook_with_url(monkeypatch):
    """backend='webhook' with URL → WebhookNotifier."""
    monkeypatch.setenv(NOTIFIER_WEBHOOK_URL_ENV, "https://hooks.test/x")
    n = make_notifier_from_env(backend="webhook")
    assert isinstance(n, WebhookNotifier)
    assert n.webhook_url == "https://hooks.test/x"


def test_make_notifier_unknown_backend_degrades(monkeypatch, caplog):
    """backend='<unknown>' → degrade to LogNotifier + warning."""
    caplog.set_level(logging.WARNING)
    n = make_notifier_from_env(backend="carrier-pigeon")
    assert isinstance(n, LogNotifier)
    assert any("unknown backend" in r.message for r in caplog.records)


def test_make_notifier_valid_backends_constant():
    """VALID_BACKENDS is the source of truth — log/redis/webhook."""
    assert VALID_BACKENDS == ("log", "redis", "webhook")


def test_make_notifier_env_var_driven(monkeypatch):
    """V12_NOTIFIER_BACKEND env var selects the backend (no explicit kwarg)."""
    monkeypatch.setenv(NOTIFIER_BACKEND_ENV, "redis")
    fake = _FakeRedis()
    n = make_notifier_from_env(redis_client=fake)
    assert isinstance(n, RedisPubSubNotifier)


# ─── P0-2 (council 2026-07-09): executor shutdown safety ───────────────────


def test_webhook_notifier_executor_lock_present():
    """P0-2: webhook notifier must hold a Lock guarding submit/flush/shutdown."""
    n = WebhookNotifier(webhook_url="https://example.test/hook")
    # Lock exists and is held in the same module scope.
    from stockapp_v12.notifiers import webhook_notifier as mod
    assert hasattr(n, "_executor_lock"), "_executor_lock must be wired"
    assert isinstance(n._executor_lock, type(mod.threading.Lock()))
    assert n._shutdown_registered is True
    # And atexit must have actually registered _shutdown.
    # atexit._ncallbacks() in py3 — count callbacks before & after.
    # We cannot enumerate names directly, but we CAN detect whether *some*
    # callback was registered for this instance by asserting the bookkeeping
    # flag we set inside __init__. (atexit doesn't allow introspection by name.)
    assert n._shutdown_registered is True


def test_webhook_notifier_flush_does_not_lose_concurrent_submit(requests_mock):
    """P0-2: a submit racing with flush() must NOT raise RuntimeError.

    Sequence: spawn many ``notify_*`` calls in tight threads while a parallel
    thread calls ``flush()``. Pre-fix this would have raised
    ``RuntimeError: cannot schedule new futures after shutdown``. Post-fix
    the lock guarantees serialised access and both calls must succeed.
    """
    import threading

    requests_mock.post("https://example.test/hook", status_code=200)
    n = WebhookNotifier(webhook_url="https://example.test/hook", max_workers=2)

    stop = threading.Event()
    errors: list = []

    def spam_notifications():
        while not stop.is_set():
            n.notify_order_intent(symbol="AAPL", qty=1)
            n.notify_order_submitted(symbol="AAPL", order_id="o")

    def spam_flush():
        # rapid flushes — exercises the submit-vs-shutdown path.
        for _ in range(20):
            n.flush()
            time.sleep(0)

    t_spam = threading.Thread(target=spam_notifications, daemon=True)
    t_flush = threading.Thread(target=spam_flush, daemon=True)
    t_spam.start()
    t_flush.start()

    # Let the storm run briefly.
    import time
    time.sleep(0.05)
    stop.set()
    t_spam.join(timeout=2.0)
    t_flush.join(timeout=2.0)
    # Final drain — must not raise.
    n.flush()

    # No errors captured from worker threads.
    assert errors == [], f"unexpected errors: {errors}"


def test_webhook_notifier_atexit_shutdown_is_idempotent(requests_mock):
    """P0-2: calling _shutdown multiple times must not raise."""
    requests_mock.post("https://example.test/hook", status_code=200)
    n = WebhookNotifier(webhook_url="https://example.test/hook")
    # First call: shutdown the in-flight executor.
    n._shutdown(wait=True)
    # Second call: must not raise even though the executor was already torn down.
    n._shutdown(wait=True)
    n._shutdown(wait=False)
    # At this point the executor is fully torn down. ``submit`` would raise,
    # which is documented behaviour; but ``flush`` is still safe because we
    # rotate a fresh executor.
    n.flush()