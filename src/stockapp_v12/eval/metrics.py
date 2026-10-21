"""
Pure metric functions for V12 evaluation (no I/O, no AutoGluon).

Moved from `v12/scripts/eval_chronos_h2h_v3.py` (B2, post-launch refactor).
100% covered by `v12/tests/test_eval_chronos_h2h_v3.py`.

This module MUST NOT import from `shadow_trade`, `shadow_ensemble`,
`stockapp_v12.services.*`, or anything with side effects — keeping it pure
is what makes `test_shadow_determinism.py`'s hit_rate/signal_hit_rate
checks reproducible.
"""

from __future__ import annotations


import numpy as np
import pandas as pd

SIGN_EPSILON = 1e-9


def naive_errors_from_actual(actual: list[float]) -> list[float]:
    """1-step differences of `actual`: actual[t] - actual[t-1].

    Returns an empty list for series of length <2 (no comparisons possible).
    """
    if len(actual) < 2:
        return []
    return [actual[t] - actual[t - 1] for t in range(1, len(actual))]


def compute_mase(
    actual: list[float], predicted: list[float], naive_errors: list[float]
) -> float:
    """Mean Absolute Scaled Error.

    MASE = mean(|actual - predicted|) / mean(|naive_error|)
    Lower is better; 1.0 = as good as a naive seasonal forecast.

    Raises:
      ValueError: if `actual` and `predicted` have different lengths, or
        mean(|naive_errors|) == 0 (degenerate series).
    """
    if len(actual) != len(predicted):
        raise ValueError(
            f"compute_mase: actual/predicted length mismatch "
            f"({len(actual)} vs {len(predicted)})"
        )
    if not naive_errors:
        raise ValueError("compute_mase: naive_errors empty — need >= 2 actuals")
    naive_scale = float(np.mean(np.abs(naive_errors)))
    if naive_scale == 0.0:
        raise ValueError(
            "compute_mase: mean(|naive_errors|) == 0 (flat series) — "
            "MASE undefined; check test set"
        )
    errs = np.abs(np.asarray(actual) - np.asarray(predicted))
    return float(np.mean(errs) / naive_scale)


def summarize_forecasts(
    rows: list[dict], key: str = "forecast_ret_pct", threshold: float = 0.5
) -> dict:
    """Aggregate stats over per-symbol forecast rows.

    Returns n / mean / median / stdev / min / max / n_above_threshold (where
    threshold is the |.| cutoff for "model produced a tradable signal"). Empty
    input returns n=0 with all stats set to None.
    """
    if not rows:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "stdev": None,
            "min": None,
            "max": None,
            "n_above_threshold": 0,
        }
    vals = pd.Series([r[key] for r in rows])
    return {
        "n": int(len(vals)),
        "mean": round(float(vals.mean()), 4),
        "median": round(float(vals.median()), 4),
        "stdev": round(float(vals.std(ddof=0)), 4),
        "min": round(float(vals.min()), 4),
        "max": round(float(vals.max()), 4),
        "n_above_threshold": int((vals.abs() > threshold).sum()),
    }


def rank_models(
    results: dict[str, dict], metric: str, lower_is_better: bool = True
) -> list[str]:
    """Sort model names by `metric`. Models missing the metric are skipped.

    Returns a list of names, best first. Empty list if no model has the metric.
    """
    items = [(name, m.get(metric)) for name, m in results.items()]
    items = [(n, v) for n, v in items if v is not None]
    if not items:
        return []
    items.sort(key=lambda nv: nv[1], reverse=not lower_is_better)
    return [n for n, _ in items]


# ---------------------------------------------------------------------------
# Directional-signal metrics (added per v3 postmortem Stage 1.2)
# ---------------------------------------------------------------------------
# For trading, MASE on magnitude is a poor proxy for edge. What matters is
# whether the sign of the forecast matches the sign of the actual return.
# Stage 1 gate: v2 sign-correctness hit_rate must be ≥ 0.53 on the 10-symbol
# OOS set (0.50 = coin flip). See v12/docs/V3_POSTMORTEM_AND_TRAINING_PLAN.md.


def sign(x: float) -> int:
    """Return -1, 0, or +1 based on the sign of `x`.

    Treats |x| < SIGN_EPSILON as 0 (avoids -0.0 / +0.0 ambiguity for tiny values).
    """
    if x > SIGN_EPSILON:
        return 1
    if x < -SIGN_EPSILON:
        return -1
    return 0


def sign_match(pct_a: float, pct_b: float) -> bool:
    """True if sign(pct_a) == sign(pct_b). Two flat values count as a match."""
    return sign(pct_a) == sign(pct_b)


def hit_rate(pct_pairs: list[tuple[float, float]]) -> dict:
    """Sign-correctness rate across (forecast, actual) return-percentage pairs.

    Returns a dict with:
      - n:            total number of pairs
      - n_correct:    count where sign(forecast) == sign(actual)
      - hit_rate:     n_correct / n (0..1), or None if n == 0
      - n_both_zero:  count of pairs where both forecast and actual were flat
                      (these count as "correct" by sign_match but represent
                      a non-call — useful for the trading-edge narrative)

    Example:
      pairs = [(1.0, 2.0), (-1.0, 1.0), (0.0, 0.0), (0.5, 0.3)]
      → 3 of 4 sign-match (the 2nd disagrees), hit_rate=0.75, n_both_zero=1
    """
    n = len(pct_pairs)
    if n == 0:
        return {"n": 0, "n_correct": 0, "hit_rate": None, "n_both_zero": 0}
    n_correct = 0
    n_both_zero = 0
    for a, b in pct_pairs:
        if sign(a) == sign(b):
            n_correct += 1
            if sign(a) == 0:
                n_both_zero += 1
    return {
        "n": n,
        "n_correct": n_correct,
        "hit_rate": round(n_correct / n, 4),
        "n_both_zero": n_both_zero,
    }


def signal_hit_rate(
    forecast_pcts: list[float], actual_pcts: list[float], threshold: float = 0.5
) -> dict:
    """Hit rate AMONG symbols where the model made a call (|forecast| > threshold).

    This is the SR-01 trading-edge metric: when the model says "go" (|forecast|
    crosses the 0.5% threshold), how often does the actual move agree? Symbols
    where the model stayed flat are excluded — we only grade "called" symbols.

    Returns a dict with:
      - n_signals:  count of pairs where |forecast| > threshold
      - n_correct:  count where sign(forecast) == sign(actual) among those
      - hit_rate:   n_correct / n_signals (0..1), or None if n_signals == 0
      - threshold:  the threshold used (echo for report readability)

    Raises:
      ValueError: if `forecast_pcts` and `actual_pcts` have different lengths.
    """
    if len(forecast_pcts) != len(actual_pcts):
        raise ValueError(
            f"signal_hit_rate: forecast/actual length mismatch "
            f"({len(forecast_pcts)} vs {len(actual_pcts)})"
        )
    signals = [
        (f, a) for f, a in zip(forecast_pcts, actual_pcts) if abs(f) > threshold
    ]
    n = len(signals)
    if n == 0:
        return {"n_signals": 0, "n_correct": 0, "hit_rate": None, "threshold": threshold}
    n_correct = sum(1 for f, a in signals if sign(f) == sign(a))
    return {
        "n_signals": n,
        "n_correct": n_correct,
        "hit_rate": round(n_correct / n, 4),
        "threshold": threshold,
    }