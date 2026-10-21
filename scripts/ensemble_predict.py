"""
Ensemble inference — average forecast_mean across N Chronos checkpoints.

Background (2026-07-01 council, Round 3 closure):
  Variant A (ctx=512, 5k steps, LR 1e-5, cold-start) and Variant B (ctx=1024,
  1k steps, LR 1e-4, phase2 v2 repro) are both fine-tunes of the same frozen
  chronos-bolt-small backbone. They diverge only in the recipe, so an
  arithmetic mean of forecast_mean should reduce per-symbol noise without
  introducing a different point estimate (a median is also acceptable — we
  use mean by default since the council backtest of Stage-1 v2 ran on mean).

  This script:
  1. Loads each model dir via eval_chronos_h2h_v3.load_model (shared).
  2. Runs the same build_test_tsdf_full slice per symbol (shared).
  3. Calls predictor.predict() per (model, symbol) — same try/except skip
     pattern as eval_chronos_h2h_v3.evaluate_one_model for missing feature
     columns (META-style skip rather than crash).
  4. Averages forecast_mean / Q10 / Q90 across models per (symbol, cutoff).
  5. Writes a report in the same shape as eval_chronos_h2h_v3's
     per_model_rows: {symbol, current_close, forecast_mean, forecast_q10,
     forecast_q90, forecast_ret_pct, actual_mean, actual_ret_pct, mase}.

  The output is compatible with the existing
  `validate_regime_gate.py` script — just point it at the ensemble
  report instead of the per-seed report.

Hardware: GPU-only (AutoGluon Chronos on RTX 3060). On Mac, only the
data-loading and report-formatting code paths are importable; the
inference call requires torch+CUDA.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

# Reuse load_model and build_test_tsdf_full from the eval script so the
# data-slice semantics stay bit-exact across single-model eval and ensemble.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from eval_chronos_h2h_v3 import (  # noqa: E402
    build_test_tsdf_full,
    compute_mase,
    load_model,
    naive_errors_from_actual,
)


REPO = Path(__file__).resolve().parents[1]


def _avg(values: list[float]) -> Optional[float]:
    clean = [v for v in values if v is not None and not (isinstance(v, float) and np.isnan(v))]
    if not clean:
        return None
    return float(np.mean(clean))


def _predict_one(predictor, model_name: str, symbol: str, cutoff_days_ago: int,
                 eval_date: Optional[str] = None) -> Optional[dict]:
    """Run one model on one symbol, return a row dict or None on skip."""
    from autogluon.timeseries import TimeSeriesDataFrame

    try:
        df, actual_close = build_test_tsdf_full(
            symbol, cutoff_days_ago=cutoff_days_ago, eval_date=eval_date
        )
    except FileNotFoundError as e:
        print(f"  [{model_name}] {symbol}: skip ({e})", file=sys.stderr)
        return None

    if len(df) < 252:
        print(f"  [{model_name}] {symbol}: skip (only {len(df)} rows)", file=sys.stderr)
        return None

    tsdf = TimeSeriesDataFrame.from_data_frame(
        df, id_column="symbol", timestamp_column="timestamp"
    )
    current_close = float(df["close"].iloc[-1])
    try:
        pred = predictor.predict(tsdf)
    except ValueError as e:
        # Model trained on enriched features that are missing from this
        # symbol's parquet — same skip pattern as eval_chronos_h2h_v3.
        if "missing from data" in str(e):
            print(
                f"  [{model_name}] {symbol}: skip (model expects features absent in parquet: {e})",
                file=sys.stderr,
            )
            return None
        raise

    try:
        forecast_mean = float(pred["mean"].iloc[:10].mean())
        forecast_q10 = float(pred["0.1"].iloc[:10].mean()) if "0.1" in pred.columns else None
        forecast_q90 = float(pred["0.9"].iloc[:10].mean()) if "0.9" in pred.columns else None
    except Exception:
        forecast_mean = float(pred.iloc[:10].mean().mean())
        forecast_q10 = None
        forecast_q90 = None

    mase_val = None
    actual_mean = None
    n_h = 0
    if cutoff_days_ago > 0 and len(actual_close) > 0:
        n_h = min(10, len(actual_close))
        actual_first10 = actual_close.iloc[:n_h].tolist()
        naive = naive_errors_from_actual([current_close] + actual_first10)
        try:
            predicted = [forecast_mean] * n_h
            mase_val = compute_mase(actual_first10, predicted, naive)
        except ValueError:
            mase_val = None
        actual_mean = float(np.mean(actual_first10))

    forecast_ret_pct = (forecast_mean - current_close) / current_close * 100.0
    actual_ret_pct = (
        (actual_mean - current_close) / current_close * 100.0
        if actual_mean is not None and current_close else None
    )
    return {
        "symbol": symbol,
        "current_close": round(current_close, 2),
        "forecast_mean": round(forecast_mean, 2),
        "forecast_q10": round(forecast_q10, 4) if forecast_q10 is not None else None,
        "forecast_q90": round(forecast_q90, 4) if forecast_q90 is not None else None,
        "forecast_ret_pct": round(forecast_ret_pct, 3),
        "actual_mean": round(actual_mean, 2) if actual_mean is not None else None,
        "actual_ret_pct": round(actual_ret_pct, 3) if actual_ret_pct is not None else None,
        "mase": round(mase_val, 4) if mase_val is not None else None,
        "n_horizons_evaluated": n_h,
    }


def ensemble_run(
    model_dirs: list[Path],
    symbols: list[str],
    cutoff_days_ago: int,
    eval_date: Optional[str] = None,
    ensemble_name: str = "ensemble",
) -> dict:
    """Run N models per symbol, return averaged per-symbol rows + metadata."""
    print(f"[ensemble] loading {len(model_dirs)} models: {[m.name for m in model_dirs]}")
    predictors = {}
    t0 = time.time()
    for m in model_dirs:
        if not m.exists():
            raise FileNotFoundError(f"model dir missing: {m}")
        print(f"  [load] {m.name} from {m}", file=sys.stderr)
        predictors[m.name] = load_model(m)
    print(f"[ensemble] all models loaded in {time.time() - t0:.1f}s")

    # Per-symbol: list of per-model row dicts
    per_symbol_collected: dict[str, list[dict]] = {s: [] for s in symbols}
    for sym in symbols:
        for m_name, pred in predictors.items():
            row = _predict_one(pred, m_name, sym, cutoff_days_ago, eval_date=eval_date)
            if row is not None:
                per_symbol_collected[sym].append(row)

    # Average across models per symbol
    ensemble_rows: list[dict] = []
    for sym in symbols:
        rows = per_symbol_collected[sym]
        if not rows:
            print(f"  [ensemble] {sym}: skipped by all models", file=sys.stderr)
            continue
        # Use the first row's non-prediction metadata as the base
        base = dict(rows[0])
        # Aggregate predictions
        forecast_means = [r["forecast_mean"] for r in rows]
        forecast_q10s = [r["forecast_q10"] for r in rows if r["forecast_q10"] is not None]
        forecast_q90s = [r["forecast_q90"] for r in rows if r["forecast_q90"] is not None]
        base["forecast_mean"] = round(_avg(forecast_means), 2)
        base["forecast_q10"] = round(_avg(forecast_q10s), 4) if forecast_q10s else None
        base["forecast_q90"] = round(_avg(forecast_q90s), 4) if forecast_q90s else None
        if base["current_close"]:
            base["forecast_ret_pct"] = round(
                (base["forecast_mean"] - base["current_close"]) / base["current_close"] * 100.0, 3
            )
        # average MASE if all models have it
        mases = [r["mase"] for r in rows if r["mase"] is not None]
        if mases and len(mases) == len(rows):
            base["mase"] = round(float(np.mean(mases)), 4)
        # per-model forecast_ret_pct for transparency
        base["per_model_forecast_ret_pct"] = {
            r["symbol"] + "_via_" + str(i): r["forecast_ret_pct"]
            for i, r in enumerate(rows)
        }
        ensemble_rows.append(base)

    # Build the report in the same shape as eval_chronos_h2h_v3
    # `by_cutoff[ck].per_model_rows[<name>]` is what validate_regime_gate
    # reads. The "model name" here is the ensemble label.
    report = {
        "ensemble_name": ensemble_name,
        "ensemble_models": [m.name for m in model_dirs],
        "symbols": symbols,
        "cutoffs": [cutoff_days_ago],
        "by_cutoff": {
            str(cutoff_days_ago): {
                "per_model_rows": {ensemble_name: ensemble_rows},
            }
        },
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--models", nargs="+", required=True,
        help="Model dir paths (absolute or relative to repo). E.g. "
             "models/chronos/round3_E05_ctx512_p3_coldstart_seed7 models/chronos/round3_phase2_v2_repro_seed7",
    )
    parser.add_argument(
        "--symbols", nargs="+",
        default=["AAPL", "NVDA", "TSLA", "JPM", "MSFT", "AMZN", "AMD", "META", "GOOGL", "SPY"],
        help="Test symbols (default: 10-symbol full universe)",
    )
    parser.add_argument(
        "--cutoff-days-ago", type=int, default=10,
        help="Days of held-out tail (default: 10)",
    )
    parser.add_argument(
        "--eval-date", default=None,
        help="YYYY-MM-DD — if set, use true walk-forward slicing instead of the cron-cutoff path",
    )
    parser.add_argument(
        "--ensemble-name", default="A_B_ensemble",
        help="Label for the averaged model in the output report",
    )
    parser.add_argument(
        "--out", required=True,
        help="Output report path (.json)",
    )
    args = parser.parse_args()

    model_dirs = []
    for m in args.models:
        p = Path(m)
        if not p.is_absolute():
            p = REPO / m
        model_dirs.append(p)

    report = ensemble_run(
        model_dirs=model_dirs,
        symbols=args.symbols,
        cutoff_days_ago=args.cutoff_days_ago,
        eval_date=args.eval_date,
        ensemble_name=args.ensemble_name,
    )
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = REPO / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"[ensemble] wrote {out_path}")


if __name__ == "__main__":
    main()