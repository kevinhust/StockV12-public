#!/usr/bin/env python3
"""
Feature Engineering Pipeline for Chronos (daily) + YOLO (5min) Training

Reads Phase 1 cleaned data from data/daily_clean/phase1 and
data/intraday_clean_5min/phase1, computes a fixed set of technical
indicators + cross-asset features, and writes to:

  data/features/daily/<symbol>.parquet
  data/features/intraday_5min/date=YYYY-MM-DD/symbol=<sym>.parquet

Usage:
  PYTHONPATH=src:scripts:. python scripts/feature_engineering.py --freq daily
  PYTHONPATH=src:scripts:. python scripts/feature_engineering.py --freq 5min
  PYTHONPATH=src:scripts:. python scripts/feature_engineering.py --freq both --symbols AAPL,MSFT

Indicators (all standard, no proprietary libraries):

  daily:
    - ret_1d, ret_5d, ret_20d           # log returns
    - vol_10d, vol_20d                   # rolling std of 1d returns
    - rsi_14                            # Wilder's RSI
    - macd, macd_signal, macd_hist      # MACD(12,26,9)
    - atr_14                            # Wilder's ATR (proxy from daily H/L/C)
    - bb_pct_b_20                       # Bollinger %B (20, 2)
    - obv                               # On-Balance Volume
    - obv_slope_10                      # OBV 10-day slope (normalized)
    - dist_ma_20, dist_ma_50            # distance from MA20/MA50 (pct)
    - sector_beta_20                    # 20-day beta vs SPY (cross-asset)

  intraday 5min:
    - ret_bar                           # bar-to-bar return
    - rsi_14                            # RSI on close
    - macd_fast                         # MACD(12,26,9) but compressed
    - atr_20                            # ATR(20) on 5min
    - vwap_dev                          # (close - vwap) / vwap
    - vol_60                            # rolling 60-bar (~5h) return std
    - dollar_vol_bar                    # close * volume

Cross-asset (loaded from same Phase 1 universe if available, else skipped):
    - SPY ret_1d / ret_5d                # benchmark daily return
    - Sector ETF ret_1d                 # XLK (Tech) / XLF (Fin) / XLE (Energy)

Schema is intentionally narrow (only model-facing features). The OHLCV base
remains in data/daily_clean/phase1.
"""

import argparse
import sys
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAILY_IN = PROJECT_ROOT / "data" / "daily_clean" / "phase1"
FIVEMIN_IN = PROJECT_ROOT / "data" / "intraday_clean_5min" / "phase1"
DAILY_IN_PHASE2 = PROJECT_ROOT / "data" / "daily_clean" / "phase2"
FIVEMIN_IN_PHASE2 = PROJECT_ROOT / "data" / "intraday_clean_5min" / "phase2"
DAILY_OUT = PROJECT_ROOT / "data" / "features" / "daily"
FIVEMIN_OUT = PROJECT_ROOT / "data" / "features" / "intraday_5min"

# Tech sector ETF proxies (Phase 1 has SPY/QQQ via symbols? Check at runtime)
SECTOR_PROXIES = {
    "Technology": "QQQ",   # closest stand-in for XLK in Phase 1 universe
    "Financial": "JPM",    # financial sector proxy if no XLF
    "Energy": "XOM",       # energy sector proxy if no XLE
    "Consumer": "AMZN",
    "Healthcare": "JNJ",
    "Industrial": "HON",
}
BENCHMARK = "SPY"  # not in Phase 1; will skip if missing


# ---------------------------------------------------------------------------
# Indicator functions
# ---------------------------------------------------------------------------
def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - sig
    return macd, sig, hist


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def _bbands(close: pd.Series, period: int = 20, num_std: float = 2.0):
    ma = close.rolling(period).mean()
    sd = close.rolling(period).std(ddof=0)
    upper = ma + num_std * sd
    lower = ma - num_std * sd
    pct_b = (close - lower) / (upper - lower).replace(0, np.nan)
    return ma, upper, lower, pct_b


