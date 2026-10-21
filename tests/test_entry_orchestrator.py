"""Tests for EntryOrchestrator (full Stage 4 wiring)."""

from unittest.mock import MagicMock

import pandas as pd

from stockapp_v12.core.execution.bracket_order import BracketResult
from stockapp_v12.pipeline.analyst import AnalystOutput
from stockapp_v12.pipeline.entry_guard import EntryEvaluationGuard
from stockapp_v12.pipeline.journal import TradeJournal
from stockapp_v12.schemas.forecast import build_forecast_output
from stockapp_v12.services.entry_orchestrator import EntryContext, EntryOrchestrator
from stockapp_v12.services.signal_rule_engine import (
    SR_PROCEED,
    SR_REGIME_GATE,
    SignalRuleEngine,
    SignalRuleResult,
)


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


def test_full_buy_path_with_mocks(fake_redis):
    # --- Mocks ---
    guard = EntryEvaluationGuard(fake_redis, mode="paper_ibkr")
    forecast = MagicMock()
    forecast.forecast_daily.return_value = _make_forecast()

    sr = MagicMock()
    sr.evaluate.return_value = SignalRuleResult(
        proceed=True, rule_code=SR_PROCEED, reason="ok", forecast=_make_forecast()
    )

    analyst_fn = MagicMock(return_value=(_make_analyst(), None))

    gatekeeper_fn = MagicMock(return_value=(True, "", {
        "size_pct": 0.025,
        "risk_amount": 1000,
        "stop_distance": 6.0,
    }))

    bracket = MagicMock()
    bracket.submit_entry_bracket.return_value = BracketResult(
        parent_order_id="p_123",
        sl_order_id="s_456",
        sl_price=94.0,
        submitted_qty=25,
        meta={"tier": "high_vol"},
    )

    pm = MagicMock()
    pm.register.return_value = MagicMock()

    journal = TradeJournal(fake_redis)

    orch = EntryOrchestrator(
        entry_guard=guard,
        forecast_service=forecast,
        sr_engine=sr,
        analyst_fn=analyst_fn,
        gatekeeper_fn=gatekeeper_fn,
        bracket_service=bracket,
        position_manager=pm,
        journal=journal,
    )

    ctx = EntryContext(
        symbol="NVDA",
        bars=_make_bars(),
        market_regime="BULL_LONG",
        actionable_symbols={"NVDA"},
        equity=100_000.0,
        mode="paper_ibkr",
    )

    res = orch.evaluate_and_maybe_enter(ctx)

    assert res.decision == "BUY"
    assert res.size_pct == 0.025
    assert res.parent_order_id == "p_123"
    assert res.sl_order_id == "s_456"
    assert analyst_fn.called
    assert gatekeeper_fn.called
    assert pm.register.called
    assert bracket.submit_entry_bracket.called


def test_sr_block_short_circuits(fake_redis):
    guard = EntryEvaluationGuard(fake_redis, mode="paper_ibkr")
    forecast = MagicMock()
    forecast.forecast_daily.return_value = _make_forecast()

    sr = MagicMock()
    sr_res = SignalRuleResult(proceed=False, rule_code="SR-01", reason="low return", forecast=_make_forecast())
    sr.evaluate.return_value = sr_res

    orch = EntryOrchestrator(
        entry_guard=guard,
        forecast_service=forecast,
        sr_engine=sr,
        analyst_fn=None,  # should not be called
        gatekeeper_fn=MagicMock(),
        bracket_service=MagicMock(),
        position_manager=MagicMock(),
        journal=TradeJournal(fake_redis),
    )

    ctx = EntryContext(
        symbol="NVDA",
        bars=_make_bars(),
        market_regime="BULL_LONG",
        actionable_symbols={"NVDA"},
        equity=100000,
    )

    res = orch.evaluate_and_maybe_enter(ctx)
    assert res.decision == "PASS"
    assert res.reason_code == "SR-01"
    assert res.sr_result is not None


# ---------------------------------------------------------------------------
# SR-07 wiring: orchestrator must populate regime_features from ctx.bars so
# the SignalRuleEngine can apply the regime gate.
# ---------------------------------------------------------------------------

def _make_bars_series(prices):
    """Build a bars DataFrame with `prices` as the close column."""
    return pd.DataFrame({"close": prices, "atr_14": [1.0] * len(prices)})


