#!/usr/bin/env python3
"""shadow_halt_decision.py — Round 4 HaltGate paper-trade logger.

Runs AFTER shadow eval cron (16:05/16:10 ET). For each model:
  1. Load all prior per-date JSONs (cron + backfill)
  2. Reconstruct HaltGate state from history
  3. Call should_halt(today) — PAPER TRADE ONLY (no trade skipping)
  4. Append decision to halt_decisions.jsonl

Council Round 4 verdict (2026-07-10):
  - Primary variant: 3d_40_reactive (window=3, threshold=0.40, reactive)
  - Also log: 3d_45_reactive, 3d_40_persistent (for OOS comparison)
  - 60-day OOS gate before any promotion consideration
  - Kill switch: if halt fires >20% of trading days, halt is too aggressive

Usage:
    # Run for all shadow models, today's date:
    python scripts/shadow_halt_decision.py

    # Specific model + date (for backfill replay):
    python scripts/shadow_halt_decision.py --model phase2_v2 --today 2026-07-10

    # Replay full history (generate decisions for all dates):
    python scripts/shadow_halt_decision.py --replay

Output: models/chronos/_shadow/{model}/halt_decisions.jsonl
  One JSON line per (date, model, variant).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

# Phase 5: bootstrap then import (no multi-hop sys.path folklore)
try:
    from stockapp_v12.app.bootstrap import ensure_src_on_path

    ensure_src_on_path()
except ImportError:
    _src = Path(__file__).resolve().parents[1] / "src"
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from stockapp_v12.eval.halt_gate import HaltGate

# ---------------------------------------------------------------------------
# Config — Council Round 4 approved variants
# ---------------------------------------------------------------------------

REPO = Path(__file__).resolve().parents[1]

# Auto-detect shadow dir — handles both layouts:
# - Mac mini: REPO = .../Stockapp/, shadow at REPO/models/chronos/_shadow/
# - NUC:      REPO = .../Stockapp/v12/, shadow at REPO/../models/chronos/_shadow/
_shadow_candidates = [
    REPO / "models" / "chronos" / "_shadow",
    REPO.parent / "models" / "chronos" / "_shadow",
]
SHADOW_DIR = next((p for p in _shadow_candidates if p.exists()), _shadow_candidates[0])

# Active HaltGate allowlist (2026-07-26 ROI review).
# Skips zombie E05 seeds {17,23,71} and zeroshot (not in daily promotion path).
# Override with --model / env HALT_MODELS (space-separated).
DEFAULT_HALT_ALLOWLIST = [
    "phase2_v2",
    "phase2_wf_20260724",  # ADR-010 D3 observation
    "E05_p3_coldstart_seed42",
    "E05_p3_coldstart_seed7",
    "E06_short_ctx",
    "E07_long_ctx",
    "E08_fewer_steps",
    "E09_higher_lr",
    "E10_more_steps",
    "ensemble",
]

# 3 variants approved by Council Round 4 for OOS comparison.
# Primary (for pass/fail decisions): 3d_40_reactive.
COUNCIL_VARIANTS = [
    {
        "name": "3d_40_reactive",
        "window": 3, "threshold": 0.40,
        "persistent": False, "recovery_threshold": 0.50,
        "min_history_days": 3, "min_trades_in_window": 10,
    },
    {
        "name": "3d_45_reactive",
        "window": 3, "threshold": 0.45,
        "persistent": False, "recovery_threshold": 0.50,
        "min_history_days": 3, "min_trades_in_window": 10,
    },
    {
        "name": "3d_40_persistent",
        "window": 3, "threshold": 0.40,
        "persistent": True, "recovery_threshold": 0.50,
        "min_history_days": 3, "min_trades_in_window": 10,
    },
]

SIGNAL_THRESHOLD_PCT = 0.5  # matches simulate_pnl DEFAULT_SIGNAL_THRESHOLD_PCT

_PER_DATE_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"


# ---------------------------------------------------------------------------
# Pure functions (testable)
# ---------------------------------------------------------------------------

def load_per_date_trades(model_dir: Path) -> list[tuple[str, list[dict]]]:
    """Load all per-date JSONs from both cron root and _backfill subdir.

    Returns list of (eval_date_str, rows) sorted by date ascending.
    Deduplicates by date (cron takes priority over backfill if both exist).
    """
    model_dir = Path(model_dir)
    by_date: dict[str, list[dict]] = {}

    # Load backfill first (lower priority)
    backfill_dir = model_dir / "_backfill"
    if backfill_dir.exists():
        for path in sorted(backfill_dir.glob(_PER_DATE_GLOB)):
            try:
                payload = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            d = payload.get("eval_date", path.stem)
            rows = payload.get("rows", [])
            by_date[d] = rows

    # Cron root overrides backfill for same date (higher priority)
    if model_dir.exists():
        for path in sorted(model_dir.glob(_PER_DATE_GLOB)):
            try:
                payload = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            d = payload.get("eval_date", path.stem)
            rows = payload.get("rows", [])
            by_date[d] = rows

    return sorted(by_date.items(), key=lambda x: x[0])


def compute_halt_decision(
    per_date: list[tuple[str, list[dict]]],
    today: str,
    variant_cfg: dict,
) -> dict:
    """Pure function: given history + today, return halt decision dict.

    Args:
      per_date: sorted list of (date_str, rows) from load_per_date_trades.
      today: ISO date string for the decision date.
      variant_cfg: HaltGate config dict (must include "name" key).

    Returns:
      Decision dict with: date, variant, halt, rolling_acc, n_prior_trades,
      reason, n_would_skip_trades, n_would_skip_actionable.
    """
    cfg = {k: v for k, v in variant_cfg.items() if k != "name"}
    gate = HaltGate(**cfg)

    # Replay ALL prior dates through should_halt → update cycle.
    # This is critical for persistent mode: the latch must transition
    # through each prior date's decision, not just accumulate trades.
    for d, rows in per_date:
        if d < today:
            gate.should_halt(d)  # updates persistent latch
            gate.update(d, rows)

    # Decide for today
    should_halt, ctx = gate.should_halt(today)

    # Count what we would skip today (paper-trade accounting)
    today_rows = [rows for d, rows in per_date if d == today]
    today_trades = []
    for rows in today_rows:
        today_trades.extend(rows)

    n_would_skip = len(today_trades)
    # Actionable = signal strong enough that simulate_pnl would take the trade.
    # Only checks |forecast| > threshold — actual_ret_pct not needed for this
    # count (today's actuals may be unsettled at decision time).
    n_would_skip_actionable = sum(
        1 for t in today_trades
        if abs(t.get("forecast_ret_pct", 0)) > SIGNAL_THRESHOLD_PCT
    )

    return {
        "date": today,
        "variant": variant_cfg["name"],
        "halt": should_halt,
        "rolling_acc": ctx["rolling_acc"],
        "n_prior_trades": ctx["n_prior_trades"],
        "reason": ctx["reason"],
        "n_would_skip_trades": n_would_skip,
        "n_would_skip_actionable": n_would_skip_actionable,
    }


def append_decision(
    model_name: str,
    decision: dict,
    model_dir: Path,
) -> Path:
    """Append a halt decision to model's halt_decisions.jsonl.

    Creates file if it doesn't exist. Does NOT deduplicate — caller
    is responsible for not double-logging same (date, variant).

    Returns path to the JSONL file written.
    """
    jsonl_path = model_dir / "halt_decisions.jsonl"
    record = {
        **decision,
        "model": model_name,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(jsonl_path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    return jsonl_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_for_model(
    model_name: str,
    today: str | None = None,
    replay: bool = False,
) -> int:
    """Run halt decision for one model. Returns number of decisions written.

    If replay=True, generates decisions for ALL dates in history (useful
    for backfilling the halt_decisions.jsonl from existing per-date JSONs).
    """
    model_dir = SHADOW_DIR / model_name
    if not model_dir.exists():
        print(f"  SKIP {model_name}: {model_dir} not found", file=sys.stderr)
        return 0

    per_date = load_per_date_trades(model_dir)
    if not per_date:
        print(f"  SKIP {model_name}: no per-date JSONs found", file=sys.stderr)
        return 0

    if today is None:
        today = date.today().isoformat()

    dates_to_eval = per_date if replay else [(today, [])]
    # For non-replay, filter per_date to just prior dates
    # (compute_halt_decision already filters d < today)

    n_written = 0
    for variant_cfg in COUNCIL_VARIANTS:
        if replay:
            for d, _ in per_date:
                decision = compute_halt_decision(per_date, d, variant_cfg)
                append_decision(model_name, decision, model_dir)
                n_written += 1
        else:
            decision = compute_halt_decision(per_date, today, variant_cfg)
            append_decision(model_name, decision, model_dir)
            n_written += 1

    return n_written


def main():
    parser = argparse.ArgumentParser(
        description="Round 4 HaltGate paper-trade logger"
    )
    parser.add_argument(
        "--model", default=None,
        help="Model name (e.g., phase2_v2). If omitted, run for all shadow models."
    )
    parser.add_argument(
        "--today", default=None,
        help="ISO date string (default: today). Used for single-date decision."
    )
    parser.add_argument(
        "--replay", action="store_true",
        help="Replay all historical dates (backfill halt_decisions.jsonl)."
    )
    parser.add_argument(
        "--shadow-dir", default=None,
        help="Override shadow models dir (default: auto-detect from script location)."
    )
    parser.add_argument(
        "--list-models", action="store_true",
        help="List available shadow models and exit."
    )
    args = parser.parse_args()

    # Allow CLI override of shadow dir
    global SHADOW_DIR
    if args.shadow_dir:
        SHADOW_DIR = Path(args.shadow_dir)

    if args.list_models:
        if not SHADOW_DIR.exists():
            print(f"Shadow dir not found: {SHADOW_DIR}")
            return
        for d in sorted(SHADOW_DIR.iterdir()):
            if d.is_dir() and not d.name.startswith("_"):
                print(d.name)
        return

    if args.model:
        models = [args.model]
    else:
        if not SHADOW_DIR.exists():
            print(f"Shadow dir not found: {SHADOW_DIR}", file=sys.stderr)
            sys.exit(1)
        import os
        env_models = os.environ.get("HALT_MODELS", "").strip()
        if env_models:
            models = [m for m in env_models.split() if m]
        else:
            available = {
                d.name for d in SHADOW_DIR.iterdir()
                if d.is_dir() and not d.name.startswith("_")
            }
            models = [m for m in DEFAULT_HALT_ALLOWLIST if m in available]
            skipped = sorted(available - set(models))
            if skipped:
                print(f"  Skipping non-active models: {skipped}")

    today_str = args.today or date.today().isoformat()
    mode = "REPLAY" if args.replay else "SINGLE-DATE"
    print(f"\n=== HaltGate paper-trade logger [{mode}] ===")
    print(f"  Date: {today_str}")
    print(f"  Models: {models}")
    print(f"  Variants: {[v['name'] for v in COUNCIL_VARIANTS]}")
    print()

    total = 0
    for model_name in models:
        n = run_for_model(model_name, today_str, args.replay)
        total += n
        print(f"  {model_name}: {n} decisions written")

    print(f"\nTotal: {total} decisions written")
    print("Paper-trade only — no trades were skipped.")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Task 8 — Champion swap on halt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HaltOutcome:
    """Whether the champion model should trigger a halt swap."""
    halted: bool
    recent_accuracy: float | None
    decision_date: str | None


def halt_outcome_for_champion(
    decisions_path: Path,
    champion_name: str,
) -> HaltOutcome:
    """Inspect halt_decisions.jsonl for the champion model.

    Returns HaltOutcome with the most recent decision for this champion.
    """
    if not decisions_path.exists():
        return HaltOutcome(halted=False, recent_accuracy=None, decision_date=None)
    latest: dict | None = None
    with decisions_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("model") != champion_name:
                continue
            if latest is None or entry.get("date", "") > latest.get("date", ""):
                latest = entry
    if latest is None:
        return HaltOutcome(halted=False, recent_accuracy=None, decision_date=None)
    return HaltOutcome(
        halted=bool(latest.get("should_halt", False)),
        recent_accuracy=latest.get("rolling_acc"),
        decision_date=latest.get("date"),
    )