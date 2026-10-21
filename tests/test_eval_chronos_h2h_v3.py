"""Tests for H2H eval pure functions: compute_mase, summarize_forecasts, rank_models.

These functions are extracted from v12/scripts/eval_chronos_h2h_v3.py so the
math is unit-testable without spinning up AutoGluon + GPU models.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# compute_mase
# ---------------------------------------------------------------------------
# MASE = mean(|actual - predicted|) / mean(|naive_error|)
# where naive_error[t] = actual[t] - actual[t-1]  (seasonal-naive with season=1)
# Lower is better; 1.0 means "as good as a naive forecast", <1 better than naive.


def test_compute_mase_perfect_forecast():
    from scripts.eval_chronos_h2h_v3 import compute_mase

    actual = [10.0, 11.0, 12.0, 13.0, 14.0]
    predicted = [10.0, 11.0, 12.0, 13.0, 14.0]  # identical
    naive_errors = [1.0, 1.0, 1.0, 1.0]  # 1-step diffs of actual
    assert compute_mase(actual, predicted, naive_errors) == 0.0


def test_compute_mase_naive_forecast_scores_1():
    from scripts.eval_chronos_h2h_v3 import compute_mase

    actual = [10.0, 11.0, 12.0, 13.0, 14.0]
    # "predicted" = actual shifted by 1 → forecast = last value, error = step diff
    predicted = [9.0, 10.0, 11.0, 12.0, 13.0]
    naive_errors = [1.0, 1.0, 1.0, 1.0]  # all unit steps
    # abs(actual - predicted) = [1,1,1,1,1], mean = 1.0
    # mean(|naive|) = 1.0  → MASE = 1.0
    assert compute_mase(actual, predicted, naive_errors) == pytest.approx(1.0)


def test_compute_mase_better_than_naive_scores_below_1():
    from scripts.eval_chronos_h2h_v3 import compute_mase

    actual = [10.0, 11.0, 12.0, 13.0, 14.0]
    predicted = [10.5, 11.3, 12.1, 13.0, 14.0]  # closer than naive
    naive_errors = [1.0, 1.0, 1.0, 1.0]
    # mean abs error = (0.5+0.3+0.1+0+0)/5 = 0.18
    # mean naive = 1.0  → MASE = 0.18
    assert compute_mase(actual, predicted, naive_errors) == pytest.approx(0.18, abs=1e-9)


def test_compute_mase_worse_than_naive_scores_above_1():
    from scripts.eval_chronos_h2h_v3 import compute_mase

    actual = [10.0, 11.0, 12.0, 13.0, 14.0]
    predicted = [100.0, 100.0, 100.0, 100.0, 100.0]  # way off
    naive_errors = [1.0, 1.0, 1.0, 1.0]
    # |actual-predicted| = [90,89,88,87,86], mean = 88.0
    # mean(|naive|) = 1.0  → MASE = 88.0
    assert compute_mase(actual, predicted, naive_errors) > 1.0
    assert math.isclose(
        compute_mase(actual, predicted, naive_errors), 88.0, abs_tol=0.01
    )


def test_compute_mase_raises_on_zero_naive_denominator():
    """If actual never changes (flat series), naive_errors sum to 0 → division
    by zero. We should raise rather than silently return inf — the test set
    is broken in that case."""
    from scripts.eval_chronos_h2h_v3 import compute_mase

    actual = [10.0, 10.0, 10.0, 10.0, 10.0]
    predicted = [10.0, 10.0, 10.0, 10.0, 10.0]
    naive_errors = [0.0, 0.0, 0.0, 0.0]
    with pytest.raises(ValueError, match="naive"):
        compute_mase(actual, predicted, naive_errors)


def test_compute_mase_raises_on_length_mismatch():
    from scripts.eval_chronos_h2h_v3 import compute_mase

    with pytest.raises(ValueError, match="length"):
        compute_mase([1.0, 2.0], [1.0], [1.0])


# ---------------------------------------------------------------------------
# naive_errors_from_actual — helper used to build naive_errors
# ---------------------------------------------------------------------------


def test_naive_errors_from_actual():
    from scripts.eval_chronos_h2h_v3 import naive_errors_from_actual

    actual = [10.0, 12.0, 11.0, 15.0]
    # 1-step diffs: [12-10, 11-12, 15-11] = [2, -1, 4]
    assert naive_errors_from_actual(actual) == [2.0, -1.0, 4.0]


def test_naive_errors_short_series_returns_empty():
    from scripts.eval_chronos_h2h_v3 import naive_errors_from_actual

    assert naive_errors_from_actual([10.0]) == []


# ---------------------------------------------------------------------------
# summarize_forecasts
# ---------------------------------------------------------------------------


def test_summarize_forecasts_basic():
    from scripts.eval_chronos_h2h_v3 import summarize_forecasts

    rows = [
        {"symbol": "AAPL", "forecast_ret_pct": 1.0},
        {"symbol": "NVDA", "forecast_ret_pct": 2.0},
        {"symbol": "TSLA", "forecast_ret_pct": -0.5},
    ]
    agg = summarize_forecasts(rows, key="forecast_ret_pct")
    assert agg["n"] == 3
    assert agg["mean"] == pytest.approx(0.833, abs=1e-3)
    assert agg["median"] == 1.0
    assert agg["min"] == -0.5
    assert agg["max"] == 2.0
    # stdev sanity: population stdev of [1, 2, -0.5] = 1.0274
    # (variance = ((1-0.833)² + (2-0.833)² + (-0.5-0.833)²) / 3 = 1.0556 → sqrt = 1.0274)
    assert agg["stdev"] == pytest.approx(1.0274, abs=1e-3)


def test_summarize_forecasts_counts_above_threshold():
    """SR-01 threshold is 0.5% in v12; a model is useful iff |ret| > 0.5
    fires often enough."""
    from scripts.eval_chronos_h2h_v3 import summarize_forecasts

    rows = [
        {"forecast_ret_pct": 0.6},
        {"forecast_ret_pct": 0.4},
        {"forecast_ret_pct": -0.7},
        {"forecast_ret_pct": 0.05},
    ]
    agg = summarize_forecasts(rows, key="forecast_ret_pct", threshold=0.5)
    assert agg["n_above_threshold"] == 2  # 0.6 and -0.7


def test_summarize_forecasts_empty_input():
    from scripts.eval_chronos_h2h_v3 import summarize_forecasts

    agg = summarize_forecasts([], key="forecast_ret_pct")
    assert agg["n"] == 0
    assert agg["mean"] is None
    assert agg["median"] is None
    assert agg["stdev"] is None


# ---------------------------------------------------------------------------
# rank_models
# ---------------------------------------------------------------------------


def test_rank_models_picks_lowest_mase():
    from scripts.eval_chronos_h2h_v3 import rank_models

    results = {
        "phase2_v2": {"mase": 0.95},
        "v3_ckpt_950k": {"mase": 0.80},  # best
        "v3_ckpt_1000k": {"mase": 0.90},
        "v3_ckpt_940k": {"mase": 0.88},
    }
    ranking = rank_models(results, metric="mase", lower_is_better=True)
    assert ranking[0] == "v3_ckpt_950k"
    assert ranking[-1] == "phase2_v2"
    # check full ordering
    assert ranking == ["v3_ckpt_950k", "v3_ckpt_940k", "v3_ckpt_1000k", "phase2_v2"]


def test_rank_models_higher_is_better():
    from scripts.eval_chronos_h2h_v3 import rank_models

    results = {
        "A": {"score": 13.0},
        "B": {"score": 14.0},  # best (higher is better here)
        "C": {"score": 12.0},
    }
    ranking = rank_models(results, metric="score", lower_is_better=False)
    assert ranking[0] == "B"
    assert ranking[-1] == "C"


def test_rank_models_skips_missing_metric():
    from scripts.eval_chronos_h2h_v3 import rank_models

    results = {
        "A": {"mase": 0.9},
        "B": {},  # missing metric — skip
        "C": {"mase": 0.8},
    }
    ranking = rank_models(results, metric="mase", lower_is_better=True)
    assert ranking == ["C", "A"]


def test_rank_models_all_missing_returns_empty():
    from scripts.eval_chronos_h2h_v3 import rank_models

    assert rank_models({"A": {}, "B": {}}, metric="mase") == []


# ---------------------------------------------------------------------------
# Directional-signal metrics (added per v3 postmortem Stage 1.2)
# ---------------------------------------------------------------------------
# For trading, "did the sign of forecast match the sign of actual return?"
# is what matters. v2 hit_rate must be ≥ 0.53 on OOS to clear the Stage 1 gate.


def test_sign_positive():
    from scripts.eval_chronos_h2h_v3 import sign
    assert sign(1.5) == 1
    assert sign(0.0001) == 1


def test_sign_negative():
    from scripts.eval_chronos_h2h_v3 import sign
    assert sign(-0.5) == -1
    assert sign(-100.0) == -1


def test_sign_zero_for_small_values():
    from scripts.eval_chronos_h2h_v3 import sign
    assert sign(0.0) == 0
    assert sign(1e-12) == 0
    assert sign(-1e-12) == 0


def test_sign_match_true_when_both_positive():
    from scripts.eval_chronos_h2h_v3 import sign_match
    assert sign_match(1.0, 0.3) is True  # both up
    assert sign_match(-1.0, -0.1) is True  # both down
    assert sign_match(0.0, 0.0) is True  # both flat


def test_sign_match_false_when_disagree():
    from scripts.eval_chronos_h2h_v3 import sign_match
    assert sign_match(1.0, -0.3) is False
    assert sign_match(-1.0, 0.5) is False
    # flat-vs-moving: sign says no call
    assert sign_match(0.0, 1.0) is False
    assert sign_match(1.0, 0.0) is False


def test_hit_rate_perfect():
    from scripts.eval_chronos_h2h_v3 import hit_rate
    pairs = [(1.0, 2.0), (-1.0, -0.5), (0.5, 0.3), (-2.0, -3.0)]
    res = hit_rate(pairs)
    assert res["n"] == 4
    assert res["n_correct"] == 4
    assert res["hit_rate"] == 1.0
    assert res["n_both_zero"] == 0


def test_hit_rate_all_wrong():
    from scripts.eval_chronos_h2h_v3 import hit_rate
    pairs = [(1.0, -1.0), (-1.0, 1.0), (0.5, -0.3)]
    res = hit_rate(pairs)
    assert res["n"] == 3
    assert res["n_correct"] == 0
    assert res["hit_rate"] == 0.0


def test_hit_rate_mixed_with_flat_pairs():
    from scripts.eval_chronos_h2h_v3 import hit_rate
    # 4 pairs: 2 correct, 1 wrong, 1 both-flat (correct)
    pairs = [(1.0, 2.0), (-1.0, 1.0), (0.0, 0.0), (0.5, 0.3)]
    res = hit_rate(pairs)
    assert res["n"] == 4
    assert res["n_correct"] == 3  # (1,2), (0,0), (0.5,0.3) all sign-match
    assert res["hit_rate"] == 0.75
    assert res["n_both_zero"] == 1


def test_hit_rate_empty():
    from scripts.eval_chronos_h2h_v3 import hit_rate
    res = hit_rate([])
    assert res["n"] == 0
    assert res["n_correct"] == 0
    assert res["hit_rate"] is None
    assert res["n_both_zero"] == 0


def test_signal_hit_rate_filters_by_threshold():
    """signal_hit_rate only counts symbols where |forecast| > threshold —
    i.e. the model made a call. Flat forecasts are excluded."""
    from scripts.eval_chronos_h2h_v3 import signal_hit_rate
    forecasts = [1.0, 0.1, -2.0, 0.0, 0.8]  # 3 of 5 above 0.5% threshold
    actuals = [0.5, 0.2, -1.5, 0.0, -0.3]
    res = signal_hit_rate(forecasts, actuals, threshold=0.5)
    # Signals are pairs (1.0, 0.5)=same sign, (-2.0, -1.5)=same, (0.8, -0.3)=diff
    # → 2 of 3 correct
    assert res["n_signals"] == 3
    assert res["n_correct"] == 2
    assert res["hit_rate"] == pytest.approx(0.6667, abs=1e-3)
    assert res["threshold"] == 0.5


def test_signal_hit_rate_no_signals_returns_null():
    from scripts.eval_chronos_h2h_v3 import signal_hit_rate
    res = signal_hit_rate([0.1, 0.2, 0.3], [0.5, -0.3, 0.1], threshold=0.5)
    assert res["n_signals"] == 0
    assert res["n_correct"] == 0
    assert res["hit_rate"] is None


def test_signal_hit_rate_length_mismatch_raises():
    from scripts.eval_chronos_h2h_v3 import signal_hit_rate
    with pytest.raises(ValueError, match="length"):
        signal_hit_rate([1.0, 2.0], [1.0])


# ---------------------------------------------------------------------------
# Smoke variant (Stage 1.1 CI gate)
# ---------------------------------------------------------------------------
class TestSmokeVariant:
    """Stage 1.1 of V3_POSTMORTEM_AND_TRAINING_PLAN.md — smoke H2H variant.

    A thin "smoke" version of the H2H eval that runs on 3 fast-loading
    symbols + 1 cutoff. Designed for PR-gate CI: <5 min runtime when GPU
    is present, <1 min wall clock without. The pure resolver picks the
    right (symbols, cutoffs) pair given a smoke flag.
    """

    def test_smoke_symbols_are_a_subset_of_full(self):
        from scripts.eval_chronos_h2h_v3 import SMOKE_SYMBOLS, DEFAULT_TEST_SYMBOLS
        assert set(SMOKE_SYMBOLS).issubset(set(DEFAULT_TEST_SYMBOLS))
        # Exactly 3 symbols for <5 min runtime.
        assert len(SMOKE_SYMBOLS) == 3

    def test_smoke_cutoffs_is_singleton(self):
        from scripts.eval_chronos_h2h_v3 import SMOKE_CUTOFFS
        assert SMOKE_CUTOFFS == [10]

    def test_resolve_smoke_args_returns_smoke_when_flag_set(self):
        from scripts.eval_chronos_h2h_v3 import (
            SMOKE_SYMBOLS, SMOKE_CUTOFFS,
            resolve_smoke_args,
        )
        # Fake argparse Namespace
        ns = SimpleNamespace(symbols=None, cutoffs=None, smoke=True)
        out = resolve_smoke_args(ns)
        assert out["symbols"] == SMOKE_SYMBOLS
        assert out["cutoffs"] == SMOKE_CUTOFFS
        assert out["source"] == "smoke"

    def test_resolve_smoke_args_returns_explicit_when_flag_unset(self):
        from scripts.eval_chronos_h2h_v3 import (
            resolve_smoke_args,
        )
        ns = SimpleNamespace(symbols=["AAPL", "JPM"], cutoffs=[5, 15], smoke=False)
        out = resolve_smoke_args(ns)
        assert out["symbols"] == ["AAPL", "JPM"]
        assert out["cutoffs"] == [5, 15]
        assert out["source"] == "explicit"

    def test_resolve_smoke_args_returns_defaults_when_no_args(self):
        from scripts.eval_chronos_h2h_v3 import (
            DEFAULT_TEST_SYMBOLS, resolve_smoke_args,
        )
        ns = SimpleNamespace(symbols=None, cutoffs=None, smoke=False)
        out = resolve_smoke_args(ns)
        assert out["symbols"] == DEFAULT_TEST_SYMBOLS
        assert out["cutoffs"] == [10]  # default per argparse
        assert out["source"] == "default"

    def test_is_gpu_available_returns_bool_without_crashing(self):
        from scripts.eval_chronos_h2h_v3 import is_gpu_available
        # Don't assert True/False (depends on env); just that it returns
        # a bool and doesn't raise.
        result = is_gpu_available()
        assert isinstance(result, bool)
