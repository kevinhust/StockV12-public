"""Tests for VetoCounter / VetoRateAlarm / MinimumTradeFloorEscape (Phase A4).

Strategy:
- Use FakeRedis for stateful Redis interaction tests.
- Use EntryOrchestrator with mocked services to verify the _record_veto
  wrapper is invoked (and is a no-op when veto_counter=None).
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest

from stockapp_v12.pipeline.analyst import AnalystOutput
from stockapp_v12.pipeline.journal import TradeJournal
from stockapp_v12.schemas.forecast import build_forecast_output
from stockapp_v12.services.entry_orchestrator import EntryOrchestrator
from stockapp_v12.services.veto_alarm import (
    MinimumTradeFloorEscape,
    VetoCounter,
    VetoRateAlarm,
    VetoThresholds,
    _bucket_for,
)

from ._fakes import FakeRedis


# ============================================================================
# _bucket_for helper
# ============================================================================


class TestBucketFor:
    def test_buy(self):
        assert _bucket_for("BUY") == "buy"

    def test_pass(self):
        assert _bucket_for("PASS") == "pass"

    def test_deny(self):
        assert _bucket_for("DENY") == "deny"

    def test_allow(self):
        assert _bucket_for("ALLOW") == "allow"

    def test_case_insensitive(self):
        assert _bucket_for("buy") == "buy"
        assert _bucket_for("Pass") == "pass"

    def test_unknown_returns_none(self):
        assert _bucket_for("FOO") is None
        assert _bucket_for("") is None
        assert _bucket_for(None) is None


# ============================================================================
# VetoCounter
# ============================================================================


class TestVetoCounterRecord:
    def test_record_buy_increments_buy_and_total(self):
        r = FakeRedis()
        c = VetoCounter(r)
        c.record("BUY", date="2026-01-15")
        snap = c.snapshot(date="2026-01-15")
        assert snap.buy == 1
        assert snap.total == 1
        assert snap.pass_ == 0
        assert snap.deny == 0
        assert snap.allow == 0

    def test_record_increments_correct_bucket(self):
        r = FakeRedis()
        c = VetoCounter(r)
        c.record("PASS", date="2026-01-15")
        c.record("PASS", date="2026-01-15")
        c.record("DENY", date="2026-01-15")
        c.record("ALLOW", date="2026-01-15")
        snap = c.snapshot(date="2026-01-15")
        assert snap.pass_ == 2
        assert snap.deny == 1
        assert snap.allow == 1
        assert snap.total == 4

    def test_record_unknown_decision_returns_false(self):
        r = FakeRedis()
        c = VetoCounter(r)
        assert c.record("FOO", date="2026-01-15") is False
        snap = c.snapshot(date="2026-01-15")
        assert snap.total == 0

    def test_record_n_multi(self):
        r = FakeRedis()
        c = VetoCounter(r)
        c.record("BUY", date="2026-01-15", n=5)
        snap = c.snapshot(date="2026-01-15")
        assert snap.buy == 5
        assert snap.total == 5

    def test_record_failure_is_swallowed(self):
        r = FakeRedis()
        r.fail_with(ConnectionError("redis down"))
        c = VetoCounter(r)
        # Should NOT raise — best-effort writes only
        assert c.record("BUY", date="2026-01-15") is False


class TestVetoCounterSnapshot:
    def test_empty_day_returns_zeros(self):
        r = FakeRedis()
        c = VetoCounter(r)
        snap = c.snapshot(date="2026-02-01")
        assert snap.date == "2026-02-01"
        assert snap.buy == 0
        assert snap.total == 0
        assert snap.veto_rate == 0.0

    def test_veto_rate_computation(self):
        r = FakeRedis()
        c = VetoCounter(r)
        # 1 buy, 4 vetoes => veto_rate = 0.80
        for _ in range(1):
            c.record("BUY", date="2026-01-15")
        for _ in range(4):
            c.record("DENY", date="2026-01-15")
        snap = c.snapshot(date="2026-01-15")
        assert snap.total == 5
        assert snap.veto_rate == pytest.approx(0.80)

    def test_all_buy_zero_veto_rate(self):
        r = FakeRedis()
        c = VetoCounter(r)
        for _ in range(10):
            c.record("BUY", date="2026-01-15")
        snap = c.snapshot(date="2026-01-15")
        assert snap.veto_rate == 0.0

    def test_all_veto_one_veto_rate(self):
        r = FakeRedis()
        c = VetoCounter(r)
        for _ in range(10):
            c.record("DENY", date="2026-01-15")
        snap = c.snapshot(date="2026-01-15")
        assert snap.veto_rate == 1.0

    def test_snapshot_failure_returns_zeros(self):
        r = FakeRedis()
        r.fail_with(ConnectionError("redis down"))
        c = VetoCounter(r)
        snap = c.snapshot(date="2026-01-15")
        assert snap.total == 0
        assert snap.veto_rate == 0.0

    def test_uses_now_provider_when_no_date(self):
        r = FakeRedis()
        fixed = datetime(2026, 3, 15, 12, 0, 0)
        c = VetoCounter(r, now_provider=lambda: fixed)
        c.record("BUY")  # uses now_provider's date
        snap = c.snapshot()
        assert snap.date == "2026-03-15"
        assert snap.buy == 1


# ============================================================================
# VetoRateAlarm
# ============================================================================


class TestVetoRateAlarmEvaluate:
    def test_sample_too_small_skips_alarm(self):
        r = FakeRedis()
        c = VetoCounter(r)
        thr = VetoThresholds(min_alarm_n_total=5)
        # Only 3 decisions
        for _ in range(3):
            c.record("DENY", date="2026-01-15")
        alarm = VetoRateAlarm(c, thresholds=thr)
        snap = alarm.evaluate(date="2026-01-15")
        assert snap.alarm is False
        assert "sample too small" in snap.alarm_reason

    def test_above_threshold_fires_alarm(self):
        r = FakeRedis()
        c = VetoCounter(r)
        # 1 buy + 9 denys = 10 decisions, veto_rate 0.9 > 0.8
        c.record("BUY", date="2026-01-15")
        for _ in range(9):
            c.record("DENY", date="2026-01-15")
        alarm = VetoRateAlarm(c)  # default thresholds
        snap = alarm.evaluate(date="2026-01-15")
        assert snap.alarm is True
        assert "veto_rate" in snap.alarm_reason
        assert "0.90" in snap.alarm_reason

    def test_below_threshold_no_alarm(self):
        r = FakeRedis()
        c = VetoCounter(r)
        # 5 buys + 2 denys = 7 decisions, veto_rate 0.286
        for _ in range(5):
            c.record("BUY", date="2026-01-15")
        for _ in range(2):
            c.record("DENY", date="2026-01-15")
        alarm = VetoRateAlarm(c)
        snap = alarm.evaluate(date="2026-01-15")
        assert snap.alarm is False
        assert "within" in snap.alarm_reason

    def test_threshold_boundary_at_max_does_not_alarm(self):
        """veto_rate == veto_rate_max is NOT an alarm (strict >)."""
        r = FakeRedis()
        c = VetoCounter(r)
        # 1 buy + 4 denys = 5 decisions, veto_rate exactly 0.8
        c.record("BUY", date="2026-01-15")
        for _ in range(4):
            c.record("DENY", date="2026-01-15")
        alarm = VetoRateAlarm(c)
        snap = alarm.evaluate(date="2026-01-15")
        assert snap.alarm is False


class TestVetoRateAlarmRaise:
    def test_raise_alarm_writes_key(self):
        r = FakeRedis()
        c = VetoCounter(r)
        for _ in range(10):
            c.record("DENY", date="2026-01-15")
        alarm = VetoRateAlarm(c)
        snap = alarm.evaluate(date="2026-01-15")
        assert snap.alarm is True
        ok = alarm.raise_alarm(snap, redis_client=r)
        assert ok is True
        assert r.get("system:alarm:veto_high:2026-01-15") is not None

    def test_raise_alarm_skips_when_not_alarmed(self):
        r = FakeRedis()
        c = VetoCounter(r)
        for _ in range(5):
            c.record("BUY", date="2026-01-15")
        alarm = VetoRateAlarm(c)
        snap = alarm.evaluate(date="2026-01-15")
        assert snap.alarm is False
        ok = alarm.raise_alarm(snap, redis_client=r)
        assert ok is False
        assert r.get("system:alarm:veto_high:2026-01-15") is None

    def test_raise_alarm_requires_redis_client(self):
        r = FakeRedis()
        c = VetoCounter(r)
        for _ in range(10):
            c.record("DENY", date="2026-01-15")
        alarm = VetoRateAlarm(c)
        snap = alarm.evaluate(date="2026-01-15")
        # No redis_client => no-op (never raises)
        assert alarm.raise_alarm(snap) is False

    def test_is_alarmed(self):
        r = FakeRedis()
        c = VetoCounter(r)
        alarm = VetoRateAlarm(c)
        for _ in range(10):
            c.record("DENY", date="2026-01-15")
        snap = alarm.evaluate(date="2026-01-15")
        alarm.raise_alarm(snap, redis_client=r)
        assert alarm.is_alarmed(redis_client=r, date="2026-01-15") is True
        assert alarm.is_alarmed(redis_client=r, date="2099-12-31") is False


# ============================================================================
# MinimumTradeFloorEscape
# ============================================================================


class TestMinimumTradeFloorEscape:
    def test_active_when_zero_buys_for_n_days_normal_regime(self):
        r = FakeRedis()
        c = VetoCounter(r)
        # 5 days, zero buys across all of them
        thr = VetoThresholds(floor_escape_min_days=5)
        floor = MinimumTradeFloorEscape(c, thresholds=thr)
        adv = floor.evaluate(current_regime="BULL_NORMAL", today="2026-01-15")
        assert adv.active is True
        assert adv.days_with_zero_buys == 5
        assert adv.panic_regime is False
        assert "guard" in adv.message.lower() or "tight" in adv.message.lower()

    def test_inactive_when_recent_buys(self):
        r = FakeRedis()
        c = VetoCounter(r)
        # Today has 2 buys — streak broken
        c.record("BUY", date="2026-01-15")
        c.record("BUY", date="2026-01-15")
        thr = VetoThresholds(floor_escape_min_days=5)
        floor = MinimumTradeFloorEscape(c, thresholds=thr)
        adv = floor.evaluate(current_regime="BULL_NORMAL", today="2026-01-15")
        assert adv.active is False
        assert adv.days_with_zero_buys == 0

    def test_inactive_in_panic_regime_even_with_zero_buys(self):
        r = FakeRedis()
        c = VetoCounter(r)
        thr = VetoThresholds(floor_escape_min_days=5, panic_regimes=("PANIC",))
        floor = MinimumTradeFloorEscape(c, thresholds=thr)
        adv = floor.evaluate(current_regime="PANIC", today="2026-01-15")
        assert adv.active is False
        assert adv.days_with_zero_buys == 5  # streak observed
        assert adv.panic_regime is True
        assert "PANIC" in adv.message

    def test_inactive_when_streak_below_threshold(self):
        r = FakeRedis()
        c = VetoCounter(r)
        thr = VetoThresholds(floor_escape_min_days=5)
        floor = MinimumTradeFloorEscape(c, thresholds=thr)
        # Today has 0, but yesterday (2026-01-14) had a buy — streak = 1
        c.record("BUY", date="2026-01-14")
        adv = floor.evaluate(current_regime="BULL_NORMAL", today="2026-01-15")
        assert adv.active is False
        assert adv.days_with_zero_buys == 1

    def test_handles_default_today(self):
        r = FakeRedis()
        c = VetoCounter(r)
        thr = VetoThresholds(floor_escape_min_days=3)
        floor = MinimumTradeFloorEscape(c, thresholds=thr)
        # No explicit today — should default to today (no buys)
        adv = floor.evaluate(current_regime="BULL_NORMAL")
        assert adv.window_days == 3
        assert adv.days_with_zero_buys == 3
        assert adv.active is True


# ============================================================================
# EntryOrchestrator._record_veto wiring
# ============================================================================


def _make_bars(price=100.0, atr=5.0):
    return pd.DataFrame({
        "close": [price],
        "atr_14": [atr],
    })


def _make_forecast():
    return build_forecast_output(
        symbol="NVDA",
        horizon_days=10,
        current_price=100.0,
        forecast_mean=108.0,
        model_id="test",
    )


def _make_analyst():
    return AnalystOutput(
        symbol="NVDA",
        side="BUY",
        confidence=0.82,
        thesis_tags=["earnings_beat"],
        invalidators=[],
        expected_holding_days=7,
        entry_rationale="Strong momentum and catalyst.",
    )


def _make_journal():
    """Tiny in-memory TradeJournal that won't touch Redis."""
    j = MagicMock(spec=TradeJournal)
    j.log_pass = MagicMock(return_value=True)
    j.log_deny = MagicMock(return_value=True)
    j.log_buy = MagicMock(return_value=True)
    j.log_exit = MagicMock(return_value=True)
    return j


