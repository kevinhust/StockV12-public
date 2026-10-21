#!/usr/bin/env python3
"""ADR-010 D3 — monthly same-recipe walk-forward retrain of phase2_v2.

Reads config/chronos_phase2_v2_recipe.yaml (GPU-verified) and trains a NEW
checkpoint without overwriting models/chronos/phase2_v2.

Usage:
  # Dry-run (print plan only)
  PYTHONPATH=src:scripts:. python scripts/retrain_phase2_vn.py --dry-run

  # Full retrain on GPU (recommended host)
  PYTHONPATH=src:scripts:. python scripts/retrain_phase2_vn.py \\
      --cutoff 2026-07-24 --device cuda

  # Custom name
  PYTHONPATH=src:scripts:. python scripts/retrain_phase2_vn.py \\
      --cutoff 2026-07-24 --name phase2_wf_20260724 --device cuda

Default output dir:
  models/chronos/phase2_wf_<cutoff YYYYMMDD>/
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml

REPO = Path(__file__).resolve().parents[1]
DEFAULT_RECIPE = REPO / "config" / "chronos_phase2_v2_recipe.yaml"
DEFAULT_FEATURES = REPO / "data" / "features" / "daily"
DEFAULT_MODELS_ROOT = REPO / "models" / "chronos"
# Live baseline — NEVER write here.
PROTECTED_BASELINE = "phase2_v2"


def load_recipe(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"recipe must be a mapping: {path}")
    hp = raw.get("hyperparameters") or {}
    required = [
        "fine_tune_steps",
        "fine_tune_lr",
        "fine_tune_batch_size",
        "context_length",
        "prediction_length",
    ]
    missing = [k for k in required if k not in hp]
    if missing:
        raise ValueError(f"recipe missing hyperparameters: {missing}")
    return raw


def parse_cutoff(s: str) -> date:
    return date.fromisoformat(s)


def previous_trading_day(d: date) -> date:
    cur = d - timedelta(days=1)
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur


def default_cutoff_from_features(features_dir: Path, probe: str = "AAPL") -> date:
    """Use last bar date of a probe symbol as retrain cutoff."""
    p = features_dir / f"{probe}.parquet"
    if not p.exists():
        # fall back to previous weekday from today
        return previous_trading_day(date.today())
    df = pd.read_parquet(p)
    ts = pd.to_datetime(df["timestamp"]).max()
    if getattr(ts, "tzinfo", None) is not None or (hasattr(ts, "tz") and ts.tz is not None):
        try:
            ts = ts.tz_convert("America/New_York")
        except Exception:
            pass
    return ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])


def output_dir_name(cutoff: date, name: Optional[str] = None) -> str:
    if name:
        if name == PROTECTED_BASELINE:
            raise ValueError(f"refusing to overwrite protected baseline {PROTECTED_BASELINE!r}")
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", name):
            raise ValueError(f"invalid model name: {name!r}")
        return name
    return f"phase2_wf_{cutoff.strftime('%Y%m%d')}"


def filter_features_by_cutoff(df: pd.DataFrame, cutoff: date) -> pd.DataFrame:
    """Keep rows with timestamp date <= cutoff (tz-aware / mixed OK)."""
    # Mixed EST/EDT object columns need utc=True then convert to NY.
    ts = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    dates = ts.dt.tz_convert("America/New_York").dt.date
    return df.loc[dates <= cutoff].copy()


def build_plan(
    recipe: dict[str, Any],
    cutoff: date,
    name: Optional[str],
    features_dir: Path,
    models_root: Path,
) -> dict[str, Any]:
    hp = recipe["hyperparameters"]
    out_name = output_dir_name(cutoff, name)
    out_dir = models_root / out_name
    return {
        "recipe_name": recipe.get("recipe_name", "phase2_v2"),
        "cutoff": cutoff.isoformat(),
        "out_name": out_name,
        "out_dir": str(out_dir),
        "protected_baseline": PROTECTED_BASELINE,
        "features_dir": str(features_dir),
        "hyperparameters": {
            "fine_tune_steps": int(hp["fine_tune_steps"]),
            "fine_tune_lr": float(hp["fine_tune_lr"]),
            "fine_tune_batch_size": int(hp["fine_tune_batch_size"]),
            "context_length": int(hp["context_length"]),
            "prediction_length": int(hp["prediction_length"]),
            "base_model": recipe.get("backbone", "autogluon/chronos-bolt-small"),
        },
        "n_symbols_recipe": (recipe.get("data") or {}).get("n_symbols"),
        "out_exists": out_dir.exists(),
    }


def _load_all_feature_frames(features_dir: Path) -> pd.DataFrame:
    """Load all daily feature parquets (symbol + timestamp + close + ...)."""
    paths = sorted(features_dir.glob("*.parquet"))
    if not paths:
        raise RuntimeError(f"No features found in {features_dir}")
    dfs = []
    for path in paths:
        df = pd.read_parquet(path)
        if "symbol" not in df.columns:
            df = df.copy()
            df["symbol"] = path.stem
        dfs.append(df)
    full = pd.concat(dfs, ignore_index=True)
    return full.sort_values(["symbol", "timestamp"]).reset_index(drop=True)


def run_train(plan: dict[str, Any], features_dir: Path, seed: int = 42, device: str = "auto") -> Path:
    """Execute fine-tune using train_chronos_baseline helpers."""
    import os

    # Local import so --dry-run works without AutoGluon.
    from scripts.train_chronos_baseline import (  # noqa: WPS433
        build_tsdf,
        evaluate_sample,
        train_finetune,
        write_report,
    )

    out_dir = Path(plan["out_dir"])
    if out_dir.name == PROTECTED_BASELINE:
        raise RuntimeError("refusing to write protected baseline")

    raw = _load_all_feature_frames(features_dir)
    n_before = len(raw)
    cutoff = parse_cutoff(plan["cutoff"])
    raw = filter_features_by_cutoff(raw, cutoff)
    n_after = len(raw)
    print(f"[data] rows {n_before} → {n_after} after cutoff {cutoff}")
    if n_after == 0:
        raise RuntimeError(f"no feature rows on/before cutoff {cutoff}")

    raw = raw.rename(columns={"symbol": "item_id"})
    # AutoGluon requires tz-naive timestamps; mixed EST/EDT object cols need utc first.
    raw["timestamp"] = (
        pd.to_datetime(raw["timestamp"], utc=True, errors="coerce")
        .dt.tz_convert("America/New_York")
        .dt.tz_localize(None)
    )
    tsdf = build_tsdf(raw, target_col="close")
    symbols_actual = sorted(raw["item_id"].unique().tolist())
    print(f"[data] items={tsdf.num_items} symbols={len(symbols_actual)}")

    hp = plan["hyperparameters"]
    if device == "cuda":
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")
    elif device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""

    base_model = str(hp["base_model"])
    if base_model in {"bolt_small", "chronos-bolt-small"}:
        base_model = "autogluon/chronos-bolt-small"

    out_dir.mkdir(parents=True, exist_ok=True)
    predictor, elapsed = train_finetune(
        tsdf,
        prediction_length=hp["prediction_length"],
        out_dir=out_dir,
        steps=hp["fine_tune_steps"],
        batch_size=hp["fine_tune_batch_size"],
        lr=hp["fine_tune_lr"],
        context_length=hp["context_length"],
        seed=seed,
        base_model=base_model,
    )

    meta = {
        "mode": "finetune",
        "recipe": plan["recipe_name"],
        "retrain_cutoff": plan["cutoff"],
        "out_name": plan["out_name"],
        "adr": "ADR-010 D3",
        "symbols": symbols_actual,
        "n_symbols": len(symbols_actual),
        "prediction_length": hp["prediction_length"],
        "context_length": hp["context_length"],
        "steps": hp["fine_tune_steps"],
        "batch_size": hp["fine_tune_batch_size"],
        "lr": hp["fine_tune_lr"],
        "seed": seed,
        "base_model": base_model,
        "elapsed_seconds": elapsed,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "rows_before_cutoff_filter": n_before,
        "rows_after_cutoff_filter": n_after,
    }
    (out_dir / "train_meta.json").write_text(json.dumps(meta, indent=2))
    (out_dir / "retrain_plan.json").write_text(json.dumps(plan, indent=2, default=str))

    try:
        evaluate_sample(predictor, tsdf, symbols_actual, out_dir)
    except Exception as e:
        print(f"Sample eval failed (non-fatal): {e}")
    try:
        write_report(predictor, "finetune", elapsed, symbols_actual, out_dir)
    except Exception as e:
        print(f"Report failed (non-fatal): {e}")

    print(f"[done] wrote {out_dir}")
    return out_dir


def main() -> int:
    p = argparse.ArgumentParser(description="ADR-010 D3 phase2 walk-forward retrain")
    p.add_argument("--recipe", type=Path, default=DEFAULT_RECIPE)
    p.add_argument("--cutoff", type=str, default=None, help="YYYY-MM-DD inclusive train end")
    p.add_argument("--name", type=str, default=None, help="output model basename")
    p.add_argument("--features-dir", type=Path, default=DEFAULT_FEATURES)
    p.add_argument("--models-root", type=Path, default=DEFAULT_MODELS_ROOT)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--device", choices=["auto", "cuda", "cpu", "mps"], default="auto")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="allow overwrite of existing out dir")
    args = p.parse_args()

    if not args.recipe.exists():
        print(f"ERROR: recipe not found: {args.recipe}", file=sys.stderr)
        return 2
    recipe = load_recipe(args.recipe)

    if args.cutoff:
        cutoff = parse_cutoff(args.cutoff)
    else:
        cutoff = default_cutoff_from_features(args.features_dir)
        print(f"[cutoff] defaulted from features → {cutoff}")

    try:
        plan = build_plan(
            recipe, cutoff, args.name, args.features_dir, args.models_root
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(json.dumps(plan, indent=2))

    if plan["out_name"] == PROTECTED_BASELINE:
        print("ERROR: refusing to overwrite phase2_v2", file=sys.stderr)
        return 2

    if args.dry_run:
        print("[dry-run] no training")
        return 0

    if plan["out_exists"] and not args.force:
        print(
            f"ERROR: {plan['out_dir']} exists (pass --force to overwrite)",
            file=sys.stderr,
        )
        return 2

    if not args.features_dir.exists():
        print(f"ERROR: features dir missing: {args.features_dir}", file=sys.stderr)
        return 2

    try:
        run_train(plan, args.features_dir, seed=args.seed, device=args.device)
    except Exception as e:
        print(f"ERROR: training failed: {e}", file=sys.stderr)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
