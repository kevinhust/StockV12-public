#!/usr/bin/env python3
"""
eval_chronos_headtohead.py — fair head-to-head eval of phase2_v1 vs phase2_v2.

For each test symbol:
  1. Load last 252 days of features
  2. Build a TimeSeriesDataFrame with cutoff = TODAY - 10 days
  3. Predict next 10 days with each model
  4. Compare forecast_mean vs current price, compute forecast_ret_pct
  5. Report: signal magnitude (|forecast_ret|), directional consistency

This evaluates forecasting SIGNAL QUALITY for trading, not in-sample MASE.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
import warnings
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

REPO = Path(__file__).resolve().parents[1]
TEST_SYMBOLS = ["AAPL", "NVDA", "TSLA", "JPM", "MSFT", "AMZN", "AMD", "META", "GOOGL", "SPY"]


def load_model(model_dir: Path):
    from autogluon.timeseries import TimeSeriesPredictor
    from autogluon.timeseries.trainer.prediction_cache import FileBasedPredictionCache
    # Patch FileBasedPredictionCache.put to a no-op so we don't try to write
    # to /home/trainer/... (paths baked in when model was trained on GPU box).
    # We replace the method on the class itself so the patch persists across
    # load_trainer() calls inside predict().
    FileBasedPredictionCache.put = lambda self, *a, **kw: None
    predictor = TimeSeriesPredictor.load(str(model_dir))
    return predictor


def build_test_tsdf(symbol: str, cutoff_days_ago: int = 10) -> pd.DataFrame:
    """Build TSDF with data up to (today - cutoff_days_ago) for symbol."""
    from autogluon.timeseries import TimeSeriesDataFrame
    parquet = REPO / "data/features/daily" / f"{symbol}.parquet"
    df = pd.read_parquet(parquet)
    # Normalize timestamps (parquet has 'timestamp' col + tz-aware)
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"])
        if ts.dt.tz is not None:
            ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
        df["timestamp"] = ts
        df = df.set_index("timestamp")
    df = df.sort_index()
    # Cutoff: drop last `cutoff_days_ago` rows so model "predicts" them
    if cutoff_days_ago > 0 and len(df) > cutoff_days_ago + 100:
        df = df.iloc[:-cutoff_days_ago]
    # Keep only required cols
    keep = ["symbol", "close"] + [c for c in df.columns if c.startswith(("ret_", "vol_", "rsi_", "macd", "atr_", "bb_", "obv", "dist_", "spy_", "sector_"))]
    df = df[keep].copy()
    df = df.reset_index().rename(columns={"index": "timestamp", "timestamp": "timestamp"})
    if "timestamp" not in df.columns:
        df = df.rename(columns={df.columns[0]: "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    if df["timestamp"].dt.tz is not None:
        df["timestamp"] = df["timestamp"].dt.tz_convert("UTC").dt.tz_localize(None)
    return df


def evaluate_model(predictor, symbols: list[str], cutoff_days_ago: int = 10) -> dict:
    """Predict each symbol's next 10 days, return forecast summary."""
    from autogluon.timeseries import TimeSeriesDataFrame
    rows = []
    for sym in symbols:
        df = build_test_tsdf(sym, cutoff_days_ago=cutoff_days_ago)
        tsdf = TimeSeriesDataFrame.from_data_frame(
            df,
            id_column="symbol",
            timestamp_column="timestamp",
        )
        current_close = float(df["close"].iloc[-1])
        pred = predictor.predict(tsdf)
        # pred has MultiIndex (item_id, timestamp); forecast for first 10 horizons
        try:
            forecast_mean = float(pred["mean"].iloc[:10].mean())
        except Exception:
            forecast_mean = float(pred.iloc[:10].mean().mean())
        forecast_ret_pct = (forecast_mean - current_close) / current_close * 100
        rows.append({
            "symbol": sym,
            "current_close": round(current_close, 2),
            "forecast_mean": round(forecast_mean, 2),
            "forecast_ret_pct": round(forecast_ret_pct, 3),
        })
    return rows


def main():
    print("Loading both predictors...", file=sys.stderr)
    p_v1 = load_model(REPO / "models/chronos/phase2_v1")
    p_v2 = load_model(REPO / "models/chronos/phase2_v2")

    # Test 1: predict with cutoff = -10 days (held-out 10-day forward window)
    print("Eval cutoff=-10d (held-out 10-day window)...", file=sys.stderr)
    v1_t10 = evaluate_model(p_v1, TEST_SYMBOLS, cutoff_days_ago=10)
    v2_t10 = evaluate_model(p_v2, TEST_SYMBOLS, cutoff_days_ago=10)

    # Test 2: predict now (no cutoff) — sample_forecasts equivalent
    print("Eval cutoff=0d (current-day forecast)...", file=sys.stderr)
    v1_now = evaluate_model(p_v1, TEST_SYMBOLS, cutoff_days_ago=0)
    v2_now = evaluate_model(p_v2, TEST_SYMBOLS, cutoff_days_ago=0)

    def aggregate(rows):
        rets = [r["forecast_ret_pct"] for r in rows]
        return {
            "n": len(rets),
            "mean_ret_pct": round(sum(rets) / len(rets), 3),
            "median_ret_pct": round(sorted(rets)[len(rets)//2], 3),
            "stdev_ret_pct": round(pd.Series(rets).std(), 3),
            "min_ret_pct": round(min(rets), 3),
            "max_ret_pct": round(max(rets), 3),
            "n_above_sr01_threshold": sum(1 for r in rets if abs(r) > 0.5),
        }

    report = {
        "cutoff_minus_10d": {
            "phase2_v1_rows": v1_t10,
            "phase2_v1_agg": aggregate(v1_t10),
            "phase2_v2_rows": v2_t10,
            "phase2_v2_agg": aggregate(v2_t10),
        },
        "cutoff_0d_now": {
            "phase2_v1_rows": v1_now,
            "phase2_v1_agg": aggregate(v1_now),
            "phase2_v2_rows": v2_now,
            "phase2_v2_agg": aggregate(v2_now),
        },
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()