def _make_orchestrator_with_counter(counter: VetoCounter | None):
    """Minimal EntryOrchestrator that won't run real services.

    We bypass __init__ via __new__ to keep the test purely about the
    _record_veto wrapper behavior.
    """
    orch = EntryOrchestrator.__new__(EntryOrchestrator)
    orch.veto_counter = counter
    return orch


def _fake_entry_result(decision: str):
    """Build a minimal EntryResult for the wrapper test."""
    from stockapp_v12.services.entry_orchestrator import EntryResult
    return EntryResult(decision=decision, reason_code="test")


class TestEntryOrchestratorRecordVeto:
    def test_record_veto_calls_counter_with_decision(self):
        r = FakeRedis()
        c = VetoCounter(r)
        orch = _make_orchestrator_with_counter(c)
        res = _fake_entry_result("BUY")
        out = orch._record_veto(res)
        assert out is res
        snap = c.snapshot(date=datetime.now().strftime("%Y-%m-%d"))
        assert snap.buy == 1

    def test_record_veto_no_op_when_counter_none(self):
        orch = _make_orchestrator_with_counter(None)
        res = _fake_entry_result("PASS")
        # Should not raise
        assert orch._record_veto(res) is res

    def test_record_veto_swallows_counter_errors(self):
        r = FakeRedis()
        c = VetoCounter(r)
        r.fail_with(ConnectionError("redis down"))
        orch = _make_orchestrator_with_counter(c)
        res = _fake_entry_result("DENY")
        # Best-effort: no exception, returns result
        assert orch._record_veto(res) is res

    def test_record_veto_counts_each_decision_type(self):
        r = FakeRedis()
        c = VetoCounter(r)
        orch = _make_orchestrator_with_counter(c)
        today = datetime.now().strftime("%Y-%m-%d")
        for _ in range(3):
            orch._record_veto(_fake_entry_result("BUY"))
        for _ in range(2):
            orch._record_veto(_fake_entry_result("PASS"))
        for _ in range(1):
            orch._record_veto(_fake_entry_result("DENY"))
        snap = c.snapshot(date=today)
        assert snap.buy == 3
        assert snap.pass_ == 2
        assert snap.deny == 1
        assert snap.total == 6