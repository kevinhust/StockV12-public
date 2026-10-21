"""
Validate the A+B ensemble + regime-aware gate on the 10-symbol ensemble
eval reports (cutoff=10 and cutoff=20).

Reuses the regime feature attachment and gate predicates from
`validate_regime_gate.py` so the comparison is bit-exact with the
single-model gate validation.

Output:
  models/chronos/_experiments/ensemble_gate_validation.csv
  models/chronos/_experiments/ensemble_gate_validation_summary.csv

Per-gate, per-cutoff, we report:
  - hit_rate after the gate is applied to ensemble forecast
  - n_after_gate (tradeable volume)
  - n_correct
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
EXP_DIR = REPO / "models/chronos/_experiments"

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from validate_regime_gate import (  # noqa: E402
    _anchor,
    attach_regime,
    evaluate_gates,
    g_and,
    g_combined,
    g_combined_short,
    g_forecast_above,
    g_low_vol,
    g_no_gate,
    g_or,
    g_sector_5d_above,
    g_spy_5d_above,
    g_sym_above_sma20,
    gate,
    hit_rate_of,
    summarize,
)


def _load_ensemble_rows(report_path: Path) -> dict[str, list[dict]]:
    """Load an ensemble JSON report and return {cutoff_str: [rows, ...]}.

    The ensemble JSON has the same shape as the eval JSON that
    validate_regime_gate expects, so we can hand the per_cutoff list
    directly to attach_regime and evaluate_gates.
    """
    if not report_path.exists():
        raise FileNotFoundError(f"missing ensemble report: {report_path}")
    report = json.loads(report_path.read_text())
    by_cutoff = report["by_cutoff"]
    out: dict[str, list[dict]] = {}
    for ck, cobj in by_cutoff.items():
        # The ensemble key is e.g. "A_B_ensemble" — pick whichever is first.
        per_model_rows = cobj["per_model_rows"]
        first_key = next(iter(per_model_rows))
        out[ck] = list(per_model_rows[first_key])
    return out


def main():
    # Ensemble eval ran on 2026-07-01 with full 10-symbol universe.
    # Anchor dates are walk-forward: cutoff=10 → 2026-06-16, cutoff=20 → 2026-06-06.
    # These match the Regime_B drawdown window dates used in validate_regime_gate
    # for the same eval date, so comparison is apples-to-apples.
    R_CUT10 = EXP_DIR / "ensemble_A_B_cut10_10syms.json"
    R_CUT20 = EXP_DIR / "ensemble_A_B_cut20_10syms.json"

    cut10_rows = _load_ensemble_rows(R_CUT10)["10"]
    cut20_rows = _load_ensemble_rows(R_CUT20)["20"]

    # Headline hit_rate (no gate) for the ensemble.
    nc, n, hr = hit_rate_of(cut10_rows)
    print(f"=== Ensemble A+B (10 syms) headline ===")
    print(f"  cutoff=10  hit_rate={hr:.4f}  n={n}  nc={nc}")
    nc, n, hr = hit_rate_of(cut20_rows)
    print(f"  cutoff=20  hit_rate={hr:.4f}  n={n}  nc={nc}")

    # Attach regime features using the same anchor dates used in the
    # single-model gate validation.
    anchor_cut10 = _anchor("2026-06-16")
    anchor_cut20 = _anchor("2026-06-06")
    cut10_with_regime = [attach_regime(r, anchor_cut10) for r in cut10_rows]
    cut20_with_regime = [attach_regime(r, anchor_cut20) for r in cut20_rows]

    # Run the same gate battery the single-model validator runs.
    datasets = {
        "ensemble_cutoff10": cut10_with_regime,
        "ensemble_cutoff20": cut20_with_regime,
    }

    rows_table = []
    for label, rows in datasets.items():
        summaries = evaluate_gates({"cutoffs": {"x": {"all_rows": rows}}}, "x")
        for s in summaries:
            rows_table.append({
                "dataset": label,
                "gate": s["gate"],
                "n_after_gate": s["n_after_gate"],
                "n_correct": s["n_correct"],
                "hit_rate": s["hit_rate"],
            })

    df = pd.DataFrame(rows_table)
    pivot = df.pivot_table(index="gate", columns="dataset", values="hit_rate", aggfunc="first")
    pivot_n = df.pivot_table(index="gate", columns="dataset", values="n_after_gate", aggfunc="first")
    pivot_correct = df.pivot_table(index="gate", columns="dataset", values="n_correct", aggfunc="first")

    print("\n=== HIT_RATE after each gate (ensemble forecast) ===")
    print(pivot.to_string())
    print("\n=== n_after_gate ===")
    print(pivot_n.to_string())
    print("\n=== n_correct ===")
    print(pivot_correct.to_string())

    # Stability summary.
    score = pd.DataFrame({
        "min_hr": pivot.min(axis=1),
        "mean_hr": pivot.mean(axis=1),
        "min_n": pivot_n.min(axis=1),
        "mean_n": pivot_n.mean(axis=1),
    }).sort_values("min_hr", ascending=False)
    print("\n=== GATE STABILITY (sorted by worst-case hit_rate) ===")
    print(score.to_string())

    # Persist.
    out_csv = EXP_DIR / "ensemble_gate_validation.csv"
    df.to_csv(out_csv, index=False)
    print(f"\nFull table saved to: {out_csv}")

    out_summary_csv = EXP_DIR / "ensemble_gate_validation_summary.csv"
    score.reset_index().to_csv(out_summary_csv, index=False)
    print(f"Summary saved to: {out_summary_csv}")


if __name__ == "__main__":
    main()