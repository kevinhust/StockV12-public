#!/usr/bin/env python3
"""Champion evaluation CLI — compute metrics across shadow models.

Usage:
    PYTHONPATH=src:scripts:. python scripts/champion_evaluation.py --report
    PYTHONPATH=src:scripts:. python scripts/champion_evaluation.py --champion E07_long_ctx
    PYTHONPATH=src:scripts:. python scripts/champion_evaluation.py --multi-window
    PYTHONPATH=src:scripts:. python scripts/champion_evaluation.py --multi-window \\
        --min-trades 15 --out docs/reviews/e-eval-YYYY-MM-DD.json

Reads _shadow/<model>/*.json and _shadow/<model>/_backfill/*.json,
computes the 8-gate metrics, prints a leaderboard table.

ADR-010 E-EVAL: multi-window (full / last 10 / last 20 trading dates) with
n_trades floor — models below floor are marked insufficient and excluded
from rankings.

Spec: docs/superpowers/specs/2026-07-10-champion-model-selection-design.md
      docs/ADR-010-Post30Day-Training-and-Observation-Plan.md
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Phase 5: single bootstrap (no ad-hoc sys.path folklore)
try:
    from stockapp_v12.app.bootstrap import ensure_src_on_path

    ensure_src_on_path()
except ImportError:
    # Editable install not present — fall back once
    _root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_root / "src"))
    sys.path.insert(0, str(_root / "scripts"))

from scripts.shadow_trade import SHADOW_ROOT  # noqa: E402

try:
    from stockapp_v12.eval.champion_selector import (  # noqa: E402
        ModelMetrics,
        check_promotion_gates,
        select_champion,
        select_challenger,
    )
    _HAS_SELECTOR = True
except ImportError:  # NUC minimal tree may lack champion_selector
    ModelMetrics = None  # type: ignore[misc, assignment]
    check_promotion_gates = None  # type: ignore[assignment]
    select_champion = None  # type: ignore[assignment]
    select_challenger = None  # type: ignore[assignment]
    _HAS_SELECTOR = False

THRESHOLD_PCT = 0.5  # same as shadow_trade default

# ADR-010 D5 observation set (daily cron); zeroshot excluded.
OBSERVATION_MODELS: list[str] = [
    "phase2_v2",
    "phase2_wf_20260724",  # ADR-010 D3 WFO retrain — observation only
    "E05_p3_coldstart_seed42",
    "E05_p3_coldstart_seed7",
    "E06_short_ctx",
    "E07_long_ctx",
    "E08_fewer_steps",
    "E09_higher_lr",
    "E10_more_steps",
]

# ADR-010: default floor for rankable windows (20d / 10d / full).
DEFAULT_MIN_TRADES = 15


def _is_actionable(forecast: float) -> bool:
    return abs(forecast) > THRESHOLD_PCT


def compute_metrics_for_model(
    name: str,
    rows: list[dict],
    hyperparam_family: str,
):
    """Compute ModelMetrics from a flat list of rows across dates.

    Returns None if no actionable trades or selector module unavailable.
    """
    if not _HAS_SELECTOR or ModelMetrics is None:
        return None
    daily_pnls: dict[str, float] = {}
    total_pnl = 0.0
    n_trades = 0
    n_wins = 0
    n_dates = 0
    mase_values: list[float] = []

    # Group by eval_date for daily aggregation
    by_date: dict[str, list[dict]] = {}
    for r in rows:
        d = r.get("eval_date", "")
        by_date.setdefault(d, []).append(r)

    for date, date_rows in by_date.items():
        daily_pnl = 0.0
        date_trades = 0
        for r in date_rows:
            f = r.get("forecast_ret_pct")
            a = r.get("actual_ret_pct")
            if f is None or a is None:
                continue
            f = float(f)
            a = float(a)
            if not _is_actionable(f):
                continue
            pnl = (1.0 if f > 0 else -1.0) * a
            daily_pnl += pnl
            date_trades += 1
            total_pnl += pnl
            n_trades += 1
            if pnl > 0:
                n_wins += 1
            mase = r.get("mase")
            if mase is not None and not math.isnan(float(mase)):
                mase_values.append(float(mase))
        if date_trades > 0:
            daily_pnls[date] = daily_pnl
            n_dates += 1

    if n_trades == 0:
        return None

    win_rate = n_wins / n_trades
    sharpe_30d = (
        statistics.mean(daily_pnls.values()) / statistics.pstdev(daily_pnls.values())
        if len(daily_pnls) > 1 and statistics.pstdev(daily_pnls.values()) > 0
        else 0.0
    )
    sharpe_7d_live = _compute_sharpe_last_n_days(daily_pnls, n=7)
    mase_30d = statistics.mean(mase_values) if mase_values else 99.0

    return ModelMetrics(
        name=name,
        pnl_30d=total_pnl,
        sharpe_30d=sharpe_30d,
        sharpe_7d_live=sharpe_7d_live,
        win_rate_30d=win_rate,
        mase_30d=mase_30d,
        n_dates_30d=n_dates,
        n_trades_30d=n_trades,
        hyperparam_family=hyperparam_family,
    )


def _compute_sharpe_last_n_days(daily_pnls: dict[str, float], n: int) -> float:
    """Compute Sharpe over the last n dates (sorted descending)."""
    sorted_dates = sorted(daily_pnls.keys(), reverse=True)[:n]
    if len(sorted_dates) < 2:
        return 0.0
    vals = [daily_pnls[d] for d in sorted_dates]
    pstdev = statistics.pstdev(vals)
    if pstdev == 0:
        return 0.0
    return statistics.mean(vals) / pstdev


def load_model_rows(model_dir: Path) -> list[dict]:
    """Load all per-date rows (cron root + _backfill) for one model dir."""
    all_rows: list[dict] = []
    if not model_dir.exists():
        return all_rows
    for json_path in sorted(model_dir.glob("2*.json")):
        try:
            data = json.loads(json_path.read_text())
            rows = data.get("rows", [])
            eval_date = data.get("eval_date") or json_path.stem
            for r in rows:
                r.setdefault("eval_date", eval_date)
            all_rows.extend(rows)
        except Exception:
            continue
    backfill = model_dir / "_backfill"
    if backfill.is_dir():
        for json_path in sorted(backfill.glob("2*.json")):
            try:
                data = json.loads(json_path.read_text())
                rows = data.get("rows", [])
                eval_date = data.get("eval_date") or json_path.stem
                for r in rows:
                    r.setdefault("eval_date", eval_date)
                all_rows.extend(rows)
            except Exception:
                continue
    return all_rows


def load_all_metrics(
    shadow_root: Path,
    model_dirs: list[str],
    hyperparam_family_map: dict[str, str],
) -> list[ModelMetrics]:
    """Read all shadow JSONs for each model and compute metrics."""
    results: list[ModelMetrics] = []
    for m in model_dirs:
        all_rows = load_model_rows(shadow_root / m)
        if not all_rows:
            continue
        metrics = compute_metrics_for_model(
            m, all_rows, hyperparam_family_map.get(m, "unknown")
        )
        if metrics:
            results.append(metrics)
    return results


def filter_rows_last_n_dates(
    rows: list[dict],
    n_dates: Optional[int],
) -> list[dict]:
    """Keep rows whose eval_date is among the last n distinct dates.

    If n_dates is None, return all rows (full window).
    """
    if n_dates is None:
        return list(rows)
    if n_dates <= 0:
        return []
    dates = sorted({r.get("eval_date", "") for r in rows if r.get("eval_date")})
    if not dates:
        return []
    keep = set(dates[-n_dates:])
    return [r for r in rows if r.get("eval_date") in keep]


def compute_window_snapshot(
    rows: list[dict],
    min_trades: int = DEFAULT_MIN_TRADES,
    threshold_pct: float = THRESHOLD_PCT,
) -> dict[str, Any]:
    """Aggregate sim metrics for one window; mark insufficient if n_trades < floor."""
    n_trades = 0
    n_wins = 0
    total_pnl = 0.0
    n_pairs = 0
    n_hit = 0
    mase_values: list[float] = []
    dates: set[str] = set()

    for r in rows:
        f = r.get("forecast_ret_pct")
        a = r.get("actual_ret_pct")
        if f is None or a is None:
            continue
        f = float(f)
        a = float(a)
        n_pairs += 1
        if r.get("eval_date"):
            dates.add(str(r["eval_date"]))
        # directional hit (sign agreement), including zeros
        if (f > 0 and a > 0) or (f < 0 and a < 0) or (f == 0 and a == 0):
            n_hit += 1
        mase = r.get("mase")
        if mase is not None:
            try:
                mv = float(mase)
                if not math.isnan(mv):
                    mase_values.append(mv)
            except (TypeError, ValueError):
                pass
        if abs(f) <= threshold_pct:
            continue
        pnl = (1.0 if f > 0 else -1.0) * a
        total_pnl += pnl
        n_trades += 1
        if pnl > 0:
            n_wins += 1

    sufficient = n_trades >= min_trades
    return {
        "n_dates": len(dates),
        "date_range": [min(dates), max(dates)] if dates else None,
        "n_pairs": n_pairs,
        "n_trades": n_trades,
        "pnl_pct": round(total_pnl, 4),
        "avg_pnl_per_trade": round(total_pnl / n_trades, 4) if n_trades else None,
        "win_rate": round(n_wins / n_trades, 4) if n_trades else None,
        "hit_rate": round(n_hit / n_pairs, 4) if n_pairs else None,
        "mase_mean": round(statistics.mean(mase_values), 4) if mase_values else None,
        "min_trades": min_trades,
        "sufficient": sufficient,
        "rankable": sufficient,
    }


def build_multiwindow_report(
    shadow_root: Path,
    models: list[str],
    windows: Optional[dict[str, Optional[int]]] = None,
    min_trades: int = DEFAULT_MIN_TRADES,
    threshold_pct: float = THRESHOLD_PCT,
) -> dict[str, Any]:
    """Build ADR-010 E-EVAL multi-window report.

    windows: name -> last N distinct eval_dates (None = full history).
    Rankings only include models with sufficient n_trades in that window.
    """
    if windows is None:
        windows = {"full": None, "10d": 10, "20d": 20}

    models_out: dict[str, Any] = {}
    for m in models:
        rows = load_model_rows(shadow_root / m)
        if not rows:
            continue
        win_snaps: dict[str, Any] = {}
        for wname, n in windows.items():
            subset = filter_rows_last_n_dates(rows, n)
            win_snaps[wname] = compute_window_snapshot(
                subset, min_trades=min_trades, threshold_pct=threshold_pct
            )
        models_out[m] = {"windows": win_snaps}

    rankings: dict[str, list[dict[str, Any]]] = {}
    for wname in windows:
        ranked: list[dict[str, Any]] = []
        for m, payload in models_out.items():
            snap = payload["windows"][wname]
            if not snap.get("rankable"):
                continue
            ranked.append(
                {
                    "model": m,
                    "pnl_pct": snap["pnl_pct"],
                    "n_trades": snap["n_trades"],
                    "win_rate": snap["win_rate"],
                    "hit_rate": snap["hit_rate"],
                    "mase_mean": snap["mase_mean"],
                }
            )
        ranked.sort(key=lambda x: (x["pnl_pct"] is not None, x["pnl_pct"]), reverse=True)
        for i, item in enumerate(ranked, start=1):
            item["rank"] = i
        rankings[wname] = ranked

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "shadow_root": str(shadow_root),
        "min_trades": min_trades,
        "threshold_pct": threshold_pct,
        "windows": {k: v for k, v in windows.items()},
        "models": models_out,
        "rankings": rankings,
        "adr": "ADR-010 E-EVAL",
    }


def print_multiwindow_report(report: dict[str, Any]) -> None:
    """Human-readable multi-window tables to stdout."""
    min_trades = report["min_trades"]
    print(f"E-EVAL multi-window report  min_trades={min_trades}")
    print(f"generated_at={report['generated_at']}")
    print()
    for wname in report["windows"]:
        print(f"=== window={wname} (rankable only if n_trades>={min_trades}) ===")
        print(
            f"{'Rank':>4} {'Model':<32} {'PnL%':>8} {'Trades':>6} "
            f"{'WinRt':>6} {'Hit':>6} {'MASE':>6} {'OK':>4}"
        )
        print("-" * 82)
        # show all models for this window, rankable first then insufficient
        rows_disp: list[tuple[str, dict]] = []
        for m, payload in report["models"].items():
            snap = payload["windows"][wname]
            rows_disp.append((m, snap))
        rows_disp.sort(
            key=lambda x: (
                not x[1].get("rankable"),
                -(x[1].get("pnl_pct") or -1e18),
            )
        )
        rank_map = {r["model"]: r["rank"] for r in report["rankings"].get(wname, [])}
        for m, snap in rows_disp:
            rk = rank_map.get(m, "-")
            ok = "yes" if snap.get("sufficient") else "no"
            wr = f"{snap['win_rate']*100:.1f}%" if snap.get("win_rate") is not None else "  n/a"
            hr = f"{snap['hit_rate']*100:.1f}%" if snap.get("hit_rate") is not None else "  n/a"
            mase = f"{snap['mase_mean']:.2f}" if snap.get("mase_mean") is not None else "  n/a"
            pnl = snap.get("pnl_pct")
            pnl_s = f"{pnl:.2f}" if pnl is not None else "n/a"
            print(
                f"{str(rk):>4} {m:<32} {pnl_s:>8} {snap['n_trades']:>6} "
                f"{wr:>6} {hr:>6} {mase:>6} {ok:>4}"
            )
        print()


HYPERPARAM_FAMILY_MAP = {
    "phase2_v2": "v1_baseline",
    "E05_p3_coldstart_seed42": "round3_seed",
    "E05_p3_coldstart_seed7": "round3_seed",
    "E06_short_ctx": "short_ctx",
    "E07_long_ctx": "long_ctx",
    "E08_fewer_steps": "fewer_steps",
    "E09_higher_lr": "higher_lr",
    "E10_more_steps": "more_steps",
    "zeroshot_bolt_small": "zeroshot",
}


def print_leaderboard(metrics_list: list) -> None:
    """Print sorted leaderboard with 8-gate pass/fail."""
    if not _HAS_SELECTOR or check_promotion_gates is None:
        print("champion_selector unavailable — use --multi-window for E-EVAL")
        return
    sorted_list = sorted(metrics_list, key=lambda m: m.sharpe_30d, reverse=True)
    print(
        f"{'Model':<35} {'PnL%':>8} {'Sharpe30':>9} "
        f"{'WinRt':>6} {'MASE':>5} {'N':>4} {'Pass':>5}"
    )
    print("-" * 80)
    for m in sorted_list:
        gate = check_promotion_gates(m)
        pass_str = "PASS" if gate.passed else "FAIL"
        print(
            f"{m.name:<35} {m.pnl_30d:>8.2f} {m.sharpe_30d:>9.2f} "
            f"{m.win_rate_30d*100:>5.1f}% {m.mase_30d:>5.2f} "
            f"{m.n_trades_30d:>4} {pass_str:>5}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute champion selection metrics from shadow data"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Print leaderboard of all models"
    )
    parser.add_argument(
        "--multi-window", action="store_true",
        help="ADR-010 E-EVAL: full / 10d / 20d windows with n_trades floor"
    )
    parser.add_argument(
        "--min-trades", type=int, default=DEFAULT_MIN_TRADES,
        help=f"Min actionable trades to rank a model in a window (default {DEFAULT_MIN_TRADES})"
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="Write multi-window JSON report to this path"
    )
    parser.add_argument(
        "--champion", type=str, default=None,
        help="Compute metrics for a single model and print details"
    )
    parser.add_argument(
        "--shadow-dir", type=Path, default=SHADOW_ROOT,
        help=f"Shadow root (default: {SHADOW_ROOT})"
    )
    parser.add_argument(
        "--models", nargs="+", default=None,
        help="Specific model basenames (default: OBSERVATION_MODELS for --multi-window, else all)"
    )
    parser.add_argument(
        "--observation-set", action="store_true",
        help="Restrict to ADR-010 observation models (8-model daily set)"
    )
    args = parser.parse_args()

    if args.champion:
        if not _HAS_SELECTOR:
            print("ERROR: champion_selector not installed; use --multi-window", file=sys.stderr)
            return 2
        metrics_list = load_all_metrics(
            args.shadow_dir, [args.champion], HYPERPARAM_FAMILY_MAP
        )
        if metrics_list:
            print_leaderboard(metrics_list)
        else:
            print(f"No data found for model: {args.champion}")
            return 1
        return 0

    if args.multi_window:
        if args.models:
            models = args.models
        else:
            # Default multi-window to ADR-010 observation set
            models = list(OBSERVATION_MODELS)
        report = build_multiwindow_report(
            args.shadow_dir,
            models=models,
            min_trades=args.min_trades,
        )
        print_multiwindow_report(report)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, indent=2, default=str))
            print(f"Wrote {args.out}")
        return 0

    if not args.models:
        if args.observation_set:
            args.models = list(OBSERVATION_MODELS)
        else:
            # Default: discover all model dirs in shadow_root
            if not args.shadow_dir.exists():
                print(f"Shadow dir not found: {args.shadow_dir}", file=sys.stderr)
                return 1
            args.models = sorted(
                p.name for p in args.shadow_dir.iterdir()
                if p.is_dir() and not p.name.startswith("_")
            )

    if not _HAS_SELECTOR:
        print(
            "ERROR: champion_selector not installed; use --multi-window for E-EVAL",
            file=sys.stderr,
        )
        return 2

    metrics_list = load_all_metrics(
        args.shadow_dir, args.models, HYPERPARAM_FAMILY_MAP
    )
    print_leaderboard(metrics_list)

    # If we have ≥2 eligible models, also print champion + challenger recommendation
    try:
        selection = select_champion(metrics_list)
        challenger = select_challenger(metrics_list, selection.champion)
        print()
        print(f"Recommended Champion:   {selection.champion}")
        print(f"Recommended Challenger: {challenger}")
    except ValueError as e:
        print()
        print(f"No champion eligible: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())