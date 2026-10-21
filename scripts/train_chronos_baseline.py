#!/usr/bin/env python3
"""
Chronos Baseline Training Script

Trains an AutoGluon TimeSeriesPredictor with a Chronos model on Phase 1 daily
data. Saves to models/chronos/baseline_v1/ which is loaded by
CHRONOS_MODEL_PATH for downstream backtest validation.

Two modes:
  --mode zeroshot   : use pretrained Chronos-Bolt zero-shot (no training, fast)
  --mode finetune   : fine-tune on Phase 1 data (slow on CPU, real baseline)

Usage:
  # Zero-shot first (sanity check, ~2 min)
  PYTHONPATH=src:scripts:. python scripts/train_chronos_baseline.py --mode zeroshot

  # Fine-tune (CPU, ~15-30 min for 50 symbols × 1000 steps small model)
  PYTHONPATH=src:scripts:. python scripts/train_chronos_baseline.py --mode finetune \
      --steps 200 --batch-size 16

Outputs:
  models/chronos/baseline_v1/
    └── Chronos[autogluon__chronos-bolt-small]/
        ├── predictor.pkl / model directories
  models/chronos/baseline_v1/train_meta.json
  models/chronos/baseline_v1/training_log.csv
  models/chronos/baseline_v1/sample_forecasts.png
  models/chronos/baseline_v1/baseline_report.md

Note: LoRA on raw chronos models is a separate code path (chronos package +
PEFT). AutoGluon's ChronosModel supports its own fine-tuning via
fine_tune=True; this script targets that path for v1.
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FEATURES_DIR = PROJECT_ROOT / "data" / "features" / "daily"
DEFAULT_OUT = PROJECT_ROOT / "models" / "chronos" / "baseline_v1"


def load_features(symbols: Optional[List[str]] = None) -> pd.DataFrame:
    """Load daily features and convert to AutoGluon TimeSeriesDataFrame format."""
    syms = symbols or sorted(p.stem for p in FEATURES_DIR.glob("*.parquet"))
    print(f"Loading features for {len(syms)} symbols...")
    dfs = []
    for sym in syms:
        path = FEATURES_DIR / f"{sym}.parquet"
        if not path.exists():
            print(f"  skip {sym}: no features")
            continue
        df = pd.read_parquet(path)
        dfs.append(df)
    if not dfs:
        raise RuntimeError("No features found. Run feature_engineering.py first.")
    full = pd.concat(dfs, ignore_index=True)
    full = full.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
    # rename for AutoGluon
    full = full.rename(columns={"symbol": "item_id", "timestamp": "timestamp"})
    return full


def build_tsdf(df: pd.DataFrame, target_col: str = "close") -> "TimeSeriesDataFrame":
    """Build AutoGluon TimeSeriesDataFrame with target + known covariates."""
    from autogluon.timeseries import TimeSeriesDataFrame

    keep = ["item_id", "timestamp", target_col]
    # also include numeric features as past covariates
    feature_cols = [c for c in df.columns
                    if c not in ("item_id", "timestamp", target_col, "symbol")
                    and pd.api.types.is_numeric_dtype(df[c])]
    keep += feature_cols
    sub = df[keep].dropna(subset=[target_col]).copy()
    # AutoGluon requires tz-naive datetime64[ns]
    sub["timestamp"] = pd.to_datetime(sub["timestamp"]).dt.tz_localize(None)
    tsdf = TimeSeriesDataFrame.from_data_frame(
        sub,
        id_column="item_id",
        timestamp_column="timestamp",
    )
    return tsdf


def train_zero_shot(tsdf, prediction_length: int, out_dir: Path, freq: str = "D",
                     seed: int = 123):
    """Zero-shot mode: just load pretrained model, no training."""
    from autogluon.timeseries import TimeSeriesPredictor

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n=== Zeroshot mode ===")
    print(f"Items: {tsdf.num_items}, freq: {tsdf.freq}, prediction_length: {prediction_length}")
    print(f"seed={seed}")

    predictor = TimeSeriesPredictor(
        prediction_length=prediction_length,
        freq=freq,
        path=str(out_dir),
        target="close",
        eval_metric="MASE",
    )

    t0 = time.time()
    # Use the modern "bolt_small" preset (the older "chronos_small" preset was
    # removed when AutoGluon renamed presets in 1.4+; bolt_small maps to the
    # chronos-bolt-small backbone which is the recommended zero-shot default).
    predictor.fit(
        train_data=tsdf,
        presets="bolt_small",
        time_limit=120,
        verbosity=2,
        random_seed=seed,
    )
    elapsed = time.time() - t0
    print(f"Fit (zeroshot) took {elapsed:.1f}s")
    return predictor, elapsed


def train_finetune(tsdf, prediction_length: int, out_dir: Path,
                   steps: int = 200, batch_size: int = 16, lr: float = 1e-5,
                   context_length: int = 512, freq: str = "D",
                   seed: int = 123, base_model: str = "autogluon/chronos-bolt-small"):
    """Fine-tune mode: actually train Chronos on our data.

    ``base_model`` controls the AutoGluon Chronos backbone to fine-tune from.
    Defaults to the public ``autogluon/chronos-bolt-small`` so existing
    callers behave unchanged. The orchestrator (``run_experiment.py``) passes
    ``base_model: phase2_v2`` from YAML when it wants warm-init from a local
    checkpoint; AutoGluon will load that path as the starting weights.
    """
    from autogluon.timeseries import TimeSeriesPredictor

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n=== Fine-tune mode ===")
    print(f"Items: {tsdf.num_items}, freq: {tsdf.freq}, prediction_length: {prediction_length}")
    print(f"steps={steps}, batch_size={batch_size}, lr={lr}, context_length={context_length}")
    print(f"seed={seed}, base_model={base_model}")

    predictor = TimeSeriesPredictor(
        prediction_length=prediction_length,
        freq=freq,
        path=str(out_dir),
        target="close",
        eval_metric="MASE",
    )

    hp = {
        "Chronos": {
            "model_path": base_model,
            "context_length": context_length,
            "fine_tune": True,
            "fine_tune_lr": lr,
            "fine_tune_steps": steps,
            "fine_tune_batch_size": batch_size,
            "eval_during_fine_tune": False,
        }
    }

    t0 = time.time()
    predictor.fit(
        train_data=tsdf,
        hyperparameters=hp,
        time_limit=3600,
        verbosity=2,
        random_seed=seed,
    )
    elapsed = time.time() - t0
    print(f"Fit (finetune) took {elapsed:.1f}s")
    return predictor, elapsed


def evaluate_sample(predictor, tsdf, symbols: List[str], out_dir: Path):
    """Generate forecasts for a few symbols, save plot + summary."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    print("\n=== Sample forecasts ===")
    # Predict using last window
    preds = predictor.predict(tsdf)
    if hasattr(preds, "to_data_frame"):
        preds_df = preds.to_data_frame()
    else:
        preds_df = preds.copy()

    # Pick a few well-known symbols
    pick = [s for s in ["AAPL", "NVDA", "TSLA", "JPM", "MSFT"] if s in symbols][:5]
    if not pick:
        pick = symbols[:5]

    fig, axes = plt.subplots(len(pick), 1, figsize=(11, 3 * len(pick)), sharex=False)
    if len(pick) == 1:
        axes = [axes]
    summary_rows = []
    for ax, sym in zip(axes, pick):
        sub_pred = preds_df.xs(sym, level="item_id") if "item_id" in preds_df.index.names else \
                   preds_df[preds_df["item_id"] == sym] if "item_id" in preds_df.columns else preds_df
        # Pull actuals
        actual_df = tsdf.to_data_frame() if hasattr(tsdf, "to_data_frame") else None
        if actual_df is not None and "item_id" in actual_df.index.names:
            try:
                actuals = actual_df.xs(sym, level="item_id")["close"].tail(60)
            except Exception:
                actuals = None
        else:
            actuals = None

        if actuals is not None and not actuals.empty:
            ax.plot(actuals.index, actuals.values, label="actual (60d history)", color="black", lw=1.2)
        if "mean" in sub_pred.columns:
            ax.plot(sub_pred.index, sub_pred["mean"].values, label="forecast mean", color="C0", lw=1.5)
        if "0.1" in sub_pred.columns and "0.9" in sub_pred.columns:
            ax.fill_between(sub_pred.index, sub_pred["0.1"].values, sub_pred["0.9"].values,
                            alpha=0.2, color="C0", label="q10-q90")
        ax.set_title(f"{sym}")
        ax.legend(loc="upper left", fontsize=8)
        ax.grid(True, alpha=0.3)
        # summary
        if "mean" in sub_pred.columns and actuals is not None and not actuals.empty:
            last_actual = float(actuals.iloc[-1])
            fc_mean = float(sub_pred["mean"].iloc[0])
            ret = (fc_mean - last_actual) / last_actual * 100
            summary_rows.append({
                "symbol": sym,
                "last_actual": round(last_actual, 2),
                "forecast_mean": round(fc_mean, 2),
                "forecast_ret_pct": round(ret, 2),
            })

    plt.tight_layout()
    plt.savefig(out_dir / "sample_forecasts.png", dpi=110, bbox_inches="tight")
    plt.close()
    print(f"Saved sample_forecasts.png")
    if summary_rows:
        pd.DataFrame(summary_rows).to_csv(out_dir / "sample_forecasts.csv", index=False)


