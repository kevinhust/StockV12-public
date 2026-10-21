"""Regression tests for P0-4 (council 2026-07-09): R-05 daily-trade counter
must be wired into the orchestrator's Stage 7 register path so production
entries can no longer bypass the daily trade cap.

Wiring:
  - ``increment_daily_trade_count(redis_client, mode=None)`` accepts an
    optional mode arg (P0-4 added).
  - ``EntryOrchestrator.__init__`` accepts an optional
    ``daily_counter_fn: Callable[[], int]`` (None preserves old behaviour).
  - ``run_stage_7_register`` calls ``orch.daily_counter_fn()`` after
    ``pm.register`` succeeds.
  - Failures are fail-closed: logged at WARNING + swallowed, but the
    position is still on the book (already registered), and a follow-up
    cap-check may undercount by 1 until Redis recovers.
"""

from __future__ import annotations

from unittest.mock import MagicMock


from stockapp_v12.bridge.gatekeeper import increment_daily_trade_count
from stockapp_v12.services.entry_orchestrator import EntryOrchestrator


# ─── increment_daily_trade_count signature ─────────────────────────────────


def test_increment_daily_trade_count_signature_accepts_optional_mode():
    """P0-4: ``mode`` is now an optional kwarg.

    Pre-fix the function was ``increment_daily_trade_count(redis_client)``.
    Post-fix the call site is ``increment_daily_trade_count(redis_client, mode)``.
    """
    captured_pipe = MagicMock()
    captured_pipe.execute.return_value = [42, 1]

    r = MagicMock()
    r.pipeline.return_value = captured_pipe

    new_count = increment_daily_trade_count(r, mode="paper_ibkr")
    assert new_count == 42
    # Pipeline was exercised — incr + expire.
    captured_pipe.incr.assert_called_once()
    captured_pipe.expire.assert_called_once()


def test_increment_daily_trade_count_works_without_mode():
    """P0-4: backtest call sites that pass only ``redis_client`` still work."""
    captured_pipe = MagicMock()
    captured_pipe.execute.return_value = [42, 1]
    r = MagicMock()
    r.pipeline.return_value = captured_pipe

    new_count = increment_daily_trade_count(r)
    assert new_count == 42


# ─── EntryOrchestrator wiring ──────────────────────────────────────────────


def test_entry_orchestrator_has_daily_counter_fn_attribute():
    """P0-4: orchestrator must expose ``daily_counter_fn`` (None default)."""
    # Build a minimal orch with required collaborators.
    from stockapp_v12.pipeline.entry_guard import EntryEvaluationGuard
    from stockapp_v12.services.signal_rule_engine import SignalRuleEngine

    orch = EntryOrchestrator(
        entry_guard=MagicMock(spec=EntryEvaluationGuard),
        forecast_service=MagicMock(),
        sr_engine=MagicMock(spec=SignalRuleEngine),
        gatekeeper_fn=MagicMock(),
        bracket_service=MagicMock(),
        position_manager=MagicMock(),
        journal=MagicMock(),
    )
    assert hasattr(orch, "daily_counter_fn")
    assert orch.daily_counter_fn is None  # default is None (backtest-safe)


def test_entry_orchestrator_accepts_daily_counter_fn_kwarg():
    """P0-4: passing a callable works without errors."""
    from stockapp_v12.pipeline.entry_guard import EntryEvaluationGuard
    from stockapp_v12.services.signal_rule_engine import SignalRuleEngine

    counter = MagicMock(return_value=7)
    orch = EntryOrchestrator(
        entry_guard=MagicMock(spec=EntryEvaluationGuard),
        forecast_service=MagicMock(),
        sr_engine=MagicMock(spec=SignalRuleEngine),
        gatekeeper_fn=MagicMock(),
        bracket_service=MagicMock(),
        position_manager=MagicMock(),
        journal=MagicMock(),
        daily_counter_fn=counter,
    )
    assert orch.daily_counter_fn is counter


# ─── run_stage_7_register calls daily_counter_fn ───────────────────────────


