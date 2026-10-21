#!/usr/bin/env python
"""
Daily Bars Cache Script for V12 Chronos Training

Fetches historical daily OHLCV data using Longbridge (preferred) and saves as
partitioned Parquet files under:

  output_dir/
    year=YYYY/
      symbol=XXXX/
        data.parquet

Features:
- Config driven (YAML) + CLI overrides
- Chunked/paginated fetching for long history (chunk_size_days)
- Incremental update support (merge + dedup)
- Corporate actions fields: adj_close, split_factor, dividend, adjusted, is_trading_day
- Price adjustment control (adjust_prices)
- Data quality: sorted, deduped, trading day flag, price sanity warnings
- Progress logging with chunk details
- max_symbols_per_run for batch processing

Usage:
    PYTHONPATH=. python scripts/cache_d1_bars.py --config config/data_pipeline.yaml

Or override:
    PYTHONPATH=. python scripts/cache_d1_bars.py \
        --symbols AAPL,MSFT \
        --start 2022-01-01 \
        --end 2025-06-20 \
        --output-dir data/d1_test

Config file example: config/data_pipeline.yaml
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pandas as pd
import yaml
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parents[1]
try:
    from stockapp_v12.app.bootstrap import ensure_src_on_path

    ensure_src_on_path()
except ImportError:
    if str(PROJECT_ROOT / "src") not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT / "src"))

# Phase 4: Longbridge via legacy cage only (no bare ensure_v11 / from src.)
from stockapp_v12.adapters.legacy_v11.longbridge import get_legacy_longbridge_service


def load_config(config_path: Optional[str] = None) -> dict:
    """Load configuration from YAML or return sensible defaults."""
    defaults = {
        "static_universe": ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA"],
        "start_date": "2020-01-01",
        "end_date": "today",
        "output_dir": "data/d1",
        "chunk_size_days": 500,
        "adjust_prices": True,
        "max_symbols_per_run": 0,
        "max_days_per_call": 2500,
        "force_refresh": False,
    }

    if config_path and Path(config_path).exists():
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f) or {}
        # Merge with defaults
        for k, v in defaults.items():
            cfg.setdefault(k, v)
        return cfg
    return defaults


def parse_date(d: str) -> date:
    if d.lower() == "today":
        return date.today()
    return date.fromisoformat(d)


def fetch_daily_bars(
    symbol: str,
    start: date,
    end: date,
    chunk_size_days: int = 500,
    max_days_per_call: int = 2500,
    adjust_prices: bool = True,
) -> Optional[pd.DataFrame]:
    """
    Fetch daily bars for a symbol over the full date range using Longbridge.
    Automatically paginates into chunks to support long history.
    """
    lb = get_legacy_longbridge_service()
    if lb is None:
        raise RuntimeError(
            "Longbridge service unavailable (archive missing or "
            "STOCKAPP_DISABLE_V11_ARCHIVE=1)"
        )
    if lb is None:
        logger.warning(f"Longbridge service unavailable for {symbol}")
        return None

    all_chunks = []
    current_start = start
    chunk_num = 1
    total_chunks = max(1, ((end - start).days // chunk_size_days) + 1)

    while current_start <= end:
        chunk_end = min(current_start + timedelta(days=chunk_size_days - 1), end)

        logger.info(
            f"Fetching chunk {chunk_num}/{total_chunks} for {symbol} "
            f"({current_start} ~ {chunk_end})"
        )

        # Calculate days for this chunk + buffer
        days_needed = (chunk_end - current_start).days + 10
        days_needed = min(days_needed, max_days_per_call)

        df = lb.get_daily_candles(symbol, days=days_needed)
        if df is not None and not df.empty:
            # Ensure date column
            if df.index.name in (None, "date", "Date"):
                df = df.reset_index()
                if "index" in df.columns:
                    df = df.rename(columns={"index": "date"})

            # Standardize columns
            df = df.rename(columns={
                "Date": "date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            })

            # Filter to this chunk's range
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df = df[(df["date"] >= current_start) & (df["date"] <= chunk_end)].copy()

            if not df.empty:
                # Add symbol early
                df["symbol"] = symbol.upper()

                # Add corporate action / adjustment fields (v1.1)
                # Longbridge with ForwardAdjust already adjusts close etc.
                df["adj_close"] = df["close"]
                df["split_factor"] = 1.0
                df["dividend"] = 0.0
                df["adjusted"] = adjust_prices

                # Simple trading day mark (volume > 0 is a strong signal for daily bars)
                df["is_trading_day"] = (df["volume"] > 0) | (df["close"] > 0)

                # Data quality warnings
                if (df["close"] <= 0).any():
                    logger.warning(f"Found non-positive close prices in chunk for {symbol}")
                if (df["volume"] < 0).any():
                    logger.warning(f"Found negative volume in chunk for {symbol}")

                all_chunks.append(df)

        current_start = chunk_end + timedelta(days=1)
        chunk_num += 1

    if not all_chunks:
        logger.warning(f"No data returned for {symbol}")
        return None

    combined = pd.concat(all_chunks, ignore_index=True)
    combined = combined.drop_duplicates(subset=["date"]).sort_values("date")

    # Ensure all v1.1 columns exist
    cols = [
        "date", "symbol", "open", "high", "low", "close", "volume",
        "adj_close", "split_factor", "dividend", "adjusted", "is_trading_day"
    ]
    for c in cols:
        if c not in combined.columns:
            if c == "symbol":
                combined[c] = symbol.upper()
            elif c == "is_trading_day":
                combined[c] = (combined.get("volume", 0) > 0) | (combined.get("close", 0) > 0)
            else:
                combined[c] = None if c not in ["adjusted"] else True

    return combined[cols].reset_index(drop=True)


def save_partitioned_parquet(df: pd.DataFrame, base_dir: Path, symbol: str):
    """Write DataFrame as partitioned Parquet (year/symbol)."""
    if df.empty:
        return

    df = df.copy()
    df["year"] = pd.to_datetime(df["date"]).dt.year.astype(str)
    base_dir = Path(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    for year, group in df.groupby("year"):
        out_dir = base_dir / f"year={year}" / f"symbol={symbol}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / "data.parquet"

        # Drop helper column before write
        group = group.drop(columns=["year"], errors="ignore")
        group.to_parquet(out_file, index=False, engine="pyarrow")
        logger.debug(f"Wrote {len(group)} rows for {symbol} {year} -> {out_file}")


def load_existing_data(base_dir: Path, symbol: str) -> pd.DataFrame:
    """Load all existing partitioned data for a symbol (if any)."""
    base_dir = Path(base_dir)
    pattern = str(base_dir / "**" / f"symbol={symbol}" / "data.parquet")
    files = list(Path().glob(pattern)) if hasattr(Path, 'glob') else []
    # Better cross-platform glob
    import glob
    files = glob.glob(pattern, recursive=True)

    if not files:
        return pd.DataFrame()

    dfs = []
    for f in files:
        try:
            dfs.append(pd.read_parquet(f))
        except Exception as e:
            logger.warning(f"Failed to read {f}: {e}")

    if not dfs:
        return pd.DataFrame()

    df = pd.concat(dfs, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.drop_duplicates(subset=["date"]).sort_values("date")
    return df


def merge_and_dedup(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Merge new data with existing, keep latest values, sorted."""
    if existing.empty:
        return new
    if new.empty:
        return existing

    combined = pd.concat([existing, new], ignore_index=True)
    combined = combined.drop_duplicates(subset=["date", "symbol"], keep="last")
    combined = combined.sort_values("date").reset_index(drop=True)
    return combined


