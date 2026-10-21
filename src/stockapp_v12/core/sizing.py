"""
V12 risk-based position sizing — confidence must NOT enter the core formula.
Spec: v12/docs/RISK_MANAGEMENT.md#position-sizing

Phase A5: internal arithmetic migrated to `decimal.Decimal` for boundary
determinism. Public return type stays `float` to avoid rippling changes
through `entry_orchestrator.py` and `lifecycle_scheduler.py` (both of
which already cast via `float(...)`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from stockapp_v12.core.limits import V12RiskLimits
from stockapp_v12.core.precision import (
    D,
    exceeds_cap,
    round_pct,
    safe_div,
    to_float,
)


@dataclass
class SizingResult:
    size_pct: float
    risk_amount: float
    stop_distance: float
    atr_14: float
    sl_atr_multiple: float
    regime_multiplier: float
    capped_by_single_position: bool


def resolve_atr_14(entry_price: float, atr_14: Optional[float], atr_pct: Optional[float]) -> float:
    if atr_14 is not None and atr_14 > 0:
        return float(atr_14)
    if atr_pct is not None and entry_price > 0 and atr_pct > 0:
        return float(entry_price) * float(atr_pct)
    raise ValueError("atr_14 or positive atr_pct with entry_price required for V12 sizing")


def calculate_risk_based_size_pct(
    nav: float,
    entry_price: float,
    atr_14: Optional[float],
    atr_pct: Optional[float],
    regime: str,
    limits: V12RiskLimits,
    sl_atr_multiple: Optional[float] = None,
    confidence: Optional[float] = None,
) -> SizingResult:
    if nav <= 0 or entry_price <= 0:
        raise ValueError("nav and entry_price must be positive")

    # Promote to Decimal at the boundary. `D(str(limits.X))` is unnecessary
    # because `limits` is a frozen dataclass of pure floats; D() round-trips
    # through str to preserve all 17 significant digits.
    nav_d = D(nav)
    ep_d = D(entry_price)
    atr_d = D(resolve_atr_14(entry_price, atr_14, atr_pct))
    sl_mult_d = D(sl_atr_multiple if sl_atr_multiple is not None else limits.sl_atr_multiple_default)
    risk_pct_d = D(limits.risk_per_trade_pct)
    cap_pct_d = D(limits.max_single_position_pct)

    stop_distance_d = atr_d * sl_mult_d
    if stop_distance_d <= 0:
        raise ValueError("stop_distance must be positive")

    risk_amount_d = nav_d * risk_pct_d
    shares_d = safe_div(risk_amount_d, stop_distance_d)
    dollar_d = shares_d * ep_d
    capped = False

    max_dollar_d = nav_d * cap_pct_d
    if exceeds_cap(dollar_d, max_dollar_d):
        dollar_d = max_dollar_d
        capped = True

    regime_mult_d = D(limits.regime_sizing.get(regime.upper(), limits.regime_sizing.get("NEUTRAL", 0.8)))
    dollar_d = dollar_d * regime_mult_d

    if limits.confidence_cap_discount_enabled and confidence is not None:
        # Conf-discount cap: tighten the dollar cap by min(conf/0.7, 1.0)
        conf_factor = min(D(confidence) / D("0.7"), D("1"))
        conf_cap_d = max_dollar_d * conf_factor
        if exceeds_cap(dollar_d, conf_cap_d):
            dollar_d = conf_cap_d
            capped = True

    # Quantize size_pct at 6 dp (boundary precision). Then re-apply the
    # hard R-02 cap with `exceeds_cap` so 0.0299999 is NOT incorrectly
    # pushed up to 0.03 by the quantize step.
    raw_size_pct_d = safe_div(dollar_d, nav_d)
    size_pct_d = round_pct(raw_size_pct_d)
    if exceeds_cap(size_pct_d, cap_pct_d):
        size_pct_d = cap_pct_d
        capped = True

    return SizingResult(
        size_pct=to_float(size_pct_d),
        risk_amount=to_float(risk_amount_d),
        stop_distance=to_float(stop_distance_d),
        atr_14=to_float(atr_d),
        sl_atr_multiple=to_float(sl_mult_d),
        regime_multiplier=to_float(regime_mult_d),
        capped_by_single_position=capped,
    )