def test_run_stage_7_register_calls_daily_counter_fn(monkeypatch):
    """P0-4: after pm.register succeeds, run_stage_7_register MUST invoke
    ``orch.daily_counter_fn`` to bump the R-05 counter.
    """
    from stockapp_v12.pipeline import stages
    from stockapp_v12.services.entry_orchestrator import EntryContext

    counter = MagicMock(return_value=11)
    pm = MagicMock()
    journal = MagicMock()
    orch = MagicMock()
    orch.pm = pm
    orch.journal = journal
    orch.daily_counter_fn = counter

    # Build a minimal but valid ctx/forecast/sr/etc.
    ctx = MagicMock(spec=EntryContext)
    ctx.mode = "paper_ibkr"
    ctx.market_regime = "BULL_LONG"

    bracket_res = MagicMock()
    bracket_res.parent_order_id = "PARENT-1"
    bracket_res.sl_order_id = "SL-1"
    bracket_res.sl_price = 100.0

    analyst = MagicMock()
    analyst.thesis_tags = ["tag"]
    analyst.expected_holding_days = 5
    analyst.to_dict.return_value = {"thesis_tags": ["tag"]}

    forecast = MagicMock()
    sr = MagicMock()
    sr.rule_code = "OK"

    result = stages.run_stage_7_register(
        orch, sym="AAPL", ctx=ctx,
        current_price=150.0, atr_14=2.0, atr_pct=0.013,
        bracket_res=bracket_res, qty=10, size_pct=0.05,
        forecast=forecast, sr=sr, analyst=analyst,
        yolo_decision=MagicMock(),
    )

    # pm.register MUST have been called BEFORE the counter bumps.
    assert pm.register.call_count == 1
    assert counter.call_count == 1, "P0-4 regression: daily_counter_fn was not called"
    assert result.decision == "BUY"
    assert result.reason_code == "OK"


def test_run_stage_7_register_skips_counter_when_none(monkeypatch):
    """P0-4: when ``daily_counter_fn is None`` (legacy call sites), the
    stage must still complete — no AttributeError, no log noise.
    """
    from stockapp_v12.pipeline import stages
    from stockapp_v12.services.entry_orchestrator import EntryContext

    pm = MagicMock()
    journal = MagicMock()
    orch = MagicMock()
    orch.pm = pm
    orch.journal = journal
    orch.daily_counter_fn = None  # legacy

    ctx = MagicMock(spec=EntryContext)
    ctx.mode = "paper_ibkr"
    ctx.market_regime = "BULL_LONG"

    bracket_res = MagicMock()
    bracket_res.parent_order_id = "PARENT-1"
    bracket_res.sl_order_id = "SL-1"
    bracket_res.sl_price = 100.0

    analyst = MagicMock()
    analyst.thesis_tags = ["tag"]
    analyst.expected_holding_days = 5
    analyst.to_dict.return_value = {"thesis_tags": ["tag"]}

    stages.run_stage_7_register(
        orch, sym="MSFT", ctx=ctx,
        current_price=400.0, atr_14=5.0, atr_pct=0.012,
        bracket_res=bracket_res, qty=5, size_pct=0.04,
        forecast=MagicMock(), sr=MagicMock(rule_code="OK"),
        analyst=analyst, yolo_decision=MagicMock(),
    )
    assert pm.register.call_count == 1


def test_run_stage_7_register_swallows_counter_failure(caplog):
    """P0-4 fail-closed posture: a Redis exception in the counter MUST NOT
    propagate to the trading path. Position is already registered, journal
    is already written, so we log + carry on (per council synthesis).
    """
    import logging

    from stockapp_v12.pipeline import stages
    from stockapp_v12.services.entry_orchestrator import EntryContext

    counter = MagicMock(side_effect=RuntimeError("redis down"))
    pm = MagicMock()
    journal = MagicMock()
    orch = MagicMock()
    orch.pm = pm
    orch.journal = journal
    orch.daily_counter_fn = counter

    ctx = MagicMock(spec=EntryContext)
    ctx.mode = "paper_ibkr"
    ctx.market_regime = "BULL_LONG"

    bracket_res = MagicMock()
    bracket_res.parent_order_id = "PARENT-1"
    bracket_res.sl_order_id = "SL-1"
    bracket_res.sl_price = 100.0

    analyst = MagicMock()
    analyst.thesis_tags = ["tag"]
    analyst.expected_holding_days = 5
    analyst.to_dict.return_value = {"thesis_tags": ["tag"]}

    with caplog.at_level(logging.WARNING, logger="stockapp_v12.pipeline.stages"):
        result = stages.run_stage_7_register(
            orch, sym="NVDA", ctx=ctx,
            current_price=900.0, atr_14=15.0, atr_pct=0.017,
            bracket_res=bracket_res, qty=2, size_pct=0.02,
            forecast=MagicMock(), sr=MagicMock(rule_code="OK"),
            analyst=analyst, yolo_decision=MagicMock(),
        )

    # Position still considered opened.
    assert result.decision == "BUY"
    # The failure was logged.
    assert any(
        "R-05" in r.message and "fail" in r.message.lower()
        for r in caplog.records
    )
