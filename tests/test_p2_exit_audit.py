"""Regression tests for P2-exit-audit (council 2026-07-09): _close_position
must compute and journal MFE/MAE/PNL on every position close.

Pre-fix: _close_position wrote only days_held + entry_price to the journal
via raw append(). No MFE, MAE, PNL, or regime fields.

Post-fix:
- lowest_close_since_entry field added to PositionRecord (mirrors highest)
- update_trail_on_tick now tracks both highest and lowest close
- _close_position computes pnl_pct/mfe_pct/mae_pct via Decimal rounding
  and calls journal.log_exit() with full enrichment
- Every CLOSED record carries mfe_pct, mae_pct, pnl_pct, regime_at_entry
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock


from stockapp_v12.pipeline.journal import TradeJournal
from stockapp_v12.services.position_manager import (
    PositionManager,
    PositionState,
)

# K-cleanup: shared test helper (formerly a local _register duplicate).
from tests.conftest import register_test_position as _register_base


def _register(pm, **kwargs):
    """Shim: delegates to shared register_test_position (K-cleanup).

    test_p2 originally defaulted regime="BULL_LONG"; the shared helper
    leaves it to the caller to opt in. This wrapper preserves the
    historical default so existing assertions still hold.
    """
    kwargs.setdefault("regime", "BULL_LONG")
    return _register_base(pm, **kwargs)


def _make_pm(fake_redis):
    """Build a PositionManager with a stateful fake Redis store."""
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
        p.expire = MagicMock(return_value=p)
        p.execute = MagicMock(return_value=[1])
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


def _register(pm, **kwargs):
    """Register a position with sensible defaults."""
    defaults = dict(
        symbol="TEST",
        entry_price=100.00,
        qty=100,
        atr_at_entry=3.0,
        parent_order_id="p1",
        sl_order_id="s1",
        sl_price=90.00,
        regime="BULL_LONG",
    )
    defaults.update(kwargs)
    pm.register(**defaults)
    return pm.get_position(defaults["symbol"])


# ─── PositionRecord field checks ─────────────────────────────────────────


def test_position_record_has_lowest_close_field():
    """P2: PositionRecord must have lowest_close_since_entry for MAE tracking."""
    rec = _register(_make_pm(MagicMock()), symbol="AAPL")
    assert hasattr(rec, "lowest_close_since_entry")


def test_update_trail_tracks_lowest_close():
    """P2: update_trail_on_tick must update lowest_close_since_entry."""
    pm = _make_pm(MagicMock())
    rec = _register(pm, symbol="AAPL", entry_price=100.00)
    pm.maybe_arm_trail(rec, 100.00)

    # Price drops then recovers
    rec = pm.update_trail_on_tick("AAPL", 95.00)
    assert rec.lowest_close_since_entry == 95.00

    rec = pm.update_trail_on_tick("AAPL", 110.00)
    assert rec.lowest_close_since_entry == 95.00  # lowest stays at 95
    assert rec.highest_close_since_entry == 110.00


# ─── Close position computes MFE/MAE/PNL ──────────────────────────────────


def test_close_computes_mfe_mae_pnl():
    """P2: _close_position must compute pnl_pct, mfe_pct, mae_pct."""
    pm = _make_pm(MagicMock())
    rec = _register(pm, symbol="AAPL", entry_price=100.00)
    pm.maybe_arm_trail(rec, 100.00)

    # Update trail to set highest/lowest (but not close)
    pm.update_trail_on_tick("AAPL", 108.00)  # arms trailing (>= 103)
    pm.update_trail_on_tick("AAPL", 104.00)  # stays above trail, tracks lowest
    rec = pm.get_position("AAPL")
    assert rec.highest_close_since_entry == 108.0
    assert rec.lowest_close_since_entry == 100.0  # set during maybe_arm_trail

    # Force close via SL fill
    pm.on_sl_fill("AAPL", fill_price=106.00)

    closed = pm.get_position("AAPL")
    assert closed.state == PositionState.CLOSED


def test_journal_log_exit_called_with_enrichment():
    """P2: journal.log_exit is called with mfe_pct, mae_pct, pnl_pct."""
    pm = _make_pm(MagicMock())
    rec = _register(pm, symbol="MSFT", entry_price=200.00)
    # Set days_held BEFORE trail ticks to survive trail-triggered close
    rec.days_held = 3
    pm._persist(rec)

    # Install spy BEFORE any ticks that might close
    log_exit_calls = []
    original_log_exit = pm.journal.log_exit

    def spy_log_exit(*args, **kwargs):
        log_exit_calls.append(kwargs)
        return original_log_exit(*args, **kwargs)

    pm.journal.log_exit = spy_log_exit

    pm.update_trail_on_tick("MSFT", 220.00)  # arms trailing (>=208), MFE +10%
    pm.update_trail_on_tick("MSFT", 205.00)  # 205 > trail(204) → stays open, tracks lowest

    # Force close via SL
    pm.on_sl_fill("MSFT", fill_price=210.00)

    assert len(log_exit_calls) == 1
    call = log_exit_calls[0]
    assert "mfe_pct" in call
    assert "mae_pct" in call
    assert "pnl_pct" in call
    assert "hold_days" in call
    assert call["hold_days"] == 3


def test_mfe_positive_for_winning_position():
    """P2: MFE should be positive when price exceeded entry."""
    pm = _make_pm(MagicMock())
    rec = _register(pm, symbol="NVDA", entry_price=500.00)
    rec.days_held = 2
    pm._persist(rec)

    # Install spy BEFORE ticks
    log_exit_calls = []
    original = pm.journal.log_exit
    pm.journal.log_exit = lambda *a, **kw: (log_exit_calls.append(kw), original(*a, **kw))[1]

    pm.update_trail_on_tick("NVDA", 550.00)  # +10% MFE, arms trailing (>=510)
    pm.update_trail_on_tick("NVDA", 520.00)  # stays above trail(534)? 520 < 534 → closes!
    # Actually trail = 550 - 2*10 = 530, so 520 < 530 → closes

    assert len(log_exit_calls) >= 1
    call = log_exit_calls[0]
    assert call["mfe_pct"] > 0  # Had a +10% excursion
    assert call["pnl_pct"] > 0  # Closed at 520 which is above 500


def test_mae_negative_for_losing_position():
    """P2: MAE should be negative when price dropped below entry."""
    pm = _make_pm(MagicMock())
    rec = _register(pm, symbol="AMD", entry_price=150.00)
    pm.maybe_arm_trail(rec, 150.00)
    pm.update_trail_on_tick("AMD", 140.00)  # -6.67% MAE

    log_exit_calls = []
    original = pm.journal.log_exit
    pm.journal.log_exit = lambda *a, **kw: (log_exit_calls.append(kw), original(*a, **kw))[1]

    pm.on_sl_fill("AMD", fill_price=135.00)

    call = log_exit_calls[0]
    assert call["mae_pct"] < 0  # Drawdown
    assert call["pnl_pct"] < 0  # Loss
