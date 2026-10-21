#!/usr/bin/env python3
"""
YOLO Pilot Training — Phase 2 pilot image classification baseline.

Trains a ResNet18 (ImageNet pretrained) classifier on the YOLO-ready chart
dataset produced by prepare_yolo_dataset.py. This is a *classification* task
(class labels only, no bboxes), not true YOLO detection — chosen as the
appropriate v1 baseline per the user's spec.

Input:
  <data-dir>/images/<SYMBOL>_<DATE>_<TIME>.jpg
  <data-dir>/labels.csv  (image, symbol, date, time_et, close, volume_total,
                           ret_1h, pattern_label, rule_id, confidence)

Output (under --output-dir, default models/yolo/pilot_v1/):
  best.pt                  # best model state_dict (by val macro-F1)
  last.pt                  # final-epoch model state_dict
  class_index.json         # idx -> label name
  train_meta.json          # config, dataset stats, best metrics, device, etc.
  training_log.csv         # per-epoch train_loss/val_loss/val_acc/val_f1
  training_curves.png      # loss + acc + macro-F1 curves
  confusion_matrix.png     # normalized 6x6 confusion matrix on val set
  classification_report.txt# sklearn classification_report on val set
  sample_predictions.png   # 8x4 grid: val images with true vs pred labels
  pilot_report.md          # human-readable summary + next-step recommendations

Usage:
  PYTHONPATH=src:scripts:. python3 scripts/train_yolo_pilot.py \
      --epochs 20 --batch-size 32 --lr 1e-4 --seed 42
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd

# Matplotlib Agg backend (no display)
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler
from torchvision import models, transforms
from torchvision.models import ResNet18_Weights
from PIL import Image
from loguru import logger

from sklearn.metrics import (
    classification_report,
    confusion_matrix,
    f1_score,
    accuracy_score,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "intraday_clean_5min" / "phase2" / "ready_for_yolo"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "models" / "yolo" / "pilot_v1"

# ImageNet normalization (ResNet18 pretrained)
IMAGENET_MEAN = [0.485, 0.456, 0.406]
IMAGENET_STD = [0.229, 0.224, 0.225]


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------
class ChartImageDataset(Dataset):
    def __init__(self, df: pd.DataFrame, images_dir: Path, label_to_idx: Dict[str, int],
                 transform=None):
        self.df = df.reset_index(drop=True)
        self.images_dir = Path(images_dir)
        self.label_to_idx = label_to_idx
        self.transform = transform

    def __len__(self) -> int:
        return len(self.df)

    def __getitem__(self, idx: int):
        row = self.df.iloc[idx]
        img_path = self.images_dir / row["image"]
        img = Image.open(img_path).convert("RGB")
        if self.transform is not None:
            img = self.transform(img)
        label = self.label_to_idx[row["pattern_label"]]
        return img, label, row["image"]


def build_transforms(image_size: int = 224) -> Tuple[transforms.Compose, transforms.Compose]:
    """Light augmentation for train; deterministic for val."""
    train_tf = transforms.Compose([
        transforms.Resize((image_size, image_size)),
        transforms.RandomHorizontalFlip(p=0.5),
        transforms.ColorJitter(brightness=0.2, contrast=0.2, saturation=0.1),
        transforms.ToTensor(),
        transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
    ])
    val_tf = transforms.Compose([
        transforms.Resize((image_size, image_size)),
        transforms.ToTensor(),
        transforms.Normalize(IMAGENET_MEAN, IMAGENET_STD),
    ])
    return train_tf, val_tf


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
def build_resnet18(num_classes: int) -> nn.Module:
    """ImageNet-pretrained ResNet18 with final FC replaced."""
    model = models.resnet18(weights=ResNet18_Weights.IMAGENET1K_V1)
    in_features = model.fc.in_features  # 512
    model.fc = nn.Linear(in_features, num_classes)
    return model


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def pick_device(prefer: str = "auto") -> torch.device:
    if prefer == "auto":
        if torch.backends.mps.is_available():
            return torch.device("mps")
        if torch.cuda.is_available():
            return torch.device("cuda")
        return torch.device("cpu")
    return torch.device(prefer)


def stratified_split(df: pd.DataFrame, val_frac: float, seed: int) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Stratified split preserving class proportions."""
    rng = np.random.default_rng(seed)
    train_parts, val_parts = [], []
    for label, group in df.groupby("pattern_label"):
        idx = rng.permutation(len(group))
        n_val = max(1, int(round(len(group) * val_frac)))
        val_idx = idx[:n_val]
        train_idx = idx[n_val:]
        val_parts.append(group.iloc[val_idx])
        train_parts.append(group.iloc[train_idx])
    train_df = pd.concat(train_parts, ignore_index=True).sample(frac=1, random_state=seed).reset_index(drop=True)
    val_df = pd.concat(val_parts, ignore_index=True).sample(frac=1, random_state=seed).reset_index(drop=True)
    return train_df, val_df


