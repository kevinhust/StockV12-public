#!/usr/bin/env python3
"""
YOLO pattern classifier — OOS hit-rate validation.

For each validation sample, the model predicts a pattern class. This script
checks whether that predicted class actually aligns with the forward return
realized over the next N trading days (a proxy for "did the pattern mean
what we thought it meant?").

Per-class definitions (mirrors Drive's YOLO_LABEL_PREDICTIVE_POWER.py):
  bullish : breakout, trend_up
  bearish : trend_down, reversal
  neutral : chop, normal

Hit criteria (forward return over N bars from image's last bar):
  bullish hit : forward_return > +BUFFER
  bearish hit : forward_return < -BUFFER
  neutral hit : |forward_return| < BUFFER  (or BUFFER * 0.5 — narrower band)

Buffer = 0.001 (10 bp) for N=1d, scaled down for shorter horizons.
Horizons: N=1, 2, 4 days (4h, 8h, 16h of bars) — kept simple for v1.

Output (under --model-dir):
  hit_rate_report.json     # per-class + per-direction hit rates + bootstrap CI
  hit_rate_summary.md      # human-readable summary

Usage:
  PYTHONPATH=src:scripts:. python3 scripts/yolo_hit_rate_validation.py \
      --model-dir models/yolo/phase2_v1 \
      --data-dir data/intraday_clean_5min/phase2/ready_for_yolo_full \
      --temporal-split --val-frac 0.2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torchvision import transforms
from loguru import logger

# Reuse from trainer
sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_yolo_pilot import (  # noqa: E402
    ChartImageDataset,
    build_resnet18,
    temporal_split,
    stratified_split,
    IMAGENET_MEAN,
    IMAGENET_STD,
    pick_device,
)

# Direction mapping per class
DIRECTION_MAP = {
    "breakout":  "bullish",
    "trend_up":  "bullish",
    "reversal":  "bearish",
    "trend_down": "bearish",
    "chop":      "neutral",
    "normal":    "neutral",
}

# Forward horizons in days (next trading day's bars)
HORIZONS = [1, 2, 4]
# Buffer for hit definition: forward return must exceed this to count as directional
BUFFER = 0.001   # 10 bp


def bootstrap_ci(hits: np.ndarray, n_boot: int = 2000, alpha: float = 0.05,
                 seed: int = 42) -> Tuple[float, float, float]:
    """Return (mean, ci_lo, ci_hi) for hit-rate via bootstrap resampling."""
    rng = np.random.default_rng(seed)
    n = len(hits)
    if n == 0:
        return 0.0, 0.0, 0.0
    means = np.empty(n_boot)
    for i in range(n_boot):
        idx = rng.integers(0, n, size=n)
        means[i] = hits[idx].mean()
    return float(hits.mean()), float(np.percentile(means, 100 * alpha / 2)), \
           float(np.percentile(means, 100 * (1 - alpha / 2)))


def compute_forward_returns(eval_df: pd.DataFrame, source_root: Path,
                            horizons: List[int]) -> Dict[int, List[float]]:
    """For each (symbol, val_date) in eval_df, compute forward returns at each horizon.

    Cross-day forward: anchor = last bar (close) of the image's day;
    forward = last bar (close) of (val_date + n_days) — where n_days is taken from
    the full chronological date list of that symbol in source_root.

    Returns 0.0 if either the anchor day or the forward day is missing.
    """
    out: Dict[int, List[float]] = {n: [] for n in horizons}

    # Cache parquet reads: (sym, date_str) -> df or None
    cache: Dict[Tuple[str, str], "pd.DataFrame | None"] = {}

    def get_day(sym: str, date_str: str):
        key = (sym, date_str)
        if key in cache:
            return cache[key]
        pq_path = source_root / f"date={date_str}" / f"symbol={sym}" / "data.parquet"
        if not pq_path.exists():
            cache[key] = None
            return None
        try:
            df = pq.ParquetFile(pq_path).read().to_pandas().sort_values("timestamp")
            cache[key] = df
            return df
        except Exception as e:
            logger.warning(f"Could not read {pq_path}: {e}")
            cache[key] = None
            return None

    # Pre-discover per-symbol chronological date list from source_root
    sym_dates: Dict[str, List[str]] = {}
    if source_root.exists():
        for date_dir in source_root.iterdir():
            if not date_dir.is_dir() or not date_dir.name.startswith("date="):
                continue
            date_str = date_dir.name.split("date=", 1)[1]
            for sym_dir in date_dir.iterdir():
                if not sym_dir.is_dir() or not sym_dir.name.startswith("symbol="):
                    continue
                sym = sym_dir.name.split("symbol=", 1)[1]
                if (sym_dir / "data.parquet").exists():
                    sym_dates.setdefault(sym, set()).add(date_str)
    for sym in sym_dates:
        sym_dates[sym] = sorted(sym_dates[sym])

    # Walk eval_df in original row order to align with pred_classes
    for sym, sub in eval_df.groupby("symbol"):
        all_dates = sym_dates.get(sym, [])
        for date_str in sub["date"]:
            df_day = get_day(sym, date_str)
            if df_day is None or len(df_day) == 0 or date_str not in all_dates:
                for n in horizons:
                    out[n].append(0.0)
                continue
            p0 = float(df_day["close"].iloc[-1])
            if p0 <= 0:
                for n in horizons:
                    out[n].append(0.0)
                continue
            idx = all_dates.index(date_str)
            for n in horizons:
                tgt = idx + n
                if tgt >= len(all_dates):
                    out[n].append(0.0)
                    continue
                fwd_date = all_dates[tgt]
                df_fwd = get_day(sym, fwd_date)
                if df_fwd is None or len(df_fwd) == 0:
                    out[n].append(0.0)
                    continue
                pN = float(df_fwd["close"].iloc[-1])
                out[n].append((pN - p0) / p0)

    return out


def main():
    parser = argparse.ArgumentParser(description="YOLO pattern classifier OOS hit-rate validation")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path,
                        default=Path("data/intraday_clean_5min/phase2"),
                        help="Root of 5min parquet partitions (for forward-return lookup)")
    parser.add_argument("--val-frac", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--image-size", type=int, default=224)
    parser.add_argument("--num-workers", type=int, default=2)
    parser.add_argument("--device", default="auto", choices=["auto", "mps", "cuda", "cpu"])
    parser.add_argument("--temporal-split", action="store_true")
    parser.add_argument("--bootstrap-iters", type=int, default=2000)
    args = parser.parse_args()

    device = pick_device(args.device)
    logger.info(f"[HIT-RATE] device={device} model={args.model_dir} data={args.data_dir}")

    # ---- load model ----
    best_ckpt = args.model_dir / "best.pt"
    if not best_ckpt.exists():
        raise FileNotFoundError(f"best.pt not found at {best_ckpt}")
    ckpt = torch.load(best_ckpt, map_location=device, weights_only=False)
    class_names: List[str] = ckpt["class_names"]
    label_to_idx = ckpt["label_to_idx"]
    model = build_resnet18(num_classes=len(class_names)).to(device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()

    # ---- load data ----
    labels_csv = args.data_dir / "labels.csv"
    df = pd.read_csv(labels_csv).dropna(subset=["pattern_label"]).reset_index(drop=True)
    images_dir = args.data_dir / "images"
    missing = [n for n in df["image"] if not (images_dir / n).exists()]
    if missing:
        raise FileNotFoundError(f"{len(missing)} images missing")
    if args.temporal_split:
        train_df, val_df = temporal_split(df, args.val_frac, args.seed)
    else:
        train_df, val_df = stratified_split(df, args.val_frac, args.seed)
    logger.info(f"[HIT-RATE] val split: {len(val_df)} samples")

    val_tf = transforms.Compose([
        transforms.Resize((args.image_size, args.image_size)),
        transforms.ToTensor(),
        transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
    ])
    val_ds = ChartImageDataset(val_df, images_dir, label_to_idx, val_tf)
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False,
                            num_workers=args.num_workers, pin_memory=False)

    # ---- inference ----
    all_preds, all_labels, all_indices = [], [], []
    with torch.no_grad():
        for x, y, _ in val_loader:
            x = x.to(device)
            logits = model(x)
            preds = logits.argmax(dim=1).cpu().numpy()
            all_preds.extend(preds.tolist())
            all_labels.extend(y.numpy().tolist())
    all_preds = np.array(all_preds)
    all_labels = np.array(all_labels)
    pred_classes = [class_names[p] for p in all_preds]
    pred_directions = np.array([DIRECTION_MAP[c] for c in pred_classes])
    logger.info(f"[HIT-RATE] inference done on {len(val_df)} samples")

    # ---- forward returns ----
    logger.info(f"[HIT-RATE] computing forward returns at horizons {HORIZONS}...")
    t0 = time.time()
    fwd_returns = compute_forward_returns(val_df, args.source_root, HORIZONS)
    logger.info(f"[HIT-RATE] forward-return lookup done in {time.time()-t0:.1f}s")

    # ---- compute hit rates ----
    out: Dict = {
        "model_dir": str(args.model_dir),
        "data_dir": str(args.data_dir),
        "n_val": len(val_df),
        "horizons_days": HORIZONS,
        "buffer_bp": int(BUFFER * 10000),
        "per_class": {},
        "per_direction": {},
        "per_class_per_horizon": {},
    }

    # Per-class hit rates at horizon 1
    horizon = 1
    fwd = np.array(fwd_returns[horizon])
    for cls in class_names:
        mask = np.array([pc == cls for pc in pred_classes])
        if mask.sum() == 0:
            continue
        fwd_cls = fwd[mask]
        direction = DIRECTION_MAP[cls]
        if direction == "bullish":
            hits = (fwd_cls > BUFFER).astype(float)
        elif direction == "bearish":
            hits = (fwd_cls < -BUFFER).astype(float)
        else:  # neutral
            hits = (np.abs(fwd_cls) < BUFFER * 0.5).astype(float)
        mean, lo, hi = bootstrap_ci(hits, args.bootstrap_iters, seed=args.seed)
        out["per_class"][cls] = {
            "n": int(mask.sum()),
            "direction": direction,
            "hit_rate": round(mean, 4),
            "ci_lo": round(lo, 4),
            "ci_hi": round(hi, 4),
            "buffer": BUFFER if direction != "neutral" else BUFFER * 0.5,
            "horizon_days": horizon,
        }

    # Per-direction aggregate at horizon 1
    for direction in ["bullish", "bearish", "neutral"]:
        mask = (pred_directions == direction)
        if mask.sum() == 0:
            continue
        fwd_dir = fwd[mask]
        if direction == "bullish":
            hits = (fwd_dir > BUFFER).astype(float)
        elif direction == "bearish":
            hits = (fwd_dir < -BUFFER).astype(float)
        else:
            hits = (np.abs(fwd_dir) < BUFFER * 0.5).astype(float)
        mean, lo, hi = bootstrap_ci(hits, args.bootstrap_iters, seed=args.seed)
        out["per_direction"][direction] = {
            "n": int(mask.sum()),
            "hit_rate": round(mean, 4),
            "ci_lo": round(lo, 4),
            "ci_hi": round(hi, 4),
            "horizon_days": horizon,
        }

    # Per-class per-horizon hit rates
    for n_bars in HORIZONS:
        fwd_h = np.array(fwd_returns[n_bars])
        out["per_class_per_horizon"][f"h{n_bars}d"] = {}
        for cls in class_names:
            mask = np.array([pc == cls for pc in pred_classes])
            if mask.sum() == 0:
                continue
            fwd_cls = fwd_h[mask]
            direction = DIRECTION_MAP[cls]
            if direction == "bullish":
                hits = (fwd_cls > BUFFER).astype(float)
            elif direction == "bearish":
                hits = (fwd_cls < -BUFFER).astype(float)
            else:
                hits = (np.abs(fwd_cls) < BUFFER * 0.5).astype(float)
            mean, lo, hi = bootstrap_ci(hits, args.bootstrap_iters, seed=args.seed)
            out["per_class_per_horizon"][f"h{n_bars}d"][cls] = {
                "n": int(mask.sum()),
                "hit_rate": round(mean, 4),
            }

    # ---- save JSON ----
    out_path = args.model_dir / "hit_rate_report.json"
    out_path.write_text(json.dumps(out, indent=2))
    logger.info(f"[HIT-RATE] saved {out_path}")

    # ---- generate markdown summary ----
    md_lines = [
        f"# YOLO Hit-Rate OOS Validation",
        f"",
        f"**Model**: `{args.model_dir.name}`  ",
        f"**Val samples**: {len(val_df)}  ",
        f"**Buffer**: ±{int(BUFFER * 10000)} bp (directional); ±{int(BUFFER * 5000)} bp (neutral)  ",
        f"**Horizons**: {HORIZONS} days (forward from image's last bar)  ",
        f"**Bootstrap CI**: {args.bootstrap_iters} resamples, 95% CI",
        f"",
        f"## Per-Direction Hit Rate (1-day forward)",
        f"",
        f"| Direction | n | Hit Rate | 95% CI |",
        f"|---|---|---|---|",
    ]
    for direction in ["bullish", "bearish", "neutral"]:
        d = out["per_direction"].get(direction)
        if d:
            md_lines.append(
                f"| {direction} | {d['n']} | {d['hit_rate']:.4f} | "
                f"[{d['ci_lo']:.4f}, {d['ci_hi']:.4f}] |"
            )
    md_lines += [
        f"",
        f"## Per-Class Hit Rate (1-day forward)",
        f"",
        f"| Class | Direction | n | Hit Rate | 95% CI |",
        f"|---|---|---|---|---|",
    ]
    for cls in class_names:
        c = out["per_class"].get(cls)
        if c:
            md_lines.append(
                f"| {cls} | {c['direction']} | {c['n']} | {c['hit_rate']:.4f} | "
                f"[{c['ci_lo']:.4f}, {c['ci_hi']:.4f}] |"
            )
    md_lines += [
        f"",
        f"## Per-Class Across Horizons (hit-rate only)",
        f"",
    ]
    header = "| Class | " + " | ".join(f"h{n}d" for n in HORIZONS) + " |"
    sep = "|---|" + "|".join(["---"] * len(HORIZONS)) + "|"
    md_lines += [header, sep]
    for cls in class_names:
        cells = [cls]
        for n in HORIZONS:
            d = out["per_class_per_horizon"][f"h{n}d"].get(cls)
            cells.append(f"{d['hit_rate']:.4f}" if d else "—")
        md_lines.append("| " + " | ".join(cells) + " |")

    md_lines += [
        f"",
        f"## Interpretation",
        f"",
        f"- **Bullish** (breakout, trend_up): hit rate > 0.55 with CI lower bound > 0.5 = real edge",
        f"- **Bearish** (trend_down, reversal): same criterion",
        f"- **Neutral** (chop, normal): hit rate ≈ 0.5 = no edge (expected, neutral = no prediction)",
        f"",
        f"Note: forward returns are computed from image's last bar (typically end of trading day),",
        f"so horizon=N days = next-day's first N bars. This is a coarse proxy for swing-trade edge;",
        f"intraday-first-N-bars analysis requires chart-bar-aligned forward windows.",
    ]
    md_path = args.model_dir / "hit_rate_summary.md"
    md_path.write_text("\n".join(md_lines))
    logger.info(f"[HIT-RATE] saved {md_path}")

    print("\n" + "=" * 60)
    print("HIT-RATE OOS VALIDATION COMPLETE")
    for direction in ["bullish", "bearish", "neutral"]:
        d = out["per_direction"].get(direction)
        if d:
            print(f"  {direction:8s} n={d['n']:5d}  hit_rate={d['hit_rate']:.4f}  "
                  f"CI=[{d['ci_lo']:.4f}, {d['ci_hi']:.4f}]")
    print("=" * 60)


if __name__ == "__main__":
    main()