"""TS-1: ForecastOutput schema — no trade action fields."""
import pytest
from pydantic import ValidationError

from stockapp_v12.schemas.forecast import ForecastOutput, assert_no_trade_action_fields, build_forecast_output


def test_valid_forecast():
    f = build_forecast_output(
        symbol="nvda",
        horizon_days=10,
        current_price=100.0,
        forecast_mean=105.0,
        model_id="chronos-2-zero",
        regime_hint="bull_long",
    )
    assert f.symbol == "NVDA"
    assert f.regime_hint == "BULL_LONG"
    assert f.expected_return_pct == pytest.approx(0.05)
    assert f.uncertainty_pct > 0


def test_forbids_action_field():
    with pytest.raises(ValidationError):
        ForecastOutput(
            symbol="AAPL",
            as_of="2026-06-18",
            horizon_days=10,
            current_price=100.0,
            forecast_mean=101.0,
            forecast_q10=99.0,
            forecast_q90=103.0,
            expected_return_pct=0.01,
            model_id="test",
            action="BUY",
        )


def test_assert_no_trade_action_fields():
    assert_no_trade_action_fields({"symbol": "X", "forecast_mean": 1.0})
    with pytest.raises(ValueError, match="forbidden"):
        assert_no_trade_action_fields({"side": "BUY"})


def test_q10_must_not_exceed_q90():
    with pytest.raises(ValidationError):
        build_forecast_output(
            symbol="AAPL",
            horizon_days=5,
            current_price=100.0,
            forecast_mean=100.0,
            forecast_q10=110.0,
            forecast_q90=90.0,
            model_id="test",
        )