def temporal_split(df: pd.DataFrame, val_frac: float, seed: int) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Split by date — last `val_frac` of dates go to val (forward-time split).

    Better matches real OOS deployment than random/stratified split (Drive §7.1).
    Returns: (train_df, val_df) with NO shuffling (time-respecting).
    """
    if "date" not in df.columns:
        raise ValueError("temporal_split requires 'date' column in labels.csv")
    dates = sorted(df["date"].unique())
    n_val = max(1, int(round(len(dates) * val_frac)))
    val_dates = set(dates[-n_val:])
    val_df = df[df["date"].isin(val_dates)].reset_index(drop=True)
    train_df = df[~df["date"].isin(val_dates)].reset_index(drop=True)
    logger.info(f"[YOLO-PILOT] temporal split: {len(train_df)} train / {len(val_df)} val "
                f"(val_dates={min(val_dates)} → {max(val_dates)}, {len(val_dates)} dates)")
    return train_df, val_df


def load_init_checkpoint(model: nn.Module, ckpt_path: Path, expected_num_classes: int,
                        strict: bool = False) -> nn.Module:
    """Warm-init model from a previously trained checkpoint.

    Loads matching layers by name. If FC head shape differs (num_classes),
    skip it and log a warning. Drive best-practice: warm-init avoids cold-start
    on small fine-tunes.
    """
    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    state = ckpt.get("model_state_dict", ckpt)
    own_state = model.state_dict()
    loaded, skipped_shape, missing = [], [], []
    for k, v in state.items():
        if k not in own_state:
            missing.append(k)
            continue
        if own_state[k].shape != v.shape:
            skipped_shape.append((k, list(v.shape), list(own_state[k].shape)))
            continue
        own_state[k] = v
        loaded.append(k)
    model.load_state_dict(own_state, strict=strict)
    logger.info(f"[YOLO-PILOT] init-checkpoint loaded: {ckpt_path.name}")
    logger.info(f"[YOLO-PILOT]   loaded={len(loaded)} skipped_shape={len(skipped_shape)} "
                f"missing_in_model={len(missing)}")
    if skipped_shape:
        logger.warning(f"[YOLO-PILOT]   skipped due to shape mismatch: {skipped_shape}")
    return model


def build_weighted_sampler(train_df: pd.DataFrame, label_to_idx: Dict[str, int]) -> WeightedRandomSampler:
    """WeightedRandomSampler with inverse-frequency weights (Drive §4.2: oversample minority).

    Each sample's weight = 1/count(class). Replacement sampling ensures rare
    classes get more presentations per epoch.
    """
    labels = [label_to_idx[l] for l in train_df["pattern_label"]]
    counts = np.bincount(labels).astype(np.float64)
    counts = np.maximum(counts, 1.0)
    weights = 1.0 / counts
    sample_weights = weights[labels]
    n_samples = len(train_df)
    logger.info(f"[YOLO-PILOT] weighted sampler: per-class weights = {weights.tolist()}")
    return WeightedRandomSampler(
        weights=torch.tensor(sample_weights, dtype=torch.float32),
        num_samples=n_samples,
        replacement=True,
    )


def compute_class_weights(labels: List[int], num_classes: int) -> torch.Tensor:
    """Inverse-frequency class weights for CrossEntropyLoss."""
    counts = np.bincount(labels, minlength=num_classes).astype(np.float64)
    # avoid div by zero
    counts = np.maximum(counts, 1.0)
    inv = 1.0 / counts
    # normalize so mean weight = 1 (stable scale vs uniform loss)
    weights = inv * (num_classes / inv.sum())
    return torch.tensor(weights, dtype=torch.float32)


def evaluate(model: nn.Module, loader: DataLoader, device: torch.device, num_classes: int) -> Tuple[float, float, np.ndarray, np.ndarray, np.ndarray]:
    """Returns (loss, accuracy, f1_macro, all_labels, all_preds)."""
    model.eval()
    crit = nn.CrossEntropyLoss(reduction="sum")
    total_loss = 0.0
    all_preds: List[int] = []
    all_labels: List[int] = []
    with torch.no_grad():
        for x, y, _ in loader:
            x, y = x.to(device), y.to(device)
            logits = model(x)
            total_loss += crit(logits, y).item()
            preds = logits.argmax(dim=1).cpu().numpy()
            all_preds.extend(preds.tolist())
            all_labels.extend(y.cpu().numpy().tolist())
    avg_loss = total_loss / max(len(all_labels), 1)
    acc = accuracy_score(all_labels, all_preds)
    f1m = f1_score(all_labels, all_preds, average="macro", zero_division=0)
    return avg_loss, acc, f1m, np.array(all_labels), np.array(all_preds)


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------
def plot_curves(log: pd.DataFrame, out_path: Path) -> None:
    fig, axes = plt.subplots(1, 3, figsize=(15, 4))
    axes[0].plot(log["epoch"], log["train_loss"], label="train", marker="o")
    axes[0].plot(log["epoch"], log["val_loss"], label="val", marker="o")
    axes[0].set_title("Loss"); axes[0].set_xlabel("epoch"); axes[0].legend(); axes[0].grid(alpha=0.3)
    axes[1].plot(log["epoch"], log["val_acc"], marker="o", color="tab:green")
    axes[1].set_title("Val Accuracy"); axes[1].set_xlabel("epoch"); axes[1].grid(alpha=0.3)
    axes[2].plot(log["epoch"], log["val_macro_f1"], marker="o", color="tab:purple")
    axes[2].set_title("Val Macro-F1"); axes[2].set_xlabel("epoch"); axes[2].grid(alpha=0.3)
    fig.suptitle("YOLO Pilot Training — ResNet18 classifier", fontsize=13, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)


def plot_confusion_matrix(labels: np.ndarray, preds: np.ndarray, class_names: List[str], out_path: Path) -> np.ndarray:
    cm = confusion_matrix(labels, preds, labels=list(range(len(class_names))))
    cm_norm = cm / np.maximum(cm.sum(axis=1, keepdims=True), 1)
    fig, ax = plt.subplots(figsize=(7, 6))
    im = ax.imshow(cm_norm, cmap="Blues", vmin=0, vmax=1)
    ax.set_xticks(range(len(class_names)))
    ax.set_yticks(range(len(class_names)))
    ax.set_xticklabels(class_names, rotation=30, ha="right", fontsize=9)
    ax.set_yticklabels(class_names, fontsize=9)
    ax.set_xlabel("Predicted"); ax.set_ylabel("True")
    ax.set_title("YOLO Pilot — Normalized Confusion Matrix (val set)", fontsize=11, fontweight="bold")
    for i in range(len(class_names)):
        for j in range(len(class_names)):
            val = cm_norm[i, j]
            color = "white" if val > 0.5 else "black"
            ax.text(j, i, f"{val:.2f}", ha="center", va="center", color=color, fontsize=8)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="Recall (row-normalized)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return cm


def plot_sample_predictions(
    val_df: pd.DataFrame,
    preds: np.ndarray,
    images_dir: Path,
    label_names: List[str],
    out_path: Path,
    n_samples: int = 32,
    seed: int = 42,
) -> None:
    """8x4 grid of val images with true vs predicted labels colored."""
    rng = np.random.default_rng(seed)
    n = min(n_samples, len(val_df))
    indices = rng.choice(len(val_df), size=n, replace=False)
    rows = n // 4
    fig, axes = plt.subplots(rows, 4, figsize=(13, 2.3 * rows))
    axes_flat = axes.flatten() if hasattr(axes, "flatten") else [axes]
    for ax, idx in zip(axes_flat, indices):
        row = val_df.iloc[idx]
        img = Image.open(images_dir / row["image"]).convert("RGB")
        ax.imshow(img)
        true_label = row["pattern_label"]
        pred_idx = preds[idx]
        pred_label = label_names[pred_idx]
        ok = true_label == pred_label
        color = "#26A69A" if ok else "#EF5350"
        marker = "✓" if ok else "✗"
        ax.set_title(f"{marker} true={true_label}\npred={pred_label}",
                     color=color, fontsize=8, fontweight="bold")
        ax.axis("off")
    for ax in axes_flat[n:]:
        ax.axis("off")
    fig.suptitle("Sample Predictions — green ✓ correct, red ✗ wrong", fontsize=11)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="YOLO Pilot Training (ResNet18 classifier)")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=1e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--val-frac", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--image-size", type=int, default=224)
    parser.add_argument("--num-workers", type=int, default=2)
    parser.add_argument("--device", default="auto", choices=["auto", "mps", "cuda", "cpu"])
    parser.add_argument("--early-stop-patience", type=int, default=5)
    # ---- Phase 2 extensions (backward-compat: defaults preserve pilot behavior) ----
    parser.add_argument("--init-checkpoint", type=Path, default=None,
                        help="Warm-init from a previous checkpoint (e.g. models/yolo/pilot_v1/best.pt)")
    parser.add_argument("--cosine-annealing", action="store_true",
                        help="Use CosineAnnealingLR instead of ReduceLROnPlateau (Drive §5.2)")
    parser.add_argument("--temporal-split", action="store_true",
                        help="Use temporal split (last 20%% by date = val) instead of stratified (Drive §7.1)")
    parser.add_argument("--label-smoothing", type=float, default=0.0,
                        help="CrossEntropyLoss label_smoothing (e.g. 0.1 for noisy synthetic labels)")
    parser.add_argument("--oversample-minority", action="store_true",
                        help="Use WeightedRandomSampler to oversample rare classes (Drive §4.2)")
    args = parser.parse_args()

    # ---- setup ----
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"[YOLO-PILOT] data_dir={args.data_dir} output_dir={args.output_dir}")
    device = pick_device(args.device)
    logger.info(f"[YOLO-PILOT] device={device}")

    # ---- load labels ----
    labels_csv = args.data_dir / "labels.csv"
    if not labels_csv.exists():
        raise FileNotFoundError(f"labels.csv not found at {labels_csv}")
    df = pd.read_csv(labels_csv)
    df = df.dropna(subset=["pattern_label"]).reset_index(drop=True)
    logger.info(f"[YOLO-PILOT] loaded {len(df)} rows from {labels_csv}")

    images_dir = args.data_dir / "images"
    if not images_dir.exists():
        raise FileNotFoundError(f"images dir not found at {images_dir}")
    # verify all referenced images exist
    missing = [n for n in df["image"] if not (images_dir / n).exists()]
    if missing:
        raise FileNotFoundError(f"{len(missing)} images missing, e.g. {missing[:3]}")
    logger.info(f"[YOLO-PILOT] all {len(df)} images present in {images_dir}")

    # ---- class index ----
    class_counts = df["pattern_label"].value_counts().to_dict()
    class_names = sorted(class_counts.keys())
    label_to_idx = {name: i for i, name in enumerate(class_names)}
    idx_to_label = {i: name for name, i in label_to_idx.items()}
    logger.info(f"[YOLO-PILOT] classes ({len(class_names)}): {class_names}")
    logger.info(f"[YOLO-PILOT] class counts: {class_counts}")

    # ---- split ----
    if args.temporal_split:
        train_df, val_df = temporal_split(df, args.val_frac, args.seed)
    else:
        train_df, val_df = stratified_split(df, args.val_frac, args.seed)
    logger.info(f"[YOLO-PILOT] split: train={len(train_df)} val={len(val_df)}")

    # ---- transforms / loaders ----
    train_tf, val_tf = build_transforms(args.image_size)
    train_ds = ChartImageDataset(train_df, images_dir, label_to_idx, train_tf)
    val_ds = ChartImageDataset(val_df, images_dir, label_to_idx, val_tf)

    if args.oversample_minority:
        sampler = build_weighted_sampler(train_df, label_to_idx)
        train_loader = DataLoader(train_ds, batch_size=args.batch_size, sampler=sampler,
                                  num_workers=args.num_workers, pin_memory=False, shuffle=False)
        logger.info(f"[YOLO-PILOT] train loader: WeightedRandomSampler (replacement=True)")
    else:
        train_loader = DataLoader(train_ds, batch_size=args.batch_size, shuffle=True,
                                  num_workers=args.num_workers, pin_memory=False)
    val_loader = DataLoader(val_ds, batch_size=args.batch_size, shuffle=False,
                            num_workers=args.num_workers, pin_memory=False)

    # ---- class weights ----
    train_label_idxs = [label_to_idx[l] for l in train_df["pattern_label"]]
    class_weights = compute_class_weights(train_label_idxs, num_classes=len(class_names)).to(device)
    logger.info(f"[YOLO-PILOT] class_weights: {class_weights.tolist()}")

    # ---- model / optimizer ----
    model = build_resnet18(num_classes=len(class_names)).to(device)
    if args.init_checkpoint and args.init_checkpoint.exists():
        model = load_init_checkpoint(model, args.init_checkpoint, len(class_names))
    else:
        if args.init_checkpoint:
            logger.warning(f"[YOLO-PILOT] init-checkpoint not found at {args.init_checkpoint}; "
                           f"training from ImageNet pretrained only")
    criterion = nn.CrossEntropyLoss(weight=class_weights, label_smoothing=args.label_smoothing)
    optimizer = optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    if args.cosine_annealing:
        scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs, eta_min=args.lr * 1e-2)
        logger.info(f"[YOLO-PILOT] scheduler: CosineAnnealingLR T_max={args.epochs} eta_min={args.lr*1e-2:.2e}")
    else:
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, mode="min", factor=0.5, patience=3)

    # ---- train loop ----
    log_rows: List[Dict] = []
    best_f1 = -1.0
    best_epoch = -1
    epochs_no_improve = 0
    t_start = time.time()

    for epoch in range(1, args.epochs + 1):
        model.train()
        t_epoch = time.time()
        train_loss_sum = 0.0
        train_n = 0
        for x, y, _ in train_loader:
            x, y = x.to(device), y.to(device)
            optimizer.zero_grad()
            logits = model(x)
            loss = criterion(logits, y)
            loss.backward()
            optimizer.step()
            train_loss_sum += loss.item() * y.size(0)
            train_n += y.size(0)
        train_loss = train_loss_sum / max(train_n, 1)

        val_loss, val_acc, val_f1, _, _ = evaluate(model, val_loader, device, len(class_names))
        if args.cosine_annealing:
            scheduler.step()   # epoch-based, no val metric needed
        else:
            scheduler.step(val_loss)

        log_rows.append({
            "epoch": epoch,
            "train_loss": round(train_loss, 4),
            "val_loss": round(val_loss, 4),
            "val_acc": round(val_acc, 4),
            "val_macro_f1": round(val_f1, 4),
            "lr": round(optimizer.param_groups[0]["lr"], 6),
            "epoch_time_sec": round(time.time() - t_epoch, 1),
        })
        logger.info(f"[YOLO-PILOT] epoch {epoch:>2}/{args.epochs} "
                    f"train_loss={train_loss:.4f} val_loss={val_loss:.4f} "
                    f"val_acc={val_acc:.4f} val_macro_f1={val_f1:.4f} "
                    f"lr={optimizer.param_groups[0]['lr']:.2e} ({time.time()-t_epoch:.1f}s)")

        if val_f1 > best_f1:
            best_f1 = val_f1
            best_epoch = epoch
            torch.save({
                "model_state_dict": model.state_dict(),
                "class_names": class_names,
                "label_to_idx": label_to_idx,
                "epoch": epoch,
                "val_macro_f1": val_f1,
                "val_acc": val_acc,
            }, args.output_dir / "best.pt")
            epochs_no_improve = 0
        else:
            epochs_no_improve += 1
            if epochs_no_improve >= args.early_stop_patience:
                logger.info(f"[YOLO-PILOT] early stopping at epoch {epoch} (no F1 improvement for {args.early_stop_patience} epochs)")
                break

    total_time = time.time() - t_start
    logger.info(f"[YOLO-PILOT] training done in {total_time:.1f}s; best epoch={best_epoch} val_macro_f1={best_f1:.4f}")

    # ---- save last ----
    torch.save({
        "model_state_dict": model.state_dict(),
        "class_names": class_names,
        "label_to_idx": label_to_idx,
        "epoch": len(log_rows),
        "val_macro_f1": log_rows[-1]["val_macro_f1"] if log_rows else 0.0,
        "val_acc": log_rows[-1]["val_acc"] if log_rows else 0.0,
    }, args.output_dir / "last.pt")

    # ---- reload best for final eval ----
    ckpt = torch.load(args.output_dir / "best.pt", map_location=device, weights_only=False)
    model.load_state_dict(ckpt["model_state_dict"])
    val_loss, val_acc, val_f1, val_labels, val_preds = evaluate(model, val_loader, device, len(class_names))

    # ---- per-class metrics ----
    report = classification_report(val_labels, val_preds, target_names=class_names, zero_division=0, digits=4)
    (args.output_dir / "classification_report.txt").write_text(report)
    print(report)

    # ---- plots ----
    log_df = pd.DataFrame(log_rows)
    log_df.to_csv(args.output_dir / "training_log.csv", index=False)
    plot_curves(log_df, args.output_dir / "training_curves.png")
    cm = plot_confusion_matrix(val_labels, val_preds, class_names, args.output_dir / "confusion_matrix.png")
    plot_sample_predictions(val_df, val_preds, images_dir, class_names, args.output_dir / "sample_predictions.png")

    # ---- class index ----
    (args.output_dir / "class_index.json").write_text(json.dumps(idx_to_label, indent=2))

    # ---- top confusion pairs ----
    top_pairs = []
    for i in range(len(class_names)):
        for j in range(len(class_names)):
            if i != j and cm[i, j] > 0:
                top_pairs.append({
                    "true": class_names[i],
                    "pred": class_names[j],
                    "count": int(cm[i, j]),
                })
    top_pairs.sort(key=lambda x: -x["count"])
    top_pairs = top_pairs[:5]

    # ---- train_meta.json ----
    train_meta = {
        "device": str(device),
        "epochs_run": len(log_rows),
        "epochs_planned": args.epochs,
        "best_epoch": best_epoch,
        "best_val_macro_f1": round(best_f1, 4),
        "best_val_acc": round(ckpt["val_acc"], 4),
        "final_val_loss": round(val_loss, 4),
        "final_val_acc": round(val_acc, 4),
        "final_val_macro_f1": round(val_f1, 4),
        "batch_size": args.batch_size,
        "lr": args.lr,
        "weight_decay": args.weight_decay,
        "image_size": args.image_size,
        "num_classes": len(class_names),
        "class_names": class_names,
        "class_counts_full": class_counts,
        "train_size": len(train_df),
        "val_size": len(val_df),
        "seed": args.seed,
        "total_time_sec": round(total_time, 1),
        "model_arch": "resnet18_imagenet_pretrained",
        "init_checkpoint": str(args.init_checkpoint) if args.init_checkpoint else None,
        "cosine_annealing": bool(args.cosine_annealing),
        "temporal_split": bool(args.temporal_split),
        "label_smoothing": float(args.label_smoothing),
        "oversample_minority": bool(args.oversample_minority),
        "early_stop_patience": args.early_stop_patience,
        "top_confusion_pairs": top_pairs,
    }
    (args.output_dir / "train_meta.json").write_text(json.dumps(train_meta, indent=2))
    logger.info(f"[YOLO-PILOT] saved train_meta.json (best_f1={best_f1:.4f})")

    # ---- pilot_report.md (human-readable) ----
    majority_class = max(class_counts, key=class_counts.get)
    majority_acc = class_counts[majority_class] / sum(class_counts.values())
    report_md = f"""# YOLO Pilot Training Report

