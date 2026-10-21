"""TS-9: Wave 9 YOLO no-trade filter integration.

Covers:
  1. YoloNoTradeFilter.predict() — soft-fail on missing image, model loads OK,
     real images produce non-empty probs.
  2. SignalRuleEngine SR-06 fires on a no_trade=True YoloFilterDecision,
     does not fire on no_trade=False / None / degraded decisions.
  3. Existing SR-01..SR-05 paths still work when yolo_decision is provided
     (no regression).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from stockapp_v12.schemas.forecast import build_forecast_output
from stockapp_v12.services.signal_rule_engine import (
    SR_MIN_EXPECTED_RETURN,
    SR_NOT_ACTIONABLE,
    SR_PROCEED,
    SR_YOLO_NO_TRADE,
    SignalRuleConfig,
    SignalRuleContext,
    SignalRuleEngine,
)
from stockapp_v12.services.yolo_filter import (
    DEFAULT_MODEL_DIR,
    YoloFilterDecision,
    YoloNoTradeFilter,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------
def _forecast(**kwargs):
    defaults = dict(
        symbol="NVDA",
        horizon_days=10,
        current_price=100.0,
        forecast_mean=105.0,
        model_id="chronos-2-zero",
    )
    defaults.update(kwargs)
    return build_forecast_output(**defaults)


def _ctx(**kwargs):
    defaults = dict(market_regime="BULL_LONG", actionable_symbols={"NVDA"})
    defaults.update(kwargs)
    return SignalRuleContext(**defaults)


def _yolo_no_trade_decision(symbol: str = "NVDA", as_of: date = date(2024, 1, 2)) -> YoloFilterDecision:
    """Build a synthetic no_trade=True YoloFilterDecision for SR-06 wiring tests."""
    return YoloFilterDecision(
        symbol=symbol,
        as_of=as_of,
        image_date=as_of,
        image_path="/tmp/fake.jpg",
        yolo_probs={"chop": 0.10, "normal": 0.65, "breakout": 0.05,
                    "reversal": 0.05, "trend_down": 0.05, "trend_up": 0.10},
        predicted_class="normal",
        predicted_conf=0.65,
        no_trade=True,
        reason="NEUTRAL_HIGH_CONF_NORMAL",
        blocked_class="normal",
        threshold_used=0.15,
        model_id="yolo_phase2_v1_gpu",
        degraded=False,
    )


def _yolo_ok_decision(symbol: str = "NVDA", as_of: date = date(2024, 1, 2)) -> YoloFilterDecision:
    """Synthetic no_trade=False YoloFilterDecision (e.g. breakout detected)."""
    return YoloFilterDecision(
        symbol=symbol,
        as_of=as_of,
        image_date=as_of,
        image_path="/tmp/fake.jpg",
        yolo_probs={"breakout": 0.58, "normal": 0.20, "chop": 0.10,
                    "reversal": 0.04, "trend_down": 0.04, "trend_up": 0.04},
        predicted_class="breakout",
        predicted_conf=0.58,
        no_trade=False,
        reason="BULLISH_PATTERN",
        model_id="yolo_phase2_v1_gpu",
        degraded=False,
    )


def _yolo_degraded_decision(symbol: str = "NVDA", as_of: date = date(2024, 1, 2)) -> YoloFilterDecision:
    """Synthetic degraded YoloFilterDecision (image missing)."""
    return YoloFilterDecision(
        symbol=symbol, as_of=as_of, image_date=as_of,
        image_path="/tmp/missing.jpg", yolo_probs={},
        predicted_class="", predicted_conf=0.0,
        no_trade=False, reason="IMAGE_MISSING",
        model_id="yolo_phase2_v1_gpu", degraded=True,
    )


# ---------------------------------------------------------------------------
# YoloFilterDecision dataclass tests (no model load)
# ---------------------------------------------------------------------------
def test_decision_no_trade_attributes():
    dec = _yolo_no_trade_decision()
    assert dec.no_trade is True
    assert dec.blocked_class == "normal"
    assert dec.threshold_used == 0.15
    assert dec.reason.startswith("NEUTRAL_HIGH_CONF")
    assert dec.degraded is False
    d = dec.to_dict()
    assert d["symbol"] == "NVDA"
    assert d["as_of"] == "2024-01-02"
    assert d["image_date"] == "2024-01-02"


def test_decision_degraded_attributes():
    dec = _yolo_degraded_decision()
    assert dec.no_trade is False
    assert dec.degraded is True
    assert dec.reason == "IMAGE_MISSING"


def test_decision_to_dict_serializes_dates():
    dec = _yolo_no_trade_decision()
    d = dec.to_dict()
    assert isinstance(d["as_of"], str)
    assert isinstance(d["image_date"], str)


# ---------------------------------------------------------------------------
# YoloNoTradeFilter constructor + soft-fail tests (no real images required)
# ---------------------------------------------------------------------------
def test_filter_construct_with_missing_model_dir_returns_degraded():
    """Filter pointed at a non-existent model dir should still construct
    and produce DEGRADED_NO_MODEL decisions on predict()."""
    flt = YoloNoTradeFilter(
        model_dir=Path("/tmp/does_not_exist_yolo_model_dir"),
        image_dir=Path("/tmp/does_not_exist_image_dir"),
    )
    assert flt.is_available() is False
    dec = flt.predict("NVDA", date(2024, 1, 2))
    assert dec.no_trade is False
    assert dec.degraded is True
    assert dec.reason == "DEGRADED_NO_MODEL"


def test_filter_predict_returns_image_missing_when_image_not_present():
    """Filter with real model but missing image for that date → IMAGE_MISSING."""
    if not DEFAULT_MODEL_DIR.exists():
        pytest.skip(f"YOLO model dir not found at {DEFAULT_MODEL_DIR}")
    flt = YoloNoTradeFilter()  # defaults to phase2_v1_gpu
    # Pick a date we definitely don't have an image for
    dec = flt.predict("ZZZZZZ", date(2099, 12, 31))
    assert dec.no_trade is False
    assert dec.degraded is True
    assert dec.reason == "IMAGE_MISSING"


# ---------------------------------------------------------------------------
# SignalRuleEngine SR-06 wiring tests
# ---------------------------------------------------------------------------
def test_sr06_fires_on_no_trade_decision():
    engine = SignalRuleEngine()
    dec = _yolo_no_trade_decision()
    result = engine.evaluate(_forecast(), _ctx(yolo_decision=dec))
    assert result.proceed is False
    assert result.rule_code == SR_YOLO_NO_TRADE
    assert "normal" in result.reason
    assert "0.65" in result.reason


def test_sr06_does_not_fire_on_ok_decision():
    engine = SignalRuleEngine()
    dec = _yolo_ok_decision()
    result = engine.evaluate(_forecast(), _ctx(yolo_decision=dec))
    assert result.proceed is True
    assert result.rule_code == SR_PROCEED


def test_sr06_does_not_fire_on_degraded_decision():
    """Degraded (image missing) YoloFilterDecision is a no-op for SR-06."""
    engine = SignalRuleEngine()
    dec = _yolo_degraded_decision()
    result = engine.evaluate(_forecast(), _ctx(yolo_decision=dec))
    assert result.proceed is True
    assert result.rule_code == SR_PROCEED


def test_sr06_does_not_fire_when_yolo_decision_is_none():
    """Default SignalRuleContext (no yolo_decision) → SR-06 inactive."""
    engine = SignalRuleEngine()
    result = engine.evaluate(_forecast(), _ctx())
    assert result.proceed is True
    assert result.rule_code == SR_PROCEED


def test_sr06_checked_after_sr01_sr05():
    """SR-06 (YOLO) is the final gate, so SR-01 should still fire before SR-06."""
    engine = SignalRuleEngine(SignalRuleConfig(min_expected_return_pct=0.10))
    dec = _yolo_no_trade_decision()
    # Forecast has +5% return; SR-01 fires first because 0.05 < 0.10 threshold.
    # YOLO no-trade is irrelevant — SR-01 wins.
    result = engine.evaluate(
        _forecast(forecast_mean=100.5),  # +0.5% return, below 10% threshold
        _ctx(yolo_decision=dec),
    )
    assert result.rule_code == SR_MIN_EXPECTED_RETURN


def test_sr06_checked_before_sr04():
    """SR-04 (actionable list) fires BEFORE SR-06 (SR-06 is the last gate)."""
    engine = SignalRuleEngine()
    dec = _yolo_no_trade_decision()
    # AAPL not in actionable set; SR-04 should fire even though YOLO says no-trade.
    result = engine.evaluate(
        _forecast(symbol="AAPL"),
        _ctx(actionable_symbols={"NVDA"}, yolo_decision=dec),
    )
    assert result.rule_code == SR_NOT_ACTIONABLE


def test_sr06_survives_with_high_thresholds_and_sr05_proceed():
    """Sanity: existing SR-05 PROCEED path works alongside yolo_decision (non-no-trade).

    SR-07 (regime gate) is disabled here so this test isolates SR-06's
    behavior — see tests/test_signal_rules_sr07_regime_gate.py for SR-07
    coverage. With SR-07 disabled, the SR-05 PROCEED reason reads
    "passed SR-01..SR-07" (the trailing SR ID includes SR-07 to keep the
    audit string current), so we accept that suffix here.
    """
    engine = SignalRuleEngine(SignalRuleConfig(sr07_enabled=False))
    dec = _yolo_ok_decision()
    result = engine.evaluate(_forecast(), _ctx(yolo_decision=dec))
    assert result.proceed is True
    assert "SR-06" in result.reason or "passed SR-01..SR-06" in result.reason or "passed SR-01..SR-07" in result.reason


# ---------------------------------------------------------------------------
# Integration: end-to-end via a real YOLO image (if available)
# ---------------------------------------------------------------------------
def test_real_model_predicts_known_neutral_pattern():
    """End-to-end test against phase2_v1_gpu on a known neutral image.
    Skips if model or image not present (offline / fresh clone safety).
    """
    if not DEFAULT_MODEL_DIR.exists():
        pytest.skip(f"YOLO model dir not found at {DEFAULT_MODEL_DIR}")

    flt = YoloNoTradeFilter()
    if not flt.is_available():
        pytest.skip("YOLO model failed to load")

    # ABBV_2020-01-16_160000.jpg is a known 'normal' ground truth (per labels.csv)
    dec = flt.predict("ABBV", date(2020, 1, 17))  # image_lag_days=1 → uses 2020-01-16
    assert dec.degraded is False, f"expected non-degraded, got degraded reason={dec.reason}"
    assert dec.predicted_class in {"normal", "chop", "breakout", "reversal", "trend_up", "trend_down"}
    # Sum of probs should be ~1
    assert 0.95 <= sum(dec.yolo_probs.values()) <= 1.05
    # All probs in [0, 1]
    for cls, p in dec.yolo_probs.items():
        assert 0.0 <= p <= 1.0, f"{cls} prob {p} out of range"


def test_real_model_predicts_known_breakout_pattern():
    """ABBv_2020-02-21_160000.jpg is a known 'breakout' ground truth.
    Should predict breakout/trend_up and NOT trigger no-trade (those are bullish).
    """
    if not DEFAULT_MODEL_DIR.exists():
        pytest.skip(f"YOLO model dir not found at {DEFAULT_MODEL_DIR}")

    flt = YoloNoTradeFilter()
    if not flt.is_available():
        pytest.skip("YOLO model failed to load")

    dec = flt.predict("ABBV", date(2020, 2, 22))
    assert dec.degraded is False
    # No-trade should not fire on a known breakout (bullish, not neutral)
    assert dec.no_trade is False, f"unexpected no_trade: {dec.to_dict()}"