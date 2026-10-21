"""TS-2: SignalRuleEngine SR-01..SR-05."""
from stockapp_v12.schemas.forecast import build_forecast_output
from stockapp_v12.services.signal_rule_engine import (
    SR_MAX_UNCERTAINTY,
    SR_MIN_EXPECTED_RETURN,
    SR_NOT_ACTIONABLE,
    SR_PROCEED,
    SR_REGIME_BLOCK,
    SignalRuleConfig,
    SignalRuleContext,
    SignalRuleEngine,
)


def _forecast(**kwargs):
    defaults = dict(
        symbol="NVDA",
        horizon_days=10,
        current_price=100.0,
        forecast_mean=105.0,
        model_id="chronos-2-zero",
    )
    defaults.update(kwargs)
    return build_forecast_output(**defaults)


def _ctx(**kwargs):
    defaults = dict(market_regime="BULL_LONG", actionable_symbols={"NVDA"})
    defaults.update(kwargs)
    return SignalRuleContext(**defaults)


def test_sr05_proceed():
    engine = SignalRuleEngine()
    result = engine.evaluate(_forecast(), _ctx())
    assert result.proceed is True
    assert result.rule_code == SR_PROCEED


def test_sr04_not_actionable():
    engine = SignalRuleEngine()
    result = engine.evaluate(_forecast(), _ctx(actionable_symbols={"AAPL"}))
    assert result.proceed is False
    assert result.rule_code == SR_NOT_ACTIONABLE


def test_sr01_low_expected_return():
    engine = SignalRuleEngine(SignalRuleConfig(min_expected_return_pct=0.10))
    result = engine.evaluate(
        _forecast(forecast_mean=100.5),
        _ctx(),
    )
    assert result.rule_code == SR_MIN_EXPECTED_RETURN


def test_sr02_high_uncertainty():
    engine = SignalRuleEngine(SignalRuleConfig(max_uncertainty_pct=0.01))
    result = engine.evaluate(
        _forecast(forecast_q10=80.0, forecast_q90=130.0),
        _ctx(),
    )
    assert result.rule_code == SR_MAX_UNCERTAINTY


def test_sr03_regime_block():
    engine = SignalRuleEngine()
    result = engine.evaluate(_forecast(), _ctx(market_regime="PANIC"))
    assert result.rule_code == SR_REGIME_BLOCK


def test_sr03_regime_hint_block():
    engine = SignalRuleEngine()
    result = engine.evaluate(
        _forecast(regime_hint="BEAR_LONG"),
        _ctx(market_regime="NEUTRAL"),
    )
    assert result.rule_code == SR_REGIME_BLOCK