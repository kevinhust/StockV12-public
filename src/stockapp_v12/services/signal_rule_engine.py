"""
SignalRuleEngine — SR-01..SR-07 between ForecastOutput and Gatekeeper.
Spec: Obsidian TSFM-Pipeline; pairs with RiskGatekeeper R-01..R-10.

Phase 2: DTOs and SR-* codes live in ``stockapp_v12.schemas.signal``;
this module owns the pure evaluation engine only. Re-exports kept for BC.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from stockapp_v12.schemas.forecast import ForecastOutput
from stockapp_v12.schemas.signal import (
    SR_MAX_UNCERTAINTY,
    SR_MIN_EXPECTED_RETURN,
    SR_NOT_ACTIONABLE,
    SR_PROCEED,
    SR_REGIME_BLOCK,
    SR_REGIME_GATE,
    SR_YOLO_NO_TRADE,
    SignalRuleConfig,
    SignalRuleContext,
    SignalRuleResult,
)

# BC re-exports (importers may keep using services.signal_rule_engine.*)
__all__ = [
    "SignalRuleEngine",
    "SignalRuleConfig",
    "SignalRuleContext",
    "SignalRuleResult",
    "SR_MIN_EXPECTED_RETURN",
    "SR_MAX_UNCERTAINTY",
    "SR_REGIME_BLOCK",
    "SR_NOT_ACTIONABLE",
    "SR_PROCEED",
    "SR_YOLO_NO_TRADE",
    "SR_REGIME_GATE",
    "pd_is_nan",
]


def pd_is_nan(x: Any) -> bool:
    """Lightweight NaN check that doesn't require pandas.

    pandas is not a hard dependency of this module; we accept floats / ints
    and treat bools / None as non-NaN.
    """
    if x is None or isinstance(x, bool):
        return False
    try:
        return math.isnan(float(x))
    except (TypeError, ValueError):
        return False


class SignalRuleEngine:
    def __init__(self, config: Optional[SignalRuleConfig] = None):
        self.config = config or SignalRuleConfig()

    def evaluate(
        self,
        forecast: ForecastOutput,
        ctx: SignalRuleContext,
    ) -> SignalRuleResult:
        sym = forecast.symbol.upper()
        cfg = self.config

        # SR-04: must be on today's actionable list
        if sym not in {s.upper() for s in ctx.actionable_symbols}:
            return SignalRuleResult(
                proceed=False,
                rule_code=SR_NOT_ACTIONABLE,
                reason=f"{sym} not in actionable:today",
                forecast=forecast,
            )

        # SR-01: minimum expected return
        if forecast.expected_return_pct < cfg.min_expected_return_pct:
            return SignalRuleResult(
                proceed=False,
                rule_code=SR_MIN_EXPECTED_RETURN,
                reason=(
                    f"expected_return {forecast.expected_return_pct:.4f} "
                    f"< {cfg.min_expected_return_pct}"
                ),
                forecast=forecast,
            )

        # SR-02: forecast uncertainty band too wide
        if forecast.uncertainty_pct > cfg.max_uncertainty_pct:
            return SignalRuleResult(
                proceed=False,
                rule_code=SR_MAX_UNCERTAINTY,
                reason=(
                    f"uncertainty {forecast.uncertainty_pct:.4f} "
                    f"> {cfg.max_uncertainty_pct}"
                ),
                forecast=forecast,
            )

        # SR-03: regime lock (market_regime authoritative; regime_hint advisory only)
        regime = (ctx.market_regime or "").upper().strip()
        if regime in cfg.blocked_regimes:
            return SignalRuleResult(
                proceed=False,
                rule_code=SR_REGIME_BLOCK,
                reason=f"market_regime {regime} blocked",
                forecast=forecast,
            )

        hint = (forecast.regime_hint or "").upper().strip()
        if hint and hint in cfg.blocked_regimes and hint != regime:
            return SignalRuleResult(
                proceed=False,
                rule_code=SR_REGIME_BLOCK,
                reason=f"regime_hint {hint} blocked (market={regime})",
                forecast=forecast,
            )

        # SR-06: optional YOLO no-trade filter (Wave 9)
        # Only fires when:
        #   - ctx.yolo_decision is not None (filter is enabled in pipeline)
        #   - decision is NOT degraded (real model verdict, not fallback)
        #   - decision.no_trade is True (high-confidence neutral pattern)
        # Placement: at the end so it acts as the final veto before SR-05/PROCEED.
        yolo = ctx.yolo_decision
        if yolo is not None and not yolo.degraded and yolo.no_trade:
            blocked_cls = yolo.blocked_class or "neutral"
            thr = yolo.threshold_used
            thr_str = f"{thr:.2f}" if thr is not None else "n/a"
            return SignalRuleResult(
                proceed=False,
                rule_code=SR_YOLO_NO_TRADE,
                reason=(
                    f"YOLO no-trade: {blocked_cls} prob {yolo.yolo_probs.get(blocked_cls, 0.0):.3f} "
                    f">= threshold {thr_str} (image_date={yolo.image_date})"
                ),
                forecast=forecast,
            )

        # SR-07: regime-aware gate (2026-07-01 council, Round 3 closure)
        # Admit if (high-conviction forecast) OR (trend-following close > SMA).
        # Backtest: worst-case hit_rate 0.50, mean 0.875 across 4 evaluation
        # windows; mean volume 3.25 trades/10-day horizon across 10 symbols.
        # Fail-closed if regime_features are missing — same posture as SR-04
        # (we'd rather skip a trade than enter without regime context).
        if cfg.sr07_enabled:
            sym_feat = ctx.regime_features.get(sym) or {}
            sma_key = f"sma_{cfg.sr07_sma_window}"
            close = sym_feat.get("close")
            sma = sym_feat.get(sma_key)

            fc_mag = abs(forecast.expected_return_pct)
            fc_pass = fc_mag >= cfg.sr07_min_forecast_pct
            trend_pass = (
                close is not None
                and sma is not None
                and not pd_is_nan(close)
                and not pd_is_nan(sma)
                and close > sma
            )

            if not (fc_pass or trend_pass):
                close_str = "n/a" if close is None else f"{close:.2f}"
                sma_str = "n/a" if sma is None else f"{sma:.2f}"
                return SignalRuleResult(
                    proceed=False,
                    rule_code=SR_REGIME_GATE,
                    reason=(
                        f"regime gate failed: |fc|={fc_mag:.4f}<{cfg.sr07_min_forecast_pct} "
                        f"and close={close_str} <= sma_{cfg.sr07_sma_window}={sma_str}"
                    ),
                    forecast=forecast,
                )

        return SignalRuleResult(
            proceed=True,
            rule_code=SR_PROCEED,
            reason="passed SR-01..SR-07",
            forecast=forecast,
        )