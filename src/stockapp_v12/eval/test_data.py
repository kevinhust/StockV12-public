"""
Test-data construction for V12 shadow eval — builds a TimeSeriesDataFrame-
ready DataFrame + a Series of held-out actual closes.

Moved from `v12/scripts/eval_chronos_h2h_v3.py::build_test_tsdf_full`
(B2, post-launch refactor). Behavior is byte-identical to the original;
the only material change is the source of `FORECAST_HORIZON` — now
imported from `stockapp_v12.eval.constants` instead of a local
`_BACKFILL_HORIZON = 10` constant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from stockapp_v12.eval.constants import BACKFILL_HORIZON

REPO = Path(__file__).resolve().parents[3]  # repo root (post-migration: src/stockapp_v12/eval/test_data.py → parents[3])
FEATURES_DIR = REPO / "data" / "features" / "daily"


def build_test_tsdf_full(
    symbol: str, cutoff_days_ago: int,
    eval_date: Optional[str] = None,
) -> tuple[pd.DataFrame, pd.Series]:
    """Build a TSDF-ready DataFrame AND a Series of the held-out actual closes.

    Two slicing modes (mutually exclusive — `eval_date` wins if both set):

    1. `eval_date` provided (true walk-forward, used by backfill_30_days.py):
       - df = rows with timestamp <= eval_date (inclusive)
       - actual_close = rows with timestamp > eval_date, up to FORECAST_HORIZON
         horizons (matches downstream mase / pnl computation)
       - This is the ONLY mode that gives true time-respecting backtests where
         the model never sees data after eval_date.

    2. `eval_date` is None (legacy cutoff path, used by cron + H2H):
       - df = parquet[:-cutoff_days_ago] (last N rows dropped)
       - actual_close = last N rows (the held-out tail)
       - The cron path varies cutoff_days_ago across [1..N] so the input
         window varies, but the eval_date label is cosmetic and does NOT
         match the actual data slice (this is a known limitation; see
         ADR-009 backfill-FAIL addendum). Backfill uses mode (1) instead.

    Args:
      symbol: ticker (e.g. "AAPL")
      cutoff_days_ago: number of trailing rows to hold out (mode 2)
      eval_date: YYYY-MM-DD string; the model sees data <= this date and is
                 graded against actuals after it (mode 1)

    Returns:
      (df_for_tsdf, actual_close_series)
        df_for_tsdf:        columns include 'symbol', 'close', 'timestamp',
                            and any numeric feature columns starting with
                            ret_/vol_/rsi_/macd/atr_/bb_/obv/dist_/spy_/sector_.
        actual_close_series: pd.Series indexed by timestamp with the held-out
                            closes; length = min(cutoff_days_ago, len(df)) in
                            mode 2, or up to FORECAST_HORIZON in mode 1.

    Raises:
      FileNotFoundError: if parquet doesn't exist for `symbol`.
      ValueError: if `eval_date` is provided but no rows <= eval_date exist
                  (i.e. eval_date is before the parquet's first timestamp).
    """
    parquet = FEATURES_DIR / f"{symbol}.parquet"
    if not parquet.exists():
        raise FileNotFoundError(f"features missing for {symbol}: {parquet}")
    df = pd.read_parquet(parquet)
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"])
        if ts.dt.tz is not None:
            ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
        df["timestamp"] = ts
        df = df.sort_values("timestamp").reset_index(drop=True)

    if eval_date is not None:
        # ----- Mode 1: true walk-forward backfill -----
        eval_ts = pd.Timestamp(eval_date)
        df_as_of = df[df["timestamp"] <= eval_ts].copy()
        if len(df_as_of) == 0:
            raise ValueError(
                f"build_test_tsdf_full({symbol}, eval_date={eval_date}): "
                f"no rows <= eval_date (parquet starts {df['timestamp'].iloc[0]})"
            )
        actual_df = df[df["timestamp"] > eval_ts].head(BACKFILL_HORIZON)
        actual_close = actual_df["close"].copy()
        actual_close.index = actual_df["timestamp"].values
        df = df_as_of
    else:
        # ----- Mode 2: legacy cutoff_days_ago path (cron + H2H) -----
        actual_close = df["close"].iloc[-cutoff_days_ago:].copy() if cutoff_days_ago > 0 else df["close"].iloc[0:0].copy()
        actual_close.index = df["timestamp"].iloc[-cutoff_days_ago:] if cutoff_days_ago > 0 else df["timestamp"].iloc[0:0]

        # Drop the held-out tail
        if cutoff_days_ago > 0 and len(df) > cutoff_days_ago + 100:
            df = df.iloc[:-cutoff_days_ago].copy()

    # Keep only required cols (both modes)
    keep = ["symbol", "close", "timestamp"] + [
        c for c in df.columns
        if c.startswith(("ret_", "vol_", "rsi_", "macd", "atr_", "bb_", "obv", "dist_", "spy_", "sector_"))
    ]
    df = df[keep].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is not None:
        df["timestamp"] = df["timestamp"].dt.tz_convert("UTC").dt.tz_localize(None)
    return df, actual_close