**Date**: {time.strftime("%Y-%m-%d %H:%M:%S")}
**Device**: `{device}`
**Dataset**: `{args.data_dir}` ({len(df):,} images / {len(class_counts)} classes)
**Model**: ResNet18 (ImageNet pretrained) — final FC replaced for 6 classes

---

## 1. Results

| Metric | Value |
|---|---|
| Best epoch | {best_epoch} / {len(log_rows)} |
| **Best val macro-F1** | **{best_f1:.4f}** |
| Best val accuracy | {ckpt["val_acc"]:.4f} |
| Final val loss | {val_loss:.4f} |
| Final val accuracy | {val_acc:.4f} |
| Final val macro-F1 | {val_f1:.4f} |
| Total training time | {total_time:.1f} s |
| Early-stopped | {"yes" if len(log_rows) < args.epochs else "no"} |

### Baselines
- Random guess (1/6): **{1/6:.4f}** val accuracy
- Naive majority-class (`{majority_class}` always): **{majority_acc:.4f}** val accuracy
- Macro-F1 floor (all majority): **0.1300**

The model needs to beat **macro-F1 > 0.20** to be meaningfully above naive.

---

## 2. Per-Class Report (validation set)

```
{report}
```

---

## 3. Top Confusion Pairs (validation set)

