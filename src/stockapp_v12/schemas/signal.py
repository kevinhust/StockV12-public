"""Signal-rule DTOs and SR-* codes — pure data layer.

Phase 2 decoupling: ``pipeline`` and other layers depend on these types
without importing ``services.signal_rule_engine`` (engine stays in services).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, FrozenSet, Mapping, Optional, Set

from stockapp_v12.schemas.forecast import ForecastOutput

if TYPE_CHECKING:
    from stockapp_v12.schemas.yolo import YoloFilterDecision

# Explicit rule IDs for audit / backtest attribution (parallel to R-xx).
SR_MIN_EXPECTED_RETURN = "SR-01"
SR_MAX_UNCERTAINTY = "SR-02"
SR_REGIME_BLOCK = "SR-03"
SR_NOT_ACTIONABLE = "SR-04"
SR_PROCEED = "SR-05"
SR_YOLO_NO_TRADE = "SR-06"
SR_REGIME_GATE = "SR-07"  # 2026-07-01 council: regime-aware gate (Round 3 closure)


@dataclass(frozen=True)
class SignalRuleConfig:
    # 2026-06-25 council (round 2): aligned 0.01 → 0.005.
    min_expected_return_pct: float = 0.005
    # 2026-06-25 council: raised 0.08 → 0.12.
    max_uncertainty_pct: float = 0.12
    blocked_regimes: FrozenSet[str] = field(
        default_factory=lambda: frozenset({"PANIC", "BEAR_LONG"})
    )
    # SR-07: regime-aware gate.
    sr07_enabled: bool = True
    sr07_min_forecast_pct: float = 0.02
    sr07_sma_window: int = 20


@dataclass(frozen=True)
class SignalRuleContext:
    """Runtime context — market_regime from Redis; actionable from PreMarket.

    yolo_decision is the optional Wave 9 YoloFilterDecision. When present and
    ``yolo_decision.no_trade`` is True, SR-06 fires and the entry is suppressed.
    A missing yolo_decision (None) is a no-op — SR-06 is skipped entirely.
    A degraded yolo_decision (model missing / image missing) is also a no-op —
    SR-06 only fires on a real, confident neutral-class verdict.

    regime_features is the SR-07 input dict: keys are UPPERCASE symbols,
    values are dicts that must include at minimum ``close`` (last close) and
    ``sma_<window>`` (rolling SMA, window matched by the engine's
    ``sr07_sma_window`` setting).
    """

    market_regime: str
    actionable_symbols: Set[str]
    yolo_decision: Optional["YoloFilterDecision"] = None
    regime_features: Mapping[str, Mapping[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalRuleResult:
    proceed: bool
    rule_code: str
    reason: str
    forecast: ForecastOutput

    @property
    def is_pass(self) -> bool:
        return not self.proceed


__all__ = [
    "SR_MIN_EXPECTED_RETURN",
    "SR_MAX_UNCERTAINTY",
    "SR_REGIME_BLOCK",
    "SR_NOT_ACTIONABLE",
    "SR_PROCEED",
    "SR_YOLO_NO_TRADE",
    "SR_REGIME_GATE",
    "SignalRuleConfig",
    "SignalRuleContext",
    "SignalRuleResult",
]