def _orch_with_real_sr(fake_redis, sr_engine, gatekeeper_return=None):
    """Build an orchestrator where the SignalRuleEngine is real (not a Mock)."""
    guard = EntryEvaluationGuard(fake_redis, mode="paper_ibkr")
    forecast = MagicMock()
    forecast.forecast_daily.return_value = _make_forecast()
    gk_ret = gatekeeper_return if gatekeeper_return is not None else (True, "", {
        "size_pct": 0.025, "risk_amount": 1000, "stop_distance": 6.0,
    })
    return EntryOrchestrator(
        entry_guard=guard,
        forecast_service=forecast,
        sr_engine=sr_engine,
        analyst_fn=None,
        gatekeeper_fn=MagicMock(return_value=gk_ret),
        bracket_service=MagicMock(),
        position_manager=MagicMock(),
        journal=TradeJournal(fake_redis),
    )


def test_entry_orchestrator_passes_regime_features_to_sr07(fake_redis):
    """When bars show close > sma_20, SR-07 should pass the trade.

    Verifies the orchestrator plumbs `close` and `sma_20` from ctx.bars into
    the SignalRuleContext.regime_features dict that SR-07 reads.
    """
    from dataclasses import replace
    sr = SignalRuleEngine(replace(SignalRuleEngine().config))  # already fresh
    orch = _orch_with_real_sr(fake_redis, sr)

    # 25 close values, all above the 20d SMA (trending up) — SR-07 should pass
    prices = [100.0 + i * 0.5 for i in range(25)]  # 100, 100.5, ..., 112
    ctx = EntryContext(
        symbol="NVDA",
        bars=_make_bars_series(prices),
        market_regime="BULL_LONG",
        actionable_symbols={"NVDA"},
        equity=100000,
    )
    res = orch.evaluate_and_maybe_enter(ctx)
    # SR-01 passes (+8% ret), SR-07 passes (close > sma_20), SR-06 disabled.
    # Gatekeeper mock returns a valid sizing decision, so we expect BUY.
    assert res.decision == "BUY", (
        f"expected BUY (SR-07 should pass on uptrending bars); got {res.decision}/{res.reason_code}"
    )


def test_entry_orchestrator_sr07_rejects_downtrend_bars(fake_redis):
    """When bars show close < sma_20 AND forecast magnitude is small, SR-07
    should fire and short-circuit the trade."""
    sr = SignalRuleEngine()
    forecast = MagicMock()
    # +0.8% ret → passes SR-01 (0.5% floor) but fails SR-07 (2% threshold)
    forecast.forecast_daily.return_value = build_forecast_output(
        symbol="NVDA", horizon_days=10, current_price=100.0,
        forecast_mean=100.8, model_id="test",
    )

    guard = EntryEvaluationGuard(fake_redis, mode="paper_ibkr")
    orch = EntryOrchestrator(
        entry_guard=guard, forecast_service=forecast, sr_engine=sr,
        analyst_fn=None, gatekeeper_fn=MagicMock(), bracket_service=MagicMock(),
        position_manager=MagicMock(), journal=TradeJournal(fake_redis),
    )

    # 25 close values, declining → close < sma_20
    prices = [110.0 - i * 1.0 for i in range(25)]
    ctx = EntryContext(
        symbol="NVDA",
        bars=_make_bars_series(prices),
        market_regime="BULL_LONG",
        actionable_symbols={"NVDA"},
        equity=100000,
    )
    res = orch.evaluate_and_maybe_enter(ctx)
    assert res.decision == "PASS"
    assert res.reason_code == SR_REGIME_GATE
    assert "regime gate failed" in res.sr_result.reason


def test_entry_orchestrator_sr07_disabled_ignores_bars(fake_redis):
    """When sr07_enabled=False, SR-07 is a no-op and the trade proceeds
    even if bars are below the SMA."""
    from dataclasses import replace
    base_cfg = SignalRuleEngine().config
    cfg = replace(base_cfg, sr07_enabled=False)
    sr = SignalRuleEngine(cfg)
    orch = _orch_with_real_sr(fake_redis, sr)

    prices = [110.0 - i * 1.0 for i in range(25)]  # declining
    ctx = EntryContext(
        symbol="NVDA",
        bars=_make_bars_series(prices),
        market_regime="BULL_LONG",
        actionable_symbols={"NVDA"},
        equity=100000,
    )
    res = orch.evaluate_and_maybe_enter(ctx)
    # SR-07 disabled, so the trade should pass SR and reach the gatekeeper
    assert res.decision == "BUY", (
        f"expected BUY (SR-07 disabled); got {res.decision}/{res.reason_code}"
    )