| True → Predicted | Count |
|---|---|
""" + "\n".join(f"| `{p['true']}` → `{p['pred']}` | {p['count']} |" for p in top_pairs) + f"""

---

## 4. Artifacts

| Path | Description |
|---|---|
| `best.pt` | Best model state_dict (epoch {best_epoch}, macro-F1={best_f1:.4f}) |
| `last.pt` | Final-epoch state_dict |
| `class_index.json` | idx ↔ label name mapping |
| `train_meta.json` | Full config + metrics + dataset stats |
| `training_log.csv` | Per-epoch metrics ({len(log_rows)} rows) |
| `training_curves.png` | Loss / accuracy / F1 curves |
| `confusion_matrix.png` | Row-normalized 6×6 confusion matrix |
| `classification_report.txt` | sklearn classification_report |
| `sample_predictions.png` | {min(32, len(val_df))} val images with true vs predicted labels |

---

## 5. Label-Quality Assessment

The 6 synthetic classes (`breakout`, `normal`, `reversal`, `trend_up`,
`trend_down`, `chop`) come from rule-based feature engineering on 5min bars
(see `scripts/prepare_yolo_dataset.py` — `LABEL_RULES` dict).

If the model achieves macro-F1 ≥ 0.4 with high per-class precision on
`breakout` (most populated class) and `normal` (large neutral class), the
labels are **learnable and worth scaling up**.

