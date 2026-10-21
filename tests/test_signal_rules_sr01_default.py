"""TS-sr01-default: SignalRuleConfig default min_expected_return_pct = 0.005 (was 0.01).

Council follow-up (2026-06-25) — config drift discovered: `.env:113`,
`.env.example:37`, the backtest default (`v12/scripts/backtest_v12_paper.py:419`),
and commit `c4e30f70` all encode 0.5% as the SR-01 intent. But
`v12/src/stockapp_v12/services/signal_rule_engine.py:25` hardcoded 0.01
(1%), creating a 2x divergence between live and backtest. Live was the
silent outlier — 4-5 fewer symbols/day would pass SR-01 vs what backtest
projected. Aligning to 0.005 before 15:30 cron so the live soak matches
the backtest contract.
"""
import pytest
from stockapp_v12.schemas.forecast import build_forecast_output
from stockapp_v12.services.signal_rule_engine import (
    SR_MIN_EXPECTED_RETURN,
    SR_PROCEED,
    SignalRuleConfig,
    SignalRuleContext,
    SignalRuleEngine,
)


def _forecast(forecast_mean: float):
    return build_forecast_output(
        symbol="AAPL",
        horizon_days=10,
        current_price=100.0,
        forecast_mean=forecast_mean,
        model_id="chronos-2-zero",
    )


def _ctx():
    return SignalRuleContext(market_regime="BULL_LONG", actionable_symbols={"AAPL"})


def test_default_sr01_threshold_is_0pct5():
    """SignalRuleConfig() default min_expected_return_pct must be 0.005 (0.5%).

    Council 2026-06-25 (round 2): align to .env / backtest intent.
    Was 0.01 (1%) — the only place in the codebase that disagreed.
    """
    cfg = SignalRuleConfig()
    assert cfg.min_expected_return_pct == pytest.approx(0.005), \
        f"expected default 0.005, got {cfg.min_expected_return_pct}"


def test_return_at_0pct5_passes_sr01():
    """Forecast return exactly at 0.5% must pass SR-01 (boundary).

    SR-07 (regime gate) is disabled here so this test isolates SR-01's
    behavior — see tests/test_signal_rules_sr07_regime_gate.py for SR-07
    coverage.
    """
    engine = SignalRuleEngine(SignalRuleConfig(sr07_enabled=False))
    out = engine.evaluate(_forecast(forecast_mean=100.5), _ctx())
    assert out.rule_code == SR_PROCEED, \
        f"expected SR_PROCEED at 0.5% return; got {out.rule_code}"


def test_return_above_0pct5_passes_sr01():
    """Forecast return 1% must pass SR-01 (well above new cap)."""
    engine = SignalRuleEngine(SignalRuleConfig(sr07_enabled=False))
    out = engine.evaluate(_forecast(forecast_mean=101.0), _ctx())
    assert out.rule_code == SR_PROCEED


def test_return_below_0pct5_vetoes_sr01():
    """Forecast return 0.3% must fire SR-01 (below new 0.5% cap)."""
    engine = SignalRuleEngine(SignalRuleConfig(sr07_enabled=False))
    out = engine.evaluate(_forecast(forecast_mean=100.3), _ctx())
    assert out.rule_code == SR_MIN_EXPECTED_RETURN, \
        f"expected SR-01 at 0.3% return; got {out.rule_code}"


def test_return_at_old_1pct_cap_still_passes():
    """Backwards compat: 1% return (was the old cap) still passes new 0.5% default."""
    engine = SignalRuleEngine(SignalRuleConfig(sr07_enabled=False))
    out = engine.evaluate(_forecast(forecast_mean=101.0), _ctx())
    assert out.rule_code == SR_PROCEED