def _obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    sign = np.sign(close.diff()).fillna(0.0)
    return (sign * volume).cumsum()


# ---------------------------------------------------------------------------
# Daily feature builder
# ---------------------------------------------------------------------------
def build_daily_features(symbol: str, df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("timestamp").reset_index(drop=True).copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    close = df["close"]
    high = df["high"]
    low = df["low"]
    vol = df["volume"]

    # Returns
    df["ret_1d"] = np.log(close / close.shift(1))
    df["ret_5d"] = np.log(close / close.shift(5))
    df["ret_20d"] = np.log(close / close.shift(20))

    # Vol
    df["vol_10d"] = df["ret_1d"].rolling(10).std()
    df["vol_20d"] = df["ret_1d"].rolling(20).std()

    # RSI
    df["rsi_14"] = _rsi(close, 14)

    # MACD
    macd, sig, hist = _macd(close)
    df["macd"] = macd
    df["macd_signal"] = sig
    df["macd_hist"] = hist

    # ATR (daily proxy from H/L/C)
    df["atr_14"] = _atr(high, low, close, 14)

    # BBands %B
    _, _, _, pct_b = _bbands(close, 20, 2.0)
    df["bb_pct_b_20"] = pct_b

    # OBV + normalized slope
    df["obv"] = _obv(close, vol)
    obv_slope = df["obv"].diff(10) / df["obv"].rolling(10).std().replace(0, np.nan)
    df["obv_slope_10"] = obv_slope.fillna(0.0)

    # Distance from MA
    ma20 = close.rolling(20).mean()
    ma50 = close.rolling(50).mean()
    df["dist_ma_20"] = (close - ma20) / ma20
    df["dist_ma_50"] = (close - ma50) / ma50

    # symbol col for traceability
    df["symbol"] = symbol

    # final cols
    cols = [
        "timestamp", "symbol", "close",
        "ret_1d", "ret_5d", "ret_20d",
        "vol_10d", "vol_20d",
        "rsi_14", "macd", "macd_signal", "macd_hist",
        "atr_14", "bb_pct_b_20", "obv", "obv_slope_10",
        "dist_ma_20", "dist_ma_50",
    ]
    return df[cols]


# ---------------------------------------------------------------------------
# 5min feature builder
# ---------------------------------------------------------------------------
def build_5min_features(symbol: str, df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("timestamp").reset_index(drop=True).copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    close = df["close"]
    high = df["high"]
    low = df["low"]
    vol = df["volume"]
    vwap = df.get("vwap", close)

    df["ret_bar"] = np.log(close / close.shift(1))
    df["rsi_14"] = _rsi(close, 14)
    macd, sig, _ = _macd(close, fast=12, slow=26, signal=9)
    df["macd_fast"] = macd
    df["macd_sig"] = sig
    df["atr_20"] = _atr(high, low, close, 20)
    df["vwap_dev"] = (close - vwap) / vwap.replace(0, np.nan)
    df["vol_60"] = df["ret_bar"].rolling(60).std()
    df["dollar_vol_bar"] = close * vol
    df["symbol"] = symbol

    cols = [
        "timestamp", "symbol", "close",
        "ret_bar", "rsi_14", "macd_fast", "macd_sig",
        "atr_20", "vwap_dev", "vol_60", "dollar_vol_bar",
    ]
    return df[cols]


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------
def _daily_input_dirs(phase: str = "all") -> List[Path]:
    """Return the list of input directories for the requested phase scope.

    phase: "1" -> [phase1]; "2" -> [phase2]; "all" -> [phase1, phase2]
    """
    if phase == "1":
        return [DAILY_IN]
    if phase == "2":
        return [DAILY_IN_PHASE2] if DAILY_IN_PHASE2.exists() else []
    # "all"
    dirs = [DAILY_IN]
    if DAILY_IN_PHASE2.exists():
        dirs.append(DAILY_IN_PHASE2)
    return dirs


def _fivemin_input_dirs(phase: str = "all") -> List[Path]:
    if phase == "1":
        return [FIVEMIN_IN]
    if phase == "2":
        return [FIVEMIN_IN_PHASE2] if FIVEMIN_IN_PHASE2.exists() else []
    dirs = [FIVEMIN_IN]
    if FIVEMIN_IN_PHASE2.exists():
        dirs.append(FIVEMIN_IN_PHASE2)
    return dirs


def load_daily_symbol(symbol: str, phase: str = "all") -> pd.DataFrame:
    files = []
    for d in _daily_input_dirs(phase):
        files.extend(sorted(d.glob(f"year=*/symbol={symbol}/data.parquet")))
    if not files:
        return pd.DataFrame()
    dfs = [pd.read_parquet(f) for f in files]
    return pd.concat(dfs).sort_values("timestamp")


def load_5min_symbol(symbol: str, phase: str = "all") -> pd.DataFrame:
    files = []
    for d in _fivemin_input_dirs(phase):
        files.extend(sorted(d.glob(f"date=*/symbol={symbol}/data.parquet")))
    if not files:
        return pd.DataFrame()
    dfs = [pd.read_parquet(f) for f in files]
    return pd.concat(dfs).sort_values("timestamp")


def list_daily_symbols(phase: str = "all") -> List[str]:
    syms: set = set()
    for d in _daily_input_dirs(phase):
        syms.update({p.parent.name.split("=")[1] for p in d.glob("year=*/symbol=*/data.parquet")})
    return sorted(syms)


def list_5min_symbols(phase: str = "all") -> List[str]:
    syms: set = set()
    for d in _fivemin_input_dirs(phase):
        syms.update({p.parent.name.split("=")[1] for p in d.glob("date=*/symbol=*/data.parquet")})
    return sorted(syms)


# ---------------------------------------------------------------------------
# Cross-asset augmentation
# ---------------------------------------------------------------------------
def add_benchmark_features(daily_features: dict, benchmark_sym: str, sector_map: dict) -> dict:
    """For each symbol, add benchmark ret + sector proxy ret columns."""
    if benchmark_sym not in daily_features:
        return daily_features  # silently skip if benchmark not available

    bench = daily_features[benchmark_sym][["timestamp", "ret_1d", "ret_5d"]].rename(
        columns={"ret_1d": "spy_ret_1d", "ret_5d": "spy_ret_5d"}
    )

    out = {}
    for sym, df in daily_features.items():
        merged = df.merge(bench, on="timestamp", how="left")
        # sector proxy ret_1d
        proxy = sector_map.get(sym)
        if proxy and proxy != sym and proxy in daily_features:
            proxy_df = daily_features[proxy][["timestamp", "ret_1d"]].rename(
                columns={"ret_1d": "sector_ret_1d"}
            )
            merged = merged.merge(proxy_df, on="timestamp", how="left")
        else:
            merged["sector_ret_1d"] = np.nan
        # 20-day rolling beta vs benchmark
        rolling_cov = merged["ret_1d"].rolling(20).cov(merged["spy_ret_1d"])
        rolling_var = merged["spy_ret_1d"].rolling(20).var()
        merged["sector_beta_20"] = (rolling_cov / rolling_var.replace(0, np.nan)).fillna(0.0)
        out[sym] = merged
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def process_daily(symbols: Optional[List[str]] = None, phase: str = "all") -> dict:
    syms = symbols or list_daily_symbols(phase)
    print(f"[daily] phase={phase} processing {len(syms)} symbols")
    DAILY_OUT.mkdir(parents=True, exist_ok=True)
    out = {}
    for sym in syms:
        raw = load_daily_symbol(sym, phase)
        if raw.empty:
            print(f"  skip {sym}: no data")
            continue
        feat = build_daily_features(sym, raw)
        feat.to_parquet(DAILY_OUT / f"{sym}.parquet", index=False)
        out[sym] = feat
        print(f"  {sym}: {len(feat)} rows")
    # Add cross-asset features (only if benchmark present)
    if BENCHMARK in out:
        sector_map = {sym: SECTOR_PROXIES.get(_infer_sector(sym), BENCHMARK) for sym in out}
        out = add_benchmark_features(out, BENCHMARK, sector_map)
        for sym, feat in out.items():
            feat.to_parquet(DAILY_OUT / f"{sym}.parquet", index=False)
    else:
        print(f"[daily] benchmark {BENCHMARK} not in universe; skipping cross-asset enrichment")
    print(f"[daily] wrote {len(out)} parquet files to {DAILY_OUT}")
    return out


def process_5min(symbols: Optional[List[str]] = None, phase: str = "all") -> None:
    syms = symbols or list_5min_symbols(phase)
    print(f"[5min] phase={phase} processing {len(syms)} symbols")
    for sym in syms:
        raw = load_5min_symbol(sym)
        if raw.empty:
            continue
        feat = build_5min_features(sym, raw)
        # write per-date for downstream consumption
        feat["date"] = feat["timestamp"].dt.strftime("%Y-%m-%d")
        for dt, ddf in feat.groupby("date"):
            out_dir = FIVEMIN_OUT / f"date={dt}" / f"symbol={sym}"
            out_dir.mkdir(parents=True, exist_ok=True)
            cols = [c for c in feat.columns if c not in ("date",)]
            ddf[cols].to_parquet(out_dir / "data.parquet", index=False)
        print(f"  {sym}: {len(feat)} rows")


def _infer_sector(symbol: str) -> str:
    sector_keywords = {
        "Technology": ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AVGO", "AMD", "CRM", "ADBE",
                       "ORCL", "CSCO", "INTC", "QCOM", "TXN", "NOW", "SNOW", "PANW", "PLTR",
                       "ARM", "SMCI", "MU", "AMAT", "LRCX", "KLAC", "ASML", "TSM", "MRVL", "ON"],
        "Consumer": ["TSLA", "RIVN", "LCID", "F", "GM", "NIO", "XPEV", "HD", "NKE", "SBUX",
                     "COST", "MAR", "ABNB", "BKNG", "UBER"],
        "Financial": ["JPM", "V", "MA", "BAC", "COIN", "HOOD", "SOFI", "BLK", "GS", "SPGI"],
        "Energy": ["XOM", "CVX", "ENPH", "SEDG", "RUN", "PLUG", "SPWR"],
        "Healthcare": ["JNJ", "LLY", "UNH", "ABBV", "MRK", "PFE"],
        "Industrial": ["CAT", "HON", "RTX", "UPS", "BA"],
    }
    for sec, members in sector_keywords.items():
        if symbol in members:
            return sec
    return "Technology"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--freq", choices=["daily", "5min", "both"], default="both")
    parser.add_argument("--phase", choices=["1", "2", "all"], default="all",
                        help="Which cleaned-data phase to ingest (1, 2, or all=both)")
    parser.add_argument("--symbols", help="Comma-separated override")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None

    if args.freq in ("daily", "both"):
        process_daily(symbols, phase=args.phase)
    if args.freq in ("5min", "both"):
        process_5min(symbols, phase=args.phase)

    # Write manifest
    manifest = {
        "daily": sorted([p.stem for p in DAILY_OUT.glob("*.parquet")]),
        "5min_symbols": list_5min_symbols(),
        "generated": pd.Timestamp.now().isoformat(),
    }
    (PROJECT_ROOT / "data" / "features" / "manifest.json").write_text(
        yaml.safe_dump(manifest, sort_keys=False)
    )
    print("\nWrote manifest: data/features/manifest.json")


if __name__ == "__main__":
    main()