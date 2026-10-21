#!/usr/bin/env python3
"""Refresh the shadow determinism baseline fixture (Phase 5, Design § 5.5).

This script regenerates ``tests/fixtures/determinism_baseline.json`` by
running the same synthetic shadow path used in
``tests/test_shadow_determinism.py`` and capturing the resulting hashes.

Usage (developer tool — NOT a test)::

    python -m scripts.refresh_determinism_baseline

The script will:

1. Compute the three SHA-256 hashes against the synthetic fixture.
2. Bump ``schema_version`` (1 → 2 → 3, ...).
3. Update ``locked_at`` (UTC ISO 8601) and ``lock_commit`` (``git rev-parse HEAD``).
4. Write the new baseline in place.
5. Exit non-zero if any of the three artifact paths is missing (sanity check).
6. Print the ``schema_version`` diff so the developer remembers to cite it
   in the commit message body.

When to run
-----------

Run when a developer has **intentionally** changed the byte format of
shadow output (e.g. added a new field to per-date rows, changed ``default=str``
to a custom encoder, reordered SUMMARY keys). This is a CI gate failure
that needs explicit acknowledgment.

Do NOT run to "make the test pass" without understanding the drift. Per
the Council § 3.3 Critic synthesis: multi-hash drift is a STOP signal —
open a Council session, do NOT silently refresh.

Safety
------

- The script is idempotent: re-running bumps schema_version monotonically.
- It never deletes the existing baseline file; it overwrites in place after
  computing the new hashes.
- It requires ``git rev-parse HEAD`` to succeed; runs from repo root or
  any subdirectory (uses ``Path(__file__).resolve().parents[1]``).
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Repo root: post-migration (2026-07-09), this script lives at scripts/
REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "determinism_baseline.json"

# Mirror the constants from v12/tests/test_shadow_determinism.py.
# If you change them here, change them there too (and re-run the test).
FIXTURE_SYMBOLS = ["AAPL", "MSFT", "NVDA"]
FIXTURE_EVAL_DATE_STR = "2026-06-15"
FIXTURE_SEED = 42
FIXTURE_OHLCV_ROWS_PER_SYMBOL = 60
FIXTURE_MODEL = "baseline_v1"
FIXTURE_ENSEMBLE_LABELS = ["variant_a", "variant_b"]
FIXTURE_THRESHOLD_PCT = 0.5
DEFAULT_FALLBACK_SPREAD_PCT = 3.0
SYMBOL_BASES = {"AAPL": 100.0, "MSFT": 200.0, "NVDA": 400.0}
# Per-symbol RNG seed offset. MUST be a literal dict (NOT derived from
# Python's hash()) because PYTHONHASHSEED randomizes hash() per process —
# which would break determinism across test runs and CI machines.
SYMBOL_SEED_OFFSETS = {"AAPL": 0, "MSFT": 100, "NVDA": 200}


def _hash_json(obj: dict) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _build_synthetic_close(symbol: str, n_rows: int) -> List[float]:
    import numpy as np
    rng = np.random.default_rng(FIXTURE_SEED + SYMBOL_SEED_OFFSETS[symbol])
    base = SYMBOL_BASES[symbol]
    return [
        float(base + 5 * math.sin(i / 7) + 0.1 * i + rng.normal(0, 0.5))
        for i in range(n_rows)
    ]


def _build_shadow_trade_rows() -> List[dict]:
    rows = []
    for sym in FIXTURE_SYMBOLS:
        closes = _build_synthetic_close(sym, FIXTURE_OHLCV_ROWS_PER_SYMBOL)
        current_close = round(closes[-1], 2)
        forecast_mean = round(current_close * 1.01, 2)
        forecast_ret_pct = round((forecast_mean - current_close) / current_close * 100, 3)
        actual_mean = round(current_close * 1.004, 2)
        actual_ret_pct = round((actual_mean - current_close) / current_close * 100, 3)
        rows.append({
            "symbol": sym,
            "current_close": current_close,
            "forecast_mean": forecast_mean,
            "forecast_ret_pct": forecast_ret_pct,
            "actual_mean": actual_mean,
            "actual_ret_pct": actual_ret_pct,
            "mase": 1.5,
            "cutoff_days_ago": 1,
            "n_horizons_evaluated": 10,
            "eval_date": FIXTURE_EVAL_DATE_STR,
            "model": FIXTURE_MODEL,
        })
    return rows


def _build_ensemble_rows() -> List[dict]:
    rows = []
    for i, sym in enumerate(FIXTURE_SYMBOLS):
        current_close = SYMBOL_BASES[sym] + 5 * math.sin(20 / 7) + 0.1 * 20
        if i == 0:
            # Force fallback: spread > 3.0%
            fa, fb = 1.8, -1.8
            chosen_ret = 1.8
            used_fallback = True
        else:
            fa, fb = 0.4, 0.5
            chosen_ret = round((fa + fb) / 2, 3)
            used_fallback = False
        forecast_mean = round(current_close * (1 + chosen_ret / 100), 2)
        actual_mean = round(current_close * 1.003, 2)
        actual_ret_pct = round((actual_mean - current_close) / current_close * 100, 3)
        per_model_forecast_ret_pct = {
            "variant_a": round(fa, 3),
            "variant_b": round(fb, 3),
        }
        per_model_spread_pct = round(abs(fa - fb), 3)
        rows.append({
            "symbol": sym,
            "current_close": round(current_close, 2),
            "forecast_mean": forecast_mean,
            "forecast_ret_pct": round(chosen_ret, 3),
            "actual_mean": actual_mean,
            "actual_ret_pct": actual_ret_pct,
            "mase": 1.7,
            "per_model_forecast_ret_pct": per_model_forecast_ret_pct,
            "per_model_spread_pct": per_model_spread_pct,
            "used_fallback": used_fallback,
            "fallback_reason": (
                f"per-model |forecast_ret_pct| spread {per_model_spread_pct:.3f}% > "
                f"fallback_spread_pct {DEFAULT_FALLBACK_SPREAD_PCT:.3f}%; "
                f"using single-model variant_a"
                if used_fallback else None
            ),
            "eval_date": FIXTURE_EVAL_DATE_STR,
        })
    return rows


def _stable_subset_per_date(obj: dict) -> dict:
    return {
        "model": obj["model"],
        "eval_date": obj["eval_date"],
        "rows": obj["rows"],
    }


def _stable_subset_summary(obj: dict) -> dict:
    return {
        "model": obj["model"],
        "accumulated": obj["accumulated"],
        "promotion_gate": obj["promotion_gate"],
    }


def _stable_subset_ensemble_per_date(obj: dict) -> dict:
    return {
        "ensemble_name": obj["ensemble_name"],
        "ensemble_models": obj["ensemble_models"],
        "eval_date": obj["eval_date"],
        "fallback_spread_pct": obj["fallback_spread_pct"],
        "n_symbols": obj["n_symbols"],
        "n_used_fallback": obj["n_used_fallback"],
        "rows": obj["rows"],
    }


def _write_shadow_trade_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    """Mirror tests/test_shadow_determinism.py: drive write_shadow_artifacts."""
    from scripts.shadow_trade import (
        DEFAULT_SIGNAL_THRESHOLD_PCT as _THR,  # noqa: F401
        aggregate_shadow_rows,
        write_shadow_artifacts,
    )
    from datetime import date
    rows = _build_shadow_trade_rows()
    per_date = {FIXTURE_EVAL_DATE_STR: rows}
    aggregate = aggregate_shadow_rows(rows, threshold=FIXTURE_THRESHOLD_PCT)
    result = {
        "model_name": FIXTURE_MODEL,
        "today": date(2026, 6, 15),
        "per_date": per_date,
        "aggregate": aggregate,
    }
    written = write_shadow_artifacts(result, tmp_path, threshold=FIXTURE_THRESHOLD_PCT)
    per_date_path = tmp_path / FIXTURE_MODEL / f"{FIXTURE_EVAL_DATE_STR}.json"
    summary_path = tmp_path / FIXTURE_MODEL / "SUMMARY.json"
    if per_date_path not in written:
        sys.exit(f"[refresh-baseline] expected {per_date_path} in written paths")
    if summary_path not in written:
        sys.exit(f"[refresh-baseline] expected {summary_path} in written paths")
    return per_date_path, summary_path


def _write_ensemble_artifact(tmp_path: Path) -> Path:
    rows = _build_ensemble_rows()
    ensemble_dir = tmp_path / "ensemble"
    ensemble_dir.mkdir(parents=True, exist_ok=True)
    ensemble_obj = {
        "ensemble_name": "A_B_ensemble",
        "ensemble_models": list(FIXTURE_ENSEMBLE_LABELS),
        "eval_date": FIXTURE_EVAL_DATE_STR,
        "today": "2026-06-15",
        "fallback_spread_pct": DEFAULT_FALLBACK_SPREAD_PCT,
        "n_symbols": len(FIXTURE_SYMBOLS),
        "n_used_fallback": sum(1 for r in rows if r["used_fallback"]),
        "rows": rows,
    }
    out_path = ensemble_dir / f"{FIXTURE_EVAL_DATE_STR}.json"
    out_path.write_text(json.dumps(ensemble_obj, indent=2, default=str))
    return out_path


def _git_head_short() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL,
        ).decode("utf-8").strip()
    except Exception:
        return "unknown"


def main():
    import tempfile
    if not FIXTURE_PATH.parent.exists():
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Load existing baseline (if present) to compute schema_version bump.
    old_baseline: Dict[str, Any] = {}
    old_schema_version = 0
    if FIXTURE_PATH.exists():
        try:
            old_baseline = json.loads(FIXTURE_PATH.read_text())
            old_schema_version = int(old_baseline.get("schema_version", 0))
        except (json.JSONDecodeError, ValueError) as e:
            sys.exit(f"[refresh-baseline] existing baseline unreadable: {e}")

    new_schema_version = old_schema_version + 1

    with tempfile.TemporaryDirectory(prefix="det_baseline_") as td:
        tmp = Path(td)

        per_date_path, summary_path = _write_shadow_trade_artifacts(tmp)
        ensemble_path = _write_ensemble_artifact(tmp)

        per_date_hash = _hash_json(
            _stable_subset_per_date(json.loads(per_date_path.read_text()))
        )
        summary_hash = _hash_json(
            _stable_subset_summary(json.loads(summary_path.read_text()))
        )
        ensemble_hash = _hash_json(
            _stable_subset_ensemble_per_date(json.loads(ensemble_path.read_text()))
        )

    new_baseline = {
        "schema_version": new_schema_version,
        "locked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fixture_symbols": FIXTURE_SYMBOLS,
        "fixture_eval_date": FIXTURE_EVAL_DATE_STR,
        "fixture_seed": FIXTURE_SEED,
        "fixture_ohlcv_rows_per_symbol": FIXTURE_OHLCV_ROWS_PER_SYMBOL,
        "backend_kind": "DeterministicFakeChronos",
        "hashes": {
            "shadow_trade_baseline_v1_2026-06-15": per_date_hash,
            "shadow_trade_baseline_v1_SUMMARY": summary_hash,
            "shadow_ensemble_2026-06-15": ensemble_hash,
        },
        "lock_commit": _git_head_short(),
    }

    FIXTURE_PATH.write_text(json.dumps(new_baseline, indent=2, default=str) + "\n")

    print(f"[refresh-baseline] wrote {FIXTURE_PATH}")
    print(f"[refresh-baseline] schema_version: {old_schema_version} → {new_schema_version}")
    print(f"[refresh-baseline] lock_commit: {new_baseline['lock_commit']}")
    print(f"[refresh-baseline] hashes:")
    print(f"  shadow_trade_baseline_v1_2026-06-15 = {per_date_hash}")
    print(f"  shadow_trade_baseline_v1_SUMMARY    = {summary_hash}")
    print(f"  shadow_ensemble_2026-06-15           = {ensemble_hash}")
    if new_schema_version > 1:
        print()
        print(f"⚠ Remember to cite schema_version bump "
              f"({old_schema_version} → {new_schema_version}) in the commit message.")


if __name__ == "__main__":
    main()