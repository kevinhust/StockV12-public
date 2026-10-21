"""Regression tests for P1-2 (council 2026-07-09): R-06 symbol cooldown
must be fail-closed on Redis errors, mirroring the H1 pattern in
``get_daily_trade_count``.

Pre-fix: ``check_symbol_cooldown`` caught only ``(TypeError, ValueError)``,
so a ``redis.exceptions.ConnectionError`` propagated uncaught — callers
that swallowed it at a higher level allowed trades through with NO
cooldown check.

Post-fix: catches all ``Exception``, logs at EXCEPTION level, returns
``(True, "R-06: ... failing closed")`` so the BUY is denied.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock


from stockapp_v12.bridge.gatekeeper import check_symbol_cooldown


# ─── Happy path: symbol NOT in cooldown ────────────────────────────────────


def test_cooldown_not_set_returns_false():
    """When the cooldown key doesn't exist, allow the trade."""
    r = MagicMock()
    r.get.return_value = None
    blocked, reason = check_symbol_cooldown(r, "AAPL")
    assert blocked is False
    assert reason == ""


def test_cooldown_set_returns_true_with_ttl():
    """When cooldown key exists, block with TTL in the reason."""
    r = MagicMock()
    r.get.return_value = "1"
    r.ttl.return_value = 7200
    blocked, reason = check_symbol_cooldown(r, "AAPL")
    assert blocked is True
    assert "R-06" in reason
    assert "7200s" in reason


def test_cooldown_key_uppercased():
    """Symbol must be uppercased before key lookup."""
    r = MagicMock()
    r.get.return_value = "1"
    r.ttl.return_value = 3600
    check_symbol_cooldown(r, "aapl")
    r.get.assert_called_once_with("risk:cooldown:AAPL")


# ─── Fail-closed: Redis errors → treat as in-cooldown ─────────────────────


def test_cooldown_redis_connection_error_fails_closed(caplog):
    """P1-2: ConnectionError must be caught and treated as in-cooldown.

    Pre-fix this raised an uncaught ConnectionError (the except clause
    only caught TypeError/ValueError), which callers might swallow and
    allow the trade through.
    """
    caplog.set_level(logging.ERROR)
    r = MagicMock()
    r.get.side_effect = ConnectionError("redis down")

    blocked, reason = check_symbol_cooldown(r, "NVDA")

    assert blocked is True, "P1-2 regression: Redis error must fail-closed (block)"
    assert "R-06" in reason
    assert "failing closed" in reason.lower()
    # Must have logged at EXCEPTION level
    assert any(
        "R-06" in r.message and "failing closed" in r.message
        for r in caplog.records
    )


def test_cooldown_redis_timeout_error_fails_closed():
    """P1-2: TimeoutError must also fail-closed."""
    import redis.exceptions

    r = MagicMock()
    r.get.side_effect = redis.exceptions.TimeoutError("redis timed out")

    blocked, reason = check_symbol_cooldown(r, "MSFT")

    assert blocked is True
    assert "R-06" in reason
    assert "failing closed" in reason.lower()


def test_cooldown_redis_decode_error_fails_closed():
    """P1-2: any Exception subclass must fail-closed, not just Redis errors."""
    r = MagicMock()
    r.get.side_effect = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")

    blocked, reason = check_symbol_cooldown(r, "TSLA")

    assert blocked is True
    assert "R-06" in reason


def test_cooldown_ttl_int_conversion_error_does_not_crash():
    """If TTL returns garbage, the int() conversion is inside the try block.

    This was the ONLY case the old (TypeError, ValueError) catch handled.
    Post-fix it's still handled, but via the broader Exception catch —
    which now also treats it as in-cooldown (fail-closed).
    """
    r = MagicMock()
    r.get.return_value = "1"  # key exists
    r.ttl.return_value = "garbage"  # int() will raise ValueError

    blocked, reason = check_symbol_cooldown(r, "AMD")

    # Key existed but TTL read failed → fail-closed (block)
    assert blocked is True
    assert "R-06" in reason
