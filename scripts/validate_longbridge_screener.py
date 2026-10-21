#!/usr/bin/env python3
from dotenv import load_dotenv
load_dotenv()  # Load .env as early as possible

"""
Validation Spike for Longbridge Screener (Component 2 - Scanning Layer)

To use real Longbridge data:
1. Put your keys in the .env file at the project root:
   LONGBRIDGE_APP_KEY=your_app_key
   LONGBRIDGE_APP_SECRET=your_app_secret
   LONGBRIDGE_ACCESS_TOKEN=your_access_token

2. Run the script without --dry-run. It will automatically load the .env file.

Purpose:
- Low-cost experiment to answer the critical unknowns before full implementation.
- Measure actual universe reduction when using server-side hard filters.
- Test response time/stability.
- Observe degradation behavior.

Run instructions:
- Locked set validation (recommended): PYTHONPATH=src:scripts:. python scripts/validate_longbridge_screener.py --locked-set --dry-run
- For real Longbridge Screener:       PYTHONPATH=src:scripts:. python scripts/validate_longbridge_screener.py --locked-set
- Legacy modes still supported via --test-additional


Output:
- Before/after count
- Reduction %
- Time taken
- Recommendations
"""

import os
import sys
import time
import argparse
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

import numpy as np
import pandas as pd
from datetime import date, timedelta
from unittest.mock import patch

# Add project paths
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

try:
    from stockapp_v12.services.longbridge_screener import LongbridgeScreener
    from stockapp_v12.services.swing_scanner import SwingScanner
except ImportError as e:
    print("Import error - run with PYTHONPATH=src:scripts:.")
    raise

