"""RK-7: V12 core sizing must not scale with confidence."""
import pytest

from stockapp_v12.core.limits import V12RiskLimits
from stockapp_v12.core.sizing import calculate_risk_based_size_pct


@pytest.fixture
def limits():
    return V12RiskLimits(confidence_cap_discount_enabled=False)


def test_size_invariant_to_confidence(limits):
    kwargs = dict(
        nav=100_000,
        entry_price=100.0,
        atr_14=5.0,
        atr_pct=None,
        regime="BULL_LONG",
        limits=limits,
    )
    low = calculate_risk_based_size_pct(**kwargs, confidence=0.71).size_pct
    high = calculate_risk_based_size_pct(**kwargs, confidence=0.99).size_pct
    assert low == pytest.approx(high)


def test_wider_stop_reduces_size_pct():
    """Risk-based: larger stop distance → smaller position (not confidence)."""
    limits = V12RiskLimits(max_single_position_pct=0.20)
    tight = calculate_risk_based_size_pct(
        nav=100_000,
        entry_price=100.0,
        atr_14=4.0,
        atr_pct=None,
        regime="BULL_LONG",
        limits=limits,
        sl_atr_multiple=1.0,
    ).size_pct
    wide = calculate_risk_based_size_pct(
        nav=100_000,
        entry_price=100.0,
        atr_14=4.0,
        atr_pct=None,
        regime="BULL_LONG",
        limits=limits,
        sl_atr_multiple=2.5,
    ).size_pct
    assert wide < tight
