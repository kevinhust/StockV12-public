"""Regression tests for P1-8 (council 2026-07-09): extract_price_atr must
assert bars are sorted ascending by date.

Pre-fix: ``iloc[-1]`` assumed the last row was the latest bar. If a caller
passed unsorted bars (e.g. from an unsorted parquet cache), the wrong day's
close fed SL math — silently producing wrong stop-loss prices.

Post-fix: raises ``ValueError`` if ``bars.index.is_monotonic_increasing`` is False.
"""

from __future__ import annotations

import pandas as pd
import pytest

from stockapp_v12.pipeline.stages import extract_price_atr
from stockapp_v12.schemas.forecast import ForecastOutput
from datetime import date


def _make_forecast(price: float = 100.0) -> ForecastOutput:
    return ForecastOutput(
        symbol="TEST",
        as_of=date(2026, 7, 8),
        horizon_days=5,
        current_price=price,
        forecast_mean=price,
        forecast_q10=price * 0.95,
        forecast_q90=price * 1.05,
        expected_return_pct=0.0,
        model_id="test-model",
    )


def _make_bars(closes, dates=None) -> pd.DataFrame:
    if dates is None:
        dates = pd.to_datetime(["2026-07-06", "2026-07-07", "2026-07-08"])
    return pd.DataFrame({"close": closes}, index=dates)


# ─── Happy path: sorted bars ──────────────────────────────────────────────


def test_sorted_bars_returns_last_close():
    bars = _make_bars([100.0, 105.0, 110.0])
    ctx = _make_ctx(bars)
    price, _, _ = extract_price_atr(ctx, _make_forecast())
    assert price == 110.0  # last bar


def test_empty_bars_falls_back_to_forecast():
    bars = pd.DataFrame({"close": []})
    bars.index = pd.to_datetime([])
    ctx = _make_ctx(bars)
    price, _, _ = extract_price_atr(ctx, _make_forecast(99.0))
    assert price == 99.0


def test_none_bars_falls_back_to_forecast():
    ctx = _make_ctx(None)
    price, _, _ = extract_price_atr(ctx, _make_forecast(99.0))
    assert price == 99.0


def test_sorted_bars_with_atr_14():
    dates = pd.to_datetime(["2026-07-06", "2026-07-07", "2026-07-08"])
    bars = pd.DataFrame({"close": [100, 105, 110], "atr_14": [2.0, 2.1, 2.2]}, index=dates)
    ctx = _make_ctx(bars)
    price, atr_14, atr_pct = extract_price_atr(ctx, _make_forecast())
    assert price == 110.0
    assert atr_14 == 2.2
    assert atr_pct is None


def test_sorted_bars_with_atr_pct():
    dates = pd.to_datetime(["2026-07-06", "2026-07-07", "2026-07-08"])
    bars = pd.DataFrame({"close": [100, 105, 110], "atr_pct": [0.02, 0.021, 0.022]}, index=dates)
    ctx = _make_ctx(bars)
    price, atr_14, atr_pct = extract_price_atr(ctx, _make_forecast())
    assert price == 110.0
    assert atr_14 is None
    assert atr_pct == 0.022


# ─── P1-8: unsorted bars raise ────────────────────────────────────────────


def test_unsorted_bars_raise():
    """P1-8: bars in descending order must raise ValueError."""
    dates = pd.to_datetime(["2026-07-08", "2026-07-07", "2026-07-06"])
    bars = pd.DataFrame({"close": [110, 105, 100]}, index=dates)
    ctx = _make_ctx(bars)
    with pytest.raises(ValueError, match="not monotonic increasing"):
        extract_price_atr(ctx, _make_forecast())


def test_shuffled_bars_raise():
    """P1-8: randomly shuffled bars must raise."""
    dates = pd.to_datetime(["2026-07-07", "2026-07-08", "2026-07-06"])
    bars = pd.DataFrame({"close": [105, 110, 100]}, index=dates)
    ctx = _make_ctx(bars)
    with pytest.raises(ValueError, match="not monotonic increasing"):
        extract_price_atr(ctx, _make_forecast())


# ─── Helper ───────────────────────────────────────────────────────────────


def _make_ctx(bars):
    """Build a minimal mock EntryContext with just the .bars attribute."""
    from unittest.mock import MagicMock
    ctx = MagicMock()
    ctx.bars = bars
    return ctx
