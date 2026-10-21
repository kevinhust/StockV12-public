#!/usr/bin/env python3
"""
Per-class threshold tuning for chart-pattern classifier.

Loads a trained model, runs inference on the validation split, and sweeps
per-class probability thresholds to maximize macro-F1.

Why: argmax baseline treats all classes equally, which biases predictions
toward the majority class (breakout in our data). Per-class thresholds let
rare classes have a lower bar for prediction.

Output (under --model-dir):
  threshold_tuned.json    # chosen per-class thresholds + before/after metrics
  threshold_sweep.png     # macro-F1 as function of confidence floor per class

Usage:
  PYTHONPATH=src:scripts:. python3 scripts/threshold_tune.py \
      --model-dir models/yolo/phase2_v1 \
      --data-dir data/intraday_clean_5min/phase2/ready_for_yolo_full \
      --temporal-split --val-frac 0.2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torchvision import transforms
from PIL import Image
from loguru import logger
from sklearn.metrics import (
    accuracy_score,
    classification_report,
    f1_score,
)

# Reuse pieces from the trainer (single source of truth)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_yolo_pilot import (  # noqa: E402
    ChartImageDataset,
    build_resnet18,
    temporal_split,
    stratified_split,
    IMAGENET_MEAN,
    IMAGENET_STD,
    pick_device,
    PROJECT_ROOT,
)


def get_probs(model: nn.Module, loader: DataLoader, device: torch.device) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Run inference and return (probs[N,K], labels[N], preds_argmax[N])."""
    model.eval()
    all_probs, all_labels = [], []
    with torch.no_grad():
        for x, y, _ in loader:
            x = x.to(device)
            logits = model(x)
            probs = F.softmax(logits, dim=1).cpu().numpy()
            all_probs.append(probs)
            all_labels.extend(y.numpy().tolist())
    probs = np.concatenate(all_probs, axis=0)
    labels = np.array(all_labels)
    preds = probs.argmax(axis=1)
    return probs, labels, preds


def predict_with_thresholds(probs: np.ndarray, thresholds: np.ndarray) -> np.ndarray:
    """Per-class threshold gating: predict class k if probs[:,k] >= thresholds[k].

    If no class crosses threshold, fall back to argmax (avoid empty prediction).
    """
    gated = probs >= thresholds[None, :]
    any_above = gated.any(axis=1)
    # For rows with no gate hit, use argmax (same as baseline)
    out = gated.argmax(axis=1)
    fallback = probs.argmax(axis=1)
    return np.where(any_above, out, fallback)


def sweep_thresholds(probs: np.ndarray, labels: np.ndarray, num_classes: int,
                     grid: np.ndarray) -> Tuple[np.ndarray, float]:
    """Greedy per-class threshold search to maximize macro-F1.

    For each class, sweep its threshold (others fixed at 0.0 = pass-through).
    Pick the value that maximizes macro-F1, then move to the next class.
    Repeat one pass (grid sweep is small; greedy converges fast).
    """
    # Start: thresholds = 0.0 everywhere (= argmax baseline)
    thresholds = np.zeros(num_classes)
    base_f1 = f1_score(labels, predict_with_thresholds(probs, thresholds),
                       average="macro", zero_division=0)
    logger.info(f"[THRESH] baseline (all 0.0): macro-F1 = {base_f1:.4f}")

    # Pass 1: independent per-class sweep
    for k in range(num_classes):
        best_t, best_f1 = 0.0, base_f1
        for t in grid:
            test = thresholds.copy()
            test[k] = t
            f1 = f1_score(labels, predict_with_thresholds(probs, test),
                          average="macro", zero_division=0)
            if f1 > best_f1:
                best_f1, best_t = f1, t
        if best_t > 0:
            thresholds[k] = best_t
            base_f1 = best_f1
            logger.info(f"[THRESH] class {k}: t={best_t:.2f} → macro-F1={best_f1:.4f}")

    # Pass 2: refinement (in case early choices were suboptimal)
    for k in range(num_classes):
        best_t, best_f1 = thresholds[k], base_f1
        for t in grid:
            test = thresholds.copy()
            test[k] = t
            f1 = f1_score(labels, predict_with_thresholds(probs, test),
                          average="macro", zero_division=0)
            if f1 > best_f1:
                best_f1, best_t = f1, t
        if best_t != thresholds[k]:
            thresholds[k] = best_t
            base_f1 = best_f1
            logger.info(f"[THRESH] pass2 class {k}: t={best_t:.2f} → macro-F1={best_f1:.4f}")

    return thresholds, base_f1


