"""Per-stage unit tests for B3 pipeline/stages.py (hygiene-G).

B3 plan called out per-stage unit tests as optional. These pin the
two highest-leverage stages (yolo_filter + extract_price_atr helper)
that were uncovered in the baseline coverage report. Each stage is
called from the coordinator; per-stage tests catch regressions before
they reach integration.

Coverage baseline: stages.py was 67% covered; this file targets
the missing branches at lines 119-129 (yolo degraded fallback) and
236-241 (extract_price_atr with forecast.current_price fallback).
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import pandas as pd

from stockapp_v12.pipeline.stages import (
    extract_price_atr,
    run_stage_3_yolo_filter,
)
from stockapp_v12.services.yolo_filter import YoloFilterDecision


# ─── run_stage_3_yolo_filter ────────────────────────────────────────────────


def test_yolo_stage_returns_none_when_filter_disabled():
    """When orch.yolo_filter is None, stage returns None (Wave 9 off)."""
    orch = MagicMock()
    orch.yolo_filter = None

    result = run_stage_3_yolo_filter(orch, sym="AAPL", as_of=date(2026, 7, 8))

    assert result is None


def test_yolo_stage_returns_decision_on_happy_path():
    """Filter returns a YoloFilterDecision → stage passes it through."""
    orch = MagicMock()
    expected = YoloFilterDecision(
        symbol="AAPL", as_of=date(2026, 7, 8),
        image_date=None, image_path=None,
        yolo_probs={"hammer": 0.85}, predicted_class="hammer",
        predicted_conf=0.85, no_trade=False,
        reason="matched", degraded=False,
    )
    orch.yolo_filter.predict.return_value = expected

    result = run_stage_3_yolo_filter(orch, sym="AAPL", as_of=date(2026, 7, 8))

    assert result is expected
    orch.yolo_filter.predict.assert_called_once()


def test_yolo_stage_falls_back_to_degraded_decision_on_filter_exception():
    """Filter raises → stage returns degraded (no_trade=False) decision.

    The degraded decision MUST NOT be suppressive (no_trade=False)
    so a broken YOLO model cannot block trades.
    """
    orch = MagicMock()
    orch.yolo_filter.predict.side_effect = RuntimeError("model not loaded")

    result = run_stage_3_yolo_filter(orch, sym="AAPL", as_of=date(2026, 7, 8))

    assert isinstance(result, YoloFilterDecision)
    assert result.degraded is True
    assert result.no_trade is False, (
        "Degraded YOLO decision must NOT be suppressive "
        "— a broken filter cannot block trades"
    )
    assert "FILTER_ERROR:RuntimeError" in result.reason
    assert result.symbol == "AAPL"


def test_yolo_stage_handles_datetime_as_of():
    """as_of can be a datetime, not just a date — stage handles both."""
    orch = MagicMock()
    orch.yolo_filter.predict.return_value = None

    result = run_stage_3_yolo_filter(
        orch, sym="AAPL", as_of=datetime(2026, 7, 8, 9, 30),
    )

    # The yolo_dt passed to predict should be a date, not datetime
    orch.yolo_filter.predict.assert_called_once()
    call_args = orch.yolo_filter.predict.call_args
    # predict is called as (sym, yolo_dt) — yolo_dt should be a date
    assert isinstance(call_args.args[1], date)


# ─── extract_price_atr ─────────────────────────────────────────────────────


def _make_ctx_with_bars(atr_14: float = 1.5, atr_pct: float = 0.01) -> MagicMock:
    """Build a fake EntryContext with bars carrying atr_14 / atr_pct columns."""
    ctx = MagicMock()
    idx = pd.date_range("2026-07-01", periods=20, freq="D")
    ctx.bars = pd.DataFrame(
        {
            "close": [150.0] * 20,
            "atr_14": [atr_14] * 20,
            "atr_pct": [atr_pct] * 20,
        },
        index=idx,
    )
    return ctx


def _make_forecast(current_price: float = 150.0) -> MagicMock:
    """Build a fake ForecastOutput with current_price attribute."""
    forecast = MagicMock()
    forecast.current_price = current_price
    return forecast


def test_extract_price_atr_uses_bars_atr_14_when_present():
    """Happy path: bars carry atr_14 → atr_14 set, atr_pct stays None.

    The implementation is if/elif (not if/if), so a row with both
    columns set returns atr_14 only.
    """
    ctx = _make_ctx_with_bars(atr_14=2.0, atr_pct=0.013)
    forecast = _make_forecast(current_price=155.0)

    price, atr_14, atr_pct = extract_price_atr(ctx, forecast)

    assert price == 150.0  # last close from bars
    assert atr_14 == 2.0
    assert atr_pct is None  # elif branch — atr_pct skipped when atr_14 set


def test_extract_price_atr_uses_bars_atr_pct_when_no_atr_14():
    """Fallback path: bars carry only atr_pct → atr_pct set, atr_14 None."""
    ctx = MagicMock()
    idx = pd.date_range("2026-07-01", periods=10, freq="D")
    ctx.bars = pd.DataFrame(
        {"close": [150.0] * 10, "atr_pct": [0.025] * 10},
        index=idx,
    )
    forecast = _make_forecast(current_price=155.0)

    price, atr_14, atr_pct = extract_price_atr(ctx, forecast)

    assert price == 150.0
    assert atr_14 is None
    assert atr_pct == 0.025


def test_extract_price_atr_falls_back_to_forecast_when_no_bars():
    """When ctx.bars is None → use forecast.current_price, atr values None."""
    ctx = MagicMock()
    ctx.bars = None
    forecast = _make_forecast(current_price=160.0)

    price, atr_14, atr_pct = extract_price_atr(ctx, forecast)

    assert price == 160.0
    assert atr_14 is None
    assert atr_pct is None


def test_extract_price_atr_atr_columns_missing_both_atr_values_none():
    """When bars exist but have neither atr_14 nor atr_pct → both None.

    price still comes from bars (last close), not from forecast
    — fallback to forecast.current_price only kicks in when bars
    is None or empty.
    """
    ctx = MagicMock()
    idx = pd.date_range("2026-07-01", periods=10, freq="D")
    ctx.bars = pd.DataFrame({"close": [150.0] * 10}, index=idx)
    forecast = _make_forecast(current_price=170.0)

    price, atr_14, atr_pct = extract_price_atr(ctx, forecast)

    assert price == 150.0  # bars present, last close wins
    assert atr_14 is None
    assert atr_pct is None