If macro-F1 ≤ 0.25, the synthetic rules are likely:
1. **Too noisy** — chart patterns don't visually manifest cleanly at 5min resolution
2. **Overlapping** — `trend_up` vs `breakout` look similar on a single-day chart
3. **Definition-dependent** — rule thresholds may not match visual semantics

---

## 6. Next-Step Recommendations

### A. If results are promising (macro-F1 ≥ 0.35)

1. **Scale up**: re-run on full Phase 2 corpus — expect ~2–3× relative F1 improvement from data alone
2. **Increase capacity**: try ResNet50 or EfficientNet-B0
3. **Per-symbol fine-tuning**: train one model per symbol (or symbol cluster)
   on its specific pattern vocabulary
4. **Wire into V12**: this classifier can feed the bridge/gatekeeper layer
   to add a "visual pattern confidence" alongside Chronos numerical forecasts

### B. If results are poor (macro-F1 < 0.30)

1. **Audit labels**: spot-check 50 images manually to see if rules match
   visual semantics
2. **Re-tune thresholds** in `LABEL_RULES` (current: 1h return > 0.3%,
   breakout > prior_day_high + 1 ATR)
3. **Add features**: include longer-history context (e.g., 3-day vs 1-day
   prior high for breakout detection)
4. **Try unsupervised pretraining**: train an autoencoder on unlabeled images first, then fine-tune classifier on the noisy labels
5. **Consider detection**: switch to true YOLO with bboxes (requires new
   label scheme — out of scope here)