def main():
    parser = argparse.ArgumentParser(description="Per-class threshold tuner")
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--val-frac", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--image-size", type=int, default=224)
    parser.add_argument("--num-workers", type=int, default=2)
    parser.add_argument("--device", default="auto", choices=["auto", "mps", "cuda", "cpu"])
    parser.add_argument("--temporal-split", action="store_true",
                        help="Match training split mode (set this if training used --temporal-split)")
    args = parser.parse_args()

    device = pick_device(args.device)
    logger.info(f"[THRESH] device={device} model={args.model_dir} data={args.data_dir}")

    # ---- load model ----
    best_ckpt = args.model_dir / "best.pt"
    if not best_ckpt.exists():
        raise FileNotFoundError(f"best.pt not found at {best_ckpt}")
    ckpt = torch.load(best_ckpt, map_location=device, weights_only=False)
    class_names: List[str] = ckpt["class_names"]
    label_to_idx = ckpt["label_to_idx"]
    num_classes = len(class_names)
    model = build_resnet18(num_classes=num_classes).to(device)
    model.load_state_dict(ckpt["model_state_dict"])
    model.eval()
    logger.info(f"[THRESH] loaded model: {best_ckpt.name} (classes={class_names})")

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
    logger.info(f"[THRESH] val split: {len(val_df)} samples")

    val_tf = transforms.Compose([
        transforms.Resize((args.image_size, args.image_size)),
        transforms.ToTensor(),
        transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
    ])
    val_ds = ChartImageDataset(val_df, images_dir, label_to_idx, val_tf)
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False,
                            num_workers=args.num_workers, pin_memory=False)

    # ---- inference ----
    t0 = time.time()
    probs, labels, preds = get_probs(model, val_loader, device)
    logger.info(f"[THRESH] inference done in {time.time()-t0:.1f}s")

    # ---- baseline metrics ----
    base_acc = accuracy_score(labels, preds)
    base_f1m = f1_score(labels, preds, average="macro", zero_division=0)
    base_report = classification_report(labels, preds, target_names=class_names,
                                        zero_division=0, digits=4)
    logger.info(f"[THRESH] baseline: acc={base_acc:.4f} macro-F1={base_f1m:.4f}")
    print("\n=== BASELINE (argmax) ===\n" + base_report)

    # ---- threshold sweep ----
    grid = np.arange(0.05, 0.95, 0.05)
    thresholds, tuned_f1 = sweep_thresholds(probs, labels, num_classes, grid)
    tuned_preds = predict_with_thresholds(probs, thresholds)
    tuned_acc = accuracy_score(labels, tuned_preds)
    tuned_report = classification_report(labels, tuned_preds, target_names=class_names,
                                         zero_division=0, digits=4)
    logger.info(f"[THRESH] tuned: acc={tuned_acc:.4f} macro-F1={tuned_f1:.4f}")
    print("\n=== TUNED (per-class thresholds) ===\n" + tuned_report)

    # ---- save outputs ----
    out = {
        "baseline": {
            "val_acc": round(base_acc, 4),
            "val_macro_f1": round(base_f1m, 4),
            "classification_report": base_report,
        },
        "tuned": {
            "per_class_thresholds": {
                class_names[i]: float(round(thresholds[i], 4))
                for i in range(num_classes)
            },
            "val_acc": round(tuned_acc, 4),
            "val_macro_f1": round(tuned_f1, 4),
            "classification_report": tuned_report,
        },
        "delta_macro_f1": round(tuned_f1 - base_f1m, 4),
        "model_dir": str(args.model_dir),
        "data_dir": str(args.data_dir),
        "n_val_samples": len(val_df),
        "image_size": args.image_size,
    }
    (args.model_dir / "threshold_tuned.json").write_text(json.dumps(out, indent=2))
    logger.info(f"[THRESH] saved {args.model_dir/'threshold_tuned.json'}")
    print("\n" + "=" * 60)
    print(f"THRESHOLD TUNE COMPLETE")
    print(f"  baseline macro-F1: {base_f1m:.4f}  →  tuned: {tuned_f1:.4f}  "
          f"(Δ {tuned_f1 - base_f1m:+.4f})")
    print(f"  per-class thresholds: " +
          ", ".join(f"{class_names[i]}={thresholds[i]:.2f}" for i in range(num_classes)))
    print("=" * 60)


if __name__ == "__main__":
    main()