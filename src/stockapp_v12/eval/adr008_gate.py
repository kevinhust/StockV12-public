"""
ADR-008 walk-forward promotion gate — single source of truth for Day-30 evidence.

Spec (per v12/docs/ADR-009-WalkForward-Backfill-Addendum.md "Day-30 Promotion
Gate Result 2026-07-01"):

    ┌───────────┬───────────┬──────────────────────────┐
    │ Metric    │ Threshold │ Field in shadow JSON     │
    ├───────────┼───────────┼──────────────────────────┤
    │ pnl       │ > 0       │ simulated_pnl_pct        │
    │ mase      │ ≤ 4.011   │ mase                     │
    │ win_rate  │ ≥ 0.40    │ win_rate                 │
    │ n_trades  │ ≥ 30      │ n_trades                 │
    └───────────┴───────────┴──────────────────────────┘

The 4.011 threshold derives from v2 walk-forward MASE (3.82) × 1.05 —
see V3_LESSONS_AND_EXP_PLAN.md for context.  Future relaxations should
update DEFAULT_THRESHOLDS *and* the ADR-009 addendum, never one without
the other.

Usage:

    from stockapp_v12.eval.adr008_gate import evaluate, default_thresholds
    result = evaluate(summary)
    if result.passed:
        promote(model_name)
    else:
        stay_on_v2(result.reason)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


# ----- canonical thresholds (do NOT change without ADR) -----

@dataclass(frozen=True)
class Adr008Thresholds:
    pnl_min: float = 0.0            # strictly > 0 (not >=)
    mase_max: float = 4.011          # v2_wf(3.82) × 1.05
    win_rate_min: float = 0.40
    n_trades_min: int = 30
    # Round 4 council verdict (2026-07-10): PnL is informational-only.
    # The 30-day backfill showed PnL is too volatile (±10pp run-to-run)
    # to be a reliable gate criterion. Halt rule paper-trade is the
    # replacement instrument, pending 60-day OOS validation.
    # Set to False to restore PnL as a hard gate (pre-Round 4 behavior).
    pnl_moratorium: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def default_thresholds() -> Adr008Thresholds:
    return Adr008Thresholds()


# ----- evaluation result -----

@dataclass
class GateCheck:
    metric: str
    passed: bool
    actual: Any
    op: str          # ">", "<=", ">=", ">=" ...
    threshold: Any
    message: str = ""
    informational: bool = False  # Round 4: if True, doesn't affect gate verdict

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class GateResult:
    passed: bool
    checks: list = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    thresholds: Optional[Adr008Thresholds] = None
    model_name: str = ""
    error: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "passed": self.passed,
            "model": self.model_name,
            "checks": [c.to_dict() for c in self.checks],
            "summary_keys": sorted(self.summary.keys()),
            "thresholds": self.thresholds.to_dict() if self.thresholds else None,
            "reason": self.reason,
        }
        if self.error:
            d["error"] = self.error
        return d


# ----- gate logic -----

def evaluate(
    summary: Dict[str, Any],
    *,
    thresholds: Optional[Adr008Thresholds] = None,
    model_name: str = "",
) -> GateResult:
    """Evaluate a shadow_trade summary dict against ADR-008.

    Required keys in `summary` (defaults to None if missing):
        - simulated_pnl_pct   (pnl)
        - mase
        - win_rate
        - n_trades
    """
    thr = thresholds or default_thresholds()
    result = GateResult(
        passed=False,
        thresholds=thr,
        model_name=model_name or str(summary.get("model") or ""),
        summary=dict(summary),
    )

    if not summary:
        result.error = "empty summary"
        result.reason = "no summary provided"
        return result

    pnl = summary.get("simulated_pnl_pct")
    mase = summary.get("mase")
    win_rate = summary.get("win_rate")
    n_trades = summary.get("n_trades")

    # --- pnl > 0 (or informational if moratorium is active) ---
    pnl_check = _check_gt(
        "pnl", pnl, thr.pnl_min, summary,
        note="simulated_pnl_pct from shadow JSON",
    )
    if thr.pnl_moratorium:
        # Round 4: PnL is reported but does NOT affect gate verdict
        pnl_check = GateCheck(
            metric="pnl",
            passed=True,
            actual=pnl,
            op=">",
            threshold=thr.pnl_min,
            message=f"informational (moratorium): {pnl} (reported, not gating)",
            informational=True,
        )
    result.checks.append(pnl_check)

    # --- mase <= 4.011 ---
    mase_check = _check_le(
        "mase", mase, thr.mase_max, summary,
        note="mase from shadow JSON",
    )
    result.checks.append(mase_check)

    # --- win_rate >= 0.40 ---
    wr_check = _check_gte(
        "win_rate", win_rate, thr.win_rate_min, summary,
        note="win_rate from shadow JSON",
    )
    result.checks.append(wr_check)

    # --- n_trades >= 30 ---
    nt_check = _check_gte(
        "n_trades", n_trades, thr.n_trades_min, summary,
        note="n_trades from shadow JSON",
    )
    result.checks.append(nt_check)

    # Only non-informational checks affect the gate verdict
    hard_checks = [c for c in result.checks if not c.informational]
    all_passed = all(c.passed for c in hard_checks)
    result.passed = all_passed
    if not all_passed:
        failed = [c for c in hard_checks if not c.passed]
        result.reason = "failures: " + ", ".join(c.metric for c in failed)
    else:
        result.reason = "all hard checks passed"

    return result


def evaluate_file(
    path: Path,
    *,
    thresholds: Optional[Adr008Thresholds] = None,
    model_name: Optional[str] = None,
) -> GateResult:
    """Convenience: load JSON from `path`, evaluate, return result.

    `path` can be either a SUMMARY.json file (one model) or a single
    per-date JSON.  Detect shape automatically:
      - has 'rows'        -> wrap as {n_trades: len(rows)}
      - has 'summary'     -> use summary field
      - otherwise         -> assume it's a single-model summary
    """
    payload = json.loads(Path(path).read_text())
    if isinstance(payload, dict) and "rows" in payload:
        wrapped: Dict[str, Any] = {
            "simulated_pnl_pct": payload.get("simulated_pnl_pct"),
            "mase": payload.get("mase"),
            "win_rate": payload.get("win_rate"),
            "n_trades": len(payload.get("rows") or []),
        }
        return evaluate(wrapped, thresholds=thresholds, model_name=model_name or payload.get("model", ""))
    if isinstance(payload, dict) and "summary" in payload and isinstance(payload["summary"], dict):
        return evaluate(payload["summary"], thresholds=thresholds, model_name=model_name or payload.get("model", ""))
    return evaluate(payload, thresholds=thresholds, model_name=model_name or "")


# ----- helpers -----

def _check_gt(metric, actual, threshold, summary, *, note=""):
    if actual is None:
        return GateCheck(metric, False, None, ">", threshold, f"missing {metric}")
    ok = float(actual) > float(threshold)
    return GateCheck(metric, ok, actual, ">", threshold,
                     f"{'passes' if ok else 'fails'} ({note}): {actual} {('>' if ok else '<=')} {threshold}")


def _check_le(metric, actual, threshold, summary, *, note=""):
    if actual is None:
        return GateCheck(metric, False, None, "<=", threshold, f"missing {metric}")
    ok = float(actual) <= float(threshold)
    return GateCheck(metric, ok, actual, "<=", threshold,
                     f"{'passes' if ok else 'fails'} ({note}): {actual} {('<=' if ok else '>')} {threshold}")


def _check_gte(metric, actual, threshold, summary, *, note=""):
    if actual is None:
        return GateCheck(metric, False, None, ">=", threshold, f"missing {metric}")
    try:
        ok = float(actual) >= float(threshold)
        actual_str = actual
    except (TypeError, ValueError):
        return GateCheck(metric, False, actual, ">=", threshold,
                         f"non-numeric {metric}: {actual!r}")
    return GateCheck(metric, ok, actual_str, ">=", threshold,
                     f"{'passes' if ok else 'fails'} ({note}): {actual} {('>=' if ok else '<')} {threshold}")


# ----- CLI helper -----

def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Evaluate ADR-008 walk-forward promotion gate")
    parser.add_argument("summary_json", type=Path, help="Path to shadow SUMMARY.json or per-date JSON")
    parser.add_argument("--model", default=None, help="Override model name")
    parser.add_argument("--exit-on-fail", action="store_true", help="Exit code 1 if gate fails")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    args = parser.parse_args(argv)

    result = evaluate_file(args.summary_json, model_name=args.model)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        verdict = "PASS" if result.passed else "FAIL"
        print(f"== ADR-008 gate {verdict} ==")
        for c in result.checks:
            mark = "OK " if c.passed else "X  "
            print(f"  {mark}{c.metric:10s} {c.actual!s:>10s} {c.op} {c.threshold}  ({c.message})")
        if result.error:
            print(f"error: {result.error}")
        if result.reason:
            print(f"reason: {result.reason}")

    if args.exit_on_fail and not result.passed:
        return 1
    return 0 if result.passed else 0  # default never gates; --exit-on-fail gates


if __name__ == "__main__":
    import sys
    sys.exit(main())
