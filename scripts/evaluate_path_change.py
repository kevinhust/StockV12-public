#!/usr/bin/env python3
"""L2 path-change evaluation — Gate Decision Architecture (Accepted 2026-07-26).

Answers only: may a *challenger* replace the current *decision-path baseline*?
Never auto-promotes. Verdicts:

  INSUFFICIENT      — sample too small (wait)
  REJECT            — enough sample; challenger fails relative quality / halt
  READY_FOR_HUMAN   — technical L2 pack green; human must still approve

Does not implement L0 (capital) or L1 (session HaltGate runtime).
PnL is informational only (Round-4 moratorium).

Usage:
  PYTHONPATH=src:scripts:. python scripts/evaluate_path_change.py \\
      --baseline phase2_v2 --challenger phase2_wf_20260724 \\
      --window 20d --min-trades 15
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from stockapp_v12.app.bootstrap import ensure_src_on_path

    ensure_src_on_path()
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

VERDICT_INSUFFICIENT = "INSUFFICIENT"
VERDICT_REJECT = "REJECT"
VERDICT_READY_FOR_HUMAN = "READY_FOR_HUMAN"

DEFAULT_MIN_TRADES = 15
DEFAULT_MIN_N_DATES = 15
DEFAULT_MAX_MASE_RATIO = 1.05  # challenger_mase <= baseline_mase * ratio
DEFAULT_MAX_EXTRA_HALT_DAYS = 1


def evaluate_path_change(
    baseline: dict[str, Any],
    challenger: dict[str, Any],
    *,
    min_trades: int = DEFAULT_MIN_TRADES,
    min_n_dates: int = DEFAULT_MIN_N_DATES,
    max_mase_ratio: float = DEFAULT_MAX_MASE_RATIO,
    baseline_halt_days: Optional[int] = None,
    challenger_halt_days: Optional[int] = None,
    max_extra_halt_days: int = DEFAULT_MAX_EXTRA_HALT_DAYS,
    baseline_name: str = "baseline",
    challenger_name: str = "challenger",
) -> dict[str, Any]:
    """Pure L2 evaluator. Metrics dicts use champion_evaluation snapshot keys."""
    reasons: list[str] = []
    checks: dict[str, Any] = {
        "c1_sample": False,
        "c2_relative_quality": False,
        "c3_halt": "skipped",
        "c5_human_required": True,
    }

    b_trades = int(baseline.get("n_trades") or 0)
    c_trades = int(challenger.get("n_trades") or 0)
    b_dates = int(baseline.get("n_dates") or 0)
    c_dates = int(challenger.get("n_dates") or 0)

    # C1 sample
    sample_ok = True
    if c_trades < min_trades:
        sample_ok = False
        reasons.append(
            f"challenger n_trades {c_trades} < {min_trades} (C1 sample floor)"
        )
    if b_trades < min_trades:
        sample_ok = False
        reasons.append(
            f"baseline n_trades {b_trades} < {min_trades} (C1 sample floor)"
        )
    if min_n_dates > 0:
        if c_dates < min_n_dates:
            sample_ok = False
            reasons.append(
                f"challenger n_dates {c_dates} < {min_n_dates} (C1 date floor)"
            )
        if b_dates < min_n_dates:
            sample_ok = False
            reasons.append(
                f"baseline n_dates {b_dates} < {min_n_dates} (C1 date floor)"
            )
    checks["c1_sample"] = sample_ok

    b_hit = baseline.get("hit_rate")
    c_hit = challenger.get("hit_rate")
    b_mase = baseline.get("mase_mean")
    c_mase = challenger.get("mase_mean")
    b_pnl = baseline.get("pnl_pct")
    c_pnl = challenger.get("pnl_pct")

    informational = {
        "delta_hit_rate": _sub(c_hit, b_hit),
        "delta_mase": _sub(c_mase, b_mase),
        "delta_pnl_pct": _sub(c_pnl, b_pnl),
        "note": "PnL is informational only (not a hard L2 reject)",
    }

    if not sample_ok:
        return _result(
            VERDICT_INSUFFICIENT,
            reasons,
            checks,
            informational,
            baseline_name,
            challenger_name,
            baseline,
            challenger,
        )

    # C2 relative quality (ties OK)
    quality_ok = True
    if b_hit is not None and c_hit is not None:
        if float(c_hit) < float(b_hit):
            quality_ok = False
            reasons.append(
                f"hit_rate challenger {float(c_hit):.4f} < baseline {float(b_hit):.4f} (C2)"
            )
    elif b_hit is None or c_hit is None:
        quality_ok = False
        reasons.append("hit_rate missing on baseline or challenger (C2)")

    if b_mase is not None and c_mase is not None:
        thresh = float(b_mase) * float(max_mase_ratio)
        if float(c_mase) > thresh:
            quality_ok = False
            reasons.append(
                f"mase challenger {float(c_mase):.4f} > {thresh:.4f} "
                f"(baseline×{max_mase_ratio}) (C2)"
            )
    # if both mase missing, do not fail solely on mase
    checks["c2_relative_quality"] = quality_ok

    # C3 halt (optional)
    if baseline_halt_days is not None and challenger_halt_days is not None:
        limit = int(baseline_halt_days) + int(max_extra_halt_days)
        if int(challenger_halt_days) > limit:
            checks["c3_halt"] = False
            reasons.append(
                f"halt_days challenger {challenger_halt_days} > "
                f"baseline {baseline_halt_days} + {max_extra_halt_days} (C3)"
            )
        else:
            checks["c3_halt"] = True
    else:
        checks["c3_halt"] = "skipped"

    halt_fail = checks["c3_halt"] is False
    if quality_ok and not halt_fail:
        verdict = VERDICT_READY_FOR_HUMAN
        reasons = [
            "L2 technical pack green: relative quality OK"
            + ("" if checks["c3_halt"] == "skipped" else "; halt OK")
            + "; human approval still required (C5)"
        ]
    else:
        verdict = VERDICT_REJECT

    return _result(
        verdict,
        reasons,
        checks,
        informational,
        baseline_name,
        challenger_name,
        baseline,
        challenger,
    )


def _sub(a: Any, b: Any) -> Optional[float]:
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 4)


def _result(
    verdict: str,
    reasons: list[str],
    checks: dict[str, Any],
    informational: dict[str, Any],
    baseline_name: str,
    challenger_name: str,
    baseline: dict[str, Any],
    challenger: dict[str, Any],
) -> dict[str, Any]:
    return {
        "layer": "L2",
        "architecture": "GATE_DECISION_ARCHITECTURE",
        "verdict": verdict,
        "auto_promote": False,
        "baseline": baseline_name,
        "challenger": challenger_name,
        "reasons": list(reasons),
        "checks": dict(checks),
        "informational": informational,
        "baseline_metrics": {
            "n_trades": baseline.get("n_trades"),
            "n_dates": baseline.get("n_dates"),
            "hit_rate": baseline.get("hit_rate"),
            "mase_mean": baseline.get("mase_mean"),
            "pnl_pct": baseline.get("pnl_pct"),
        },
        "challenger_metrics": {
            "n_trades": challenger.get("n_trades"),
            "n_dates": challenger.get("n_dates"),
            "hit_rate": challenger.get("hit_rate"),
            "mase_mean": challenger.get("mase_mean"),
            "pnl_pct": challenger.get("pnl_pct"),
        },
    }


def evaluate_from_shadow(
    shadow_dir: Path,
    baseline: str,
    challenger: str,
    *,
    window: str = "20d",
    min_trades: int = DEFAULT_MIN_TRADES,
    min_n_dates: int = DEFAULT_MIN_N_DATES,
    max_mase_ratio: float = DEFAULT_MAX_MASE_RATIO,
    baseline_halt_days: Optional[int] = None,
    challenger_halt_days: Optional[int] = None,
) -> dict[str, Any]:
    """Load multi-window report and evaluate L2 on one window."""
    from scripts.champion_evaluation import build_multiwindow_report
    from scripts.shadow_trade import SHADOW_ROOT

    root = shadow_dir or SHADOW_ROOT
    report = build_multiwindow_report(
        Path(root),
        models=[baseline, challenger],
        min_trades=min_trades,
    )
    if baseline not in report.get("models", {}):
        return {
            "layer": "L2",
            "verdict": VERDICT_INSUFFICIENT,
            "auto_promote": False,
            "reasons": [f"baseline model missing under shadow: {baseline}"],
            "checks": {},
            "informational": {},
        }
    if challenger not in report.get("models", {}):
        return {
            "layer": "L2",
            "verdict": VERDICT_INSUFFICIENT,
            "auto_promote": False,
            "reasons": [f"challenger model missing under shadow: {challenger}"],
            "checks": {},
            "informational": {},
        }
    if window not in report.get("windows", {}):
        return {
            "layer": "L2",
            "verdict": VERDICT_INSUFFICIENT,
            "auto_promote": False,
            "reasons": [f"window {window!r} not in report"],
            "checks": {},
            "informational": {},
        }

    b = report["models"][baseline]["windows"][window]
    c = report["models"][challenger]["windows"][window]
    out = evaluate_path_change(
        b,
        c,
        min_trades=min_trades,
        min_n_dates=min_n_dates,
        max_mase_ratio=max_mase_ratio,
        baseline_halt_days=baseline_halt_days,
        challenger_halt_days=challenger_halt_days,
        baseline_name=baseline,
        challenger_name=challenger,
    )
    out["window"] = window
    out["multiwindow_generated_at"] = report.get("generated_at")
    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description="L2 path-change eval (INSUFFICIENT|REJECT|READY_FOR_HUMAN)"
    )
    p.add_argument("--baseline", required=True)
    p.add_argument("--challenger", required=True)
    p.add_argument("--shadow-dir", type=Path, default=None)
    p.add_argument("--window", default="20d", help="multi-window name (default 20d)")
    p.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    p.add_argument("--min-n-dates", type=int, default=DEFAULT_MIN_N_DATES)
    p.add_argument("--max-mase-ratio", type=float, default=DEFAULT_MAX_MASE_RATIO)
    p.add_argument("--baseline-halt-days", type=int, default=None)
    p.add_argument("--challenger-halt-days", type=int, default=None)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    from scripts.shadow_trade import SHADOW_ROOT

    shadow = args.shadow_dir or SHADOW_ROOT
    result = evaluate_from_shadow(
        shadow,
        args.baseline,
        args.challenger,
        window=args.window,
        min_trades=args.min_trades,
        min_n_dates=args.min_n_dates,
        max_mase_ratio=args.max_mase_ratio,
        baseline_halt_days=args.baseline_halt_days,
        challenger_halt_days=args.challenger_halt_days,
    )
    result["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    print(
        f"L2 path-change  baseline={args.baseline}  challenger={args.challenger}  "
        f"window={args.window}"
    )
    print(f"verdict={result['verdict']}  auto_promote={result.get('auto_promote')}")
    for r in result.get("reasons", []):
        print(f"  - {r}")
    info = result.get("informational") or {}
    if info:
        print(
            f"informational: Δhit={info.get('delta_hit_rate')} "
            f"Δmase={info.get('delta_mase')} Δpnl={info.get('delta_pnl_pct')} "
            f"(PnL not hard gate)"
        )
    if result["verdict"] == VERDICT_READY_FOR_HUMAN:
        print("Next: human approval required before any decision-path swap.")
    elif result["verdict"] == VERDICT_INSUFFICIENT:
        print("Next: accumulate more shadow dates/trades; do not promote.")
    else:
        print("Next: keep baseline decision path; challenger stays observation-only.")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, default=str))
        print(f"Wrote {args.out}")

    # exit codes: 0 ready, 1 reject, 2 insufficient / error
    v = result["verdict"]
    if v == VERDICT_READY_FOR_HUMAN:
        return 0
    if v == VERDICT_REJECT:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