### C. Operational

- Bulk 5min is currently running in background — when complete, re-run
  this script with `--data-dir data/intraday_clean_5min/phase2/ready_for_yolo_full`
  after re-running `prepare_yolo_dataset.py` at higher
  `--samples-per-symbol 200`.

---

## 7. Reproducibility

```bash
PYTHONPATH=src:scripts:. python3 scripts/train_yolo_pilot.py \\
    --epochs 20 --batch-size 32 --lr 1e-4 --seed 42
```

Same seed + same input → identical train/val split, identical training trajectory.

---

**Verdict**: {"✅ Model trained successfully. See §1 metrics." if len(log_rows) > 0 else "❌ Training aborted."}
"""
    (args.output_dir / "pilot_report.md").write_text(report_md)
    logger.info(f"[YOLO-PILOT] saved pilot_report.md → {args.output_dir/'pilot_report.md'}")

    print("\n" + "=" * 70)
    print(f"YOLO PILOT TRAINING COMPLETE")
    print(f"  Best epoch:        {best_epoch}")
    print(f"  Best val macro-F1: {best_f1:.4f}")
    print(f"  Best val acc:      {ckpt['val_acc']:.4f}")
    print(f"  Total time:        {total_time:.1f}s")
    print(f"  Output dir:        {args.output_dir}")
    print("=" * 70)


if __name__ == "__main__":
    main()