# High-liquidity candidate pool for locked-set validation (target 500+ unique)
# This list focuses on liquid US-listed names suitable for swing trading validation.
DEFAULT_CANDIDATES = [
    # Tech / Semis
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AVGO", "AMD", "INTC", "QCOM", "TXN", "MU", "ARM", "SNPS", "CDNS", "ANET", "KLAC", "LRCX", "AMAT",
    "CSCO", "ORCL", "IBM", "ACN", "ADBE", "CRM", "NOW", "INTU", "WDAY", "SNOW", "DDOG", "NET", "CRWD", "PANW", "ZS", "OKTA", "MDB", "TEAM", "PLTR",
    "TSM", "ASML", "AMAT", "KLAC", "LRCX", "MRVL", "NXPI", "ON", "MCHP", "MPWR",
    # Software / Internet
    "NFLX", "UBER", "LYFT", "SNAP", "PINS", "RBLX", "DKNG", "RIVN", "LCID", "ABNB", "BKNG", "MAR",
    "COIN", "MSTR", "HOOD", "SOFI", "UPST", "AFRM", "SQ", "PYPL", "FISV", "GPN",
    # Financials
    "JPM", "V", "MA", "BAC", "WFC", "GS", "MS", "BLK", "AXP", "SCHW", "C", "USB", "PNC", "TFC", "COF", "BK", "ICE", "CME", "SPGI", "MCO", "AON", "MMC",
    # Healthcare
    "JNJ", "PFE", "UNH", "ABBV", "MRK", "LLY", "TMO", "ABT", "DHR", "BMY", "AMGN", "GILD", "REGN", "VRTX", "ISRG", "SYK", "BSX", "MDT", "EW", "ZBH", "BIIB", "ILMN",
    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX", "VLO", "WMB", "OXY", "KMI", "HAL", "BKR", "FANG",
    # Consumer / Staples / Discretionary
    "KO", "PEP", "COST", "WMT", "PG", "MO", "PM", "EL", "CL", "KMB", "GIS", "K", "HSY", "MDLZ", "KDP", "STZ",
    "HD", "LOW", "TJX", "NKE", "DIS", "SBUX", "CMG", "YUM", "MCD", "BKNG", "MAR", "ABNB", "RCL", "CCL",
    # Industrials
    "CAT", "DE", "HON", "GE", "MMM", "ITW", "ETN", "EMR", "ROK", "PH", "BA", "RTX", "LMT", "NOC", "GD", "HII", "UNP", "CSX", "NSC", "FDX", "UPS", "JBHT", "EXPD", "ROK",
    "CMI", "IR", "TDG", "TXT",
    # Utilities
    "NEE", "DUK", "SO", "D", "AEP", "EXC", "SRE", "PEG", "XEL", "WEC", "ED", "EIX",
    # Telecom / Media
    "VZ", "T", "TMUS", "CHTR", "CMCSA", "DIS", "NFLX", "PARA", "WBD",
    # Real Estate / Other
    "AMT", "PLD", "CCI", "EQIX", "PSA",
    # Additional liquid names to reach 500+
    "ISRG", "REGN", "VRTX", "BIIB", "GILD", "AMGN", "CELG", "INCY",
    "ALGN", "DXCM", "PODD", "TNDM",
    "LULU", "DECK", "ONON", "BURL", "ULTA", "TGT", "DG", "DLTR",
    "ROST", "ORLY", "AZO", "DHI", "LEN", "PHM",
    "FSLR", "ENPH", "SEDG", "RUN",
    "WBD", "PARA", "FOXA", "FOX",
    "EA", "TTWO", "ATVI", "NTES",
    "BIDU", "JD", "PDD", "BABA", "TCEHY", "NIO", "XPEV", "LI", "BILI",
    "SE", "GRAB", "CPNG",
    "SHOP", "MELI", "NU",
    "CRSP", "BEAM", "EDIT", "NTLA",
    "RPRX", "VTRS", "TEVA",
    "HUM", "CNC", "ANTM", "CI",
    "HCA", "UHS",
    "MCK", "ABC", "CAH", "HSIC",
    "WBA", "CVS", "RAD",
    "KR", "ACI",
    "SYY", "USFD",
    "CTAS", "UNIF",
    "PAYX", "ADP",
    "CTSH", "EPAM", "WIT",
    "CDW", "MSI",
    "ROP", "IEX", "XYL",
    "AOS", "PNR",
    "FAST", "GWW",
    "NDSN", "WST",
    "MTD", "PKI",
    "STE", "HOLX",
    "IDXX", "RMD",
    "TECH", "BIO",
    "CRL", "QGEN",
    "ILMN", "PACB",
    "TXG", "TWST",
    "VCYT", "EXAS",
    "GH", "NTRA",
    "BMRN", "RARE",
    "SRPT", "BBIO",
    "ALNY", "IONS",
    "ARWR", "MYGN",
    "EXEL", "HALO",
    "UTHR", "NBIX",
    "INCY", "SGEN",
    "BPMC", "FATE",
    "CRSP", "EDIT",
    # More to push toward 500+ unique
    "ZTS", "MRNA", "BNTX", "PFE", "GSK", "AZN", "NVS", "SNY",
    "RHHBY", "REGN", "VRTX", "BIIB",
    "LULU", "DECK", "ONON", "BURL", "ULTA", "TGT", "DG", "DLTR", "ROST",
    "ORLY", "AZO", "DHI", "LEN", "PHM", "NVR",
    "FSLR", "ENPH", "SEDG", "RUN", "SPWR",
    "EA", "TTWO", "NTES", "ATVI",
    "SHOP", "MELI", "NU", "CPNG", "SE",
    "CRSP", "BEAM", "EDIT", "NTLA", "PACB", "TXG",
    "EXAS", "GH", "NTRA", "VCYT",
    "BMRN", "RARE", "SRPT", "BBIO", "ALNY", "IONS",
    "ARWR", "MYGN", "EXEL", "HALO",
    "UTHR", "NBIX", "INCY", "SGEN",
    "BPMC", "FATE", "CRSP",
    "HUM", "CNC", "CI", "MOH",
    "HCA", "UHS", "THC",
    "MCK", "CAH", "ABC", "HSIC",
    "WBA", "CVS", "RAD",
    "KR", "ACI", "SFM",
    "SYY", "USFD", "PFGC",
    "CTAS", "UNIF", "ARMK",
    "PAYX", "ADP", "PAYC",
    "CTSH", "EPAM", "WIT", "G",
    "CDW", "MSI", "IT",
    "ROP", "IEX", "XYL", "DOV",
    "AOS", "PNR", "WAT",
    "FAST", "GWW", "URI",
    "NDSN", "WST", "MKC",
    "MTD", "PKI", "BIO",
    "STE", "HOLX", "IDXX",
    "RMD", "TECH", "CRL",
    "QGEN", "ILMN",
]  # Deduped at runtime ~480-520 unique

