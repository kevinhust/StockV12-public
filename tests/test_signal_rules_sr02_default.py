"""TS-sr02-default: SignalRuleConfig default uncertainty cap = 0.12 (was 0.08).

Council follow-up (2026-06-25) — held-out SR-02 pass rate at 8% cap was 3/10,
matching the live journal at 11:15 (AAPL/NVDA vetoed at uncertainty 0.13-0.14).
Backtest_v12_paper.py already runs at 0.15, acknowledging Chronos-bolt bands
are 20%+. 0.12 is the middle-ground production cap (12%).
"""
import pytest
from stockapp_v12.schemas.forecast import build_forecast_output
from stockapp_v12.services.signal_rule_engine import (
    SR_MAX_UNCERTAINTY,
    SR_PROCEED,
    SignalRuleConfig,
    SignalRuleContext,
    SignalRuleEngine,
)


def _forecast(q10: float, q90: float):
    return build_forecast_output(
        symbol="AAPL",
        horizon_days=10,
        current_price=100.0,
        forecast_mean=104.0,
        forecast_q10=q10,
        forecast_q90=q90,
        model_id="chronos-2-zero",
    )


def _ctx():
    return SignalRuleContext(market_regime="BULL_LONG", actionable_symbols={"AAPL"})


def test_default_sr02_cap_is_12pct():
    """SignalRuleConfig() default must be 0.12 — was 0.08, council raised to 12%."""
    cfg = SignalRuleConfig()
    assert cfg.max_uncertainty_pct == pytest.approx(0.12), \
        f"expected default 0.12, got {cfg.max_uncertainty_pct}"


def test_uncertainty_at_12pct_passes_sr02():
    """Uncertainty exactly at cap (12%) must pass SR-02."""
    engine = SignalRuleEngine()  # use new default 0.12
    # (q90 - q10) / current = 12/100 = 0.12 → at cap
    out = engine.evaluate(_forecast(q10=94.0, q90=106.0), _ctx())
    assert out.rule_code == SR_PROCEED, \
        f"expected SR_PROCEED at 12% uncertainty; got {out.rule_code}"


def test_uncertainty_above_12pct_vetoes_sr02():
    """Uncertainty 15% must still fire SR-02."""
    engine = SignalRuleEngine()
    out = engine.evaluate(_forecast(q10=92.5, q90=107.5), _ctx())
    assert out.rule_code == SR_MAX_UNCERTAINTY


def test_uncertainty_at_old_8pct_still_passes():
    """Backwards compat: 8% (old production cap) still passes the new 12% default."""
    engine = SignalRuleEngine()
    out = engine.evaluate(_forecast(q10=96.0, q90=104.0), _ctx())
    assert out.rule_code == SR_PROCEED