#!/usr/bin/env python3
"""
backfill_30_days.py — synthetic walk-forward backfill over the past N trading
days for the v12 shadow pipeline.

Why this exists (Round 2 closure / ADR-008 followup):
  The cron-driven shadow trade accumulates ~1 trading day per weekday.
  Reaching n_dates=30 takes ~6 calendar weeks. To expedite the Day-30
  promotion gate, we INFERENCE-ONLY replay the past N trading days
  using the existing trained models (no retraining). Per (model, date,
  symbol) prediction is ~3–5 s on RTX 4090; 30 dates × 10 symbols × 2
  models ≈ 30–50 min wall-clock.

Architecture:
  - Predictor is loaded ONCE per model and reused across all 30 dates
    (loading AG Chronos takes ~30 s).
  - For each `today` (= eval_date + 1 trading day), run
    run_shadow_for_model(cutoffs=[1]) so the eval_date matches `today - 1`.
  - Outputs go to `_shadow/<model>/_backfill/<eval_date>.json` (separate
    subdir so cron `_shadow/<model>/<date>.json` and synthetic backfill
    don't collide).
  - Each row gets `eval_source: "backfill"` via aggregate_all_per_date_jsons.
  - Dates already covered by live cron are skipped (we don't double-count).

Eval-date selection rules:
  - If --end-date is given: latest eval_date is --end-date (inclusive).
    Must be a weekday; cron shouldn't have covered it.
  - Otherwise: auto-detect as (earliest cron date - 1 trading day).
  - Walk backward --n-days weekdays from end-date.
  - If today (== eval_date+1) falls on a weekend, skip and continue
    (eval_date itself on a weekend is an error).

Usage:
  # Auto-detect window: pick up at the day before the earliest cron data,
  # walk back 30 trading days.
  PYTHONPATH=src:scripts:. python3 scripts/backfill_30_days.py \\
      --models phase2_v2 E05_p3_coldstart_seed42 \\
      --n-days 30 \\
      --symbols AAPL NVDA TSLA JPM MSFT AMZN AMD META GOOGL SPY

  # Explicit window (e.g., to backfill before the cron even started):
  PYTHONPATH=src:scripts:. python3 scripts/backfill_30_days.py \\
      --models phase2_v2 \\
      --end-date 2026-06-23 \\
      --n-days 30 \\
      --symbols AAPL NVDA TSLA JPM MSFT AMZN AMD META GOOGL SPY

Notes:
  - Each model is loaded once. Inference uses the SAME model the cron
    uses (chronos-bolt-small base + fine-tuned checkpoint for seeded
    models). No retraining.
  - Cuts are inference-only: rf__fine_tune_steps=0 implicitly because
    we never call fit(). New evidence comes ONLY from the source files.
  - To verify the gate on live-only data: run shadow_trade.py --status
    after the backfill completes; the output now shows n_dates_by_source
    so you can isolate the cron slice.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

# Reuse shadow_trade helpers + model loader.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from scripts.eval_chronos_h2h_v3 import load_model  # noqa: E402
from scripts.shadow_trade import (  # noqa: E402
    DEFAULT_SIGNAL_THRESHOLD_PCT,
    DEFAULT_TEST_SYMBOLS,
    SHADOW_ROOT,
    aggregate_all_per_date_jsons,
    check_shadow_gate,
    run_shadow_for_model,
    write_shadow_artifacts,
)


def trading_days_before(end_date: date, n_days: int) -> list[date]:
    """Return up to `n_days` trading days STRICTLY BEFORE end_date,
    in DESCENDING order (newest first). Both end_date and n_days are
    exclusive so callers can compose cleanly.

    Mon=0..Fri=4 are trading days; Sat/Sun are skipped.
    """
    days: list[date] = []
    cur = end_date - timedelta(days=1)
    while len(days) < n_days:
        if cur.weekday() < 5:
            days.append(cur)
        cur = cur - timedelta(days=1)
    return days


def find_earliest_cron_date(model_dir: Path) -> Optional[date]:
    """Return the earliest YYYY-MM-DD.json date in model_dir, or None."""
    if not model_dir.exists():
        return None
    dates: list[date] = []
    for path in sorted(model_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json")):
        try:
            d = date.fromisoformat(path.stem)
            dates.append(d)
        except ValueError:
            continue
    return min(dates) if dates else None


def is_weekday(d: date) -> bool:
    return d.weekday() < 5


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--models", nargs="+", required=True,
        help="Model dir basenames (same as shadow_trade.py --models)",
    )
    p.add_argument(
        "--symbols", nargs="+", default=DEFAULT_TEST_SYMBOLS,
        help=f"Test symbols (default: {DEFAULT_TEST_SYMBOLS})",
    )
    p.add_argument(
        "--n-days", type=int, default=30,
        help="Number of trading days to backfill (default: 30, per ADR-008 gate)",
    )
    p.add_argument(
        "--end-date", type=str, default=None,
        help=(
            "Latest eval_date (YYYY-MM-DD) to backfill. Default: auto-detect "
            "as (earliest cron date in any model dir) - 1 day."
        ),
    )
    p.add_argument(
        "--threshold", type=float, default=DEFAULT_SIGNAL_THRESHOLD_PCT,
        help="Signal threshold %% (default: 0.5)",
    )
    args = p.parse_args()

    # Resolve end_date (the LATEST eval_date to backfill)
    if args.end_date:
        end_eval_date = date.fromisoformat(args.end_date)
        if not is_weekday(end_eval_date):
            print(
                f"ERROR: --end-date {end_eval_date} is not a weekday",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        # Auto-detect from earliest cron date across requested models.
        earliest: Optional[date] = None
        for m in args.models:
            d = find_earliest_cron_date(SHADOW_ROOT / m)
            if d is not None and (earliest is None or d < earliest):
                earliest = d
        if earliest is None:
            print(
                "ERROR: no cron data found in any model dir — cannot auto-detect "
                "--end-date. Provide --end-date explicitly.",
                file=sys.stderr,
            )
            sys.exit(2)
        end_eval_date = earliest - timedelta(days=1)
        # Walk forward to next weekday if necessary
        while not is_weekday(end_eval_date):
            end_eval_date = end_eval_date - timedelta(days=1)
        print(
            f"[auto-detect] earliest cron date={earliest.isoformat()}, "
            f"end_eval_date={end_eval_date.isoformat()}",
            file=sys.stderr,
        )

    eval_dates_desc = trading_days_before(end_eval_date, args.n_days)
    print(
        f"[config] backfill {len(eval_dates_desc)} trading days: "
        f"{eval_dates_desc[-1].isoformat()}..{eval_dates_desc[0].isoformat()} "
        f"(end={end_eval_date.isoformat()})",
        file=sys.stderr,
    )

    chronos_root = Path(__file__).resolve().parents[1] / "models" / "chronos"
    by_model: dict[str, dict] = {}

    for m in args.models:
        model_dir = SHADOW_ROOT / m
        backfill_dir = model_dir / "_backfill"
        backfill_dir.mkdir(parents=True, exist_ok=True)

        # Skip dates already covered by cron (defensive: --end-date should
        # already pre-date cron, but operators may pass overlapping windows).
        eval_dates = []
        for e in eval_dates_desc:
            cron_path = model_dir / f"{e.isoformat()}.json"
            backfill_path = backfill_dir / f"{e.isoformat()}.json"
            if cron_path.exists():
                print(
                    f"  [{m}] skip {e.isoformat()} (covered by cron)",
                    file=sys.stderr,
                )
                continue
            if backfill_path.exists():
                print(
                    f"  [{m}] skip {e.isoformat()} (already backfilled)",
                    file=sys.stderr,
                )
                continue
            eval_dates.append(e)
        if not eval_dates:
            print(f"[{m}] nothing to backfill", file=sys.stderr)
            continue

        # Load predictor ONCE for this model; reuse across all dates.
        pdir = chronos_root / m
        if not pdir.exists():
            print(f"ERROR: model dir {pdir} does not exist", file=sys.stderr)
            sys.exit(2)
        print(f"\n[load] {m} from {pdir}", file=sys.stderr)
        predictor = load_model(pdir)

        # Loop from oldest to newest (less surprising than desc order)
        # Pass eval_dates (true walk-forward path) so each (model, symbol,
        # eval_date) tuple gets a distinct data slice. The `today` arg is
        # still required by the signature but unused in this mode.
        for e in sorted(eval_dates):
            today = e + timedelta(days=1)
            if not is_weekday(today):
                # Today falls on a weekend → cron likely wouldn't have
                # fired anyway, but our inference target still works.
                # We just verify weekend today doesn't crash anything.
                pass
            print(
                f"  [{m}] eval_date={e.isoformat()} (today={today.isoformat()})",
                file=sys.stderr,
            )
            result = run_shadow_for_model(
                predictor, m, args.symbols, cutoffs=[1], today=today,
                threshold=args.threshold,
                eval_dates=[e],
            )
            # Build the per-date JSON ourselves, written to _backfill/.
            # We CANNOT call write_shadow_artifacts here because that
            # writes to model_dir directly (cron territory) and rebuilds
            # SUMMARY.json from accumulated data — we want to write a
            # single backfill day without rebuilding SUMMARY until the end.
            for eval_date, rows in result["per_date"].items():
                for r in rows:
                    r.setdefault("eval_date", eval_date)
                    r["eval_source"] = "backfill"
                out_path = backfill_dir / f"{eval_date}.json"
                out_path.write_text(json.dumps({
                    "model": m,
                    "eval_date": eval_date,
                    "rows": rows,
                    "source": "backfill",
                }, indent=2, default=str))
                print(f"    wrote {out_path}", file=sys.stderr)

        by_model[m] = {
            "n_backfill_dates": len(eval_dates),
            "date_range": [
                min(eval_dates).isoformat(),
                max(eval_dates).isoformat(),
            ] if eval_dates else None,
        }

    # Rebuild SUMMARY.json for each model via the standard write path
    # (now that _backfill/ is populated, this will accumulate both cron
    # AND backfill rows correctly).
    for m in args.models:
        # Use write_shadow_artifacts with a stub result that adds NO new
        # per-date JSON (the backfill writes already happened). We do this
        # so SUMMARY.json accumulates properly without re-running models.
        from datetime import datetime
        summary_path = SHADOW_ROOT / m / "SUMMARY.json"
        if not summary_path.exists():
            # Nothing to accumulate yet
            print(f"[skip SUMMARY] {m}: no SUMMARY.json exists yet", file=sys.stderr)
            continue
        existing = json.loads(summary_path.read_text())
        acc = aggregate_all_per_date_jsons(
            SHADOW_ROOT / m, threshold=args.threshold,
            source_filter=None,
        )
        gate = check_shadow_gate(acc["aggregate"])
        new_summary = {
            "model": m,
            "today": existing.get("today", date.today().isoformat()),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "current_run_aggregate": existing.get("current_run_aggregate"),
            "accumulated": acc,
            "promotion_gate": gate,
        }
        summary_path.write_text(json.dumps(new_summary, indent=2, default=str))
        print(
            f"[SUMMARY] {m}: n_dates={acc['n_dates']} "
            f"(cron={acc['n_dates_by_source']['cron']}, "
            f"backfill={acc['n_dates_by_source']['backfill']}) "
            f"n_rows={acc['aggregate']['n_rows']} "
            f"gate={'PASS' if gate['cleared'] else 'FAIL'}",
            file=sys.stderr,
        )

    print("\n[done] backfill complete", file=sys.stderr)


if __name__ == "__main__":
    main()
