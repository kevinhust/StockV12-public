"""Regression tests for P1-1 (council 2026-07-09): entry_guard atomic
SADD+EXPIRE via Lua EVAL.

Pre-fix: ``begin_entry_evaluation`` did SISMEMBER (check) then SADD (act)
in two separate Redis round-trips. Two concurrent orchestrators could
both pass the check and both proceed to evaluate the same symbol.

Post-fix: the check+act is collapsed into a single atomic Lua EVAL that
returns 1 (first caller wins) or 0 (already evaluated — skip).
"""

from __future__ import annotations

import logging
import threading


from stockapp_v12.pipeline.entry_guard import EntryEvaluationGuard


# ─── Atomic mark: first caller proceeds, second is blocked ────────────────


def test_atomic_first_caller_proceeds(fake_redis):
    """SADD returns 1 → first caller → ok=True."""
    fake_redis.sismember.return_value = False
    fake_redis.eval.return_value = 1
    guard = EntryEvaluationGuard(fake_redis, mode="paper_ibkr")

    ok, code = guard.begin_entry_evaluation("AAPL")
    assert ok is True
    assert code is None


def test_atomic_second_caller_blocked(fake_redis):
    """SADD returns 0 → already evaluated → ok=False, INV-1_ALREADY_EVALUATED."""
    fake_redis.sismember.return_value = False  # INV-2 passes (no open position)
    fake_redis.eval.return_value = 0  # SADD returns 0 → symbol already in set
    guard = EntryEvaluationGuard(fake_redis, mode="paper_ibkr")

    ok, code = guard.begin_entry_evaluation("AAPL")
    assert ok is False
    assert code == "INV-1_ALREADY_EVALUATED"


def test_atomic_inv2_open_position_blocks_before_eval(fake_redis):
    """INV-2 check happens BEFORE the Lua EVAL — short-circuits."""
    # sismember returns True for position index → INV-2 fires
    fake_redis.sismember.return_value = True
    guard = EntryEvaluationGuard(fake_redis, mode="paper_ibkr")

    ok, code = guard.begin_entry_evaluation("NVDA")
    assert ok is False
    assert code == "INV-2_OPEN_POSITION"
    # EVAL must NOT have been called (INV-2 short-circuited before INV-1)
    fake_redis.eval.assert_not_called()


# ─── Fail-closed on Redis errors ──────────────────────────────────────────


def test_atomic_redis_eval_error_fails_closed(caplog, fake_redis):
    """Lua EVAL raises → fail-closed (don't proceed with evaluation)."""
    caplog.set_level(logging.WARNING)
    fake_redis.sismember.return_value = False
    fake_redis.eval.side_effect = ConnectionError("redis down")
    guard = EntryEvaluationGuard(fake_redis, mode="paper_ibkr")

    ok, code = guard.begin_entry_evaluation("MSFT")
    assert ok is False
    assert code == "INV-1_EVAL_ERROR"
    assert any("INV-1" in r.message and "failing closed" in r.message
               for r in caplog.records)


def test_atomic_inv2_redis_error_fails_closed(caplog, fake_redis):
    """INV-2 SISMEMBER raises → fail-closed."""
    caplog.set_level(logging.WARNING)
    fake_redis.sismember.side_effect = ConnectionError("redis down")
    guard = EntryEvaluationGuard(fake_redis, mode="paper_ibkr")

    ok, code = guard.begin_entry_evaluation("TSLA")
    assert ok is False
    assert code == "INV-2_EVAL_ERROR"


# ─── Concurrency simulation: only one caller wins ─────────────────────────


def test_concurrent_callers_only_one_proceeds():
    """Simulate the race the old code had: two threads calling
    begin_entry_evaluation for the same symbol at the same time.

    With the atomic Lua EVAL, only the thread whose SADD returns 1
    proceeds; the other gets SADD==0 and is blocked.
    """
    # Use a real dict-backed fake that simulates SADD atomicity.
    class _AtomicFakeRedis:
        def __init__(self):
            self._sets = {}
            self._lock = threading.Lock()

        def sismember(self, key, member):
            with self._lock:
                return member in self._sets.get(key, set())

        def eval(self, script, numkeys, key, member, ttl):
            # Simulate atomic SADD + EXPIRE
            with self._lock:
                if key not in self._sets:
                    self._sets[key] = set()
                if member in self._sets[key]:
                    return 0  # already exists → not added
                self._sets[key].add(member)
                return 1  # newly added

    r = _AtomicFakeRedis()
    guard = EntryEvaluationGuard(r, mode="paper_ibkr")

    results = []
    barrier = threading.Barrier(2)

    def try_enter():
        barrier.wait()  # both threads fire at the same time
        ok, code = guard.begin_entry_evaluation("AAPL")
        results.append((ok, code))

    t1 = threading.Thread(target=try_enter)
    t2 = threading.Thread(target=try_enter)
    t1.start()
    t2.start()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    # Exactly one thread got ok=True, the other got ok=False
    oks = [r[0] for r in results]
    assert oks.count(True) == 1, f"expected 1 proceed, got {oks.count(True)}"
    assert oks.count(False) == 1
    # The blocked thread must have the INV-1 reason
    blocked = [r for r in results if not r[0]]
    assert blocked[0][1] == "INV-1_ALREADY_EVALUATED"


# ─── Lua script sanity ────────────────────────────────────────────────────


def test_lua_script_is_loaded_in_module():
    """Lua lives on RedisEntryEvalStore (Phase 3 store port), not entry_guard."""
    from stockapp_v12.adapters.stores import redis_stores as mod

    assert hasattr(mod, "_TRY_MARK_EVALUATED_LUA")
    lua = mod._TRY_MARK_EVALUATED_LUA
    assert "SADD" in lua
    assert "EXPIRE" in lua
    assert "return" in lua
