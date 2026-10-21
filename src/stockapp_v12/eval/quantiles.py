"""Forecast quantile extraction helper (hygiene-C dedup, L1).

Consolidates the duplicated ``pred["mean"]/["0.1"]/["0.9"]`` fallback
logic that lived in two places before hygiene-C:

  - ``eval/predict.py:82-84`` (B2-extracted eval path)
  - ``services/ensemble_chronos_backend.py:177-180`` (production path)

Both try the AutoGluon quantile schema first; if absent, fall back to
``pred.iloc[:horizon].mean().mean()`` (proxy mean) with a 0.95/1.05
spread for q10/q90. Single source of truth here, callers import.

Future call sites: any new backend or eval script that consumes a
GluonTS / AutoGluon ``TimeSeriesPredictor`` prediction should use
this helper. If the schema ever changes (e.g. quantiles move from
``0.1``/``0.9`` to ``q10``/``q90``), update here once.
"""

from __future__ import annotations

from typing import Any, Tuple


def extract_quantile_band(
    pred: Any,
    horizon: int,
) -> Tuple[float, float, float]:
    """Extract ``(forecast_mean, forecast_q10, forecast_q90)`` from a predictor output.

    Tries the canonical AutoGluon quantile schema first (``pred["mean"]``,
    ``pred["0.1"]``, ``pred["0.9"]``); on any ``Exception`` (missing
    columns, schema drift) falls back to ``pred.iloc[:horizon].mean().mean()``
    for the mean and a 0.95/1.05 proxy spread for the quantiles.

    :param pred: A GluonTS / AutoGluon ``TimeSeriesPredictor.predict``
        output (a pandas object indexed by item_id+timestamp with
        quantile columns).
    :param horizon: Number of future steps to average over
        (``prediction_length`` in production, ``FORECAST_HORIZON``
        in eval).
    :return: ``(forecast_mean, forecast_q10, forecast_q90)`` as floats.
    """
    try:
        forecast_mean = float(pred["mean"].iloc[:horizon].mean())
        forecast_q10 = float(pred["0.1"].iloc[:horizon].mean())
        forecast_q90 = float(pred["0.9"].iloc[:horizon].mean())
    except Exception:
        forecast_mean = float(pred.iloc[:horizon].mean().mean())
        forecast_q10 = forecast_mean * 0.95
        forecast_q90 = forecast_mean * 1.05
    return forecast_mean, forecast_q10, forecast_q90


__all__ = ["extract_quantile_band"]