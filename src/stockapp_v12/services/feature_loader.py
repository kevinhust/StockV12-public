"""
feature_loader — load daily feature-engineered bars from data/features/daily/<sym>.parquet.

Contract:
- 22 columns total = close + 19 numeric features + (timestamp + symbol)
- Timestamp is tz-naive UTC (US/Eastern tz-aware inputs converted at load time)
- Graceful on missing files: load_single_symbol returns None; load_daily_bars skips.

Schema (DAILY_FEATURE_COLS, in order Chronos was trained on):
  ret_1d, ret_5d, ret_20d, vol_10d, vol_20d, rsi_14, macd, macd_signal, macd_hist,
  atr_14, bb_pct_b_20, obv, obv_slope_10, dist_ma_20, dist_ma_50,
  spy_ret_1d, spy_ret_5d, sector_ret_1d, sector_beta_20
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

# 19 numeric feature columns expected by Chronos-Bolt phase2_v1.
# Order matches the training-time order — do NOT reorder casually.
DAILY_FEATURE_COLS: tuple[str, ...] = (
    "ret_1d", "ret_5d", "ret_20d",
    "vol_10d", "vol_20d",
    "rsi_14",
    "macd", "macd_signal", "macd_hist",
    "atr_14",
    "bb_pct_b_20",
    "obv", "obv_slope_10",
    "dist_ma_20", "dist_ma_50",
    "spy_ret_1d", "spy_ret_5d",
    "sector_ret_1d", "sector_beta_20",
)


class FeatureLoaderError(ValueError):
    """Raised when a parquet is missing required base features (schema drift)."""


def _path_for(symbol: str, features_dir: Path) -> Path:
    return features_dir / f"{symbol}.parquet"


def _normalize_timestamps(df: pd.DataFrame) -> pd.DataFrame:
    """Convert tz-aware timestamps to tz-naive UTC; preserve column + index."""
    if "timestamp" not in df.columns:
        raise FeatureLoaderError("parquet missing required 'timestamp' column")
    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
    df = df.assign(timestamp=ts).set_index("timestamp")
    df.index.name = "timestamp"
    # Mirror the backtest's load_real_bars contract: timestamp is BOTH index
    # name AND column (downstream code reads bars[["timestamp", "close", ...]]).
    df["timestamp"] = df.index
    return df


def _validate_feature_cols(df: pd.DataFrame, symbol: str) -> None:
    """Raise FeatureLoaderError if any required base feature is missing."""
    missing = [c for c in DAILY_FEATURE_COLS if c not in df.columns]
    if missing:
        raise FeatureLoaderError(
            f"symbol={symbol} parquet missing required feature columns: {missing}. "
            f"Re-run v12/scripts/feature_engineering.py --freq daily --phase all to rebuild."
        )


def load_single_symbol(
    symbol: str,
    *,
    features_dir: str | Path = "data/features/daily",
    lookback_days: int = 252,
    require_min_rows: int = 100,
) -> Optional[pd.DataFrame]:
    """
    Load daily features for one symbol.

    Returns None if the parquet does not exist or has fewer rows than
    require_min_rows. Raises FeatureLoaderError if the parquet exists
    but is missing required base feature columns (schema drift).

    Args:
        symbol: ticker, e.g. "AAPL".
        features_dir: directory containing <symbol>.parquet files.
        lookback_days: keep only the most recent N rows (trading-day granularity).
        require_min_rows: drop symbols with fewer rows (returns None).
    """
    path = _path_for(symbol, Path(features_dir))
    if not path.exists():
        return None

    df = pd.read_parquet(path)
    _validate_feature_cols(df, symbol)
    df = _normalize_timestamps(df)

    # Apply lookback window (most recent N rows). Sort ascending so .tail(N) is correct.
    df = df.sort_index()
    if lookback_days and lookback_days > 0 and len(df) > lookback_days:
        df = df.tail(lookback_days)

    if len(df) < require_min_rows:
        return None

    return df


def load_daily_bars(
    symbols: Iterable[str],
    *,
    features_dir: str | Path = "data/features/daily",
    lookback_days: int = 252,
    require_min_rows: int = 100,
) -> dict[str, pd.DataFrame]:
    """
    Load daily features for many symbols. Skips symbols whose parquets are
    missing or below require_min_rows. Raises FeatureLoaderError on schema drift.

    Returns: {symbol: DataFrame} — keys are a strict subset of `symbols`.
    """
    out: dict[str, pd.DataFrame] = {}
    features_path = Path(features_dir)
    for sym in symbols:
        df = load_single_symbol(
            sym,
            features_dir=features_path,
            lookback_days=lookback_days,
            require_min_rows=require_min_rows,
        )
        if df is not None:
            out[sym] = df
    return out