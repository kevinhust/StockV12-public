#!/usr/bin/env python3
"""DEPRECATED emergency fallback — prefer Longbridge.

Production path: ``scripts/refresh_shadow_features.py`` (longport SDK).
Keep this file only if Longbridge OpenAPI is down and an operator
explicitly chooses Yahoo; do **not** wire it into cron.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

SYMBOLS = [
    "AAPL",
    "NVDA",
    "TSLA",
    "JPM",
    "MSFT",
    "AMZN",
    "AMD",
    "META",
    "GOOGL",
    "SPY",
]
OUT_CLEAN = ROOT / "data" / "daily_clean" / "phase1"
START = "2018-01-01"
ET = ZoneInfo("America/New_York")


def fetch(sym: str) -> pd.DataFrame:
    end = (date.today() + timedelta(days=1)).isoformat()
    df = yf.Ticker(sym).history(start=START, end=end, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"empty history for {sym}")
    df = df.reset_index()
    col = "Date" if "Date" in df.columns else df.columns[0]
    ts = pd.to_datetime(df[col])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(ET)
    else:
        ts = ts.dt.tz_convert(ET)
    out = pd.DataFrame(
        {
            "timestamp": ts,
            "open": df["Open"].astype(float).values,
            "high": df["High"].astype(float).values,
            "low": df["Low"].astype(float).values,
            "close": df["Close"].astype(float).values,
            "volume": df["Volume"].astype(float).values,
            "adj_close": df.get("Adj Close", df["Close"]).astype(float).values,
            "symbol": sym,
        }
    )
    return out.dropna(subset=["close"]).sort_values("timestamp").reset_index(drop=True)


def main() -> int:
    OUT_CLEAN.mkdir(parents=True, exist_ok=True)
    for sym in SYMBOLS:
        print(f"[fetch] {sym}", flush=True)
        df = fetch(sym)
        for year, g in df.groupby(df["timestamp"].dt.year):
            dest = OUT_CLEAN / f"year={int(year)}" / f"symbol={sym}"
            dest.mkdir(parents=True, exist_ok=True)
            g.to_parquet(dest / "data.parquet", index=False)
        print(f"  rows={len(df)} last={df['timestamp'].iloc[-1]}", flush=True)

    from feature_engineering import process_daily

    print("[feature_engineering] daily phase1", flush=True)
    process_daily(symbols=SYMBOLS, phase="1")

    from check_feature_freshness import check_features_dir
    from datetime import datetime

    res = check_features_dir(
        ROOT / "data" / "features" / "daily",
        SYMBOLS,
        as_of=datetime.now(ET),
        max_staleness_days=1,
    )
    print(
        f"freshness ok={res.get('ok')} n_ok={res.get('n_ok')} "
        f"n_stale={res.get('n_stale')} expected={res.get('expected_last')}"
    )
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
