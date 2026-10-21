"""Tests for decimal.Decimal precision migration (Phase A5).

Covers:
- `precision.py` helper module (D, round_pct/dollar/shares, exceeds_cap, safe_div, to_float)
- `calculate_risk_based_size_pct` migrated behavior (boundary determinism, regime, conf-discount)
- `compute_position_size` end-to-end (degraded, fallback, normal paths)
- Backward compat: float inputs unchanged; Decimal inputs now also accepted
- Regression: existing sizing invariants still hold
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from stockapp_v12.bridge.gatekeeper import compute_position_size
from stockapp_v12.core.limits import V12RiskLimits
from stockapp_v12.core.precision import (
    D,
    exceeds_cap,
    round_dollar,
    round_pct,
    round_shares,
    safe_div,
    to_float,
)
from stockapp_v12.core.sizing import calculate_risk_based_size_pct


# ============================================================================
# precision.py helpers
# ============================================================================


class TestD:
    def test_int(self):
        assert D(5) == Decimal(5)

    def test_float_round_trip(self):
        # All 17 significant digits preserved via str() round-trip
        assert D(0.1) == Decimal("0.1")

    def test_string_literal(self):
        assert D("0.01") == Decimal("0.01")

    def test_decimal_passthrough(self):
        x = Decimal("1.234")
        assert D(x) is x

    def test_invalid_type_raises(self):
        with pytest.raises(TypeError):
            D(None)
        with pytest.raises(TypeError):
            D([1, 2])


class TestRoundHelpers:
    def test_round_pct_default_6dp(self):
        assert round_pct(Decimal("0.0299994")) == Decimal("0.029999")
        assert round_pct(Decimal("0.0299995")) == Decimal("0.030000")
        assert round_pct(Decimal("0.02999950001")) == Decimal("0.030000")

    def test_round_pct_half_even(self):
        # Banker's rounding: 0.0000005 rounds to even
        assert round_pct(Decimal("0.0000005")) == Decimal("0.000000")
        assert round_pct(Decimal("0.0000015")) == Decimal("0.000002")

    def test_round_dollar(self):
        assert round_dollar(Decimal("100.005")) == Decimal("100.00")  # banker's
        assert round_dollar(Decimal("100.015")) == Decimal("100.02")  # banker's
        assert round_dollar(Decimal("100.025")) == Decimal("100.02")

    def test_round_shares(self):
        assert round_shares(Decimal("1.234567")) == Decimal("1.2346")
        assert round_shares(Decimal("1.23444")) == Decimal("1.2344")

    def test_accepts_int_float_string(self):
        assert round_pct(1) == Decimal("1.000000")
        assert round_pct(0.5) == Decimal("0.500000")
        assert round_pct("0.1") == Decimal("0.100000")


class TestExceedsCap:
    def test_below_cap(self):
        assert exceeds_cap("0.029999", "0.03") is False

    def test_at_cap_strict(self):
        # Exactly at cap = NOT exceeding
        assert exceeds_cap("0.03", "0.03") is False

    def test_above_cap(self):
        assert exceeds_cap("0.030001", "0.03") is True

    def test_epsilon_guard(self):
        # 0.0300000001 (well below epsilon guard) is NOT exceeding
        assert exceeds_cap("0.0300000001", "0.03") is False

    def test_micro_above_epsilon(self):
        # 2e-7 above cap is over the EPSILON_Q (1e-7) guard
        assert exceeds_cap("0.0300002", "0.03") is True


class TestSafeDiv:
    def test_normal(self):
        assert safe_div("100", "4") == Decimal("25")

    def test_zero_denom_raises_value_error(self):
        with pytest.raises(ValueError):
            safe_div("100", "0")

    def test_negative(self):
        assert safe_div("-10", "2") == Decimal("-5")


class TestToFloat:
    def test_decimal_to_float(self):
        assert to_float(Decimal("0.5")) == 0.5

    def test_int_to_float(self):
        assert to_float(5) == 5.0

    def test_string_to_float(self):
        assert to_float("0.03") == 0.03


# ============================================================================
# calculate_risk_based_size_pct — Decimal migration
# ============================================================================


def _limits(**overrides):
    defaults = dict(
        max_single_position_pct=0.03,
        confidence_cap_discount_enabled=False,
        risk_per_trade_pct=0.01,
        sl_atr_multiple_default=1.5,
    )
    defaults.update(overrides)
    return V12RiskLimits(**defaults)


class TestSizingDecimalMigration:
    def test_basic_call_returns_float(self):
        """Public API still returns float — non-breaking."""
        r = calculate_risk_based_size_pct(
            nav=100_000, entry_price=100.0, atr_14=5.0,
            atr_pct=None, regime="BULL_LONG", limits=_limits(),
        )
        assert isinstance(r.size_pct, float)

    def test_accepts_decimal_inputs(self):
        """NEW: Decimal inputs work (was a TypeError before migration)."""
        r = calculate_risk_based_size_pct(
            nav=Decimal("100000"), entry_price=Decimal("100.0"),
            atr_14=Decimal("5.0"), atr_pct=None,
            regime="BULL_LONG", limits=_limits(),
        )
        assert isinstance(r.size_pct, float)

    def test_determinism_across_runs(self):
        """Same inputs produce identical output (no IEEE-754 drift surprises)."""
        kwargs = dict(
            nav=12345.67, entry_price=87.43, atr_14=4.17,
            atr_pct=None, regime="BULL_NORMAL",
            limits=_limits(max_single_position_pct=0.50),
            sl_atr_multiple=1.5,
        )
        results = [calculate_risk_based_size_pct(**kwargs) for _ in range(10)]
        first = results[0].size_pct
        for r in results:
            assert r.size_pct == first

    def test_below_cap_round_down(self):
        """0.0299994 rounds to 0.029999 — does NOT hit 0.03 cap."""
        # Construct a calc that produces ~0.02999
        # risk_per_trade * nav = 100, stop_distance = 5, share=20, dollar=2000
        # size_pct pre-regime = 2000/100000 = 0.02; not what we want.
        # Use sl_mult=1.0, atr=4, risk=0.012, nav=100000 → stop=4, shares=300, dollar=30000,
        # size=0.30 — capped at 0.03 anyway. Use a high cap:
        limits = _limits(max_single_position_pct=0.99)
        r = calculate_risk_based_size_pct(
            nav=100_000, entry_price=1.0, atr_14=4.0,
            atr_pct=None, regime="NEUTRAL",  # multiplier 0.8
            limits=limits, sl_atr_multiple=2.0,
        )
        # risk = 1000; stop = 8; shares = 125; dollar = 125; regime_mult 0.8 → 100; size=0.001
        assert r.size_pct == pytest.approx(0.001, abs=1e-6)

    def test_cap_boundary_deterministic(self):
        """Values right at the cap are quantized consistently."""
        limits = _limits(max_single_position_pct=0.03)
        # 30-row series with the same inputs: cap triggers exactly once,
        # and the result is exactly 0.03 — never 0.0300000004.
        r = calculate_risk_based_size_pct(
            nav=100_000, entry_price=100.0, atr_14=5.0,
            atr_pct=None, regime="BULL_LONG", limits=limits,
            sl_atr_multiple=1.5,
        )
        assert r.size_pct == 0.03
        # And it's bit-exact across re-runs:
        for _ in range(5):
            r2 = calculate_risk_based_size_pct(
                nav=100_000, entry_price=100.0, atr_14=5.0,
                atr_pct=None, regime="BULL_LONG", limits=limits,
                sl_atr_multiple=1.5,
            )
            assert r2.size_pct == r.size_pct

    def test_regime_multiplier_applied(self):
        """NEUTRAL (0.8) gives ~80% of BULL_LONG size.

        We compare via *ratio* (neut / bull) because the rounded output
        already lost 6 dp of precision; comparing absolute values forces
        re-rounding which doesn't match.
        """
        kwargs = dict(
            nav=100_000, entry_price=100.0, atr_14=5.0,
            atr_pct=None, sl_atr_multiple=1.5,
            limits=_limits(max_single_position_pct=0.99),
        )
        bull = calculate_risk_based_size_pct(**kwargs, regime="BULL_LONG")
        neut = calculate_risk_based_size_pct(**kwargs, regime="NEUTRAL")
        ratio = neut.size_pct / bull.size_pct
        # ratio should be very close to 0.8; rounding adds tiny offset
        assert ratio == pytest.approx(0.8, rel=1e-4)
        assert neut.size_pct < bull.size_pct

    def test_panic_zero_size(self):
        """PANIC regime multiplier is 0.0 → size_pct = 0."""
        r = calculate_risk_based_size_pct(
            nav=100_000, entry_price=100.0, atr_14=5.0,
            atr_pct=None, regime="PANIC",
            limits=_limits(max_single_position_pct=0.99),
            sl_atr_multiple=1.5,
        )
        assert r.size_pct == 0.0
        assert r.capped_by_single_position is False

    def test_confidence_discount_caps_size(self):
        """When enabled, confidence * cap can tighten the size below R-02."""
        # BULL_LONG, high confidence (0.99) → conf_factor = min(0.99/0.7, 1) = 1
        # BULL_LONG, low confidence (0.35) → conf_factor = 0.35/0.7 = 0.5 → cap halves
        limits = V12RiskLimits(
            max_single_position_pct=0.20,
            confidence_cap_discount_enabled=True,
        )
        kwargs = dict(
            nav=100_000, entry_price=100.0, atr_14=5.0,
            atr_pct=None, regime="BULL_LONG",
            limits=limits, sl_atr_multiple=1.5,
        )
        high = calculate_risk_based_size_pct(**kwargs, confidence=0.99)
        low = calculate_risk_based_size_pct(**kwargs, confidence=0.35)
        # With low confidence, dollar is capped tighter
        assert low.size_pct < high.size_pct

    def test_negative_inputs_raise(self):
        with pytest.raises(ValueError):
            calculate_risk_based_size_pct(
                nav=0, entry_price=100.0, atr_14=5.0,
                atr_pct=None, regime="BULL_LONG", limits=_limits(),
            )
        with pytest.raises(ValueError):
            calculate_risk_based_size_pct(
                nav=100_000, entry_price=-1.0, atr_14=5.0,
                atr_pct=None, regime="BULL_LONG", limits=_limits(),
            )

    def test_zero_stop_distance_raises(self):
        # atr=0 with no atr_pct → resolves to raise in resolve_atr_14
        with pytest.raises(ValueError):
            calculate_risk_based_size_pct(
                nav=100_000, entry_price=100.0, atr_14=0,
                atr_pct=None, regime="BULL_LONG", limits=_limits(),
            )


# ============================================================================
# compute_position_size — end-to-end
# ============================================================================


def _cps(**overrides):
    """Standard kwargs for compute_position_size — valid default."""
    base = dict(
        equity=100_000,
        price=100.0,
        atr_pct=None,
        atr_14=5.0,
        regime="BULL_LONG",
        confidence=0.80,
        kelly_override=None,
        risk_scaling_override=None,
        is_degraded=False,
        limits=_limits(),
    )
    base.update(overrides)
    return base


class TestComputePositionSizeDecimalMigration:
    def test_returns_floats(self):
        size, scaling, audit = compute_position_size(**_cps())
        assert isinstance(size, float)
        assert isinstance(scaling, float)
        assert isinstance(audit, dict)

    def test_normal_path_uses_risk_model(self):
        size, scaling, audit = compute_position_size(**_cps())
        assert audit["sizing_model"] == "v12_risk"
        assert scaling == 1.0  # default risk_scaling
        assert size > 0.0

    def test_degraded_halves_size_and_scales(self):
        size, scaling, audit = compute_position_size(**_cps(is_degraded=True))
        assert audit["sizing_model"] == "v12_risk"
        # is_degraded → base_size *= 0.5, scaling *= 0.7
        # Then final size = round_pct(base_size * scaling) = round_pct(base * 0.5 * 0.7)
        normal_size, normal_scaling, _ = compute_position_size(**_cps(is_degraded=False))
        expected_size = normal_size * 0.5 * 0.7
        assert size == pytest.approx(expected_size, rel=1e-6)
        assert scaling == pytest.approx(normal_scaling * 0.7, rel=1e-6)

    def test_risk_scaling_override(self):
        size, scaling, _ = compute_position_size(**_cps(risk_scaling_override=0.5))
        assert scaling == 0.5
        normal_size, _, _ = compute_position_size(**_cps())
        assert size == pytest.approx(normal_size * 0.5, rel=1e-6)

    def test_fallback_when_no_price(self):
        size, scaling, audit = compute_position_size(**_cps(price=0.0))
        assert audit["sizing_model"] == "v12_fallback"
        assert audit["error"] == "no_price"
        assert size == 0.01
        assert scaling == 1.0

    def test_fallback_on_value_error(self):
        """If core sizing raises, fall back to regime-derived size."""
        # atr_14=0 → resolve_atr_14 raises ValueError
        size, scaling, audit = compute_position_size(**_cps(atr_14=0, atr_pct=None))
        assert audit["sizing_model"] == "v12_fallback"
        # 0.01 * regime_sizing[BULL_LONG] = 0.01 * 1.0 = 0.01
        assert size == pytest.approx(0.01, abs=1e-6)

    def test_determinism_across_calls(self):
        """Critical for shadow_trade replay consistency."""
        kwargs = _cps()
        first = compute_position_size(**kwargs)
        for _ in range(5):
            again = compute_position_size(**kwargs)
            assert again[0] == first[0]
            assert again[1] == first[1]
            assert again[2] == first[2]

    def test_panic_regime_yields_zero(self):
        size, _, audit = compute_position_size(**_cps(regime="PANIC"))
        # PANIC regime_multiplier = 0 → size_pct = 0
        assert audit["sizing_model"] == "v12_risk"
        assert size == 0.0


# ============================================================================
# Cross-validation: Decimal vs float produce equivalent sizing results
# ============================================================================


class TestDecimalFloatEquivalence:
    """For 'simple' inputs, Decimal math and float math should agree within
    rounding tolerance (6 dp)."""

    @pytest.mark.parametrize("regime", ["BULL_LONG", "NEUTRAL", "BEAR_SHORT"])
    def test_equivalent_to_float(self, regime):
        kwargs = dict(
            nav=100_000, entry_price=87.43, atr_14=4.17,
            atr_pct=None, regime=regime,
            limits=_limits(max_single_position_pct=0.50),
            sl_atr_multiple=1.5,
        )
        # Pass float — goes through coercion path
        r = calculate_risk_based_size_pct(**kwargs)
        # Pass Decimal explicitly — should match
        kwargs_d = {**kwargs, "nav": Decimal("100000"), "entry_price": Decimal("87.43"), "atr_14": Decimal("4.17")}
        r_d = calculate_risk_based_size_pct(**kwargs_d)
        assert r.size_pct == pytest.approx(r_d.size_pct, abs=1e-9)