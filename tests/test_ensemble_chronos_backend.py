"""Tests for EnsembleChronosBackend (Round 3 closure, 2026-07-01).

Coverage:
  - happy path: 2 models with similar forecasts → average is used
  - fallback path: 2 models with spread > 3% → single-model (first) is used
  - below-fallback path: 2 models with spread ≤ 3% → average is used
  - one-model-fails: only 1 of 2 succeeds → average the survivor
  - all-models-fail: returns empty signal list (degraded path)
  - detect_batch: parallel multi-symbol
  - _resolve_current_price: missing 'close' falls back to adj_close/price/last
  - factory make_ensemble_backend_from_env: empty env → None; colon-split → list
"""
import logging
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd
import pytest

from stockapp_v12.services.ensemble_chronos_backend import (
    DEFAULT_FALLBACK_SPREAD_PCT,
    ENSEMBLE_ENV_VAR,
    EnsembleChronosBackend,
    FALLBACK_SPREAD_ENV_VAR,
    make_ensemble_backend_from_env,
)


# --- Helpers ----------------------------------------------------------------


def _bars(n: int = 60, last_close: float = 100.0) -> pd.DataFrame:
    """Build a single-symbol OHLCV-like frame with the columns the backend expects."""
    dates = pd.date_range("2026-01-01", periods=n, freq="D")
    rng = np.random.default_rng(0)
    closes = last_close + rng.normal(0, 0.5, n).cumsum() * 0.1
    closes[-1] = last_close  # pin final close
    return pd.DataFrame({
        "symbol": ["AAPL"] * n,
        "timestamp": dates,
        "close": closes,
    })


@dataclass
class _FakePredictor:
    """Stand-in for an AutoGluon TimeSeriesPredictor.

    Predicts (mean, q10, q90) flat over the next 10 horizons = `forecast_price`.
    """
    forecast_price: float
    forecast_q10: Optional[float] = None
    forecast_q90: Optional[float] = None
    fail: bool = False

    def predict(self, tsdf) -> pd.DataFrame:
        if self.fail:
            raise ValueError("simulated predict failure")
        n = 10
        mean = self.forecast_price
        q10 = self.forecast_q10 if self.forecast_q10 is not None else mean * 0.95
        q90 = self.forecast_q90 if self.forecast_q90 is not None else mean * 1.05
        idx = pd.MultiIndex.from_product(
            [["TICKER"], pd.date_range("2026-03-01", periods=n, freq="D")],
            names=["item_id", "timestamp"],
        )
        return pd.DataFrame({"mean": [mean] * n, "0.1": [q10] * n, "0.9": [q90] * n},
                            index=idx)


def _backend_with_predictors(predictors: List[_FakePredictor],
                              model_paths: Optional[List[str]] = None,
                              **kwargs) -> EnsembleChronosBackend:
    """Build an EnsembleChronosBackend with the given fake predictors pre-loaded.

    Skips TimeSeriesPredictor.load by injecting _predictors directly.
    """
    paths = model_paths or [f"/fake/model_{i}" for i in range(len(predictors))]
    be = EnsembleChronosBackend(model_paths=paths, **kwargs)
    be._predictors = list(predictors)
    return be


# --- Tests ------------------------------------------------------------------


def test_constructor_rejects_empty_model_paths():
    with pytest.raises(ValueError, match="at least one"):
        EnsembleChronosBackend(model_paths=[])


def test_happy_path_average_when_models_agree():
    """Two models with 1% apart: average forecast_mean is used."""
    df = _bars(last_close=100.0)
    a = _FakePredictor(forecast_price=102.0)  # +2.0%
    b = _FakePredictor(forecast_price=101.0)  # +1.0%
    be = _backend_with_predictors([a, b], fallback_spread_pct=3.0)

    sigs = be.detect(df)

    assert len(sigs) == 1
    meta = sigs[0]["meta"]
    assert meta["current_price"] == 100.0
    # Average of 102 and 101 = 101.5
    assert meta["forecast_price"] == pytest.approx(101.5, rel=1e-6)
    # Spread = (2.0% - 1.0%) = 1.0%, below 3% threshold → fallback_used=False
    emeta = meta["_ensemble_meta"]
    assert emeta["fallback_used"] is False
    assert emeta["per_model_spread_pct"] == pytest.approx(1.0, rel=1e-3)
    assert emeta["n_models"] == 2
    assert sigs[0]["type"] == "CHRONOS_LONG"  # avg is positive


