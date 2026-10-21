#!/usr/bin/env python3
"""
eval_chronos_h2h_v3.py — head-to-head eval of phase2_v3 checkpoints vs phase2_v2.

For each candidate model + cutoff, on each test symbol:
  1. Build a TimeSeriesDataFrame with the last `cutoff_days_ago` days dropped
  2. Predict next 10 days
  3. Compare forecast vs actual, compute:
     - forecast_ret_pct    (signal magnitude, % vs current close)
     - mase                (proper held-out error scaled by naive)

Then aggregate per (model, cutoff) and rank by MASE.

This is the rigorous H2H eval of the 1M-step chunked retrain. The output is a
report that tells us whether 950k (peak), 940k, or 1000k (final) is the right
checkpoint to promote — and how each compares to phase2_v2 (the current live).

Usage:
  # Run on the GPU box where the checkpoints live:
  PYTHONPATH=src:scripts:. python3 scripts/eval_chronos_h2h_v3.py \\
      --models phase2_v2 phase2_v3_ckpt_940k phase2_v3_ckpt_950k phase2_v3_ckpt_1000k \\
      --cutoffs 10 20 30 \\
      --out models/chronos/h2h_v3_report.json
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=UserWarning)

# ---------------------------------------------------------------------------
# Pure functions moved to `stockapp_v12.eval.*` (B2 refactor). This file
# now keeps only CLI glue + GPU gating + ranking aggregation. Tests in
# `v12/tests/test_eval_chronos_h2h_v3.py` still import from this module
# (re-exports below) for backward compatibility — see module docstring.
# ---------------------------------------------------------------------------

# B2 (post-launch refactor): constants + pure metrics moved to
# `stockapp_v12.eval.*` — keep imports for backward-compat with shadow scripts
# that still reference `scripts.eval_chronos_h2h_v3` constants.
from stockapp_v12.eval.constants import DEFAULT_TEST_SYMBOLS, SMOKE_CUTOFFS, SMOKE_SYMBOLS
from stockapp_v12.eval.metrics import (
    compute_mase,
    hit_rate,
    naive_errors_from_actual,
    rank_models,
    sign,
    sign_match,
    signal_hit_rate,
    summarize_forecasts,
)
from stockapp_v12.eval.test_data import FEATURES_DIR, REPO, build_test_tsdf_full
from stockapp_v12.eval.backtest_pnl import simulate_pnl  # B2: re-export for back-compat
from stockapp_v12.eval.predict import predict_one_symbol_for_date  # B2: re-export for back-compat
from stockapp_v12.eval.metrics import SIGN_EPSILON  # B2: re-export for back-compat


def is_gpu_available() -> bool:
    """True iff torch can import and CUDA reports ≥1 device.

    Defensive: returns False on any import / runtime error so the smoke
    gate never crashes the caller — it's a hint, not a contract.
    """
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def resolve_smoke_args(args) -> dict:
    """Pick (symbols, cutoffs) for this run. Pure function — no I/O.

    Priority order:
      1. `--smoke` flag set → use SMOKE_SYMBOLS + SMOKE_CUTOFFS (3 sym × 1 cutoff)
      2. user explicitly passed --symbols / --cutoffs → use those
      3. neither → use DEFAULT_TEST_SYMBOLS + [10] (argparse default)

    Returns:
      dict with keys: symbols (list[str]), cutoffs (list[int]),
                       source ("smoke" | "explicit" | "default")
    """
    smoke = getattr(args, "smoke", False)
    user_symbols = getattr(args, "symbols", None)
    user_cutoffs = getattr(args, "cutoffs", None)
    if smoke:
        return {"symbols": list(SMOKE_SYMBOLS),
                "cutoffs": list(SMOKE_CUTOFFS),
                "source": "smoke"}
    if user_symbols is not None and user_cutoffs is not None:
        return {"symbols": list(user_symbols),
                "cutoffs": list(user_cutoffs),
                "source": "explicit"}
    return {"symbols": list(DEFAULT_TEST_SYMBOLS),
            "cutoffs": list(user_cutoffs) if user_cutoffs is not None else [10],
            "source": "default"}


def load_model(model_dir: Path):
    """Load a TimeSeriesPredictor with FileBasedPredictionCache patched out.

    The cache was originally written to /home/trainer/... on the GPU box, so we
    no-op its `.put` method to avoid file-not-found errors when reloading on
    a different machine.
    """
    from autogluon.timeseries import TimeSeriesPredictor
    from autogluon.timeseries.trainer.prediction_cache import FileBasedPredictionCache

    FileBasedPredictionCache.put = lambda self, *a, **kw: None
    return TimeSeriesPredictor.load(str(model_dir))


def evaluate_one_model(
    predictor, model_name: str, symbols: list[str], cutoff_days_ago: int
) -> dict:
    """For one model, predict each symbol's next 10 days, compare to held-out.

    Returns dict with:
      - per_symbol_rows: list of {symbol, current_close, forecast_mean,
            forecast_ret_pct, actual_mean, mase, n_horizons_evaluated}
      - aggregate: result of summarize_forecasts on the per-symbol rows
    """
    from autogluon.timeseries import TimeSeriesDataFrame

    per_symbol = []
    for sym in symbols:
        try:
            df, actual_close = build_test_tsdf_full(sym, cutoff_days_ago=cutoff_days_ago)
        except FileNotFoundError as e:
            print(f"  [{model_name}] {sym}: skip ({e})", file=sys.stderr)
            continue

        if len(df) < 252:
            print(f"  [{model_name}] {sym}: skip (only {len(df)} rows)", file=sys.stderr)
            continue

        tsdf = TimeSeriesDataFrame.from_data_frame(
            df, id_column="symbol", timestamp_column="timestamp"
        )
        current_close = float(df["close"].iloc[-1])
        try:
            pred = predictor.predict(tsdf)
        except ValueError as e:
            # Model was trained on enriched features (sector_*, spy_*, ...)
            # that are missing from this symbol's parquet (e.g. META after
            # the 2022-01-29..2022-06-08 gap backfill re-populated OHLCV
            # but didn't re-run feature engineering). Skip this symbol
            # rather than fail the entire eval.
            if "missing from data" in str(e):
                print(f"  [{model_name}] {sym}: skip (model expects features absent in parquet: {e})", file=sys.stderr)
                continue
            raise
        # pred has MultiIndex (item_id, timestamp); first 10 horizons = forecast
        try:
            forecast_mean = float(pred["mean"].iloc[:10].mean())
        except Exception:
            forecast_mean = float(pred.iloc[:10].mean().mean())
        forecast_ret_pct = (forecast_mean - current_close) / current_close * 100

        # MASE: only meaningful if we have a held-out window
        if cutoff_days_ago > 0 and len(actual_close) > 0:
            # forecast horizon vs actual — use first 10 actuals (or fewer)
            n_h = min(10, len(actual_close))
            actual_first10 = actual_close.iloc[:n_h].tolist()
            naive = naive_errors_from_actual([current_close] + actual_first10)
            try:
                # "predicted" for the 10 horizons = forecast_mean (single number
                # for all 10 horizons from the current close)
                predicted = [forecast_mean] * n_h
                mase_val = compute_mase(actual_first10, predicted, naive)
            except ValueError as e:
                mase_val = None
                print(f"  [{model_name}] {sym}: MASE skipped ({e})", file=sys.stderr)
            actual_mean = float(np.mean(actual_first10))
        else:
            mase_val = None
            actual_mean = None
            n_h = 0

        per_symbol.append({
            "symbol": sym,
            "current_close": round(current_close, 2),
            "forecast_mean": round(forecast_mean, 2),
            "forecast_ret_pct": round(forecast_ret_pct, 3),
            "actual_mean": round(actual_mean, 2) if actual_mean is not None else None,
            "actual_ret_pct": round((actual_mean - current_close) / current_close * 100, 3)
                if (actual_mean is not None and current_close) else None,
            "mase": round(mase_val, 4) if mase_val is not None else None,
            "n_horizons_evaluated": n_h,
        })

    return {
        "model_name": model_name,
        "cutoff_days_ago": cutoff_days_ago,
        "per_symbol_rows": per_symbol,
        "aggregate": summarize_forecasts(per_symbol, key="forecast_ret_pct"),
    }


def _directional_metrics_from_rows(rows: list[dict], threshold: float = 0.5) -> dict:
    """Compute hit_rate and signal_hit_rate from per-symbol rows.

    Rows must include `forecast_ret_pct` and `actual_ret_pct` (None entries
    are filtered out so cutoffs without held-out actuals are skipped).
    """
    pairs = [
        (r["forecast_ret_pct"], r["actual_ret_pct"])
        for r in rows
        if r.get("forecast_ret_pct") is not None and r.get("actual_ret_pct") is not None
    ]
    f_pcts = [p[0] for p in pairs]
    a_pcts = [p[1] for p in pairs]
    return {
        "hit_rate": hit_rate(pairs),
        "signal_hit_rate": signal_hit_rate(f_pcts, a_pcts, threshold=threshold),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--models", nargs="+", required=True,
        help="Model dir basenames under models/chronos/ (e.g. phase2_v2 phase2_v3_ckpt_950k)",
    )
    parser.add_argument(
        "--symbols", nargs="+", default=DEFAULT_TEST_SYMBOLS,
        help=f"Test symbols (default: {DEFAULT_TEST_SYMBOLS})",
    )
    parser.add_argument(
        "--cutoffs", nargs="+", type=int, default=[10],
        help="Days held out (default: 10). Can pass multiple for robustness.",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Output report path (default: <repo>/models/chronos/h2h_v3_report.json)",
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help=(
            "Stage 1.1 smoke variant: use 3 fast-loading symbols + 1 cutoff. "
            "Designed for PR-gate CI; <5 min on GPU, errors immediately "
            "without GPU (so the smoke variant never silently degrades on "
            "a CPU-only CI runner)."
        ),
    )
    args = parser.parse_args()
    if args.out is None:
        args.out = REPO / "models" / "chronos" / "h2h_v3_report.json"

    # Stage 1.1 CI gate: --smoke requires GPU. Local repro without GPU
    # should still pass pytest; the full eval stays opt-in.
    if args.smoke and not is_gpu_available():
        print(
            "ERROR: --smoke requires GPU (torch.cuda.is_available() == False). "
            "Either run on the GPU box or omit --smoke and use the explicit "
            "--models flag with a smaller symbol list for local validation.",
            file=sys.stderr,
        )
        sys.exit(3)

    # Apply the smoke/default/explicit resolution for symbols + cutoffs.
    resolved = resolve_smoke_args(args)
    args.symbols = resolved["symbols"]
    args.cutoffs = resolved["cutoffs"]
    print(
        f"[config] symbols={args.symbols} cutoffs={args.cutoffs} source={resolved['source']}",
        file=sys.stderr,
    )

    chronos_root = REPO / "models" / "chronos"
    predictors: dict[str, object] = {}
    for m in args.models:
        pdir = chronos_root / m
        if not pdir.exists():
            print(f"ERROR: {pdir} does not exist", file=sys.stderr)
            sys.exit(2)
        print(f"[load] {m} from {pdir}", file=sys.stderr)
        predictors[m] = load_model(pdir)

    report = {
        "models": args.models,
        "symbols": args.symbols,
        "cutoffs": args.cutoffs,
        "by_cutoff": {},
    }
    for cutoff in args.cutoffs:
        print(f"\n=== cutoff = {cutoff} days ===", file=sys.stderr)
        per_model_results: dict[str, dict] = {}
        per_model_rows: dict[str, list[dict]] = {}
        for name, predictor in predictors.items():
            print(f"[eval] {name} @ cutoff={cutoff}d on {len(args.symbols)} syms", file=sys.stderr)
            res = evaluate_one_model(predictor, name, args.symbols, cutoff)
            dirm = _directional_metrics_from_rows(res["per_symbol_rows"], threshold=0.5)
            per_model_results[name] = {
                "mase": _mean_metric(res["per_symbol_rows"], "mase"),
                "signal_agg": res["aggregate"],
                "directional": dirm,
            }
            per_model_rows[name] = res["per_symbol_rows"]
        report["by_cutoff"][str(cutoff)] = {
            "per_model": per_model_results,
            "per_model_rows": per_model_rows,
            "ranking_by_mase": rank_models(per_model_results, "mase", lower_is_better=True),
        }

    # Cross-cutoff mean MASE summary
    cross = {}
    for name in args.models:
        mase_vals = [
            report["by_cutoff"][str(c)]["per_model"][name]["mase"]
            for c in args.cutoffs
            if report["by_cutoff"][str(c)]["per_model"][name]["mase"] is not None
        ]
        cross[name] = {
            "mean_mase_across_cutoffs": round(float(np.mean(mase_vals)), 4) if mase_vals else None,
            "n_cutoffs_evaluated": len(mase_vals),
        }
    report["cross_cutoff_summary"] = cross
    report["ranking_by_mean_mase"] = rank_models(cross, "mean_mase_across_cutoffs", lower_is_better=True)

    # Cross-cutoff hit-rate summary (NEW per Stage 1.2 — primary trading-edge metric)
    cross_hr = {}
    for name in args.models:
        hr_vals, shr_vals = [], []
        for c in args.cutoffs:
            dirm = report["by_cutoff"][str(c)]["per_model"][name]["directional"]
            if dirm["hit_rate"]["hit_rate"] is not None:
                hr_vals.append(dirm["hit_rate"]["hit_rate"])
            if dirm["signal_hit_rate"]["hit_rate"] is not None:
                shr_vals.append(dirm["signal_hit_rate"]["hit_rate"])
        cross_hr[name] = {
            "mean_hit_rate_across_cutoffs": round(float(np.mean(hr_vals)), 4) if hr_vals else None,
            "mean_signal_hit_rate_across_cutoffs": round(float(np.mean(shr_vals)), 4) if shr_vals else None,
            "n_cutoffs_evaluated": len(hr_vals),
        }
    report["cross_cutoff_hit_rate"] = cross_hr
    report["ranking_by_mean_hit_rate"] = rank_models(
        cross_hr, "mean_hit_rate_across_cutoffs", lower_is_better=False
    )
    report["ranking_by_mean_signal_hit_rate"] = rank_models(
        cross_hr, "mean_signal_hit_rate_across_cutoffs", lower_is_better=False
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    print(f"\n[write] {args.out}", file=sys.stderr)
    # Print final ranking — MASE (magnitude) and Hit Rate (direction) side by side
    print("\n=== H2H WINNER (by mean MASE — lower is better) ===")
    for i, name in enumerate(report["ranking_by_mean_mase"], 1):
        c = cross[name]
        print(f"  #{i} {name}: mean_mase={c['mean_mase_across_cutoffs']}  ({c['n_cutoffs_evaluated']} cutoffs)")
    print("\n=== H2H WINNER (by mean hit_rate — higher is better, Stage-1 gate ≥ 0.53) ===")
    for i, name in enumerate(report["ranking_by_mean_hit_rate"], 1):
        c = cross_hr[name]
        print(f"  #{i} {name}: mean_hit_rate={c['mean_hit_rate_across_cutoffs']}  signal_hit_rate={c['mean_signal_hit_rate_across_cutoffs']}  ({c['n_cutoffs_evaluated']} cutoffs)")


def _mean_metric(rows: list[dict], key: str) -> Optional[float]:
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return None
    return round(float(np.mean(vals)), 4)


if __name__ == "__main__":
    main()