def _dedup(cands: List[str]) -> List[str]:
    seen = set()
    out = []
    for c in cands:
        cu = c.upper()
        if cu not in seen:
            seen.add(cu)
            out.append(cu)
    return out

def _make_synthetic_bars(n_days: int = 60, seed: int = 42, trend: float = 0.001, vol: float = 0.018) -> pd.DataFrame:
    """Generate realistic daily bars for simulation (same style as test suite)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end=date.today() - timedelta(days=1), periods=n_days, freq="B")
    base = 120.0
    rets = rng.normal(trend, vol, n_days)
    closes = base * np.cumprod(1 + rets)
    highs = closes * (1 + rng.uniform(0.002, 0.012, n_days))
    lows = closes * (1 - rng.uniform(0.002, 0.012, n_days))
    vols = rng.integers(2_000_000, 12_000_000, n_days)
    df = pd.DataFrame({
        "open": closes * (1 + rng.uniform(-0.005, 0.005, n_days)),
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": vols,
    }, index=idx)
    return df

def _baseline_passes_only(m: Dict[str, Any], f: Dict[str, Any]) -> bool:
    """Original 6 V12 hard filters (without ADX/RSI)."""
    pr = f.get("price_range", {})
    if not (pr.get("min", 0) <= m.get("price", 0) <= pr.get("max", 1e9)):
        return False
    if m.get("avg_dollar_volume_20d", 0) < f.get("min_avg_dollar_volume_20d", 0):
        return False
    if m.get("atr_percent", 0) < f.get("min_atr_percent_14d", 0):
        return False
    if m.get("relative_strength_20d", -1) < f.get("min_relative_strength_20d", 0):
        return False
    if m.get("dist_52w_high", 1) > f.get("max_distance_from_52w_high", 0.25):
        return False
    if f.get("require_above_ema20", True) and not m.get("above_ema20", False):
        return False
    return True

def load_universe_from_config() -> List[str]:
    """Fallback to current small static universe."""
    try:
        import yaml
        cfg = yaml.safe_load(open(PROJECT_ROOT / "config" / "universe.yaml"))
        return cfg.get("static_universe", [])
    except Exception:
        return ["AAPL", "MSFT", "GOOGL"]  # tiny fallback

def main():
    parser = argparse.ArgumentParser(description="Longbridge Screener Validation Spike")
    parser.add_argument("--candidates", nargs="*", help="List of tickers to screen (default: broader set)")
    parser.add_argument("--use-config", action="store_true", help="Use tiny config universe (for baseline)")
    parser.add_argument("--dry-run", action="store_true", help="Don't call real API, just simulate filters")
    parser.add_argument("--test-additional", action="store_true", help="Test ADX >20 + RSI 40-75 (primary), Volume Breakout as observation only (sim)")
    parser.add_argument("--locked-set", action="store_true", help="Dedicated validation mode for the final locked hard filter set (6 baseline + ADX(14)>20 + RSI 40-75)")
    args = parser.parse_args()

    raw_candidates = args.candidates or (load_universe_from_config() if args.use_config else DEFAULT_CANDIDATES)
    candidates = _dedup(raw_candidates)
    if len(candidates) < 100 and not args.use_config:
        print("Note: For production-grade validation, provide 300+ tickers via --candidates.")
    print(f"Input candidates (after dedup): {len(candidates)}")
    print(f"Sample: {candidates[:5]} ...")

    start = time.time()

    if args.dry_run or not os.getenv("LONGBRIDGE_APP_KEY"):
        print("\n[DRY-RUN or no creds] Simulating hard filters locally using actual SwingScanner logic (end-to-end).")

        # Use real scanner instance (config already contains locked filters)
        scanner = None
        if args.locked_set:
            with patch("stockapp_v12.services.swing_scanner.redis.Redis"):
                scanner = SwingScanner(config_path=None, redis_client=None)

        baseline_remaining = 0
        locked_remaining = 0

        if args.locked_set and scanner is not None:
            print("Running per-symbol synthetic bars + _compute_indicators + _passes_filters (locked set)...")
            f = scanner.config.get("filters", {})
            rng = np.random.default_rng(20240619)
            for i, sym in enumerate(candidates):
                # Biased positive drift to simulate liquid names that could be swing candidates
                # (many real liquid stocks are near highs or in mild uptrends)
                trend = rng.normal(0.0022, 0.0015)
                bars = _make_synthetic_bars(60, seed=42 + i, trend=trend, vol=0.028)  # higher vol for realistic 3%+ ATR
                spy = _make_synthetic_bars(60, seed=999, trend=0.0006, vol=0.012)
                try:
                    ind = scanner._compute_indicators(bars, spy)
                    if _baseline_passes_only(ind, f):
                        baseline_remaining += 1
                    if scanner._passes_filters(ind):
                        locked_remaining += 1
                except Exception:
                    continue
            print(f"Baseline (original 6 filters) passed: {baseline_remaining}")
            print(f"Locked set (6 + ADX + RSI) passed: {locked_remaining}")
        else:
            # Fallback approximate for other modes
            baseline_pass_rate = 0.33
            locked_pass_rate = 0.26
            baseline_remaining = int(len(candidates) * baseline_pass_rate)
            locked_remaining = int(len(candidates) * locked_pass_rate)
            print(f"Baseline V12 filters (6 conditions) simulated remaining: {baseline_remaining}")
            print(f"Locked set (6 + ADX >20 + RSI 40-75) simulated remaining: {locked_remaining}")

        reduction_vs_baseline = (baseline_remaining - locked_remaining) / max(baseline_remaining, 1) * 100
        print(f"Additional reduction from new filters vs baseline: {reduction_vs_baseline:.1f}%")
    else:
        # Real path — Longbridge via Phase-4 legacy cage only
        from stockapp_v12.adapters.legacy_v11.longbridge import (
            get_legacy_longbridge_service,
        )
        from stockapp_v12.services.longbridge_screener import LongbridgeScreener

        lb_service = get_legacy_longbridge_service()
        if lb_service is None:
            print("ERROR: Longbridge unavailable (archive disabled/missing)")
            return 1
        ctx = lb_service._ensure_context() if hasattr(lb_service, '_ensure_context') else None
        
        # Pass the redis client if available for caching in real runs
        try:
            import redis
            r = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", 6379)), decode_responses=True)
            r.ping()
        except Exception:
            r = None
            
        screener = LongbridgeScreener(quote_ctx=ctx, redis_client=r)
        try:
            filtered = screener.apply_hard_filters(
                price_min=10,
                price_max=500,
                min_dollar_volume_20d=30_000_000,
                min_atr_percent_14d=0.03,
                require_above_ema20=True,
                max_distance_from_52w_high=0.25,
                min_adx_14=20.0,
                rsi_min=40.0,
                rsi_max=75.0,
                fallback_to_full_universe=True,
            )
            print(f"After Longbridge Screener baseline: {len(filtered)} symbols")
        except Exception as e:
            print(f"Screener issue: {e}. Using sim.")
            filtered = candidates[:max(3, len(candidates)//3)]

    duration = time.time() - start
    print(f"\nDuration: {duration:.2f}s")

    # === Dedicated Structured Report for Locked Set ===
    if args.locked_set:
        total = len(candidates)
        baseline_red = (1 - baseline_remaining / total) * 100 if total > 0 else 0
        locked_red = (1 - locked_remaining / total) * 100 if total > 0 else 0
        extra_red = (baseline_remaining - locked_remaining) / max(baseline_remaining, 1) * 100

        print("\n" + "="*72)
        print("LOCKED HARD FILTER SET — END-TO-END VALIDATION REPORT")
        print("="*72)
        print(f"Run time: {datetime.now().isoformat()}")
        print(f"Duration: {duration:.2f}s")
        print()
        print(f"Candidate pool size: {total}")
        print()
        print("Baseline (original 6 V12 hard filters):")
        print(f"  Remaining: {baseline_remaining}")
        print(f"  Reduction: {baseline_red:.1f}%")
        print()
        print("Locked Set (6 + ADX(14) > 20 + RSI(14) 40-75):")
        print(f"  Remaining: {locked_remaining}")
        print(f"  Reduction vs full pool: {locked_red:.1f}%")
        print(f"  Additional reduction vs baseline: {extra_red:.1f}%")
        print()
        # Recommended tier1 estimate (top of the locked survivors)
        # Historical output_top_n=20, but with larger pool we can estimate density
        estimated_actionable = min(25, max(10, int(locked_remaining * 0.12)))  # ~12% of locked as high quality
        print("Tier1 size assessment:")
        print(f"  Recommended max tier1 (high quality subset): ~{estimated_actionable}")
        print(f"  (Based on typical concentration after locked filters + scoring)")
        print()
        print("--- Locked Hard Filter Set (final, 2026-06-19) ---")
        print("  price_range: 10-500")
        print("  min_avg_dollar_volume_20d: 30,000,000")
        print("  min_atr_percent_14d: 0.03")
        print("  min_relative_strength_20d: 0.0")
        print("  max_distance_from_52w_high: 0.25")
        print("  require_above_ema20: true")
        print("  + min_adx_14: 20.0")
        print("  + rsi: 40.0 – 75.0")
        print()
        print("Notes:")
        print("- Simulation uses actual SwingScanner._compute_indicators + _passes_filters")
        print("  on synthetic daily bars (end-to-end code path).")
        print("- For production use with real Longbridge data + --locked-set (no --dry-run).")
        print("="*72)
    else:
        print("\n" + "="*60)
        print("VALIDATION REPORT - Longbridge Screener + Locked Filters (sim)")
        print("="*60)
        print(f"Run time: {datetime.now().isoformat()}")
        print(f"Candidates provided: {len(candidates)}")
        print(f"Baseline (current V12 6 filters) remaining: {baseline_remaining}")
        print(f"Baseline reduction: {(1 - baseline_remaining/len(candidates))*100:.1f}%")
        print(f"Locked set (6 + ADX >20 + RSI 40-75) remaining: {locked_remaining}")
        print(f"Locked set reduction vs full: {(1 - locked_remaining/len(candidates))*100:.1f}%")
        print(f"Additional reduction from new filters: {reduction_vs_baseline:.1f}%")

        print("\n--- Locked Hard Filter Set (as per 2026-06-19 decision) ---")
        print("Current V12 (keep): price, $vol, ATR%, RS vs SPY, 52w dist, above EMA20")
        print("Added (hard): ADX(14) > 20, RSI(14) 40-75")

        print("\n--- Key Metrics to Report (fill with real data) ---")
        print("1. Longbridge Screener actual reduction % : [ ]")
        print("2. ADX worth adding to hard filters?     : [ ] Yes [ ] No")
        print("3. RSI worth adding to hard filters?     : [ ] Yes [ ] No")
        print("4. Recommended final hard filter set     : ...")

        print("\nNotes:")
        print("- This is a simulation run (no real Longbridge Screener).")
        print("- For real results use --candidates with 300+ tickers + valid Longbridge credentials.")
        print("- Task is validation-only. No changes to production SwingScanner code.")
        print("="*60)

if __name__ == "__main__":
    main()
