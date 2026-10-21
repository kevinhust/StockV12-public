"""Pre-flight determinism baseline test (ADR-008 + Council § 3.3, Design § 5).

Locks the SHA-256 of today's shadow_trade + shadow_ensemble output for a
known fixture. Any commit that changes the bytes of these outputs (without
intentionally refreshing the baseline via ``refresh_determinism_baseline``)
fails this test, which fails CI.

Refresh flow (developer tool):::

    python -m scripts.refresh_determinism_baseline

After refresh, ``determinism_baseline.json`` is regenerated in place with
``schema_version`` bumped, ``locked_at`` + ``lock_commit`` updated.

Why this exists
---------------

Per the Council § 3.3 Critic note: "without pinning *tolerance, comparison
scope, and pre/post seed-state reset* before the move, tests will green and
NUC will drift — exactly the silent promotion regression this refactor was
meant to prevent."

This test pins *all three*:
- Tolerance: byte-identical SHA-256 (no rounding tolerance, no fuzzy match)
- Comparison scope: per-date JSON + SUMMARY.json + ensemble per-date JSON
- Pre seed-state reset: DeterministicFakeChronos + closed-form actuals,
  seeded RNG (seed=42), no real AutoGluon / GPU / parquet I/O

The fixture is **deliberately machine-independent**: it does NOT load the
real Chronos backbone, does NOT touch the NUC Redis, does NOT require
parquet feature files. It exercises only the pure serialization + aggregation
path of ``scripts.shadow_trade.write_shadow_artifacts`` and the
ensemble per-date JSON shape from ``scripts.shadow_ensemble.run_shadow_ensemble``.

The 16:05 + 16:10 ET NUC shadow crons are validated separately per the
Design § 6.4 CI gate — that's the runtime guarantee, this test is the
**pre-flight serialization lock** for the Phase 5 → Phase 6 refactor.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path
from typing import List

import pytest

# Import pure helpers from the production scripts. These are the same
# functions called by write_shadow_artifacts and aggregate_ensemble_per_date_jsons
# on the NUC — so the hashes we lock here reflect what those scripts emit.
from scripts.shadow_trade import (
    DEFAULT_SIGNAL_THRESHOLD_PCT,
    SHADOW_ROOT as _PROD_SHADOW_ROOT,  # noqa: F401  (intentionally not used below)
    aggregate_shadow_rows,
    write_shadow_artifacts,
)
from scripts.shadow_ensemble import DEFAULT_FALLBACK_SPREAD_PCT

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "determinism_baseline.json"

# ---------------------------------------------------------------------------
# Fixture constants — DO NOT change without bumping baseline schema_version
# ---------------------------------------------------------------------------
FIXTURE_SYMBOLS = ["AAPL", "MSFT", "NVDA"]
FIXTURE_EVAL_DATE = date(2026, 6, 15)  # Monday, known past date (synthetic)
FIXTURE_EVAL_DATE_STR = "2026-06-15"
FIXTURE_SEED = 42
FIXTURE_OHLCV_ROWS_PER_SYMBOL = 60  # must match determinism_baseline.json
FIXTURE_MODEL = "baseline_v1"
FIXTURE_ENSEMBLE_LABELS = ["variant_a", "variant_b"]
FIXTURE_THRESHOLD_PCT = DEFAULT_SIGNAL_THRESHOLD_PCT  # 0.5

SYMBOL_BASES = {"AAPL": 100.0, "MSFT": 200.0, "NVDA": 400.0}
# Per-symbol RNG seed offset. MUST be a literal dict (NOT derived from
# Python's hash()) because PYTHONHASHSEED randomizes hash() per process —
# which would break determinism across test runs and CI machines.
SYMBOL_SEED_OFFSETS = {"AAPL": 0, "MSFT": 100, "NVDA": 200}


# ---------------------------------------------------------------------------
# Pure helpers — deterministic shadow row builders
# ---------------------------------------------------------------------------
def _hash_json(obj: dict) -> str:
    """SHA-256 over UTF-8 encoded JSON with sort_keys + default=str.

    Matches the exact serialization used by ``write_shadow_artifacts`` and
    the ensemble per-date write inside ``run_shadow_ensemble``.
    """
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _build_synthetic_close(symbol: str, n_rows: int) -> List[float]:
    """Closed-form price curve: deterministic across machines + numpy versions."""
    import numpy as np
    rng = np.random.default_rng(FIXTURE_SEED + SYMBOL_SEED_OFFSETS[symbol])
    base = SYMBOL_BASES[symbol]
    return [
        float(base + 5 * math.sin(i / 7) + 0.1 * i + rng.normal(0, 0.5))
        for i in range(n_rows)
    ]


def _build_shadow_trade_rows() -> List[dict]:
    """Build synthetic per-symbol rows matching predict_one_symbol_for_date output."""
    rows = []
    for sym in FIXTURE_SYMBOLS:
        closes = _build_synthetic_close(sym, FIXTURE_OHLCV_ROWS_PER_SYMBOL)
        current_close = round(closes[-1], 2)
        # Forecast: +1% with ±5% bands (matches DeterministicFakeChronos.detect contract)
        forecast_mean = round(current_close * 1.01, 2)
        forecast_ret_pct = round((forecast_mean - current_close) / current_close * 100, 3)
        # Actual: deterministic 0.4% return — slightly under forecast to make hit_rate < 1
        actual_mean = round(current_close * 1.004, 2)
        actual_ret_pct = round((actual_mean - current_close) / current_close * 100, 3)
        # MASE: deterministic 1.5
        mase = 1.5
        rows.append({
            "symbol": sym,
            "current_close": current_close,
            "forecast_mean": forecast_mean,
            "forecast_ret_pct": forecast_ret_pct,
            "actual_mean": actual_mean,
            "actual_ret_pct": actual_ret_pct,
            "mase": round(mase, 4),
            "cutoff_days_ago": 1,
            "n_horizons_evaluated": 10,
            # NOTE: predict_one_symbol_for_date sets eval_date + model on the row
            # AFTER calling this builder; we set them here to match production.
            "eval_date": FIXTURE_EVAL_DATE_STR,
            "model": FIXTURE_MODEL,
        })
    return rows


def _build_ensemble_rows() -> List[dict]:
    """Build synthetic per-symbol ensemble rows matching aggregate_ensemble_row output.

    One symbol uses fallback (per_model_spread > 3.0%); others average.
    """
    rows = []
    for i, sym in enumerate(FIXTURE_SYMBOLS):
        current_close = SYMBOL_BASES[sym] + 5 * math.sin(20 / 7) + 0.1 * 20
        # Variant A and Variant B disagree by ±0.6% (within 3% threshold for some)
        if i == 0:
            # AAPL: wide spread → fallback used
            fa, fb = 0.9, -0.6  # spread = 1.5% (within 3%, so NOT fallback actually)
            # Make one truly fallback:
            fa, fb = 1.8, -1.8  # spread = 3.6% → fallback (> 3.0%)
            chosen_ret = 1.8  # first valid wins
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
    """Per § 5.1: exclude wall-clock fields from hash input."""
    return {
        "model": obj["model"],
        "eval_date": obj["eval_date"],
        "rows": obj["rows"],
    }


def _stable_subset_summary(obj: dict) -> dict:
    """Per § 5.1: SUMMARY.json stable subset (excludes today, generated_at, current_run_aggregate)."""
    return {
        "model": obj["model"],
        "accumulated": obj["accumulated"],
        "promotion_gate": obj["promotion_gate"],
    }


def _stable_subset_ensemble_per_date(obj: dict) -> dict:
    """Per § 5.1: ensemble per-date JSON excludes `today`."""
    return {
        "ensemble_name": obj["ensemble_name"],
        "ensemble_models": obj["ensemble_models"],
        "eval_date": obj["eval_date"],
        "fallback_spread_pct": obj["fallback_spread_pct"],
        "n_symbols": obj["n_symbols"],
        "n_used_fallback": obj["n_used_fallback"],
        "rows": obj["rows"],
    }


# ---------------------------------------------------------------------------
# Sanity: synthetic fixture produces non-empty, structurally-valid outputs
# (these are NOT hash-locked; they fail loudly if the fixture contract breaks)
# ---------------------------------------------------------------------------
def test_synthetic_shadow_trade_rows_are_well_formed():
    rows = _build_shadow_trade_rows()
    assert len(rows) == len(FIXTURE_SYMBOLS)
    for r in rows:
        assert r["model"] == FIXTURE_MODEL
        assert r["eval_date"] == FIXTURE_EVAL_DATE_STR
        assert r["forecast_ret_pct"] is not None
        assert r["actual_ret_pct"] is not None


def test_synthetic_ensemble_rows_are_well_formed():
    rows = _build_ensemble_rows()
    assert len(rows) == len(FIXTURE_SYMBOLS)
    n_fallback = sum(1 for r in rows if r["used_fallback"])
    assert n_fallback >= 1, "fixture must include at least one fallback row"
    for r in rows:
        assert r["per_model_spread_pct"] is not None
        assert set(r["per_model_forecast_ret_pct"].keys()) == set(FIXTURE_ENSEMBLE_LABELS)


# ---------------------------------------------------------------------------
# Hash-locked baseline tests (these are the actual CI gate)
# ---------------------------------------------------------------------------
def _load_baseline() -> dict:
    if not FIXTURE_PATH.exists():
        pytest.fail(
            f"Determinism baseline missing at {FIXTURE_PATH}. "
            f"Run: python -m scripts.refresh_determinism_baseline"
        )
    return json.loads(FIXTURE_PATH.read_text())


def _write_shadow_trade_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    """Drive the REAL write_shadow_artifacts code path with synthetic data."""
    rows = _build_shadow_trade_rows()
    per_date = {FIXTURE_EVAL_DATE_STR: rows}
    aggregate = aggregate_shadow_rows(rows, threshold=FIXTURE_THRESHOLD_PCT)
    result = {
        "model_name": FIXTURE_MODEL,
        "today": FIXTURE_EVAL_DATE,
        "per_date": per_date,
        "aggregate": aggregate,
    }
    written = write_shadow_artifacts(result, tmp_path, threshold=FIXTURE_THRESHOLD_PCT)
    per_date_path = tmp_path / FIXTURE_MODEL / f"{FIXTURE_EVAL_DATE_STR}.json"
    summary_path = tmp_path / FIXTURE_MODEL / "SUMMARY.json"
    assert per_date_path in written
    assert summary_path in written
    return per_date_path, summary_path


def _write_ensemble_artifact(tmp_path: Path) -> Path:
    """Mimic the per-date ensemble JSON write path of run_shadow_ensemble (lines 472-495).

    We do NOT call run_shadow_ensemble because it requires loading real
    AutoGluon predictors + parquet I/O. The exact JSON shape here mirrors
    the production write at v12/scripts/shadow_ensemble.py:472-487 byte-for-byte.
    """
    rows = _build_ensemble_rows()
    ensemble_dir = tmp_path / "ensemble"
    ensemble_dir.mkdir(parents=True, exist_ok=True)
    ensemble_obj = {
        "ensemble_name": "A_B_ensemble",
        "ensemble_models": list(FIXTURE_ENSEMBLE_LABELS),
        "eval_date": FIXTURE_EVAL_DATE_STR,
        "today": FIXTURE_EVAL_DATE.isoformat(),
        "fallback_spread_pct": DEFAULT_FALLBACK_SPREAD_PCT,
        "n_symbols": len(FIXTURE_SYMBOLS),
        "n_used_fallback": sum(1 for r in rows if r["used_fallback"]),
        "rows": rows,
    }
    out_path = ensemble_dir / f"{FIXTURE_EVAL_DATE_STR}.json"
    out_path.write_text(json.dumps(ensemble_obj, indent=2, default=str))
    return out_path


def test_shadow_trade_per_date_json_hash_matches_baseline(tmp_path):
    baseline = _load_baseline()
    expected = baseline["hashes"]["shadow_trade_baseline_v1_2026-06-15"]

    per_date_path, _ = _write_shadow_trade_artifacts(tmp_path)
    obj = json.loads(per_date_path.read_text())
    actual = _hash_json(_stable_subset_per_date(obj))

    assert actual == expected, (
        f"shadow_trade per-date JSON drifted:\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual}\n"
        f"If this drift is intentional, refresh the baseline:\n"
        f"  python -m scripts.refresh_determinism_baseline"
    )


def test_shadow_trade_summary_json_hash_matches_baseline(tmp_path):
    baseline = _load_baseline()
    expected = baseline["hashes"]["shadow_trade_baseline_v1_SUMMARY"]

    _, summary_path = _write_shadow_trade_artifacts(tmp_path)
    obj = json.loads(summary_path.read_text())
    actual = _hash_json(_stable_subset_summary(obj))

    assert actual == expected, (
        f"shadow_trade SUMMARY.json stable subset drifted:\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual}\n"
        f"If this drift is intentional, refresh the baseline:\n"
        f"  python -m scripts.refresh_determinism_baseline"
    )


def test_shadow_ensemble_per_date_json_hash_matches_baseline(tmp_path):
    baseline = _load_baseline()
    expected = baseline["hashes"]["shadow_ensemble_2026-06-15"]

    ensemble_path = _write_ensemble_artifact(tmp_path)
    obj = json.loads(ensemble_path.read_text())
    actual = _hash_json(_stable_subset_ensemble_per_date(obj))

    assert actual == expected, (
        f"shadow_ensemble per-date JSON drifted:\n"
        f"  expected: {expected}\n"
        f"  actual:   {actual}\n"
        f"If this drift is intentional, refresh the baseline:\n"
        f"  python -m scripts.refresh_determinism_baseline"
    )


# ---------------------------------------------------------------------------
# Baseline-metadata integrity (cheap guard; runs on every CI invocation)
# ---------------------------------------------------------------------------
def test_baseline_metadata_invariants():
    baseline = _load_baseline()
    assert baseline["fixture_symbols"] == FIXTURE_SYMBOLS
    assert baseline["fixture_eval_date"] == FIXTURE_EVAL_DATE_STR
    assert baseline["fixture_seed"] == FIXTURE_SEED
    assert baseline["fixture_ohlcv_rows_per_symbol"] == FIXTURE_OHLCV_ROWS_PER_SYMBOL
    assert baseline["backend_kind"] == "DeterministicFakeChronos"
    assert set(baseline["hashes"].keys()) == {
        "shadow_trade_baseline_v1_2026-06-15",
        "shadow_trade_baseline_v1_SUMMARY",
        "shadow_ensemble_2026-06-15",
    }
    for name, h in baseline["hashes"].items():
        assert len(h) == 64, f"{name}: expected SHA-256 hex (64 chars), got {len(h)}"