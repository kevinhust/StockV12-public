"""Regression tests for P1-3 (council 2026-07-09): process_pending_action must
reconcile with broker before applying local mutations on EXIT/TRIM paths.

Pre-fix: TRIM path mutated rec.qty and rec.sl_price unconditionally without
checking whether the broker agrees on current qty. If the broker already
trimmed or closed the position, we'd be acting on stale data — moving SL
on a phantom position or closing a position that doesn't exist at broker.

Post-fix:
- broker_reconcile_fn parameter added (optional, backward-compatible).
- When provided + action is EXIT/TRIM: reconcile is called first.
- If reconcile raises, returns None, or disagrees → mutation skipped.
- Entire method wrapped in try/except → no pipeline crashes.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from stockapp_v12.pipeline.journal import TradeJournal
from stockapp_v12.services.position_manager import PositionManager, PositionState

# K-cleanup: shared test helper (formerly a local _register duplicate).
from tests.conftest import register_test_position as _register_base


def _make_pm(fake_redis):
    """Build a PositionManager with a stateful fake Redis store.

    Mirrors the pattern from test_position_lifecycle.py — the conftest
    fake_redis is a plain MagicMock that returns None for everything;
    we need actual state for register/get_position/persist to work.
    """
    store: Dict[str, Any] = {}

    def _get(k):
        return store.get(k)

    def _set(k, v):
        store[k] = v
        return True

    def _setex(k, ttl, v):
        store[k] = v
        return True

    def _sadd(k, v):
        if k not in store or not isinstance(store[k], set):
            store[k] = set()
        store[k].add(str(v).upper() if not isinstance(v, (set, list)) else v)
        return True

    def _sismember(k, v):
        s = store.get(k) or set()
        return str(v).upper() in {str(x).upper() for x in (s if isinstance(s, (set, list)) else [s])}

    def _smembers(k):
        return store.get(k) or set()

    def _srem(k, v):
        s = store.get(k)
        if isinstance(s, set):
            s.discard(str(v).upper())
        return True

    def _make_pipe():
        p = MagicMock()
        p.set = MagicMock(side_effect=lambda k, v: (_set(k, v), p)[1])
        p.sadd = MagicMock(side_effect=lambda k, v: (_sadd(k, v), p)[1])
        p.srem = MagicMock(side_effect=lambda k, v: (_srem(k, v), p)[1])
        p.setex = MagicMock(side_effect=lambda k, ttl, v: (_setex(k, ttl, v), p)[1])
        p.expire = MagicMock(return_value=p)
        p.execute = MagicMock(return_value=[1, 1, 1, 1])
        return p

    fake_redis.get.side_effect = _get
    fake_redis.set.side_effect = _set
    fake_redis.setex.side_effect = _setex
    fake_redis.sadd.side_effect = _sadd
    fake_redis.sismember.side_effect = _sismember
    fake_redis.smembers.side_effect = _smembers
    fake_redis.srem.side_effect = _srem
    fake_redis.pipeline.side_effect = _make_pipe

    journal = TradeJournal(fake_redis)
    pm = PositionManager(fake_redis, journal=journal, mode="paper_ibkr")
    pm._test_store = store
    return pm


def _register_with_pending(pm, *, symbol, qty, entry_price, sl_price,
                           atr_at_entry, pending_action):
    """Register a position and set pending_action on it.

    K-cleanup: delegates the actual register() call to the shared
    register_test_position helper from conftest, then applies the
    pending_action mutation that is unique to this test suite.
    """
    rec = _register_base(
        pm,
        symbol=symbol,
        entry_price=entry_price,
        qty=qty,
        atr_at_entry=atr_at_entry,
        parent_order_id="test-parent",
        sl_order_id="test-sl",
        sl_price=sl_price,
    )
    assert rec is not None, f"register failed for {symbol}"
    rec.pending_action = pending_action
    pm._persist(rec)
    return pm, rec


# ─── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def pm_with_trim_position(fake_redis):
    """PositionManager with an open position that has pending_action=TRIM."""
    pm = _make_pm(fake_redis)
    return _register_with_pending(
        pm, symbol="AAPL", qty=100, entry_price=150.00, sl_price=140.00,
        atr_at_entry=3.0, pending_action="TRIM",
    )


@pytest.fixture
def pm_with_exit_position(fake_redis):
    """PositionManager with an open position that has pending_action=EXIT."""
    pm = _make_pm(fake_redis)
    return _register_with_pending(
        pm, symbol="MSFT", qty=50, entry_price=400.00, sl_price=380.00,
        atr_at_entry=8.0, pending_action="EXIT",
    )


# ─── Tests: broker reconcile blocks TRIM mutation ─────────────────────────


def test_trim_skipped_on_qty_mismatch(pm_with_trim_position):
    """P1-3: TRIM must skip local mutation when broker qty disagrees."""
    pm, _ = pm_with_trim_position

    result = pm.process_pending_action("AAPL", broker_reconcile_fn=lambda s: 200)

    assert result is not None
    assert result["executed"] is False
    assert result["reason"] == "QTY_MISMATCH"

    # Position unchanged
    updated = pm.get_position("AAPL")
    assert updated.qty == 100
    assert updated.sl_price == 140.00
    assert updated.pending_action is None  # cleared after skip


def test_trim_skipped_on_broker_no_position(pm_with_trim_position):
    """P1-3: TRIM must skip when broker reports no position (None)."""
    pm, _ = pm_with_trim_position

    result = pm.process_pending_action("AAPL", broker_reconcile_fn=lambda s: None)

    assert result is not None
    assert result["executed"] is False
    assert result["reason"] == "BROKER_NO_POSITION"

    updated = pm.get_position("AAPL")
    assert updated.qty == 100
    assert updated.sl_price == 140.00


def test_trim_skipped_on_reconcile_exception(pm_with_trim_position):
    """P1-3: TRIM must skip when broker reconcile raises an exception."""
    pm, _ = pm_with_trim_position

    def failing_reconcile(symbol):
        raise ConnectionError("broker API down")

    result = pm.process_pending_action("AAPL", broker_reconcile_fn=failing_reconcile)

    assert result is not None
    assert result["executed"] is False
    assert result["reason"] == "RECONCILE_FAILED"

    updated = pm.get_position("AAPL")
    assert updated.qty == 100
    assert updated.sl_price == 140.00


def test_trim_proceeds_when_broker_matches(pm_with_trim_position):
    """P1-3: TRIM proceeds normally when broker qty matches local qty."""
    pm, _ = pm_with_trim_position

    result = pm.process_pending_action("AAPL", broker_reconcile_fn=lambda s: 100)

    assert result is not None
    assert result["executed"] is True
    assert result["new_qty"] == 50

    updated = pm.get_position("AAPL")
    assert updated.qty == 50
    assert updated.sl_price == 150.00  # moved to breakeven


# ─── Tests: EXIT path also reconciles ─────────────────────────────────────


def test_exit_skipped_on_qty_mismatch(pm_with_exit_position):
    """P1-3: EXIT must also reconcile and skip when broker qty disagrees."""
    pm, _ = pm_with_exit_position

    result = pm.process_pending_action("MSFT", broker_reconcile_fn=lambda s: 0)

    assert result is not None
    assert result["executed"] is False
    assert result["reason"] == "QTY_MISMATCH"

    # Position NOT closed
    updated = pm.get_position("MSFT")
    assert updated is not None
    assert updated.state != PositionState.CLOSED


def test_exit_skipped_on_broker_no_position(pm_with_exit_position):
    """P1-3: EXIT must skip when broker returns None (position not found)."""
    pm, _ = pm_with_exit_position

    result = pm.process_pending_action("MSFT", broker_reconcile_fn=lambda s: None)

    assert result is not None
    assert result["executed"] is False
    assert result["reason"] == "BROKER_NO_POSITION"

    updated = pm.get_position("MSFT")
    assert updated is not None
    assert updated.state != PositionState.CLOSED


def test_exit_proceeds_on_reconcile_match(pm_with_exit_position):
    """P1-3: EXIT proceeds when broker qty matches."""
    pm, _ = pm_with_exit_position

    result = pm.process_pending_action("MSFT", broker_reconcile_fn=lambda s: 50)

    assert result is not None
    assert result["executed"] is True

    updated = pm.get_position("MSFT")
    assert updated.state == PositionState.CLOSED


# ─── Tests: backward compatibility (no broker_reconcile_fn) ───────────────


def test_trim_without_reconcile_fn_still_works(pm_with_trim_position):
    """P1-3: When no broker_reconcile_fn provided, backward-compatible behavior."""
    pm, _ = pm_with_trim_position

    result = pm.process_pending_action("AAPL")  # no reconcile_fn

    assert result is not None
    assert result["executed"] is True
    assert result["new_qty"] == 50


# ─── Tests: internal error handling ──────────────────────────────────────


def test_internal_error_returns_safe_result(fake_redis):
    """P1-3: Unexpected internal errors don't crash the pipeline — return safe dict."""
    pm = _make_pm(fake_redis)
    _register_with_pending(
        pm, symbol="NVDA", qty=100, entry_price=500.00, sl_price=470.00,
        atr_at_entry=10.0, pending_action="TRIM",
    )

    # Sabotage _persist to raise — process_pending_action calls _persist
    # at multiple points; if it raises, the outer try/except catches it.
    pm._persist = MagicMock(side_effect=RuntimeError("Redis exploded"))

    result = pm.process_pending_action("NVDA")

    assert result is not None
    assert result["executed"] is False
    assert result["reason"] == "INTERNAL_ERROR"


# ─── Tests: non-mutating actions skip reconcile ───────────────────────────


def test_tighten_skips_reconcile(pm_with_trim_position):
    """P1-3: TIGHTEN doesn't affect qty, so broker reconcile is not required."""
    pm, rec = pm_with_trim_position
    rec.pending_action = "TIGHTEN"
    pm._persist(rec)

    reconcile_called = []

    def reconcile_fn(symbol):
        reconcile_called.append(symbol)
        return 999  # wrong qty, but shouldn't matter for TIGHTEN

    result = pm.process_pending_action("AAPL", broker_reconcile_fn=reconcile_fn)

    assert result is not None
    assert result["executed"] is True
    assert len(reconcile_called) == 0  # reconcile NOT called for TIGHTEN
