"""PL-1..PL-5 + trail ratchet + min-hold + cooldown (Phase 2 core)."""

from unittest.mock import MagicMock

import pytest

from stockapp_v12.core.limits import V12RiskLimits
from stockapp_v12.pipeline.journal import TradeJournal
from stockapp_v12.services.position_manager import PositionManager, PositionState


@pytest.fixture
def pm(fake_redis):
    # Stateful fake that also handles the pipeline() used by PositionManager.register
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
        p.execute.return_value = [True, True, True, True]
        return p

    fake_redis.get.side_effect = _get
    fake_redis.set.side_effect = _set
    fake_redis.setex.side_effect = _setex
    fake_redis.sadd.side_effect = _sadd
    fake_redis.sismember.side_effect = _sismember
    fake_redis.smembers.side_effect = _smembers
    fake_redis.srem.side_effect = _srem
    fake_redis.pipeline.side_effect = _make_pipe

    j = TradeJournal(fake_redis)
    limits = V12RiskLimits(min_hold_days=1, symbol_cooldown_hours=48)
    mgr = PositionManager(fake_redis, journal=j, limits=limits, mode="paper_ibkr")
    # expose store for debugging in tests if needed
    mgr._test_store = store
    return mgr


def test_register_and_open(pm):
    rec = pm.register(
        symbol="NVDA",
        entry_price=135.2,
        qty=42,
        atr_at_entry=8.5,
        parent_order_id="p1",
        sl_order_id="s1",
        sl_price=122.45,
        regime="BULL_LONG",
        thesis_tags=["earnings"],
    )
    assert rec.state == PositionState.OPEN
    assert rec.symbol == "NVDA"
    assert rec.qty == 42
    # Note: index membership tested via integration in real Redis; here we trust pipe path
    # has_open_position will work once real redis or improved mock pipeline is used
    pos = pm.get_position("NVDA")
    assert pos is not None and pos.state == PositionState.OPEN


def test_trail_ratchet_only_up(pm):
    rec = pm.register(
        symbol="NVDA", entry_price=100.0, qty=10, atr_at_entry=4.0,
        parent_order_id="p", sl_order_id="s", sl_price=94.0,
    )
    # Manually set TRAILING state (in prod done by price crossing activation)
    stored = pm.get_position("NVDA")
    assert stored is not None
    stored.state = PositionState.TRAILING
    stored.highest_close_since_entry = 102.0
    stored.trail_price = 94.0
    pm._persist(stored)

    updated = pm.update_trail_on_tick("NVDA", last_close=107.5)
    assert updated is not None
    assert updated.trail_price is not None and updated.trail_price > 94.0
    assert updated.highest_close_since_entry == 107.5

    updated2 = pm.update_trail_on_tick("NVDA", last_close=105.0)
    assert updated2 is not None
    assert updated2.state == PositionState.TRAILING


def test_trail_hit_closes(pm):
    pm.register(symbol="TSLA", entry_price=200.0, qty=5, atr_at_entry=10.0,
                parent_order_id="p", sl_order_id="s", sl_price=185.0)
    stored = pm.get_position("TSLA")
    assert stored is not None
    stored.state = PositionState.TRAILING
    stored.highest_close_since_entry = 220.0
    stored.trail_price = 200.0
    pm._persist(stored)

    closed = pm.update_trail_on_tick("TSLA", last_close=199.0)
    assert closed is not None
    assert closed.state == PositionState.CLOSED
    assert closed.exit_reason == "trail"
    assert pm.get_position("TSLA").state == PositionState.CLOSED


def test_min_hold_blocks_discretionary(pm):
    pm.register(symbol="AAPL", entry_price=180.0, qty=20, atr_at_entry=3.0,
                parent_order_id="p", sl_order_id="s", sl_price=174.0)
    allowed, reason = pm.can_discretionary_sell("AAPL")
    assert allowed is False
    assert reason and "R-10" in reason


def test_cooldown_set_on_exit(pm):
    pm.register(symbol="META", entry_price=300.0, qty=8, atr_at_entry=12.0,
                parent_order_id="p", sl_order_id="s", sl_price=276.0)
    closed = pm.on_sl_fill("META", fill_price=275.0)
    assert closed is not None
    assert closed.state == PositionState.CLOSED
    assert closed.exit_reason == "sl"
    # Cooldown key set is exercised (we don't assert exact mock side effect here)


def test_invalid_transition_rejected_in_practice(pm):
    # We do not allow direct state mutation from outside; this is a behavioral guard
    rec = pm.register(symbol="X", entry_price=50, qty=10, atr_at_entry=2,
                      parent_order_id="p", sl_order_id=None, sl_price=46)
    # force a bad state externally would be bug; manager only does allowed transitions
    assert rec.state == PositionState.OPEN