def main():
    parser = argparse.ArgumentParser(description="Cache daily OHLCV bars for V12 training")
    parser.add_argument("--config", default="config/data_pipeline.yaml", help="Path to YAML config")
    parser.add_argument("--symbols", help="Comma separated symbols (overrides config)")
    parser.add_argument("--start", help="Start date YYYY-MM-DD (overrides config)")
    parser.add_argument("--end", help="End date YYYY-MM-DD or 'today' (overrides config)")
    parser.add_argument("--output-dir", help="Output directory (overrides config)")
    parser.add_argument("--chunk-size", type=int, help="Chunk size in days for pagination (overrides config)")
    parser.add_argument("--adjust", action="store_true", help="Enable price adjustment (overrides config)")
    parser.add_argument("--max-symbols", type=int, help="Max symbols to process this run (overrides config)")
    parser.add_argument("--force", action="store_true", help="Force refresh all data")
    args = parser.parse_args()

    cfg = load_config(args.config)

    # Apply CLI overrides
    if args.symbols:
        cfg["static_universe"] = [s.strip().upper() for s in args.symbols.split(",")]
    if args.start:
        cfg["start_date"] = args.start
    if args.end:
        cfg["end_date"] = args.end
    if args.output_dir:
        cfg["output_dir"] = args.output_dir
    if args.chunk_size is not None:
        cfg["chunk_size_days"] = args.chunk_size
    if args.adjust:
        cfg["adjust_prices"] = True
    if args.max_symbols is not None:
        cfg["max_symbols_per_run"] = args.max_symbols
    if args.force:
        cfg["force_refresh"] = True

    start = parse_date(cfg["start_date"])
    end = parse_date(cfg["end_date"])
    output_dir = Path(cfg["output_dir"])
    universe = cfg.get("static_universe", [])
    chunk_size = cfg.get("chunk_size_days", 500)
    adjust_prices = cfg.get("adjust_prices", True)
    max_symbols = cfg.get("max_symbols_per_run", 0)
    max_days = cfg.get("max_days_per_call", 2500)
    force = cfg.get("force_refresh", False)

    logger.info(f"Starting Daily Bars Cache")
    logger.info(f"Universe: {universe}")
    logger.info(f"Range: {start} to {end}")
    logger.info(f"Output: {output_dir}")
    logger.info(f"Force refresh: {force}")

    output_dir.mkdir(parents=True, exist_ok=True)

    if max_symbols > 0:
        universe = universe[:max_symbols]
        logger.info(f"Limited to first {max_symbols} symbols for this run")

    for symbol in universe:
        logger.info(f"Processing {symbol}...")

        existing = pd.DataFrame()
        effective_start = start
        if not force:
            existing = load_existing_data(output_dir, symbol)
            if not existing.empty:
                last_date = existing["date"].max()
                logger.info(f"  Existing data up to {last_date} ({len(existing)} rows)")
                # Fetch only newer data
                fetch_start = last_date + timedelta(days=1)
                if fetch_start > end:
                    logger.info(f"  Data up to date for {symbol}, skipping.")
                    continue
                effective_start = max(start, fetch_start)

        new_df = fetch_daily_bars(
            symbol,
            effective_start,
            end,
            chunk_size_days=chunk_size,
            max_days_per_call=max_days,
            adjust_prices=adjust_prices,
        )
        if new_df is None or new_df.empty:
            logger.warning(f"  No new data for {symbol} in range.")
            continue

        merged = merge_and_dedup(existing, new_df)

        # Write all relevant partitions (overwrite for this symbol's years)
        save_partitioned_parquet(merged, output_dir, symbol)

        logger.info(f"  {symbol}: total {len(merged)} rows after merge")

    logger.info("Daily bars cache completed successfully.")


if __name__ == "__main__":
    main()
