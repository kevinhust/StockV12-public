"""Integration test: EnsembleChronosBackend wired through ForecastService.

Verifies the full chain `EnsembleChronosBackend → ForecastService →
ForecastOutput` produces a non-degraded ForecastOutput with the expected
expected_return_pct / forecast_mean values.

This is the same code path that lifecycle_scheduler.py triggers when
CHRONOS_ENSEMBLE_MODEL_PATHS is set — only here we inject the backend
directly so the test runs on Mac without needing GPU.
"""
import pandas as pd
import pytest

from stockapp_v12.services.ensemble_chronos_backend import EnsembleChronosBackend
from stockapp_v12.services.forecast_service import ForecastService


class _FakePredictor:
    """Lightweight predictor that bypasses AutoGluon entirely."""
    def __init__(self, forecast_price: float, fail: bool = False):
        self.forecast_price = forecast_price
        self.fail = fail

    def predict(self, tsdf):
        if self.fail:
            raise ValueError("simulated predict failure")
        n = 10
        idx = pd.MultiIndex.from_product(
            [["TICKER"], pd.date_range("2026-03-01", periods=n, freq="D")],
            names=["item_id", "timestamp"],
        )
        return pd.DataFrame(
            {
                "mean": [self.forecast_price] * n,
                "0.1": [self.forecast_price * 0.95] * n,
                "0.9": [self.forecast_price * 1.05] * n,
            },
            index=idx,
        )


def _backend(predictors, fallback_spread_pct=3.0):
    """Build an EnsembleChronosBackend with the given fake predictors pre-loaded."""
    paths = [f"/fake/model_{i}" for i in range(len(predictors))]
    be = EnsembleChronosBackend(
        model_paths=paths,
        fallback_spread_pct=fallback_spread_pct,
    )
    be._predictors = list(predictors)
    return be


def _bars(last_close: float = 100.0, n: int = 60, symbol: str = "AAPL") -> pd.DataFrame:
    import numpy as np
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    rng = np.random.default_rng(int(last_close * 1000) % (2**32))  # symbol-stable RNG
    closes = last_close + rng.normal(0, 0.5, n).cumsum() * 0.1
    closes[-1] = last_close
    return pd.DataFrame({
        "symbol": [symbol] * n,
        "timestamp": dates,
        "close": closes,
    })


def test_forecast_service_with_ensemble_emits_non_degraded_output():
    """Two models with 1% spread → ForecastOutput should be non-degraded."""
    be = _backend([_FakePredictor(102.0), _FakePredictor(101.0)])
    svc = ForecastService(backend=be, model_id="chronos-ensemble")

    df = _bars(last_close=100.0)
    out = svc.forecast_daily("AAPL", df)

    assert out is not None
    assert out.degraded is False
    # Average of 102 and 101 = 101.5 → +1.5% expected return
    assert out.current_price == pytest.approx(100.0, rel=1e-6)
    assert out.forecast_mean == pytest.approx(101.5, rel=1e-6)
    assert out.expected_return_pct == pytest.approx(0.015, rel=1e-3)
    assert out.model_id == "chronos-ensemble"
    assert out.symbol == "AAPL"


def test_forecast_service_with_ensemble_fallback_path():
    """Two models with 5% spread → first model's forecast wins."""
    be = _backend(
        [_FakePredictor(105.0), _FakePredictor(100.0)],
        fallback_spread_pct=3.0,
    )
    svc = ForecastService(backend=be)

    out = svc.forecast_daily("AAPL", _bars(last_close=100.0))

    assert out is not None
    assert out.degraded is False
    # Fallback to first → forecast_mean = 105.0
    assert out.forecast_mean == pytest.approx(105.0, rel=1e-6)
    assert out.expected_return_pct == pytest.approx(0.05, rel=1e-3)


def test_forecast_service_with_ensemble_all_models_fail_degrades():
    """All models fail → ForecastService returns degraded ForecastOutput."""
    be = _backend([_FakePredictor(0.0, fail=True), _FakePredictor(0.0, fail=True)])
    svc = ForecastService(backend=be)

    out = svc.forecast_daily("NVDA", _bars(last_close=200.0))

    assert out is not None
    assert out.degraded is True
    # Degraded forecast: forecast_mean = current_price → expected_return = 0
    assert out.expected_return_pct == 0.0


def test_forecast_service_with_ensemble_batch():
    """Batch path: detect_batch runs each symbol with its own df."""
    be = _backend([_FakePredictor(102.0), _FakePredictor(101.0)])
    svc = ForecastService(backend=be)

    df_a = _bars(last_close=100.0)
    df_n = _bars(last_close=200.0)
    out = svc.forecast_daily_batch({"AAPL": df_a, "NVDA": df_n})

    assert set(out.keys()) == {"AAPL", "NVDA"}
    # current_price reflects each symbol's last close
    assert out["AAPL"].current_price == pytest.approx(100.0, rel=1e-6)
    assert out["NVDA"].current_price == pytest.approx(200.0, rel=1e-6)
    # Fake predictor returns constant forecast regardless of input,
    # so expected_return_pct reflects the per-symbol current_price.
    # AAPL: (101.5 - 100) / 100 = +1.5%
    assert out["AAPL"].expected_return_pct == pytest.approx(0.015, rel=1e-3)
    # NVDA: (101.5 - 200) / 200 = -49.25% (huge negative since fake predictor
    # doesn't scale with input). We just confirm it's negative.
    assert out["NVDA"].expected_return_pct < 0