def write_report(predictor, mode: str, elapsed: float, symbols: List[str], out_dir: Path):
    """Write a baseline report markdown."""
    report = out_dir / "baseline_report.md"
    leader = predictor.leaderboard(silent=True) if hasattr(predictor, "leaderboard") else None
    lines = [
        "# Chronos Baseline v1 — Training Report",
        "",
        f"**Generated**: {pd.Timestamp.now().isoformat()}",
        f"**Mode**: {mode}",
        f"**Symbols**: {len(symbols)} ({', '.join(symbols[:10])}{'...' if len(symbols) > 10 else ''})",
        f"**Training time**: {elapsed:.1f}s",
        "",
        "## Predictor",
        "",
        f"- Path: `{out_dir}`",
        f"- Target: `close`",
        f"- Eval metric: MASE",
        "",
    ]
    if leader is not None and len(leader) > 0:
        lines += ["## Leaderboard", "", leader.to_string(index=False), ""]
    lines += [
        "## Sample forecasts",
        "",
        "See `sample_forecasts.png` and `sample_forecasts.csv` for predictions on AAPL/NVDA/TSLA/JPM/MSFT.",
        "",
        "## Reproducibility",
        "",
        "```bash",
        "PYTHONPATH=src:scripts:. python scripts/train_chronos_baseline.py --mode "
        + mode
        + " --steps 200",
        "```",
    ]
    report.write_text("\n".join(lines))
    print(f"Wrote {report}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["zeroshot", "finetune"], default="zeroshot")
    parser.add_argument("--symbols", help="Comma-separated subset")
    parser.add_argument("--prediction-length", type=int, default=10)
    parser.add_argument("--context-length", type=int, default=512)
    parser.add_argument("--steps", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=1e-5)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--freq", default="D")
    parser.add_argument(
        "--seed",
        type=int,
        default=123,
        help="Random seed for python / numpy / torch / AutoGluon. "
             "Round 2 P0 plumbing: previously trainer ignored this; now it "
             "is propagated to TimeSeriesPredictor(random_seed=...) and "
             "fit(random_seed=...) so seed_sweep YAMLs actually sweep.",
    )
    parser.add_argument(
        "--base-model",
        default="autogluon/chronos-bolt-small",
        help="AutoGluon Chronos backbone to fine-tune from. Defaults to the "
             "public autogluon/chronos-bolt-small so existing callers behave "
             "unchanged. Round 2 P0 plumbing: previously this was hardcoded; "
             "the orchestrator now passes a local checkpoint path (e.g. "
             "'phase2_v2') for warm-init experiments.",
    )
    parser.add_argument(
        "--device",
        choices=["auto", "cuda", "mps", "cpu"],
        default="auto",
        help="Compute device for training. AutoGluon Chronos picks device via "
             "torch.cuda.is_available() at fit() time, so we just set env hints: "
             "cuda → CUDA_VISIBLE_DEVICES=0; mps → PYTORCH_ENABLE_MPS_FALLBACK=1; "
             "cpu → CUDA_VISIBLE_DEVICES=\"\". Default 'auto' leaves it alone.",
    )
    args = parser.parse_args()

    # Seed process-level RNGs (python / numpy / torch) BEFORE importing
    # autogluon. This guarantees that any module-level random calls
    # (data shuffling, init, etc.) are reproducible. AG's own random_seed
    # is propagated separately via TimeSeriesPredictor(random_seed=...)
    # and fit(random_seed=...).
    import random as _random
    _random.seed(args.seed)
    np.random.seed(args.seed)
    try:
        import torch
        torch.manual_seed(args.seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(args.seed)
    except ImportError:
        pass
    print(f"[seed] process-level seeded with seed={args.seed}")

    # Translate --device into env-var hints that AutoGluon/Chronos will honor.
    # AutoGluon Chronos calls torch.device() internally; we cannot inject a
    # torch.device object directly. The standard idiom is CUDA_VISIBLE_DEVICES.
    if args.device == "cuda":
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
    elif args.device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    elif args.device == "mps":
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

    try:
        import torch
        if torch.cuda.is_available() and args.device != "cpu":
            print(f"[device] torch CUDA available → AutoGluon will use {torch.cuda.get_device_name(0)}")
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available() and args.device != "cpu":
            print(f"[device] torch MPS available → AutoGluon will use MPS (Apple Silicon)")
        else:
            print(f"[device] no GPU → AutoGluon will use CPU (slow)")
    except ImportError:
        print(f"[device] torch not importable — cannot auto-detect; relying on --device flag only")

    out_dir = Path(args.out)

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    df = load_features(symbols)
    print(f"Loaded {len(df)} rows × {len(df.columns)} cols")
    symbols_actual = sorted(df["item_id"].unique().tolist())

    tsdf = build_tsdf(df, target_col="close")
    print(f"TimeSeriesDataFrame: {tsdf.num_items} items, freq={tsdf.freq}")

    # Wipe any prior model in out_dir
    if out_dir.exists():
        for child in out_dir.iterdir():
            if child.name in ("sample_forecasts.png", "sample_forecasts.csv",
                              "baseline_report.md", "train_meta.json"):
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    if args.mode == "zeroshot":
        predictor, elapsed = train_zero_shot(
            tsdf, args.prediction_length, out_dir, freq=args.freq, seed=args.seed,
        )
    else:
        predictor, elapsed = train_finetune(
            tsdf, args.prediction_length, out_dir,
            steps=args.steps, batch_size=args.batch_size, lr=args.lr,
            context_length=args.context_length, freq=args.freq,
            seed=args.seed, base_model=args.base_model,
        )

    meta = {
        "mode": args.mode,
        "symbols": symbols_actual,
        "n_symbols": len(symbols_actual),
        "prediction_length": args.prediction_length,
        "context_length": args.context_length,
        "steps": args.steps,
        "batch_size": args.batch_size,
        "lr": args.lr,
        "seed": args.seed,
        "base_model": args.base_model,
        "elapsed_seconds": elapsed,
        "generated": pd.Timestamp.now().isoformat(),
    }
    (out_dir / "train_meta.json").write_text(json.dumps(meta, indent=2))

    try:
        evaluate_sample(predictor, tsdf, symbols_actual, out_dir)
    except Exception as e:
        print(f"Sample eval failed (non-fatal): {e}")

    try:
        write_report(predictor, args.mode, elapsed, symbols_actual, out_dir)
    except Exception as e:
        print(f"Report write failed (non-fatal): {e}")

    print(f"\nDone. Model at: {out_dir}")


if __name__ == "__main__":
    main()