def test_fallback_path_when_models_disagree_wildly():
    """Two models with 5% spread: single-model fallback to first."""
    df = _bars(last_close=100.0)
    a = _FakePredictor(forecast_price=105.0)  # +5.0%
    b = _FakePredictor(forecast_price=100.0)  #  0.0%
    be = _backend_with_predictors([a, b], fallback_spread_pct=3.0)

    sigs = be.detect(df)

    assert len(sigs) == 1
    meta = sigs[0]["meta"]
    # Spread = 5%, > 3% threshold → fallback to first (a)
    assert meta["forecast_price"] == pytest.approx(105.0)
    emeta = meta["_ensemble_meta"]
    assert emeta["fallback_used"] is True
    assert emeta["per_model_spread_pct"] == pytest.approx(5.0, rel=1e-3)
    assert emeta["fallback_spread_threshold_pct"] == 3.0


def test_below_threshold_uses_average_even_when_negative():
    """Two bearish models within 3%: average is used."""
    df = _bars(last_close=100.0)
    a = _FakePredictor(forecast_price=95.0)   # -5.0%
    b = _FakePredictor(forecast_price=96.5)   # -3.5%
    be = _backend_with_predictors([a, b], fallback_spread_pct=3.0)

    sigs = be.detect(df)

    meta = sigs[0]["meta"]
    # Average = 95.75
    assert meta["forecast_price"] == pytest.approx(95.75, rel=1e-3)
    emeta = meta["_ensemble_meta"]
    assert emeta["fallback_used"] is False
    assert sigs[0]["type"] == "CHRONOS_SHORT"


def test_one_model_fails_uses_survivor():
    """One predictor raises: surviving model's forecast is used (single-model averaging)."""
    df = _bars(last_close=100.0)
    a = _FakePredictor(forecast_price=102.0)        # +2.0%
    b = _FakePredictor(forecast_price=99.0, fail=True)  # fails
    be = _backend_with_predictors([a, b], fallback_spread_pct=3.0)

    sigs = be.detect(df)

    assert len(sigs) == 1
    meta = sigs[0]["meta"]
    # Only a survived → forecast_price = 102
    assert meta["forecast_price"] == pytest.approx(102.0)
    emeta = meta["_ensemble_meta"]
    assert emeta["n_models"] == 1
    assert emeta["fallback_used"] is False


def test_all_models_fail_returns_empty_signal_list(caplog):
    """All predictors raise: backend returns [] → ForecastService degrades."""
    df = _bars(last_close=100.0)
    a = _FakePredictor(forecast_price=100.0, fail=True)
    b = _FakePredictor(forecast_price=100.0, fail=True)
    be = _backend_with_predictors([a, b], fallback_spread_pct=3.0)

    with caplog.at_level(logging.WARNING, logger="stockapp_v12.services.ensemble_chronos_backend"):
        sigs = be.detect(df)

    assert sigs == []
    msgs = [r.getMessage() for r in caplog.records]
    assert any("all" in m.lower() and "invalid" in m.lower() for m in msgs), msgs


def test_detect_batch_runs_each_symbol():
    df_a = _bars(last_close=100.0)
    df_n = _bars(last_close=200.0)
    a = _FakePredictor(forecast_price=102.0)
    b = _FakePredictor(forecast_price=101.0)
    be = _backend_with_predictors([a, b], fallback_spread_pct=3.0)

    out = be.detect_batch({"AAPL": df_a, "NVDA": df_n})

    assert set(out.keys()) == {"AAPL", "NVDA"}
    assert out["AAPL"][0]["meta"]["current_price"] == 100.0
    assert out["NVDA"][0]["meta"]["current_price"] == 200.0


def test_detect_batch_per_symbol_failure_returns_empty_list():
    """One symbol's df is malformed → empty signal list for that symbol only."""
    df_good = _bars(last_close=100.0)
    df_bad = pd.DataFrame()  # empty
    a = _FakePredictor(forecast_price=102.0)
    b = _FakePredictor(forecast_price=101.0)
    be = _backend_with_predictors([a, b], fallback_spread_pct=3.0)

    out = be.detect_batch({"AAPL": df_good, "BAD": df_bad})

    assert len(out["AAPL"]) == 1
    assert out["BAD"] == []


def test_resolve_current_price_uses_close_column():
    df = _bars(last_close=123.45)
    a = _FakePredictor(forecast_price=125.0)
    b = _FakePredictor(forecast_price=124.0)
    be = _backend_with_predictors([a, b])

    sigs = be.detect(df)
    assert sigs[0]["meta"]["current_price"] == 123.45


