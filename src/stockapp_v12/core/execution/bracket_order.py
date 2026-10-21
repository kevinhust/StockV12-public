"""
BracketOrderService — submits parent + protective SL for swing entries.

Spec: POSITION_LIFECYCLE.md, TRADING_PIPELINE.md (Stage 4 bracket contract)
Owner: only this module writes parent_order_id / sl_order_id / sl_price into position records (via callback).

Does NOT manage trail or state — that is PositionManager.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Tuple, Dict, Any
from zoneinfo import ZoneInfo

from stockapp_v12.core.limits import V12RiskLimits

EST = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class BracketResult:
    """Result of a bracket submission."""
    parent_order_id: str
    sl_order_id: Optional[str]
    sl_price: float
    submitted_qty: int
    meta: Dict[str, Any]  # includes tier, atr_used, etc for audit


class BrokerAdapter(Protocol):
    """Minimal broker abstraction for bracket + paper mode.

    Canonical import for hexagonal consumers:
    ``stockapp_v12.contracts.BrokerAdapter`` (re-export; same object identity).
    Class remains defined here for backward compatibility with core/execution
    importers. Phase 2 may move the Protocol body into contracts.
    """

    def submit_bracket_order(
        self,
        *,
        symbol: str,
        side: str,  # "BUY"
        qty: int,
        entry_price: float,
        stop_price: float,
        mode: str = "paper_ibkr",
    ) -> Tuple[str, Optional[str]]:  # (parent_id, sl_id) or raise
        ...

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        ...

    def cancel_order(self, order_id: str) -> bool:
        ...


class BracketOrderService:
    """
    Pure submission service. Stateless except for injected broker.
    All risk parameters (ATR multiples) come from caller (Gatekeeper + limits).
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        *,
        limits: Optional[V12RiskLimits] = None,
    ):
        self.broker = broker
        self.limits = limits or V12RiskLimits()

    def calculate_sl_price(
        self,
        entry_price: float,
        atr: float,
        *,
        sl_atr_multiple: Optional[float] = None,
        tier: str = "high_vol",
    ) -> float:
        """SL = entry - (atr * multiple). Only for BUY (longs)."""
        mult = sl_atr_multiple or self._get_sl_multiple(tier)
        return round(entry_price - (atr * mult), 2)

    def _get_sl_multiple(self, tier: str) -> float:
        # These should ultimately come from loaded config, but defaults per RISK_MANAGEMENT
        tier_map = {
            "bluechip": 1.5,
            "mid_vol": 1.5,
            "high_vol": 1.2,
            "extreme_vol": 1.0,
        }
        return tier_map.get(tier.lower(), self.limits.sl_atr_multiple_default)

    def submit_entry_bracket(
        self,
        *,
        symbol: str,
        qty: int,
        entry_price: float,
        atr: float,
        mode: str = "paper_ibkr",
        tier: str = "high_vol",
        sl_atr_multiple: Optional[float] = None,
    ) -> BracketResult:
        """
        Submit MKT/LMT parent + STP child SL.

        Returns BracketResult with ids for PositionManager.register.
        On paper mode the broker may return synthetic ids.
        """
        if qty <= 0 or entry_price <= 0 or atr <= 0:
            raise ValueError("qty, entry_price, atr must be positive")

        sl_price = self.calculate_sl_price(
            entry_price, atr, sl_atr_multiple=sl_atr_multiple, tier=tier
        )

        parent_id, sl_id = self.broker.submit_bracket_order(
            symbol=symbol.upper(),
            side="BUY",
            qty=qty,
            entry_price=entry_price,
            stop_price=sl_price,
            mode=mode,
        )

        return BracketResult(
            parent_order_id=parent_id,
            sl_order_id=sl_id,
            sl_price=sl_price,
            submitted_qty=qty,
            meta={
                "tier": tier,
                "atr_used": atr,
                "sl_atr_multiple": sl_atr_multiple or self._get_sl_multiple(tier),
                "mode": mode,
            },
        )

    # Future: support TRIM partial, modify SL for TIGHTEN etc. (kept minimal for P2)
