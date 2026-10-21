#!/usr/bin/env python3
"""
shadow_ensemble.py — daily shadow harness for the A+B ensemble backend.

Why this exists (2026-07-01 council, Round 3 closure):
  The ensemble (EnsembleChronosBackend) is brand-new code. We need live
  evidence of how it behaves in real market conditions before promoting
  it to Tier 2 paper. Backtest is necessary but not sufficient — we
  need:
    - spread distribution (does 3% fallback threshold match reality?)
    - fallback hit rate (how often does the ensemble degrade to single?)
    - daily hit rate accumulation vs backtest baseline
    - per-model forecast divergence tracking

This script runs the same per-symbol predict path that Tier 2 paper would
trigger via lifecycle_scheduler, but in shadow mode (writes only, no
trades). It mirrors shadow_trade.py's API surface so existing tooling
(per-date JSONs, SUMMARY.json) just works.

Output structure:
  models/chronos/_shadow/ensemble/YYYY-MM-DD.json          — ensemble result
  models/chronos/_shadow/ensemble/per_model/{m}/YYYY-MM-DD.json — raw per-model
  models/chronos/_shadow/ensemble/SUMMARY.json             — accumulated stats
  models/chronos/_shadow/SUMMARY.json                      — cross-model (if --out)

Per-date JSON shape (ensemble):
  {
    "ensemble_name": "A_B_ensemble",
    "eval_date": "2026-07-02",
    "fallback_spread_pct": 3.0,
    "n_symbols": 10,
    "n_used_fallback": 1,  # how many symbols triggered single-model fallback
    "rows": [
      {
        "symbol": "AAPL",
        "current_close": 299.24,
        "forecast_mean": 300.43,
        "forecast_ret_pct": 0.398,
        "actual_mean": ...,            # filled once 10-day window completes
        "actual_ret_pct": ...,
        "mase": 1.97,
        "per_model_forecast_ret_pct": {"variant_a": 0.92, "variant_b": -0.13},
        "per_model_spread_pct": 1.05,
        "used_fallback": false,
        "fallback_reason": null,
      }
    ]
  }

Per-date JSON shape (per_model): identical to shadow_trade.py
  {"model": str, "eval_date": str, "rows": [...]}

SUMMARY.json shape (ensemble):
  {
    "ensemble_name": "A_B_ensemble",
    "today": "2026-07-02",
    "generated_at": "...",
    "current_run_aggregate": {...},
    "accumulated": {
      "n_dates": int,
      "date_range": [first, last],
      "aggregate": {
        "hit_rate": float,
        "signal_hit_rate": float,
        "simulated_pnl": {...},
        # NEW: spread / fallback stats
        "spread_pcts": [...],
        "spread_p50": float,
        "spread_p95": float,
        "fallback_count": int,
        "fallback_rate": float,
      },
      "n_dates_by_source": {"cron": int, "backfill": int},
    }
  }
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

# Reuse helpers from sibling scripts so we don't drift.
SCRIPT_DIR = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[1]  # = /Users/dev/Stockapp
SHADOW_ROOT = REPO / "models" / "chronos" / "_shadow"
ENSEMBLE_DIR_NAME = "ensemble"
DEFAULT_TEST_SYMBOLS = [
    "AAPL", "NVDA", "TSLA", "JPM", "MSFT", "AMZN", "AMD", "META", "GOOGL", "SPY"
]
FORECAST_HORIZON = 10
DEFAULT_FALLBACK_SPREAD_PCT = 3.0
ENSEMBLE_ENV_VAR = "CHRONOS_ENSEMBLE_MODEL_PATHS"
FALLBACK_SPREAD_ENV_VAR = "CHRONOS_ENSEMBLE_FALLBACK_SPREAD_PCT"

# Cron runs `python scripts/shadow_ensemble.py` → path[0] is scripts/, not
# repo root. Insert REPO so `from scripts.*` works even if PYTHONPATH is lean.
# (SCRIPT_DIR alone is NOT enough for the scripts package.)
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
_SRC = REPO / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
# B2 (post-launch refactor): pure helpers (metrics + predict + simulate_pnl)
# now live in `stockapp_v12.eval.*`. Only `load_model` stays in
# `scripts.eval_chronos_h2h_v3` (it's I/O glue, not a pure helper).
from scripts.eval_chronos_h2h_v3 import load_model
from stockapp_v12.eval import (

    DEFAULT_TEST_SYMBOLS,
    build_test_tsdf_full,
    compute_mase,
    hit_rate,
    naive_errors_from_actual,
    predict_one_symbol_for_date,
    signal_hit_rate,
    simulate_pnl,
)

# Use the eval package's symbol list as canonical default
DEFAULT_TEST_SYMBOLS = list(DEFAULT_TEST_SYMBOLS)


# ---------------------------------------------------------------------------
# Ensemble aggregation (the spread + fallback logic)
# ---------------------------------------------------------------------------


def aggregate_ensemble_row(
    per_model_rows: List[Optional[dict]],
    model_labels: List[str],
    fallback_spread_pct: float,
) -> Optional[dict]:
    """Combine per-model rows into one ensemble row.

    Mirrors the logic in EnsembleChronosBackend._build_signal but produces
    a per-symbol flat dict for shadow JSON output (not a signal list).

    Returns None if all models returned None for this symbol.
    """
    valid_rows = [r for r in per_model_rows if r is not None]
    if not valid_rows:
        return None

    # Per-model forecast_ret_pct (skip NaN)
    per_model_pcts: Dict[str, float] = {}
    for label, r in zip(model_labels, per_model_rows):
        if r is None or r.get("forecast_ret_pct") is None:
            continue
        per_model_pcts[label] = r["forecast_ret_pct"]

    if not per_model_pcts:
        return None

    pcts = list(per_model_pcts.values())
    spread = float(max(pcts) - min(pcts))
    used_fallback = spread > fallback_spread_pct
    if used_fallback:
        # First valid model wins (matches EnsembleChronosBackend contract)
        first_label = next(iter(per_model_pcts))
        first_row = next(r for r, lbl in zip(per_model_rows, model_labels)
                         if lbl == first_label and r is not None)
        chosen = {
            "forecast_mean": first_row["forecast_mean"],
            "forecast_ret_pct": first_row["forecast_ret_pct"],
            "mase": first_row.get("mase"),
            "actual_mean": first_row.get("actual_mean"),
            "actual_ret_pct": first_row.get("actual_ret_pct"),
            "current_close": first_row["current_close"],
        }
        fallback_reason = (
            f"per-model |forecast_ret_pct| spread {spread:.3f}% > "
            f"fallback_spread_pct {fallback_spread_pct:.3f}%; "
            f"using single-model {first_label}"
        )
    else:
        # Average across valid models
        means = [
            r["forecast_mean"] for r in per_model_rows
            if r is not None and r.get("forecast_mean") is not None
        ]
        mase_vals = [
            r["mase"] for r in per_model_rows
            if r is not None and r.get("mase") is not None
        ]
        actual_means = [
            r["actual_mean"] for r in per_model_rows
            if r is not None and r.get("actual_mean") is not None
        ]
        chosen = {
            "forecast_mean": round(float(np.mean(means)), 2),
            "mase": round(float(np.mean(mase_vals)), 4) if mase_vals else None,
            "actual_mean": round(float(np.mean(actual_means)), 2) if actual_means else None,
            "current_close": valid_rows[0]["current_close"],
        }
        if chosen["current_close"] > 0:
            chosen["forecast_ret_pct"] = round(
                (chosen["forecast_mean"] - chosen["current_close"])
                / chosen["current_close"] * 100.0, 3
            )
        else:
            chosen["forecast_ret_pct"] = None
        if chosen.get("actual_mean") is not None and chosen["current_close"] > 0:
            chosen["actual_ret_pct"] = round(
                (chosen["actual_mean"] - chosen["current_close"])
                / chosen["current_close"] * 100.0, 3
            )
        fallback_reason = None

    return {
        "symbol": valid_rows[0]["symbol"],
        "current_close": chosen["current_close"],
        "forecast_mean": chosen["forecast_mean"],
        "forecast_ret_pct": chosen.get("forecast_ret_pct"),
        "actual_mean": chosen.get("actual_mean"),
        "actual_ret_pct": chosen.get("actual_ret_pct"),
        "mase": chosen.get("mase"),
        "per_model_forecast_ret_pct": {
            k: round(v, 3) for k, v in per_model_pcts.items()
        },
        "per_model_spread_pct": round(spread, 3),
        "used_fallback": used_fallback,
        "fallback_reason": fallback_reason,
        "n_models_valid": len(per_model_pcts),
    }


# ---------------------------------------------------------------------------
# Aggregated SUMMARY stats (with new spread / fallback fields)
# ---------------------------------------------------------------------------


def aggregate_ensemble_per_date_jsons(
    ensemble_dir: Path, threshold: float = 0.5
) -> dict:
    """Read all YYYY-MM-DD.json in ensemble_dir, build accumulated stats.

    Adds new spread/fallback aggregates that shadow_trade doesn't have:
      - spread_pcts: per-symbol-per-day list of |max - min| forecast pcts
      - spread_p50, spread_p95: distribution percentiles
      - fallback_count: how many (symbol, day) tuples used fallback
      - fallback_rate: fallback_count / total_tuples
    """
    per_date_files = sorted(ensemble_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"))
    if not per_date_files:
        return {
            "n_dates": 0,
            "date_range": None,
            "aggregate": None,
            "n_dates_by_source": {"cron": 0, "backfill": 0},
            "n_rows_by_source": {"cron": 0, "backfill": 0},
        }

    all_rows: List[dict] = []
    n_dates_by_source = {"cron": 0, "backfill": 0}
    n_rows_by_source = {"cron": 0, "backfill": 0}
    for fp in per_date_files:
        # Files at the top level are cron data
        source = "cron" if fp.parent == ensemble_dir else "backfill"
        payload = json.loads(fp.read_text())
        rows = payload.get("rows", [])
        for r in rows:
            r = dict(r)
            r.setdefault("eval_date", payload.get("eval_date"))
            r["_source"] = source
            all_rows.append(r)
        n_dates_by_source[source] += 1
        n_rows_by_source[source] += len(rows)

    if not all_rows:
        return {
            "n_dates": len(per_date_files),
            "date_range": None,
            "aggregate": None,
            "n_dates_by_source": n_dates_by_source,
            "n_rows_by_source": n_rows_by_source,
        }

    # Standard aggregate (reuses shadow_trade's helpers)
    pairs = [
        (r["forecast_ret_pct"], r["actual_ret_pct"])
        for r in all_rows
        if r.get("forecast_ret_pct") is not None
        and r.get("actual_ret_pct") is not None
    ]
    f_pcts = [p[0] for p in pairs]
    a_pcts = [p[1] for p in pairs]
    mase_vals = [r["mase"] for r in all_rows if r.get("mase") is not None]

    # Spread distribution
    spread_pcts = [
        r["per_model_spread_pct"] for r in all_rows
        if r.get("per_model_spread_pct") is not None
    ]
    fallback_count = sum(1 for r in all_rows if r.get("used_fallback"))

    spread_stats = {
        "n": len(spread_pcts),
        "min": round(float(np.min(spread_pcts)), 3) if spread_pcts else None,
        "p50": round(float(np.percentile(spread_pcts, 50)), 3) if spread_pcts else None,
        "p95": round(float(np.percentile(spread_pcts, 95)), 3) if spread_pcts else None,
        "max": round(float(np.max(spread_pcts)), 3) if spread_pcts else None,
    }

    dates = sorted({r["eval_date"] for r in all_rows if r.get("eval_date")})
    return {
        "n_dates": len(per_date_files),
        "date_range": [dates[0], dates[-1]] if dates else None,
        "aggregate": {
            "n_rows": len(all_rows),
            "n_pairs": len(pairs),
            "hit_rate": hit_rate(pairs),
            "signal_hit_rate": signal_hit_rate(f_pcts, a_pcts, threshold=threshold),
            "mase_mean": round(float(np.mean(mase_vals)), 4) if mase_vals else None,
            "simulated_pnl": simulate_pnl(f_pcts, a_pcts, threshold=threshold),
            # NEW ensemble-specific fields
            "spread_pcts": spread_pcts,
            "spread_stats": spread_stats,
            "fallback_count": fallback_count,
            "fallback_rate": round(fallback_count / len(all_rows), 4) if all_rows else 0.0,
        },
        "n_dates_by_source": n_dates_by_source,
        "n_rows_by_source": n_rows_by_source,
    }


# ---------------------------------------------------------------------------
# Main: load models, run per-symbol, write artifacts
# ---------------------------------------------------------------------------


def run_shadow_ensemble(
    model_paths: List[Path],
    symbols: List[str],
    today: date,
    cutoff_days_ago: int = 1,
    fallback_spread_pct: float = DEFAULT_FALLBACK_SPREAD_PCT,
    threshold: float = 0.5,
    ensemble_dir_name: Optional[str] = None,
    ensemble_name: Optional[str] = None,
    trade_path: bool = True,
) -> dict:
    """Run ensemble for one (today, cutoff) tuple. Returns the result dict.

    Side effects:
      - Writes per-date JSON to SHADOW_ROOT/<ensemble_dir_name>/YYYY-MM-DD.json
      - Writes per-model JSON under per_model/{label}/
      - Refreshes SHADOW_ROOT/<ensemble_dir_name>/SUMMARY.json

    ADR-010 E-SEAT: pass ensemble_dir_name like ``ensemble_seat_e09`` and
    trade_path=False so paper decision path (``ensemble/``) is never touched.

    ``ensemble_dir_name`` defaults to the module global ENSEMBLE_DIR_NAME
    (looked up at call time so tests can monkeypatch it).
    """
    if ensemble_dir_name is None:
        ensemble_dir_name = ENSEMBLE_DIR_NAME
    # Safety: seat paths must not collide with the production decision dir.
    if not trade_path and ensemble_dir_name == "ensemble":
        raise ValueError(
            "trade_path=False requires ensemble_dir_name != 'ensemble' "
            "(would overwrite decision path)"
        )
    if ensemble_name is None:
        if str(ensemble_dir_name).startswith("ensemble_seat"):
            ensemble_name = f"seat_{ensemble_dir_name}"
        else:
            # Production "ensemble" and test monkeypatched dirs
            ensemble_name = "A_B_ensemble"
    # CRON PATH SEMANTICS — mirrors shadow_trade.py --predict-today:
    # - single-cutoff (cutoff_days_ago == 1, the default cron path):
    #     eval_date label = today (the decision date), NOT today - cutoff.
    #     The predict() call uses the legacy cutoff branch (eval_date=None)
    #     so parquet-tail slicing stays "drop last N rows" — matches
    #     shadow_trade.py line ~516.
    # - multi-cutoff walk-forward (cutoff_days_ago > 1):
    #     eval_date label = today - cutoff, AND predict() is called with
    #     eval_date set so build_test_tsdf_full slices data <= eval_date.
    # This change fixes the regression where today=2026-07-13 produced
    # ensemble/2026-07-12.json, causing HaltGate the next day to read the
    # wrong artifact and emit insufficient_trades noise for the ensemble row.
    if cutoff_days_ago == 1:
        eval_date_str = today.isoformat()
        predict_eval_date: Optional[str] = None
    else:
        eval_date_str = (today - timedelta(days=cutoff_days_ago)).isoformat()
        predict_eval_date = eval_date_str
    print(f"[ensemble] today={today.isoformat()}  eval_date={eval_date_str}  "
          f"cutoff={cutoff_days_ago}  n_models={len(model_paths)}  "
          f"fallback_spread_pct={fallback_spread_pct}", file=sys.stderr)

    # Load all models
    t0 = time.time()
    predictors: Dict[str, Any] = {}
    for p in model_paths:
        label = p.name
        if not p.exists():
            raise FileNotFoundError(f"model dir missing: {p}")
        print(f"  [load] {label} from {p}", file=sys.stderr)
        predictors[label] = load_model(p)
    print(f"[ensemble] all models loaded in {time.time() - t0:.1f}s", file=sys.stderr)

    # Per-symbol: collect per-model row + build ensemble row
    ensemble_rows: List[dict] = []
    per_model_per_date: Dict[str, List[dict]] = {label: [] for label in predictors}
    n_used_fallback = 0
    for sym in symbols:
        per_model_rows: List[Optional[dict]] = []
        for label, pred in predictors.items():
            r = predict_one_symbol_for_date(
                pred, sym, cutoff_days_ago=cutoff_days_ago,
                eval_date=predict_eval_date,
            )
            per_model_rows.append(r)
            if r is not None:
                r_eval = dict(r)
                r_eval["eval_date"] = eval_date_str
                per_model_per_date[label].append(r_eval)

        ens_row = aggregate_ensemble_row(
            per_model_rows, list(predictors.keys()), fallback_spread_pct,
        )
        if ens_row is not None:
            ens_row["eval_date"] = eval_date_str
            ensemble_rows.append(ens_row)
            if ens_row["used_fallback"]:
                n_used_fallback += 1

    # Write per-date ensemble JSON
    ensemble_dir = SHADOW_ROOT / ensemble_dir_name
    ensemble_dir.mkdir(parents=True, exist_ok=True)
    ensemble_json = {
        "ensemble_name": ensemble_name,
        "ensemble_models": list(predictors.keys()),
        "eval_date": eval_date_str,
        "today": today.isoformat(),
        "fallback_spread_pct": fallback_spread_pct,
        "n_symbols": len(symbols),
        "n_used_fallback": n_used_fallback,
        "rows": ensemble_rows,
        "trade_path": bool(trade_path),
        "ensemble_dir_name": ensemble_dir_name,
    }
    out_path = ensemble_dir / f"{eval_date_str}.json"
    out_path.write_text(json.dumps(ensemble_json, indent=2, default=str))
    print(f"[ensemble] wrote {out_path}", file=sys.stderr)

    # Write per-model per-date JSONs (mirror shadow_trade layout)
    for label, rows in per_model_per_date.items():
        model_dir = ensemble_dir / "per_model" / label
        model_dir.mkdir(parents=True, exist_ok=True)
        model_json = {
            "model": label,
            "eval_date": eval_date_str,
            "rows": rows,
        }
        (model_dir / f"{eval_date_str}.json").write_text(
            json.dumps(model_json, indent=2, default=str)
        )

    # Build current-run aggregate (this date only)
    pairs_today = [
        (r["forecast_ret_pct"], r["actual_ret_pct"])
        for r in ensemble_rows
        if r.get("forecast_ret_pct") is not None
        and r.get("actual_ret_pct") is not None
    ]
    f_today = [p[0] for p in pairs_today]
    a_today = [p[1] for p in pairs_today]
    mase_today = [r["mase"] for r in ensemble_rows if r.get("mase") is not None]
    current_aggregate = {
        "n_rows": len(ensemble_rows),
        "n_pairs": len(pairs_today),
        "hit_rate": hit_rate(pairs_today),
        "signal_hit_rate": signal_hit_rate(f_today, a_today, threshold=threshold),
        "mase_mean": round(float(np.mean(mase_today)), 4) if mase_today else None,
        "n_used_fallback": n_used_fallback,
        "fallback_rate": round(
            n_used_fallback / len(ensemble_rows), 4
        ) if ensemble_rows else 0.0,
    }

    # Accumulated across all per-date JSONs
    accumulated = aggregate_ensemble_per_date_jsons(ensemble_dir, threshold=threshold)

    summary = {
        "ensemble_name": ensemble_name,
        "ensemble_models": list(predictors.keys()),
        "today": today.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "fallback_spread_pct": fallback_spread_pct,
        "current_run_aggregate": current_aggregate,
        "accumulated": accumulated,
        "trade_path": bool(trade_path),
        "ensemble_dir_name": ensemble_dir_name,
    }
    summary_path = ensemble_dir / "SUMMARY.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str))
    print(f"[ensemble] wrote {summary_path}", file=sys.stderr)

    return summary


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--models", nargs="+", required=False, default=None,
        help="Model dir basenames or absolute paths. Default: read from "
             f"${ENSEMBLE_ENV_VAR} (colon-separated)."
    )
    p.add_argument(
        "--symbols", nargs="+", default=DEFAULT_TEST_SYMBOLS,
        help=f"Test symbols (default: {DEFAULT_TEST_SYMBOLS})"
    )
    p.add_argument(
        "--cutoff-days-ago", type=int, default=1,
        help="Days of held-out tail (default: 1, matches cron live mode)"
    )
    p.add_argument(
        "--fallback-spread-pct", type=float, default=None,
        help=f"Per-model spread threshold (default: ${FALLBACK_SPREAD_ENV_VAR} or 3.0)"
    )
    p.add_argument(
        "--threshold", type=float, default=0.5,
        help="Signal threshold for hit-rate / PnL aggregation"
    )
    p.add_argument(
        "--today", type=str, default=None,
        help="Override 'today' (YYYY-MM-DD) for reproducibility / manual backfills"
    )
    p.add_argument(
        "--out", type=Path, default=None,
        help="(Unused) cross-model SUMMARY.json path. Kept for shadow_trade compat."
    )
    p.add_argument(
        "--ensemble-dir-name", type=str, default=ENSEMBLE_DIR_NAME,
        help=(
            f"Subdir under _shadow/ for artifacts (default: {ENSEMBLE_DIR_NAME}). "
            "ADR-010 E-SEAT: use e.g. ensemble_seat_e09 so production "
            f"{ENSEMBLE_DIR_NAME}/ is never overwritten."
        ),
    )
    p.add_argument(
        "--ensemble-name", type=str, default=None,
        help="Label stored in JSON (default: A_B_ensemble or seat_<dir>)",
    )
    p.add_argument(
        "--seat", action="store_true",
        help=(
            "ADR-010 E-SEAT mode: force trade_path=False. Requires "
            "--ensemble-dir-name starting with ensemble_seat (or set it)."
        ),
    )
    args = p.parse_args()

    # Resolve model paths from env var or CLI
    if args.models:
        model_paths = [Path(m) for m in args.models]
    else:
        env = os.getenv(ENSEMBLE_ENV_VAR, "").strip()
        if not env:
            print(
                f"ERROR: --models not provided and ${ENSEMBLE_ENV_VAR} unset/empty. "
                f"Either pass --models path1 path2 ... or set the env var.",
                file=sys.stderr,
            )
            sys.exit(2)
        model_paths = [Path(p) for p in env.split(":") if p.strip()]
    if not model_paths:
        print("ERROR: no model paths resolved", file=sys.stderr)
        sys.exit(2)

    # Relative basenames → models/chronos/<name>
    chronos_root = REPO / "models" / "chronos"
    resolved_paths: List[Path] = []
    for mp in model_paths:
        if mp.is_absolute() or mp.exists():
            resolved_paths.append(mp)
        else:
            resolved_paths.append(chronos_root / mp)
    model_paths = resolved_paths

    # Resolve fallback spread pct
    if args.fallback_spread_pct is not None:
        spread = float(args.fallback_spread_pct)
    else:
        env = os.getenv(FALLBACK_SPREAD_ENV_VAR, "").strip()
        try:
            spread = float(env) if env else DEFAULT_FALLBACK_SPREAD_PCT
        except (TypeError, ValueError):
            spread = DEFAULT_FALLBACK_SPREAD_PCT

    # Resolve today
    if args.today:
        today = datetime.strptime(args.today, "%Y-%m-%d").date()
    else:
        today = date.today()

    ensemble_dir_name = args.ensemble_dir_name
    trade_path = True
    if args.seat:
        trade_path = False
        if ensemble_dir_name == ENSEMBLE_DIR_NAME:
            # Default seat dir from second model name if possible
            b_name = model_paths[-1].name if model_paths else "seat"
            ensemble_dir_name = f"ensemble_seat_{b_name}"
        if not ensemble_dir_name.startswith("ensemble_seat"):
            print(
                f"ERROR: --seat requires --ensemble-dir-name starting with "
                f"'ensemble_seat' (got {ensemble_dir_name!r})",
                file=sys.stderr,
            )
            sys.exit(2)

    summary = run_shadow_ensemble(
        model_paths=model_paths,
        symbols=args.symbols,
        today=today,
        cutoff_days_ago=args.cutoff_days_ago,
        fallback_spread_pct=spread,
        threshold=args.threshold,
        ensemble_dir_name=ensemble_dir_name,
        ensemble_name=args.ensemble_name,
        trade_path=trade_path,
    )

    # Print a compact one-line summary for cron logs
    agg = summary["accumulated"]["aggregate"] or {}
    spread_stats = agg.get("spread_stats", {})
    print(
        f"\n[ensemble_shadow] date={summary['today']} "
        f"n_dates={summary['accumulated']['n_dates']} "
        f"n_rows_today={summary['current_run_aggregate']['n_rows']} "
        f"fallback_today={summary['current_run_aggregate']['n_used_fallback']} "
        f"hit_rate_accum={agg.get('hit_rate')} "
        f"spread_p95={spread_stats.get('p95')} "
        f"fallback_rate_accum={agg.get('fallback_rate')}"
    )


if __name__ == "__main__":
    main()