def test_resolve_current_price_falls_back_to_adj_close():
    df = pd.DataFrame({
        "symbol": ["AAPL"] * 5,
        "timestamp": pd.date_range("2026-01-01", periods=5, freq="D"),
        "adj_close": [100.0, 101.0, 102.0, 103.0, 110.0],
    })
    a = _FakePredictor(forecast_price=112.0)
    b = _FakePredictor(forecast_price=111.0)
    be = _backend_with_predictors([a, b])

    sigs = be.detect(df)
    assert sigs[0]["meta"]["current_price"] == 110.0


def test_resolve_current_price_raises_when_no_price_column():
    df = pd.DataFrame({
        "symbol": ["AAPL"] * 5,
        "timestamp": pd.date_range("2026-01-01", periods=5, freq="D"),
        "volume": [1000] * 5,
    })
    a = _FakePredictor(forecast_price=100.0)
    b = _FakePredictor(forecast_price=100.0)
    be = _backend_with_predictors([a, b])

    with pytest.raises(ValueError, match="no price column"):
        be.detect(df)


def test_signal_type_long_vs_short():
    df = _bars(last_close=100.0)
    a_long = _FakePredictor(forecast_price=102.0)
    b_long = _FakePredictor(forecast_price=101.5)
    be = _backend_with_predictors([a_long, b_long])
    assert be.detect(df)[0]["type"] == "CHRONOS_LONG"

    a_short = _FakePredictor(forecast_price=98.0)
    b_short = _FakePredictor(forecast_price=98.5)
    be2 = _backend_with_predictors([a_short, b_short])
    assert be2.detect(df)[0]["type"] == "CHRONOS_SHORT"


def test_factory_returns_none_when_env_unset(monkeypatch):
    monkeypatch.delenv(ENSEMBLE_ENV_VAR, raising=False)
    assert make_ensemble_backend_from_env() is None


def test_factory_returns_none_when_env_empty(monkeypatch):
    monkeypatch.setenv(ENSEMBLE_ENV_VAR, "")
    assert make_ensemble_backend_from_env() is None


def test_factory_parses_colon_separated_paths(monkeypatch):
    monkeypatch.setenv(ENSEMBLE_ENV_VAR, "/path/a:/path/b:/path/c")
    monkeypatch.setenv(FALLBACK_SPREAD_ENV_VAR, "5.5")
    be = make_ensemble_backend_from_env()
    assert be is not None
    assert be.n_models == 3
    assert be.model_paths == ["/path/a", "/path/b", "/path/c"]
    assert be.fallback_spread_pct == 5.5


def test_factory_default_fallback_spread(monkeypatch):
    monkeypatch.setenv(ENSEMBLE_ENV_VAR, "/path/a:/path/b")
    monkeypatch.delenv(FALLBACK_SPREAD_ENV_VAR, raising=False)
    be = make_ensemble_backend_from_env()
    assert be.fallback_spread_pct == DEFAULT_FALLBACK_SPREAD_PCT


def test_factory_invalid_fallback_spread_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(ENSEMBLE_ENV_VAR, "/path/a:/path/b")
    monkeypatch.setenv(FALLBACK_SPREAD_ENV_VAR, "not_a_number")
    be = make_ensemble_backend_from_env()
    assert be.fallback_spread_pct == DEFAULT_FALLBACK_SPREAD_PCT


def test_factory_trims_whitespace(monkeypatch):
    monkeypatch.setenv(ENSEMBLE_ENV_VAR, " /path/a : /path/b : /path/c ")
    be = make_ensemble_backend_from_env()
    assert be.model_paths == ["/path/a", "/path/b", "/path/c"]


def test_model_labels_use_basename():
    paths = ["/home/trainer/models/foo", "/home/trainer/models/bar"]
    be = EnsembleChronosBackend(model_paths=paths)
    assert be.model_labels == ["foo", "bar"]


def test_ensemble_meta_audit_fields_complete():
    """All audit fields must be present so journal logs can attribute trades."""
    df = _bars(last_close=100.0)
    a = _FakePredictor(forecast_price=101.0)
    b = _FakePredictor(forecast_price=99.0)
    be = _backend_with_predictors(
        [a, b],
        model_paths=["/m/variant_a", "/m/variant_b"],
        fallback_spread_pct=3.0,
    )

    sigs = be.detect(df)
    emeta = sigs[0]["meta"]["_ensemble_meta"]
    expected_keys = {
        "n_models", "model_labels", "per_model_forecast_ret_pct",
        "per_model_spread_pct", "fallback_used",
        "fallback_spread_threshold_pct", "ensemble_model_id",
    }
    assert expected_keys.issubset(set(emeta.keys()))
    assert emeta["n_models"] == 2
    assert emeta["model_labels"] == ["variant_a", "variant_b"]
    assert emeta["ensemble_model_id"] == "chronos-ensemble"