#!/usr/bin/env python3
"""Compare two (or more) shadow models across multi-windows (ADR-010).

Default use: phase2_v2 vs phase2_wf_YYYYMMDD after a D3 retrain.

Usage:
  PYTHONPATH=src:scripts:. python scripts/compare_shadow_models.py \\
      --models phase2_v2 phase2_wf_20260724 \\
      --min-trades 15 \\
      --out docs/reviews/compare-phase2-v2-vs-wf-20260724.json

  # Also merge offline H2H report if present:
  PYTHONPATH=src:scripts:. python scripts/compare_shadow_models.py \\
      --models phase2_v2 phase2_wf_20260724 \\
      --h2h models/chronos/h2h_phase2_v2_vs_wf_20260724.json
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
    _root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_root / "src"))
    sys.path.insert(0, str(_root / "scripts"))

from scripts.champion_evaluation import (  # noqa: E402
    DEFAULT_MIN_TRADES,
    build_multiwindow_report,
)
from scripts.shadow_trade import SHADOW_ROOT  # noqa: E402


def load_h2h_summary(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    # Flexible: support list of rows or nested ranking
    rows = data.get("rows") or data.get("results") or data.get("by_model")
    if rows is None and isinstance(data, dict):
        # try to extract per-model aggregates if present
        return {"raw_keys": list(data.keys())[:20], "note": "passthrough", "data": data}
    return {"rows": rows, "source": str(path)}


def pairwise_deltas(
    report: dict[str, Any],
    baseline: str,
    challenger: str,
) -> dict[str, Any]:
    """Compute challenger - baseline deltas per window."""
    out: dict[str, Any] = {}
    for wname, models in report.get("models", {}).items():
        pass
    # report structure: models[model][windows][wname]
    model_payloads = report.get("models", {})
    if baseline not in model_payloads or challenger not in model_payloads:
        return {"error": "missing model in multiwindow report"}

    for wname in report.get("windows", {}):
        b = model_payloads[baseline]["windows"][wname]
        c = model_payloads[challenger]["windows"][wname]
        out[wname] = {
            "baseline": baseline,
            "challenger": challenger,
            "baseline_sufficient": b.get("sufficient"),
            "challenger_sufficient": c.get("sufficient"),
            "delta_pnl_pct": _sub(c.get("pnl_pct"), b.get("pnl_pct")),
            "delta_hit_rate": _sub(c.get("hit_rate"), b.get("hit_rate")),
            "delta_win_rate": _sub(c.get("win_rate"), b.get("win_rate")),
            "delta_mase": _sub(c.get("mase_mean"), b.get("mase_mean")),
            "baseline_n_trades": b.get("n_trades"),
            "challenger_n_trades": c.get("n_trades"),
            "baseline_pnl_pct": b.get("pnl_pct"),
            "challenger_pnl_pct": c.get("pnl_pct"),
            "both_rankable": bool(b.get("rankable") and c.get("rankable")),
        }
    return out


def _sub(a, b):
    if a is None or b is None:
        return None
    return round(float(a) - float(b), 4)


def print_compare(models: list[str], report: dict[str, Any], deltas: dict[str, Any]) -> None:
    print(f"Shadow compare  models={models}  min_trades={report['min_trades']}")
    print(f"generated_at={report['generated_at']}")
    print()
    for wname in report["windows"]:
        print(f"=== window={wname} ===")
        print(
            f"{'Model':<28} {'PnL%':>8} {'Trades':>6} {'Win%':>6} "
            f"{'Hit%':>6} {'MASE':>6} {'OK':>4}"
        )
        print("-" * 72)
        for m in models:
            if m not in report["models"]:
                print(f"{m:<28} {'MISSING':>8}")
                continue
            s = report["models"][m]["windows"][wname]
            wr = f"{s['win_rate']*100:.1f}" if s.get("win_rate") is not None else "n/a"
            hr = f"{s['hit_rate']*100:.1f}" if s.get("hit_rate") is not None else "n/a"
            mase = f"{s['mase_mean']:.2f}" if s.get("mase_mean") is not None else "n/a"
            pnl = f"{s['pnl_pct']:.2f}" if s.get("pnl_pct") is not None else "n/a"
            ok = "yes" if s.get("sufficient") else "no"
            print(
                f"{m:<28} {pnl:>8} {s['n_trades']:>6} {wr:>6} {hr:>6} {mase:>6} {ok:>4}"
            )
        if wname in deltas and "error" not in deltas:
            d = deltas[wname]
            print(
                f"  Δ({d['challenger']} - {d['baseline']}): "
                f"pnl={d['delta_pnl_pct']} hit={d['delta_hit_rate']} "
                f"win={d['delta_win_rate']} mase={d['delta_mase']} "
                f"both_rankable={d['both_rankable']}"
            )
        print()


def main() -> int:
    p = argparse.ArgumentParser(description="Compare shadow models multi-window")
    p.add_argument("--models", nargs="+", required=True)
    p.add_argument("--shadow-dir", type=Path, default=SHADOW_ROOT)
    p.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    p.add_argument("--baseline", type=str, default=None, help="default: first model")
    p.add_argument("--challenger", type=str, default=None, help="default: second model")
    p.add_argument("--h2h", type=Path, default=None, help="optional offline H2H JSON")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    if len(args.models) < 2:
        print("ERROR: need at least 2 models", file=sys.stderr)
        return 2

    baseline = args.baseline or args.models[0]
    challenger = args.challenger or args.models[1]

    report = build_multiwindow_report(
        args.shadow_dir,
        models=args.models,
        min_trades=args.min_trades,
    )
    deltas = pairwise_deltas(report, baseline, challenger)
    print_compare(args.models, report, deltas)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline": baseline,
        "challenger": challenger,
        "models": args.models,
        "multiwindow": report,
        "deltas": deltas,
        "h2h": load_h2h_summary(args.h2h) if args.h2h else None,
        "adr": "ADR-010 D3 compare",
    }

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, default=str))
        print(f"Wrote {args.out}")

    # L2 path-change (Gate Decision Architecture) — never auto-promote
    print("=== L2 path-change (evaluate_path_change) ===")
    try:
        from scripts.evaluate_path_change import evaluate_path_change

        if "20d" in report.get("windows", {}):
            b = report["models"][baseline]["windows"]["20d"]
            c = report["models"][challenger]["windows"]["20d"]
            l2 = evaluate_path_change(
                b,
                c,
                min_trades=args.min_trades,
                baseline_name=baseline,
                challenger_name=challenger,
            )
            payload["l2_path_change"] = l2
            print(f"verdict={l2['verdict']}  auto_promote={l2['auto_promote']}")
            for reason in l2.get("reasons", []):
                print(f"  - {reason}")
            print("Path swap requires READY_FOR_HUMAN + explicit human approval.")
        else:
            print("No 20d window in report.")
    except Exception as exc:  # pragma: no cover - defensive
        print(f"L2 evaluate skipped: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
