#!/usr/bin/env python3
"""Refresh shadow-universe daily features via **Longbridge** (production path).

Uses ``stockapp_v12.adapters.marketdata.longbridge_daily`` (native longport SDK,
no V11 archive). Requires env:
  LONGBRIDGE_APP_KEY, LONGBRIDGE_APP_SECRET, LONGBRIDGE_ACCESS_TOKEN

  1. Fetch OHLCV for shadow 10 via Longbridge QuoteContext
  2. Write ``data/daily_clean/phase1/year=*/symbol=*/data.parquet``
  3. Rebuild ``data/features/daily/<SYM>.parquet``

Usage (NUC):
  source scripts/source_archive_free_env.sh
  set -a; source .env; set +a   # if not already in env
  "$PYTHON" scripts/refresh_shadow_features.py

Or: bash scripts/run_refresh_shadow_features.sh
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

# Load .env if present (NUC ops often keep keys there)
_env = ROOT / ".env"
if _env.is_file():
    try:
        from dotenv import load_dotenv

        load_dotenv(_env, override=False)
    except ImportError:
        pass

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
# Longbridge OpenAPI caps kline count (~500); enough for indicators + Chronos ctx
LOOKBACK_DAYS = 500
# polite spacing between symbols (rate limits)
_SYMBOL_SLEEP_S = 0.35
ET = ZoneInfo("America/New_York")


def main() -> int:
    from stockapp_v12.adapters.marketdata.longbridge_daily import (
        LONGBRIDGE_SDK_AVAILABLE,
        get_longbridge_daily_client,
    )

    if not LONGBRIDGE_SDK_AVAILABLE:
        print(
            "ERROR: longport SDK not installed. "
            "Run: .venv/bin/pip install 'longport>=3.0.0'",
            file=sys.stderr,
        )
        return 2
    for k in ("LONGBRIDGE_APP_KEY", "LONGBRIDGE_APP_SECRET", "LONGBRIDGE_ACCESS_TOKEN"):
        if not os.getenv(k):
            print(f"ERROR: missing env {k}", file=sys.stderr)
            return 2

    import time

    client = get_longbridge_daily_client()
    OUT_CLEAN.mkdir(parents=True, exist_ok=True)
    ok_syms: list[str] = []
    for i, sym in enumerate(SYMBOLS):
        if i:
            time.sleep(_SYMBOL_SLEEP_S)
        print(f"[longbridge] {sym} ...", flush=True)
        frame = client.get_daily_candles_frame(sym, days=LOOKBACK_DAYS)
        if frame is None or frame.empty:
            print(f"  FAIL empty for {sym}", flush=True)
            continue
        for year, g in frame.groupby(frame["timestamp"].dt.year):
            dest = OUT_CLEAN / f"year={int(year)}" / f"symbol={sym}"
            dest.mkdir(parents=True, exist_ok=True)
            g.to_parquet(dest / "data.parquet", index=False)
        print(
            f"  rows={len(frame)} last={frame['timestamp'].iloc[-1]}",
            flush=True,
        )
        ok_syms.append(sym)

    if len(ok_syms) < len(SYMBOLS):
        print(
            f"ERROR: only {len(ok_syms)}/{len(SYMBOLS)} symbols fetched: {ok_syms}",
            file=sys.stderr,
        )
        return 1

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
