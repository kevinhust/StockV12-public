"""Tests for KillSwitch (Phase A1)."""

from stockapp_v12.bridge.killswitch import (
    HALT_KEY,
    HALT_REASON_KEY,
    HaltState,
    KillSwitch,
)

from ._fakes import FakeRedis  # sibling helper (uses pytest package semantics)  # noqa: F401  (sibling-module import)


def test_is_halted_false_when_no_key():
    r = FakeRedis()
    ks = KillSwitch(r)
    assert ks.is_halted() is False
    assert ks.get_halt_reason() == ""


def test_is_halted_true_when_key_present():
    r = FakeRedis()
    r.set(HALT_KEY, "1")
    r.set(HALT_REASON_KEY, "market_crash")
    r.set("system:halt:at", "2026-07-07T00:00:00+00:00")
    r.set("system:halt:by", "unit_test")
    ks = KillSwitch(r)
    state = ks.status()
    assert state.is_halted is True
    assert state.reason == "market_crash"
    assert state.halted_at == "2026-07-07T00:00:00+00:00"
    assert state.halted_by == "unit_test"


def test_halt_sets_key_and_reason():
    r = FakeRedis()
    ks = KillSwitch(r)
    state = ks.halt("drift_detected", halted_by="scheduler")
    assert state.is_halted is True
    assert state.reason == "drift_detected"
    assert state.halted_by == "scheduler"
    # Reading back: key should be set
    assert r.get(HALT_KEY) == "1"
    assert r.get(HALT_REASON_KEY) == "drift_detected"


def test_halt_with_ttl_uses_setex():
    r = FakeRedis()
    ks = KillSwitch(r)
    ks.halt("timed_test", halted_by="x", ttl_seconds=600)
    assert r.get(HALT_KEY) == "1"


def test_resume_returns_true_when_cleared():
    r = FakeRedis()
    r.set(HALT_KEY, "1")
    ks = KillSwitch(r)
    cleared = ks.resume(resumed_by="operator")
    assert cleared is True
    assert r.get(HALT_KEY) is None
    assert r.get(HALT_REASON_KEY) is None


def test_resume_returns_false_when_no_halt():
    r = FakeRedis()
    ks = KillSwitch(r)
    cleared = ks.resume(resumed_by="operator")
    assert cleared is False


def test_fail_closed_redis_error_treats_as_halted():
    r = FakeRedis()
    r.fail_with(ConnectionError("redis down"))
    ks = KillSwitch(r, fail_closed_on_error=True)
    state = ks.status()
    assert state.is_halted is True
    assert "redis_unreachable" in state.reason


def test_fail_open_redis_error_treats_as_not_halted():
    r = FakeRedis()
    r.fail_with(ConnectionError("redis down"))
    ks = KillSwitch(r, fail_closed_on_error=False)
    state = ks.status()
    assert state.is_halted is False


def test_require_not_halted_raises_when_halted():
    state = HaltState(is_halted=True, reason="manual")
    raised = False
    try:
        KillSwitch.require_not_halted(state)
    except SystemExit as e:
        raised = True
        assert "manual" in str(e)
    assert raised, "expected SystemExit when halted"


def test_require_not_halted_no_op_when_not_halted():
    state = HaltState(is_halted=False, reason="")
    # Should not raise
    KillSwitch.require_not_halted(state)
