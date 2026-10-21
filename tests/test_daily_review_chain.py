"""Test the full DailyReview → pending_action → next-session生效 chain for multi-day."""

from unittest.mock import MagicMock


from stockapp_v12.pipeline.journal import TradeJournal
from stockapp_v12.services.daily_review import DailyReview
from stockapp_v12.services.position_manager import PositionManager, PositionState


def _price_provider(prices):
    def get(s):
        return prices.get(s)
    return get


def test_daily_review_writes_pending_and_next_day_processes(fake_redis):
    # Use stateful-like setup for index/reads (similar to lifecycle tests)
    store = {}

    def _get(k): return store.get(k)
    def _set(k, v): store[k] = v; return True
    def _sadd(k, v):
        if k not in store or not isinstance(store.get(k), set): store[k] = set()
        store[k].add(str(v).upper()); return True
    def _sismember(k, v):
        s = store.get(k) or set(); return str(v).upper() in {str(x).upper() for x in s}
    def _smembers(k): return store.get(k) or set()
    def _srem(k, v):
        s = store.get(k)
        if isinstance(s, set): s.discard(str(v).upper()); return True

    def _make_pipe():
        p = MagicMock()
        p.set = MagicMock(side_effect=lambda k, v: (_set(k, v), p)[1])
        p.sadd = MagicMock(side_effect=lambda k, v: (_sadd(k, v), p)[1])
        p.execute.return_value = [True, True]
        return p

    fake_redis.get.side_effect = _get
    fake_redis.set.side_effect = _set
    fake_redis.sadd.side_effect = _sadd
    fake_redis.sismember.side_effect = _sismember
    fake_redis.smembers.side_effect = _smembers
    fake_redis.srem.side_effect = _srem
    fake_redis.pipeline.side_effect = _make_pipe

    j = TradeJournal(fake_redis)
    limits = MagicMock(min_hold_days=1, trailing_activation_atr=1.0, trailing_distance_atr=2.0)
    pm = PositionManager(fake_redis, journal=j, limits=limits, mode="paper_ibkr")

    # Register
    pm.register(symbol="BAD", entry_price=100.0, qty=10, atr_at_entry=5.0,
                parent_order_id="p", sl_order_id="s", sl_price=90.0)

    # Simulate aging + loss
    for _ in range(6):
        pm.increment_days_held("BAD")
    # Force update price effect via direct for test
    rec = pm.get_position("BAD")
    rec.days_held = 6
    pm._persist(rec)

    prices = {"BAD": 70.0}
    review = DailyReview(pm, j, price_provider=_price_provider(prices), regime_provider=lambda: "NEUTRAL",
                         apply_immediately=False)
    decisions = review.run()

    assert len(decisions) >= 1
    assert any(d.action == "EXIT" for d in decisions)

    pos_after = pm.get_position("BAD")
    assert pos_after and pos_after.pending_action == "EXIT"

    result = pm.process_pending_action("BAD")
    assert result and result["action"] == "EXIT"

    final = pm.get_position("BAD")
    assert final.state == PositionState.CLOSED


def test_daily_review_hold_and_tighten(fake_redis):
    # Minimal stateful for this test
    store = {}
    def _get(k): return store.get(k)
    def _set(k, v): store[k] = v; return True
    def _sadd(k, v):
        if k not in store or not isinstance(store.get(k), set): store[k] = set()
        store[k].add(str(v).upper()); return True
    def _smembers(k): return store.get(k) or set()
    def _make_pipe():
        p = MagicMock(); p.set.side_effect = lambda k,v: (_set(k,v),p)[1]; p.sadd.side_effect=lambda k,v:(_sadd(k,v),p)[1]; p.execute.return_value=[True,True]; return p
    fake_redis.get.side_effect=_get; fake_redis.set.side_effect=_set; fake_redis.sadd.side_effect=_sadd; fake_redis.smembers.side_effect=_smembers; fake_redis.pipeline.side_effect=_make_pipe

    j = TradeJournal(fake_redis)
    pm = PositionManager(fake_redis, journal=j, mode="paper_ibkr")

    pm.register(symbol="GOOD", entry_price=100, qty=5, atr_at_entry=4,
                parent_order_id="p", sl_order_id="s", sl_price=92)
    pm.increment_days_held("GOOD")
    rec = pm.get_position("GOOD")
    if rec: rec.days_held = 1; pm._persist(rec)

    prices = {"GOOD": 105.0}
    review = DailyReview(pm, j, _price_provider(prices), lambda: "BULL_LONG")
    decs = review.run()

    assert len(decs) >= 1
    assert any(d.action == "HOLD" for d in decs)
    pos = pm.get_position("GOOD")
    assert pos is None or pos.pending_action is None
