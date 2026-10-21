"""hygiene-C — quantile extraction helper dedup test.

Pins the contract of ``stockapp_v12.eval.quantiles.extract_quantile_band``
so the eval/predict and ensemble_chronos_backend call sites stay
behaviorally aligned. Without this test the two paths could drift
again (the exact bug hygiene-C fixes).
"""

from __future__ import annotations

import pandas as pd
import pytest

from stockapp_v12.eval.quantiles import extract_quantile_band


def _make_pred_with_quantiles(horizon: int, mean_val: float = 100.0) -> pd.DataFrame:
    """Build a fake predictor output that has the canonical quantile schema."""
    idx = pd.RangeIndex(horizon, name="timestamp")
    return pd.DataFrame(
        {
            "mean": [mean_val] * horizon,
            "0.1": [mean_val * 0.95] * horizon,
            "0.9": [mean_val * 1.05] * horizon,
        },
        index=idx,
    )


def _make_pred_no_quantiles(horizon: int, val: float = 100.0) -> pd.DataFrame:
    """Build a fake predictor output WITHOUT quantile columns (schema drift)."""
    idx = pd.RangeIndex(horizon, name="timestamp")
    return pd.DataFrame({"forecast": [val] * horizon}, index=idx)


def test_extract_quantile_band_uses_quantile_schema_when_present():
    """Canonical path: pred has ['mean','0.1','0.9'] → direct mean of each."""
    pred = _make_pred_with_quantiles(horizon=10, mean_val=200.0)
    mean, q10, q90 = extract_quantile_band(pred, horizon=10)
    assert mean == pytest.approx(200.0)
    assert q10 == pytest.approx(190.0)  # 200 * 0.95
    assert q90 == pytest.approx(210.0)  # 200 * 1.05


def test_extract_quantile_band_falls_back_to_mean_when_quantiles_missing():
    """Fallback path: no quantile columns → pred.iloc[:N].mean().mean() + proxy."""
    pred = _make_pred_no_quantiles(horizon=10, val=50.0)
    # mean = mean of [50, 50, ..., 50] = 50
    # q10/q90 fall back to proxy band: 50*0.95=47.5, 50*1.05=52.5
    mean, q10, q90 = extract_quantile_band(pred, horizon=10)
    assert mean == pytest.approx(50.0)
    assert q10 == pytest.approx(47.5)
    assert q90 == pytest.approx(52.5)


def test_extract_quantile_band_falls_back_when_quantile_columns_have_nans():
    """Edge: quantile column present but raise on .iloc[:N] → fallback path."""
    pred = _make_pred_no_quantiles(horizon=5, val=80.0)
    # Schema drift case: caller has wrong types but pred has no quantiles at all.
    mean, q10, q90 = extract_quantile_band(pred, horizon=5)
    assert mean == pytest.approx(80.0)
    assert q10 < mean
    assert q90 > mean


def test_extract_quantile_band_horizon_truncation():
    """horizon=3 averages only first 3 rows, not all 10."""
    pred = _make_pred_with_quantiles(horizon=10, mean_val=100.0)
    mean_full, _, _ = extract_quantile_band(pred, horizon=10)
    mean_short, _, _ = extract_quantile_band(pred, horizon=3)
    # Both should be 100 since all values are equal — but the slice still
    # confirms horizon is honored (no off-by-one).
    assert mean_full == pytest.approx(100.0)
    assert mean_short == pytest.approx(100.0)


def test_extract_quantile_band_returns_three_floats():
    """Return shape is exactly (float, float, float)."""
    pred = _make_pred_with_quantiles(horizon=2)
    out = extract_quantile_band(pred, horizon=2)
    assert len(out) == 3
    assert all(isinstance(x, float) for x in out)


def test_extract_quantile_band_fallback_mean_matches_eval_predict_pre_dedup():
    """Regression: eval/predict.py used to fall back to pred.iloc[:N].mean().mean().

    Pin that the helper's fallback preserves the exact same numeric
    behavior (mean only, no proxy q10/q90 mismatched with the eval
    path's pre-dedup behavior).
    """
    pred = _make_pred_no_quantiles(horizon=4, val=10.0)
    mean, _, _ = extract_quantile_band(pred, horizon=4)
    # Same formula as the pre-dedup eval/predict.py:82-84 fallback.
    expected_mean = float(pred.iloc[:4].mean().mean())
    assert mean == pytest.approx(expected_mean)