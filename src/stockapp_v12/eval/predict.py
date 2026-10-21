"""
Unified per-symbol prediction helper for V12 shadow eval.

This module consolidates the two previously-divergent copies of
`predict_one_symbol_for_date` from `v12/scripts/shadow_trade.py` and
`v12/scripts/shadow_ensemble.py` (B2, post-launch refactor).

Canonical version is `shadow_trade`'s implementation, with the ensemble
copy's defensive `ValueError("missing from data")` handling preserved
(see Ensemble Diff #3 in the B2 plan). The latent `n_h` NameError bug
from the ensemble copy (Diff #4) is fixed by re-computing `n_h` in the
return dict.

Both `shadow_trade.py` and `shadow_ensemble.py` now import this single
implementation. The return row schema is unchanged; per-date JSON output
remains byte-identical to pre-B2 baselines (validated by
`v12/tests/test_shadow_determinism.py`).
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from stockapp_v12.eval.constants import FORECAST_HORIZON
from stockapp_v12.eval.metrics import compute_mase, naive_errors_from_actual
from stockapp_v12.eval.quantiles import extract_quantile_band
from stockapp_v12.eval.test_data import build_test_tsdf_full

logger = logging.getLogger(__name__)


def predict_one_symbol_for_date(
    predictor, symbol: str, cutoff_days_ago: int = 1,
    eval_date: Optional[str] = None,
) -> Optional[dict]:
    """Predict next FORECAST_HORIZON days for `symbol`.

    Two slicing modes (eval_date wins if both set):

    1. `eval_date` set (YYYY-MM-DD, true walk-forward, used by
       backfill_30_days.py): model sees data <= eval_date and is graded
       against actuals after it. Every (model, symbol, eval_date) tuple
       gets a distinct data slice.

    2. `eval_date` None (legacy cron path): caller passes
       `cutoff_days_ago` and we drop that many rows from the end of the
       parquet. Cron varies cutoff across [1..N] to vary the input window.

    Returns a per-symbol row dict, or None on skip (file missing or
    <252 rows, OR the model's expected feature set is absent from the
    parquet — see Ensemble Diff #3).
    """
    from autogluon.timeseries import TimeSeriesDataFrame

    try:
        df, actual_close = build_test_tsdf_full(
            symbol, cutoff_days_ago=cutoff_days_ago, eval_date=eval_date,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"  [{symbol}] skip ({e})", file=__import__("sys").stderr)
        return None
    if len(df) < 252:
        print(f"  [{symbol}] skip (only {len(df)} rows)", file=__import__("sys").stderr)
        return None

    tsdf = TimeSeriesDataFrame.from_data_frame(
        df, id_column="symbol", timestamp_column="timestamp"
    )
    current_close = float(df["close"].iloc[-1])
    try:
        pred = predictor.predict(tsdf)
    except ValueError as e:
        # Ensemble Diff #3: gracefully skip on "missing from data"
        # (model expects feature columns absent from this parquet).
        if "missing from data" in str(e):
            print(
                f"  [{symbol}] skip (model expects features absent: {e})",
                file=__import__("sys").stderr,
            )
            return None
        raise

    try:
        forecast_mean, _, _ = extract_quantile_band(pred, FORECAST_HORIZON)
    except Exception as e:
        logger.warning(
            "[predict] extract_quantile_band failed for %s: %s: %s — "
            "falling back to pred.iloc[:N].mean().mean() (no quantile band)",
            symbol, type(e).__name__, e,
        )
        forecast_mean = float(pred.iloc[:FORECAST_HORIZON].mean().mean())
    forecast_ret_pct = (forecast_mean - current_close) / current_close * 100

    # Actual & MASE — only meaningful if we have a held-out window
    actual_mean: Optional[float] = None
    actual_ret_pct: Optional[float] = None
    mase_val: Optional[float] = None
    n_h = 0
    if cutoff_days_ago > 0 and len(actual_close) > 0:
        n_h = min(FORECAST_HORIZON, len(actual_close))
        actual_first10 = actual_close.iloc[:n_h].tolist()
        naive = naive_errors_from_actual([current_close] + actual_first10)
        try:
            predicted = [forecast_mean] * n_h
            mase_val = compute_mase(actual_first10, predicted, naive)
        except ValueError:
            mase_val = None
        actual_mean = float(np.mean(actual_first10))
        actual_ret_pct = (actual_mean - current_close) / current_close * 100

    return {
        "symbol": symbol,
        "current_close": round(current_close, 2),
        "forecast_mean": round(forecast_mean, 2),
        "forecast_ret_pct": round(forecast_ret_pct, 3),
        "actual_mean": round(actual_mean, 2) if actual_mean is not None else None,
        "actual_ret_pct": round(actual_ret_pct, 3) if actual_ret_pct is not None else None,
        "mase": round(mase_val, 4) if mase_val is not None else None,
        "cutoff_days_ago": cutoff_days_ago,
        # Ensemble Diff #4 fix: when cutoff==0 and no actuals, n_h is 0
        # (not the undeclared local from the original ensemble copy).
        "n_horizons_evaluated": min(FORECAST_HORIZON, len(actual_close)) if cutoff_days_ago > 0 else 0,
    }