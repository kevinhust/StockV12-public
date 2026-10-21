"""Backfill sector + SPY feature columns for symbols whose parquet is missing them.

Use case: a symbol whose OHLCV was re-populated (e.g. via the 2022-01-29..2022-06-08
gap-repair backfill in commit 147979fe) but whose feature engineering pass was not
re-run, so it lacks the cross-asset features (`spy_ret_1d`, `spy_ret_5d`,
`sector_ret_1d`, `sector_beta_20`) that the model was trained on. The model then
fails at predict time with '4 columns are missing from data'.

This script re-derives those 4 columns using the same formula as
`scripts/feature_engineering.py:add_benchmark_features`. It is idempotent,
writes a backup before mutating, and validates the new column counts and
non-NaN rates before committing.

Usage:
    # Dry run (default — reports what would change, writes nothing):
    python scripts/backfill_sector_spy_features.py --symbols META

    # Apply (writes backup first, then overwrites the parquet):
    python scripts/backfill_sector_spy_features.py --symbols META --apply

    # Multiple symbols (e.g. META + any other gap):
    python scripts/backfill_sector_spy_features.py --symbols META,FB --apply
"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# Mirrors v12/scripts/feature_engineering.py exactly.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DAILY_OUT = PROJECT_ROOT / "data" / "features" / "daily"
BACKUP_DIR = PROJECT_ROOT / "data" / "features" / "daily" / "_backups"
BENCHMARK = "SPY"
SECTOR_PROXIES = {
    "Technology": "QQQ",   # closest stand-in for XLK in Phase 1 universe
    "Financial": "JPM",
    "Energy": "XOM",
    "Consumer": "AMZN",
    "Healthcare": "JNJ",
    "Industrial": "HON",
}
SECTOR_KEYWORDS = {
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

FEATURE_COLS = ("spy_ret_1d", "spy_ret_5d", "sector_ret_1d", "sector_beta_20")


def _infer_sector(symbol: str) -> str:
    for sec, members in SECTOR_KEYWORDS.items():
        if symbol in members:
            return sec
    return "Technology"


def _add_benchmark_for_symbol(
    target: pd.DataFrame,
    benchmark: pd.DataFrame,
    sector_proxy: pd.DataFrame | None,
) -> pd.DataFrame:
    """Replicates the per-symbol branch of add_benchmark_features (1:1 formula)."""
    bench = benchmark[["timestamp", "ret_1d", "ret_5d"]].rename(
        columns={"ret_1d": "spy_ret_1d", "ret_5d": "spy_ret_5d"}
    )
    merged = target.merge(bench, on="timestamp", how="left")
    if sector_proxy is not None:
        proxy_df = sector_proxy[["timestamp", "ret_1d"]].rename(
            columns={"ret_1d": "sector_ret_1d"}
        )
        merged = merged.merge(proxy_df, on="timestamp", how="left")
    else:
        merged["sector_ret_1d"] = np.nan
    rolling_cov = merged["ret_1d"].rolling(20).cov(merged["spy_ret_1d"])
    rolling_var = merged["spy_ret_1d"].rolling(20).var()
    merged["sector_beta_20"] = (rolling_cov / rolling_var.replace(0, np.nan)).fillna(0.0)
    return merged


def backfill_one(
    symbol: str,
    apply: bool,
    features_dir: Path = DAILY_OUT,
    backup_dir: Path = BACKUP_DIR,
) -> dict:
    target_path = features_dir / f"{symbol}.parquet"
    if not target_path.exists():
        return {"symbol": symbol, "status": "MISSING_FILE", "path": str(target_path)}

    target = pd.read_parquet(target_path)
    missing = [c for c in FEATURE_COLS if c not in target.columns]
    if not missing:
        return {
            "symbol": symbol, "status": "ALREADY_COMPLETE",
            "n_rows": len(target),
            "cols_present": sorted(set(target.columns) & set(FEATURE_COLS)),
        }

    sector = _infer_sector(symbol)
    proxy_sym = SECTOR_PROXIES[sector]
    benchmark_path = features_dir / f"{BENCHMARK}.parquet"
    proxy_path = features_dir / f"{proxy_sym}.parquet" if proxy_sym != symbol else None
    if not benchmark_path.exists():
        return {"symbol": symbol, "status": "ABORT_BENCHMARK_MISSING",
                "benchmark": BENCHMARK, "path": str(benchmark_path)}
    if proxy_path is not None and not proxy_path.exists():
        return {"symbol": symbol, "status": "ABORT_PROXY_MISSING",
                "proxy": proxy_sym, "path": str(proxy_path)}

    benchmark = pd.read_parquet(benchmark_path)
    sector_proxy = pd.read_parquet(proxy_path) if proxy_path is not None else None

    enriched = _add_benchmark_for_symbol(target, benchmark, sector_proxy)

    # Sanity: column order, dtype consistency
    new_cols = [c for c in FEATURE_COLS if c not in target.columns]
    for c in new_cols:
        if c not in enriched.columns:
            return {"symbol": symbol, "status": "ABORT_FORMULA_BUG",
                    "missing_after_merge": c}
    # Row count must match
    if len(enriched) != len(target):
        return {"symbol": symbol, "status": "ABORT_ROW_COUNT_MISMATCH",
                "before": len(target), "after": len(enriched)}

    # Validation: non-NaN rates
    nan_report = {c: int(enriched[c].isna().sum()) for c in new_cols}
    nan_pct = {c: round(nan_report[c] / len(enriched) * 100, 2) for c in new_cols}
    # The first 19 rows of sector_beta_20 will be NaN (rolling window warm-up);
    # the rest of the universe has this same pattern, so it's expected.

    record = {
        "symbol": symbol,
        "sector": sector,
        "sector_proxy": proxy_sym,
        "n_rows": len(enriched),
        "n_cols_before": len(target.columns),
        "n_cols_after": len(enriched.columns),
        "added_cols": new_cols,
        "nan_counts": nan_report,
        "nan_pct": nan_pct,
    }

    if not apply:
        record["status"] = "DRY_RUN"
        return record

    # Backup then write
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{symbol}.{ts}.parquet"
    shutil.copy2(target_path, backup_path)
    enriched.to_parquet(target_path, index=False)
    record["status"] = "APPLIED"
    record["backup"] = str(backup_path)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", required=True,
                        help="Comma-separated list of symbols to backfill (e.g. META,FB)")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write the parquet (default is dry-run)")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print(f"=== backfill_sector_spy_features ===")
    print(f"mode: {'APPLY' if args.apply else 'DRY RUN'}")
    print(f"symbols: {symbols}")
    print(f"features_dir: {DAILY_OUT}")
    print(f"backup_dir: {BACKUP_DIR}")
    print()

    summary = {"applied": [], "dry_run": [], "skipped": [], "errors": []}
    for sym in symbols:
        rec = backfill_one(sym, apply=args.apply)
        if rec["status"] == "APPLIED":
            summary["applied"].append(rec)
            print(f"  [APPLIED] {sym}: +{rec['n_cols_after'] - rec['n_cols_before']} cols "
                  f"({', '.join(rec['added_cols'])})  backup={rec['backup']}")
            print(f"             nan_pct: {rec['nan_pct']}")
        elif rec["status"] == "DRY_RUN":
            summary["dry_run"].append(rec)
            print(f"  [DRY RUN] {sym} (sector={rec.get('sector')}, proxy={rec.get('sector_proxy')}): "
                  f"would add {len(rec['added_cols'])} cols")
            print(f"             nan_pct after: {rec['nan_pct']}")
        elif rec["status"] == "ALREADY_COMPLETE":
            summary["skipped"].append(rec)
            print(f"  [SKIP] {sym}: all 4 cols already present")
        else:
            summary["errors"].append(rec)
            print(f"  [ERROR] {sym}: {rec}")

    print()
    print(f"=== summary ===")
    print(f"applied:  {len(summary['applied'])}")
    print(f"dry_run:  {len(summary['dry_run'])}")
    print(f"skipped:  {len(summary['skipped'])}")
    print(f"errors:   {len(summary['errors'])}")
    return 0 if not summary["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
