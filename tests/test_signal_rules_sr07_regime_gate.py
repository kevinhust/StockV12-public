"""TS-SR07: SignalRuleEngine SR-07 regime gate.

The gate admits a trade if:
  (a) the model's forecast magnitude meets sr07_min_forecast_pct (high
      conviction override), OR
  (b) the symbol's last close is above its N-day SMA (trend-following).

Backtested winner from `v12/scripts/validate_regime_gate.py` on 2 regimes
× 2 cutoffs. Worst-case hit_rate 0.50, mean 0.875, mean volume 3.25/horizon.
"""
from __future__ import annotations


from stockapp_v12.schemas.forecast import build_forecast_output
from stockapp_v12.services.signal_rule_engine import (
    SR_PROCEED,
    SR_REGIME_GATE,
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
    defaults = dict(
        market_regime="BULL_LONG",
        actionable_symbols={"NVDA"},
    )
    defaults.update(kwargs)
    return SignalRuleContext(**defaults)


def _regime(close, sma_20):
    return {"NVDA": {"close": close, "sma_20": sma_20}}


# --- happy paths ----------------------------------------------------------

def test_sr07_pass_via_high_conviction_forecast():
    """Forecast magnitude ≥ 2 % bypasses the trend check (high-conviction
    override)."""
    engine = SignalRuleEngine()
    fc = _forecast(forecast_mean=110.0)  # +10 % ret, |ret| ≥ 0.02
    # trend fails: close below SMA
    ctx = _ctx(regime_features=_regime(close=95.0, sma_20=100.0))
    result = engine.evaluate(fc, ctx)
    assert result.proceed is True
    assert result.rule_code == SR_PROCEED


def test_sr07_pass_via_trend_following():
    """Close > SMA passes even with a tiny forecast magnitude."""
    engine = SignalRuleEngine()
    fc = _forecast(forecast_mean=100.5)  # +0.5 %, |ret| < 0.02 → fc_pass=False
    ctx = _ctx(regime_features=_regime(close=110.0, sma_20=100.0))
    result = engine.evaluate(fc, ctx)
    assert result.proceed is True
    assert result.rule_code == SR_PROCEED


def test_sr07_pass_via_both_clauses():
    """Both clauses can fire simultaneously (no precedence conflict)."""
    engine = SignalRuleEngine()
    fc = _forecast(forecast_mean=110.0)
    ctx = _ctx(regime_features=_regime(close=110.0, sma_20=100.0))
    result = engine.evaluate(fc, ctx)
    assert result.proceed is True
    assert result.rule_code == SR_PROCEED


# --- fail paths -----------------------------------------------------------

def test_sr07_fail_close_below_sma_and_weak_forecast():
    engine = SignalRuleEngine()
    fc = _forecast(forecast_mean=100.5)  # +0.5 % < 2 %
    ctx = _ctx(regime_features=_regime(close=95.0, sma_20=100.0))
    result = engine.evaluate(fc, ctx)
    assert result.proceed is False
    assert result.rule_code == SR_REGIME_GATE
    assert "regime gate failed" in result.reason


def test_sr07_fail_close_equal_to_sma_treated_as_below():
    """close == sma is the boundary — must NOT pass via trend clause
    (matches validate_regime_gate.py semantics: `close > sma20`)."""
    engine = SignalRuleEngine()
    fc = _forecast(forecast_mean=100.5)
    ctx = _ctx(regime_features=_regime(close=100.0, sma_20=100.0))
    result = engine.evaluate(fc, ctx)
    assert result.proceed is False
    assert result.rule_code == SR_REGIME_GATE


def test_sr07_fail_short_signal_below_threshold():
    """Short forecast (negative ret) with magnitude < 2 % and below SMA
    must be rejected — symmetric with the long case.

    Note: production SR-01 floor (0.5 %) rejects all negative returns before
    SR-07 runs (the pipeline is long-only). To exercise SR-07 on a short
    signal here, we lower SR-01 to a permissive value (-1.0).
    """
    cfg = SignalRuleConfig(min_expected_return_pct=-1.0)
    engine = SignalRuleEngine(cfg)
    fc = _forecast(forecast_mean=99.5)  # -0.5 %
    ctx = _ctx(regime_features=_regime(close=95.0, sma_20=100.0))
    result = engine.evaluate(fc, ctx)
    assert result.proceed is False
    assert result.rule_code == SR_REGIME_GATE


# --- fail-closed on missing regime_features -------------------------------
#
# All these tests use forecast_mean=100.5 (+0.5 % ret) so the high-conviction
# clause (|fc| ≥ 2 %) does NOT fire — forcing the trend clause (and therefore
# the missing-data path) to be the only thing under test.

def test_sr07_fail_when_regime_features_missing_for_symbol():
    """If the orchestrator didn't populate features for this symbol, fail
    closed (same posture as SR-04)."""
    engine = SignalRuleEngine()
    fc = _forecast(forecast_mean=100.5)  # +0.5 %, |ret| < 2 % → fc_pass=False
    ctx = _ctx(regime_features={})  # symbol absent
    result = engine.evaluate(fc, ctx)
    assert result.proceed is False
    assert result.rule_code == SR_REGIME_GATE


def test_sr07_fail_when_close_key_missing():
    engine = SignalRuleEngine()
    fc = _forecast(forecast_mean=100.5)
    ctx = _ctx(regime_features={"NVDA": {"sma_20": 100.0}})  # no close
    result = engine.evaluate(fc, ctx)
    assert result.proceed is False
    assert result.rule_code == SR_REGIME_GATE


def test_sr07_fail_when_sma_key_missing():
    engine = SignalRuleEngine()
    fc = _forecast(forecast_mean=100.5)
    ctx = _ctx(regime_features={"NVDA": {"close": 105.0}})  # no sma_20
    result = engine.evaluate(fc, ctx)
    assert result.proceed is False
    assert result.rule_code == SR_REGIME_GATE


def test_sr07_fail_when_values_are_nan():
    """pandas NaN must not silently pass via `close > sma` (NaN comparisons
    are False, but the inputs could also be Python float('nan'))."""
    engine = SignalRuleEngine()
    fc = _forecast(forecast_mean=100.5)
    ctx = _ctx(regime_features={"NVDA": {"close": float("nan"), "sma_20": 100.0}})
    result = engine.evaluate(fc, ctx)
    assert result.proceed is False
    assert result.rule_code == SR_REGIME_GATE


# --- configurability ------------------------------------------------------

def test_sr07_disabled_skips_gate_check():
    """When sr07_enabled=False, neither clause fires (back-compat for
    pre-gate deployments that want SR-01..SR-06 only)."""
    cfg = SignalRuleConfig(sr07_enabled=False)
    engine = SignalRuleEngine(cfg)
    fc = _forecast(forecast_mean=100.5)
    ctx = _ctx(regime_features=_regime(close=95.0, sma_20=100.0))
    result = engine.evaluate(fc, ctx)
    assert result.proceed is True
    assert result.rule_code == SR_PROCEED


def test_sr07_min_forecast_pct_threshold_is_used():
    """Raising the threshold to 3 % must reject the +2.5 % forecast that
    would otherwise pass."""
    cfg = SignalRuleConfig(sr07_min_forecast_pct=0.03)
    engine = SignalRuleEngine(cfg)
    fc = _forecast(forecast_mean=102.5)  # +2.5 % (< 3 % threshold)
    ctx = _ctx(regime_features=_regime(close=95.0, sma_20=100.0))
    result = engine.evaluate(fc, ctx)
    assert result.proceed is False
    assert result.rule_code == SR_REGIME_GATE


def test_sr07_sma_window_drives_feature_key():
    """The engine must look up sma_<window> based on `sr07_sma_window`, so
    different SMA windows can be plumbed without changing the schema."""
    cfg = SignalRuleConfig(sr07_sma_window=10)
    engine = SignalRuleEngine(cfg)
    fc = _forecast(forecast_mean=100.5)
    # Only sma_10 provided (not sma_20); with sma_window=10 the gate
    # should evaluate close vs sma_10 and pass.
    ctx = _ctx(regime_features={"NVDA": {"close": 110.0, "sma_10": 100.0}})
    result = engine.evaluate(fc, ctx)
    assert result.proceed is True
    assert result.rule_code == SR_PROCEED


def test_sr07_sma_window_mismatch_uses_default_key():
    """If sma_<window> is not present, fail closed."""
    cfg = SignalRuleConfig(sr07_sma_window=10)
    engine = SignalRuleEngine(cfg)
    fc = _forecast(forecast_mean=100.5)
    ctx = _ctx(regime_features={"NVDA": {"close": 110.0, "sma_20": 100.0}})  # wrong window
    result = engine.evaluate(fc, ctx)
    assert result.proceed is False
    assert result.rule_code == SR_REGIME_GATE


# --- ordering vs other rules ----------------------------------------------

def test_sr07_runs_after_sr04_not_actionable():
    """If the symbol isn't actionable, SR-04 fires first (don't burn cycles
    fetching regime features for non-actionable names)."""
    engine = SignalRuleEngine()
    fc = _forecast()
    ctx = _ctx(actionable_symbols={"AAPL"}, regime_features={})
    result = engine.evaluate(fc, ctx)
    assert result.rule_code == "SR-04"
    assert result.proceed is False


def test_sr07_runs_after_sr03_regime_block():
    """Regime block (PANIC/BEAR_LONG) takes precedence — backstop before
    the SR-07 trend check."""
    engine = SignalRuleEngine()
    fc = _forecast()
    ctx = _ctx(market_regime="PANIC", regime_features=_regime(close=110.0, sma_20=100.0))
    result = engine.evaluate(fc, ctx)
    assert result.rule_code == "SR-03"
    assert result.proceed is False