"""Regression tests for P2-atomicity (council 2026-07-09): _close_position
must commit srem (index) + setex (cooldown) + set/expire (record) as a
single Redis transaction.

Pre-fix: 3 separate Redis calls. A crash between any two leaves:
- Index says symbol is gone, but record still OPEN in Redis
- OR index has symbol, but cooldown is set, but record is still OPEN
- OR index has symbol, no cooldown, but record is CLOSED

Each of these causes reconcile_with_broker() to misjudge the position.

Post-fix: all three writes go through a single pipeline() that executes
as a transaction. Either all commit or none do.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from stockapp_v12.pipeline.journal import TradeJournal
from stockapp_v12.services.position_manager import (
    PositionManager,
    PositionState,
)

# K-cleanup: shared test helper (formerly a local _register duplicate).
from tests.conftest import register_test_position as _register_base


def _make_pm(fake_redis):
    """Build PositionManager with stateful fake Redis that tracks pipeline
    calls so we can assert all writes happened in one transaction.
    """
    store: Dict[str, Any] = {}
    pipeline_calls: List[Dict[str, Any]] = []  # one entry per pipeline().execute()

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

    def _expire(k, ttl):
        return True

    def _make_pipe():
        p = MagicMock()
        ops = []
        p._ops = ops

        def _track(op_name, *args, **kwargs):
            ops.append((op_name, args, kwargs))
            return p

        def _set_and_track(k, v):
            _set(k, v)
            ops.append(("set", (k, v), {}))
            return p

        def _setex_and_track(k, ttl, v):
            _setex(k, ttl, v)
            ops.append(("setex", (k, ttl, v), {}))
            return p

        def _srem_and_track(k, v):
            _srem(k, v)
            ops.append(("srem", (k, v), {}))
            return p

        def _expire_and_track(k, ttl):
            ops.append(("expire", (k, ttl), {}))
            return p

        p.set = MagicMock(side_effect=_set_and_track)
        p.setex = MagicMock(side_effect=_setex_and_track)
        p.sadd = MagicMock(side_effect=lambda k, v: (_sadd(k, v), p)[1])
        p.srem = MagicMock(side_effect=_srem_and_track)
        p.expire = MagicMock(side_effect=_expire_and_track)
        p.execute = MagicMock(side_effect=lambda: (pipeline_calls.append(ops), [1] * len(ops))[1])
        return p

    fake_redis.get.side_effect = _get
    fake_redis.set.side_effect = _set
    fake_redis.setex.side_effect = _setex
    fake_redis.sadd.side_effect = _sadd
    fake_redis.sismember.side_effect = _sismember
    fake_redis.smembers.side_effect = _smembers
    fake_redis.srem.side_effect = _srem
    fake_redis.expire.side_effect = _expire
    fake_redis.pipeline.side_effect = _make_pipe

    journal = TradeJournal(fake_redis)
    pm = PositionManager(fake_redis, journal=journal, mode="paper_ibkr")
    pm._test_store = store
    pm._test_pipeline_calls = pipeline_calls
    return pm


def _register(pm, **kwargs):
    """Shim: delegates to shared register_test_position (K-cleanup)."""
    return _register_base(pm, **kwargs)


# ─── Pipeline atomicity ──────────────────────────────────────────────────


def test_close_position_uses_redis_pipeline():
    """P2: _close_position must use redis pipeline() for atomic writes."""
    pm = _make_pm(MagicMock())
    _register(pm, symbol="AAPL")
    pm._test_pipeline_calls.clear()  # ignore register's pipeline

    pm.on_sl_fill("AAPL", fill_price=95.00)

    # Exactly one pipeline transaction for the close
    assert len(pm._test_pipeline_calls) == 1, (
        f"Expected 1 pipeline transaction, got {len(pm._test_pipeline_calls)}"
    )
    ops = pm._test_pipeline_calls[0]
    op_names = [op[0] for op in ops]
    # All three write types must be in the same transaction
    assert "srem" in op_names, "srem (index removal) missing from close pipeline"
    assert "setex" in op_names, "setex (cooldown) missing from close pipeline"
    assert "set" in op_names, "set (record) missing from close pipeline"


def test_close_position_no_bare_srem_after_refactor():
    """P2: source must not call srem directly outside of pipeline for close."""
    import inspect
    from stockapp_v12.services import position_manager as pm_mod
    src = inspect.getsource(pm_mod)
    # In _close_position, srem should be inside a pipe.srem(...) call, not
    # a bare self._r.srem(...) call. The bare srem patterns we want to
    # catch are the 3-call sequence in the close path.
    # We verify by checking that close doesn't have the anti-pattern.
    close_section = src.split("def _close_position")[1].split("def ")[0]
    # No bare self._r.srem in _close_position anymore
    assert "self._r.srem(self._index_key" not in close_section, (
        "P2: _close_position still uses bare srem instead of pipeline"
    )
    assert "self._r.setex(self._cooldown_key" not in close_section, (
        "P2: _close_position still uses bare setex instead of pipeline"
    )


def test_close_position_atomic_under_pipeline_failure():
    """P2: if pipeline.execute() raises, position remains OPEN (no partial commit)."""
    pm = _make_pm(MagicMock())
    _register(pm, symbol="MSFT")

    # Replace the pipeline factory to return a pipe whose execute raises
    def failing_pipeline():
        pipe = MagicMock()
        pipe.srem = MagicMock(return_value=pipe)
        pipe.setex = MagicMock(return_value=pipe)
        pipe.set = MagicMock(return_value=pipe)
        pipe.expire = MagicMock(return_value=pipe)
        pipe.execute = MagicMock(side_effect=ConnectionError("redis transaction failed"))
        return pipe

    pm._r.pipeline = failing_pipeline

    with pytest.raises(ConnectionError):
        pm.on_sl_fill("MSFT", fill_price=95.00)

    # The close failed — position still OPEN
    rec = pm.get_position("MSFT")
    assert rec is not None
    assert rec.state == PositionState.OPEN, (
        "Position should remain OPEN when pipeline transaction fails"
    )


def test_register_still_uses_pipeline():
    """Sanity: register() must still work correctly with the new helper.

    The pipeline path for register was already in place; we just want to
    confirm the refactor didn't break it.
    """
    pm = _make_pm(MagicMock())
    rec = pm.register(
        symbol="NVDA",
        entry_price=500.0,
        qty=10,
        atr_at_entry=10.0,
        parent_order_id="p1",
        sl_order_id="s1",
        sl_price=480.0,
    )
    assert rec.state == PositionState.OPEN
    assert pm.get_position("NVDA") is not None
