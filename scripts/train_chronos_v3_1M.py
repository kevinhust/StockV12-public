#!/usr/bin/env python3
"""
Chronos phase2_v3 — 1M-step chunked retrain from phase2_v2 warm init.

Design (2026-06-26):
- Warm init from `models/chronos/phase2_v2` (the current live model)
- Total target: 1,000,000 fine-tune steps (vs v2's 1,000)
- Chunked: 10,000 steps per chunk, 100 chunks total
- Each chunk creates a NEW TimeSeriesPredictor at its own dir, warm-inits
  from the previous chunk's Chronos fine-tuned-ckpt, fine-tunes for
  --chunk-steps more steps, saves in place.
- Resumable: skip chunks whose `phase2_v3_ckpt_Nk/predictor.pkl` already
  exists. State in `phase2_v3_progress.json`.
- Polite on shared GPU: batch_size=16 (vs v2's 32), `--chunk-time-limit`
  caps each chunk so the GPU box can be preempted cleanly when other
  jobs need it.

Output layout (under `models/chronos/`):
  phase2_v3_ckpt_10k/
  phase2_v3_ckpt_20k/
  ...
  phase2_v3_ckpt_1000k/    ← final
  phase2_v3_progress.json  ← resumable state

AutoGluon gotchas (verified against autogluon.timeseries.models.chronos):
- `model_path` in hp["Chronos"] must be a path `BaseChronosPipeline
  .from_pretrained()` can load, i.e. a HuggingFace pipeline dir with
  config.json + model.safetensors. For our trained predictors that's
  `<predictor>/models/Chronos[...]/W0/fine-tuned-ckpt/`.
- AutoGluon refuses `fit()` on an already-fit predictor. We must create
  a fresh TimeSeriesPredictor each chunk.

Usage:
  PYTHONPATH=src:scripts:. python scripts/train_chronos_v3_1M.py \\
      --warm-init phase2_v2 \\
      --chunk-steps 10000 \\
      --total-steps 10000 \\
      --batch-size 16 \\
      --device cuda
  # ^ smoke-test: only chunk 0 runs.
  # For full 1M, set --total-steps 1000000.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FEATURES_DIR = PROJECT_ROOT / "data" / "features" / "daily"
DEFAULT_OUT_BASE = PROJECT_ROOT / "models" / "chronos"


def chunk_dir(out_base: Path, total_steps: int) -> Path:
    return out_base / f"phase2_v3_ckpt_{(total_steps // 1000)}k"


def load_features(symbols: Optional[list] = None):
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
    full = full.rename(columns={"symbol": "item_id", "timestamp": "timestamp"})
    return full


def build_tsdf(df, target_col: str = "close"):
    from autogluon.timeseries import TimeSeriesDataFrame
    keep = ["item_id", "timestamp", target_col]
    feature_cols = [
        c for c in df.columns
        if c not in ("item_id", "timestamp", target_col, "symbol")
        and pd.api.types.is_numeric_dtype(df[c])
    ]
    keep += feature_cols
    sub = df[keep].dropna(subset=[target_col]).copy()
    sub["timestamp"] = pd.to_datetime(sub["timestamp"]).dt.tz_localize(None)
    return TimeSeriesDataFrame.from_data_frame(
        sub, id_column="item_id", timestamp_column="timestamp"
    )


def _find_fine_tuned_ckpt(predictor_path: Path) -> Path:
    """Walk to <predictor>/models/Chronos*/W0/fine-tuned-ckpt.

    The Chronos subdir name includes the original HF model id in
    brackets (e.g. Chronos[autogluon__chronos-bolt-small]), so we glob.
    """
    candidates = list((predictor_path / "models").glob("Chronos*"))
    if not candidates:
        return (
            predictor_path
            / "models"
            / "Chronos[autogluon__chronos-bolt-small]"
            / "W0"
            / "fine-tuned-ckpt"
        )
    return candidates[0] / "W0" / "fine-tuned-ckpt"


def find_warm_init_ckpt(out_base: Path, warm_init_name: str) -> Path:
    """Locate the fine-tuned-ckpt dir inside a warm-init predictor
    (e.g. phase2_v2).

    AutoGluon Chronos stores fine-tuned weights at:
      <predictor>/models/Chronos[...]/W0/fine-tuned-ckpt/

    That dir contains config.json + model.safetensors, which is what
    `BaseChronosPipeline.from_pretrained()` needs (verified against
    autogluon.timeseries.models.chronos.model).
    """
    p = out_base / warm_init_name
    if not p.exists():
        raise FileNotFoundError(f"warm-init predictor missing: {p}")
    ckpt = _find_fine_tuned_ckpt(p)
    if not ckpt.exists():
        raise FileNotFoundError(
            f"warm-init fine-tuned-ckpt missing under {p}. "
            f"Has the predictor actually been fine-tuned?"
        )
    return ckpt


def find_prev_chunk_ckpt(prev_chunk_predictor: Path) -> Path:
    """Locate the fine-tuned-ckpt dir under one of our trained chunk outputs."""
    ckpt = _find_fine_tuned_ckpt(prev_chunk_predictor)
    if not ckpt.exists():
        raise FileNotFoundError(
            f"No fine-tuned-ckpt under {prev_chunk_predictor}. "
            f"Did this chunk's predictor finish training?"
        )
    return ckpt


def run_one_chunk(
    chunk_idx: int,
    prev_ckpt_dir: Path,
    dst_predictor: Path,
    tsdf,
    prediction_length: int,
    chunk_steps: int,
    batch_size: int,
    context_length: int,
    lr: float,
    time_limit_sec: int,
) -> dict:
    """Create a NEW TimeSeriesPredictor at dst_predictor, warm-init
    from prev_ckpt_dir, fine-tune for chunk_steps more steps, save in place.

    AutoGluon refuses `fit()` on an already-fit predictor, so we cannot
    use TimeSeriesPredictor.load() to "continue" — we must create a fresh
    predictor each chunk and pass the previous chunk's Chronos
    fine-tuned-ckpt dir as `model_path`.
    """
    from autogluon.timeseries import TimeSeriesPredictor

    print(f"\n=== Chunk {chunk_idx}: warm-init {prev_ckpt_dir} → {dst_predictor.name} ===")
    dst_predictor.mkdir(parents=True, exist_ok=True)
    # Wipe any prior partial content from a failed previous attempt.
    for child in dst_predictor.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    hp = {
        "Chronos": {
            # Local path → warm init. AutoGluon hands this to
            # BaseChronosPipeline.from_pretrained() which loads the HF
            # weights from this dir instead of downloading.
            "model_path": str(prev_ckpt_dir),
            "context_length": context_length,
            "fine_tune": True,
            "fine_tune_lr": lr,
            "fine_tune_steps": chunk_steps,
            "fine_tune_batch_size": batch_size,
            "eval_during_fine_tune": False,
        }
    }

    predictor = TimeSeriesPredictor(
        prediction_length=prediction_length,
        freq="D",
        path=str(dst_predictor),
        target="close",
        eval_metric="MASE",
    )

    fit_t0 = time.time()
    predictor.fit(
        train_data=tsdf,
        hyperparameters=hp,
        time_limit=time_limit_sec,
        verbosity=2,
    )
    fit_elapsed = time.time() - fit_t0

    return {
        "chunk_idx": chunk_idx,
        "prev_ckpt_dir": str(prev_ckpt_dir),
        "dst": str(dst_predictor),
        "fit_seconds": round(fit_elapsed, 1),
        "chunk_steps": chunk_steps,
        "batch_size": batch_size,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--warm-init", default="phase2_v2",
                   help="predictor dir name under models/chronos/")
    p.add_argument("--out-base", default=str(DEFAULT_OUT_BASE))
    p.add_argument("--total-steps", type=int, default=1_000_000)
    p.add_argument("--chunk-steps", type=int, default=10_000)
    p.add_argument("--batch-size", type=int, default=16,
                   help="polite default for shared GPU; v2 used 32")
    p.add_argument("--context-length", type=int, default=512)
    p.add_argument("--lr", type=float, default=1e-5)
    p.add_argument("--prediction-length", type=int, default=10)
    p.add_argument("--device", choices=["auto", "cuda", "cpu"], default="auto")
    p.add_argument("--from-chunk", type=int, default=0,
                   help="0-based chunk index to start from (default: resume)")
    p.add_argument("--max-chunks", type=int, default=None,
                   help="cap number of NEW chunks to run this invocation")
    p.add_argument("--chunk-time-limit", type=int, default=1500,
                   help="hard cap seconds per chunk (yields GPU cleanly)")
    args = p.parse_args()

    # Device hints (same convention as train_chronos_baseline.py).
    if args.device == "cuda":
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
    elif args.device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    elif args.device == "auto":
        try:
            import torch
            if torch.cuda.is_available():
                print(f"[device] CUDA available → {torch.cuda.get_device_name(0)}")
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                print("[device] MPS available")
            else:
                print("[device] CPU only")
        except ImportError:
            pass

    out_base = Path(args.out_base)
    warm_init_ckpt = find_warm_init_ckpt(out_base, args.warm_init)
    print(f"[warm-init] ckpt={warm_init_ckpt}")

    total_chunks = args.total_steps // args.chunk_steps
    if total_chunks * args.chunk_steps != args.total_steps:
        print(
            f"WARNING: --total-steps {args.total_steps} not divisible by "
            f"--chunk-steps {args.chunk_steps}; will run {total_chunks * args.chunk_steps}",
            file=sys.stderr,
        )

    # Resumable state.
    progress_path = out_base / "phase2_v3_progress.json"
    if progress_path.exists():
        progress = json.loads(progress_path.read_text())
    else:
        progress = {
            "completed_chunks": 0,
            "total_steps_run": 0,
            "started_at": time.time(),
            "last_updated": time.time(),
            "warm_init": args.warm_init,
            "chunk_steps": args.chunk_steps,
            "total_target": args.total_steps,
            "batch_size": args.batch_size,
            "lr": args.lr,
            "chunk_history": [],
        }
    print(
        f"[progress] completed={progress['completed_chunks']}  "
        f"total_steps_run={progress['total_steps_run']}  "
        f"target={args.total_steps}"
    )

    # Load features ONCE — they don't change between chunks.
    df = load_features()
    print(f"[features] {len(df)} rows × {len(df.columns)} cols")
    tsdf = build_tsdf(df)
    print(f"[tsdf] {tsdf.num_items} items, freq={tsdf.freq}")

    start_chunk = max(args.from_chunk, progress["completed_chunks"])
    if args.max_chunks is not None:
        end_chunk = min(start_chunk + args.max_chunks, total_chunks)
    else:
        end_chunk = total_chunks
    print(f"[plan] running chunks [{start_chunk}, {end_chunk}) of {total_chunks} total")

    overall_t0 = time.time()
    for chunk_idx in range(start_chunk, end_chunk):
        target_steps = (chunk_idx + 1) * args.chunk_steps
        dst = chunk_dir(out_base, target_steps)

        if dst.exists() and (dst / "predictor.pkl").exists():
            print(f"[chunk {chunk_idx}] {dst.name} already exists — skip")
            # Still update progress so future --from-chunk=0 resumes correctly.
            progress["completed_chunks"] = chunk_idx + 1
            progress["total_steps_run"] = target_steps
            progress["last_updated"] = time.time()
            progress_path.write_text(json.dumps(progress, indent=2))
            continue

        if chunk_idx == 0:
            prev_ckpt = warm_init_ckpt
        else:
            prev_predictor = chunk_dir(out_base, chunk_idx * args.chunk_steps)
            prev_ckpt = find_prev_chunk_ckpt(prev_predictor)

        result = run_one_chunk(
            chunk_idx=chunk_idx,
            prev_ckpt_dir=prev_ckpt,
            dst_predictor=dst,
            tsdf=tsdf,
            prediction_length=args.prediction_length,
            chunk_steps=args.chunk_steps,
            batch_size=args.batch_size,
            context_length=args.context_length,
            lr=args.lr,
            time_limit_sec=args.chunk_time_limit,
        )
        result["timestamp"] = time.time()
        progress["chunk_history"].append(result)
        progress["completed_chunks"] = chunk_idx + 1
        progress["total_steps_run"] = target_steps
        progress["last_updated"] = time.time()
        progress_path.write_text(json.dumps(progress, indent=2))
        elapsed = time.time() - overall_t0
        print(
            f"[chunk {chunk_idx}] done in {result['fit_seconds']:.1f}s. "
            f"Wall total: {elapsed / 60:.1f}min, steps so far: {target_steps}"
        )

    # Final summary.
    final = chunk_dir(out_base, progress["total_steps_run"])
    print(f"\n=== v3 retrain complete (or paused) ===")
    print(f"chunks done: {progress['completed_chunks']}/{total_chunks}")
    print(f"steps run:   {progress['total_steps_run']}/{args.total_steps}")
    print(f"final ckpt:  {final}")
    print(f"progress:    {progress_path}")


if __name__ == "__main__":
    main()
