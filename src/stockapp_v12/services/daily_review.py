"""
DailyReview for V12 swing — 16:15 ET job.

Reads open positions from PositionManager, applies decision matrix from POSITION_LIFECYCLE.md,
updates days_held, sets pending_action (HOLD/TRIM/EXIT/TIGHTEN), appends to journal.

Does NOT execute orders (execution happens next session via pending_action consumer or manual).

This completes the review → pending_action → (next day生效) chain for multi-day positions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, List

from stockapp_v12.core.precision import D, round_pct
from stockapp_v12.services._notifier_compat import notifier  # K-cleanup: notifier shim
from stockapp_v12.services.position_manager import PositionManager, PositionState, PositionRecord
from stockapp_v12.pipeline.journal import TradeJournal

logger = logging.getLogger(__name__)


@dataclass
class ReviewDecision:
    symbol: str
    action: str  # HOLD | TRIM | EXIT | TIGHTEN | ALERT
    reason_code: str
    days_held: int
    unrealized_pnl_pct: float
    details: Dict[str, Any]


class DailyReview:
    """
    Owner of Stage 6 REVIEW per TRADING_PIPELINE.md and POSITION_LIFECYCLE.md.

    Matrix (simplified from spec):
    - Profit > 2x ATR and catalyst valid: HOLD
    - Profit > 2x ATR, catalyst gone: TRIM 50%
    - etc.
    """

    def __init__(
        self,
        position_manager: PositionManager,
        journal: TradeJournal,
        price_provider,  # callable symbol -> last_close or None
        regime_provider: Optional[callable] = None,  # -> current regime str
        mode: str = "paper_ibkr",
        apply_immediately: bool = False,  # P2-spec: spec says execution happens next session via pending_action consumer. paper-soak can opt-in via force_immediately=True.
    ):
        self.pm = position_manager
        self.journal = journal
        self.get_price = price_provider
        self.get_regime = regime_provider or (lambda: "NEUTRAL")
        self.mode = mode
        self.apply_immediately = apply_immediately

    def run(self) -> List[ReviewDecision]:
        """Run the daily review. Idempotent per symbol per day recommended by caller."""
        decisions: List[ReviewDecision] = []
        open_positions = self.pm.list_open_positions(self.mode)
        current_regime = self.get_regime()

        for pos in open_positions:
            if pos.state == PositionState.CLOSED:
                continue

            last_price = self.get_price(pos.symbol)
            if last_price is None or last_price <= 0:
                logger.warning("No price for %s, skipping review", pos.symbol)
                continue

            # P1-4 (council 2026-07-09): use Decimal for pnl/unrealized computation
            # to avoid float representational drift that compounds across multi-day
            # positions. The decision matrix in _decide() still works with float
            # thresholds (2.0, 0.8, -1.5, etc.) — we convert at the boundary.
            entry_d = D(pos.entry_price)
            last_d = D(last_price)
            atr_d = D(pos.atr_at_entry) if pos.atr_at_entry else D(0)
            unrealized_d = (last_d - entry_d) / entry_d if entry_d > 0 else D(0)
            unrealized = float(unrealized_d)
            if atr_d > 0 and entry_d > 0:
                pnl_atr = float(unrealized_d / (atr_d / entry_d))
            else:
                pnl_atr = 0.0

            action, reason = self._decide(pos, unrealized, pnl_atr, current_regime)

            # Always increment days (once per trading day)
            self.pm.increment_days_held(pos.symbol)

            size_change = 0.0
            if action in ("TRIM", "EXIT", "TIGHTEN"):
                if self.apply_immediately:
                    if action == "TRIM":
                        rec_after = self.pm.trim_position(pos.symbol, trim_pct=0.5, reason=reason)
                        if rec_after:
                            size_change = (pos.qty - rec_after.qty) / pos.qty if pos.qty > 0 else 0.5
                    else:
                        self.pm.close_for_review(pos.symbol, reason=reason)
                        size_change = 1.0
                else:
                    # Strict pending mode (for future executor consumption)
                    self.pm.apply_pending_action(pos.symbol, action)

            self.journal.append(
                pos.symbol,
                mode=self.mode,
                stage="review",
                decision=action,
                reason_code=reason,
                reason_detail=f"pnl_pct={float(round_pct(unrealized * 100)):.1f}, days={pos.days_held+1}, regime={current_regime}",
                extra={
                    "unrealized_pnl_pct": float(round_pct(unrealized * 100)),
                    "days_held": pos.days_held + 1,
                    "pending_action": action if action != "HOLD" else None,
                    "size_change": float(round_pct(size_change)),
                },
            )

            # Critical notification (no buttons - user will handle approve via other means if needed)
            notifier.notify_daily_review(
                symbol=pos.symbol,
                action=action,
                reason_code=reason,
                pnl_pct=float(round_pct(unrealized * 100)),
                days_held=pos.days_held + 1,
            )

            dec = ReviewDecision(
                symbol=pos.symbol,
                action=action,
                reason_code=reason,
                days_held=pos.days_held + 1,
                unrealized_pnl_pct=float(round_pct(unrealized * 100)),
                details={"regime": current_regime, "pnl_atr": pnl_atr},
            )
            decisions.append(dec)
            logger.info("DailyReview %s -> %s (%s)", pos.symbol, action, reason)

        if not open_positions:
            logger.info("DailyReview: no open positions")

        return decisions

    def _decide(self, pos: PositionRecord, unrealized: float, pnl_atr: float, regime: str) -> tuple[str, str]:
        days = pos.days_held
        profit_atr = pnl_atr
        loss_atr = -pnl_atr if pnl_atr < 0 else 0

        if regime.upper() == "PANIC":
            return "EXIT", "REGIME_PANIC"

        # Strong profit protection (main HOLD path)
        if profit_atr >= 2.0:
            # Good profit + not too many days → HOLD to let it run
            if days <= 10:
                return "HOLD", "PROFIT_STRONG"
            # Very long hold with big profit → consider trim for risk management
            return "TRIM", "PROFIT_MATURE_RISK_MANAGE"

        # Moderate profit
        if profit_atr > 0.8 and profit_atr < 2.0:
            if days < 4:
                return "HOLD", "PROFIT_EARLY"
            return "TIGHTEN", "PROFIT_MATURE_TIGHTEN"

        # Loss cases - protect capital
        if loss_atr >= 1.5:  # >1.5x ATR loss
            if days >= 3:
                return "EXIT", "LOSS_LARGE_DAYS"
            return "ALERT", "LOSS_LARGE_EARLY"

        if loss_atr > 0.7:
            if days >= 5:
                return "EXIT", "LOSS_MODERATE_DAYS"
            return "HOLD", "LOSS_EARLY_PROTECT"

        # Breakeven or small profit/loss
        if abs(profit_atr) < 0.3:
            if days < 5:
                return "HOLD", "NEUTRAL_EARLY"
            return "TIGHTEN", "NEUTRAL_MATURE"

        # Default
        if days < 5:
            return "HOLD", "DEFAULT_EARLY"
        return "TIGHTEN", "DEFAULT_MATURE"

    # Convenience for testing / manual
    def force_action(self, symbol: str, action: str, reason: str = "MANUAL"):
        self.pm.apply_pending_action(symbol, action)
        self.journal.append(
            symbol, mode=self.mode, stage="review", decision=action,
            reason_code=reason, reason_detail="forced",
        )
