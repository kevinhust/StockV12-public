"""
PositionManager — authoritative owner of position:{mode}:{symbol} lifecycle.

Spec: POSITION_LIFECYCLE.md (states, transitions, field ownership, trail ratchet, min-hold, cooldown)

Key guarantees:
- Single writer for state, trail fields, days_held, pending_action (via review), exit fields.
- Only PositionManager mutates position records.
- BracketOrderService only supplies order ids at register time.
- TradeJournal is append-only (never mutates position).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from typing import Callable, List, Optional, Protocol
from zoneinfo import ZoneInfo

from stockapp_v12.adapters.stores.redis_stores import RedisPositionStore
from stockapp_v12.contracts.position_store import PositionStore
from stockapp_v12.core.limits import V12RiskLimits
from stockapp_v12.core.precision import round_dollar, round_pct, round_shares
from stockapp_v12.pipeline.journal import TradeJournal
from stockapp_v12.services._notifier_compat import notifier  # K-cleanup: notifier shim

logger = logging.getLogger(__name__)
EST = ZoneInfo("America/New_York")


class PositionState(str, Enum):
    OPEN = "OPEN"
    TRAILING = "TRAILING"
    CLOSED = "CLOSED"
    # CANDIDATE / PENDING live in watchlist only; we start persisted state at OPEN


@dataclass
class PositionRecord:
    """In-memory + persisted representation (lifecycle_schema: 1)."""
    symbol: str
    mode: str
    state: PositionState
    entry_price: float
    entry_at: str  # ISO
    qty: int
    atr_at_entry: float
    sl_price: float

    lifecycle_schema: int = 1
    sl_order_id: Optional[str] = None
    parent_order_id: Optional[str] = None
    trail_price: Optional[float] = None
    highest_close_since_entry: Optional[float] = None
    lowest_close_since_entry: Optional[float] = None  # P2: for MAE tracking
    days_held: int = 0
    pending_action: Optional[str] = None  # null | TRIM | EXIT | TIGHTEN (set by DailyReview)
    catalyst_type_at_entry: Optional[str] = None
    regime_at_entry: Optional[str] = None
    thesis_tags: List[str] = None  # type: ignore[assignment]
    expected_holding_days: Optional[int] = None
    closed_at: Optional[str] = None
    exit_reason: Optional[str] = None  # sl | trail | review_exit | panic | manual | reconcile_*

    def to_redis(self) -> str:
        d = asdict(self)
        d["state"] = self.state.value
        if self.thesis_tags is None:
            d["thesis_tags"] = []
        return json.dumps(d, ensure_ascii=False)

    @staticmethod
    def from_redis(raw: str) -> "PositionRecord":
        d = json.loads(raw)
        d["state"] = PositionState(d["state"])
        if "thesis_tags" not in d or d["thesis_tags"] is None:
            d["thesis_tags"] = []
        return PositionRecord(**d)


class PriceProvider(Protocol):
    """Injectable for trailing stop checks and unrealized calc."""
    def get_last_close(self, symbol: str) -> Optional[float]: ...


class PositionManager:
    """
    Core lifecycle engine.

    Responsibilities:
    - register on entry fill
    - arm / ratchet trailing
    - react to broker SL fills (external callback path)
    - periodic trail tick (called by scheduler 5m)
    - min-hold enforcement helper (for discretionary SELL)
    - apply DailyReview pending_action
    - cooldown + index maintenance
    """

    LIFECYCLE_SCHEMA = 1

    def __init__(
        self,
        redis_client=None,
        *,
        journal: TradeJournal,
        limits: Optional[V12RiskLimits] = None,
        price_provider: Optional[PriceProvider] = None,
        mode: str = "paper_ibkr",
        store: Optional[PositionStore] = None,
    ):
        # Phase 3: prefer PositionStore; redis_client wrapped for BC.
        if store is not None:
            self._store: PositionStore = store
        elif redis_client is not None:
            self._store = RedisPositionStore(redis_client)
        else:
            raise ValueError("PositionManager requires redis_client or store")
        self._r = redis_client  # BC: tests/helpers may still inspect
        self.journal = journal
        self.limits = limits or V12RiskLimits()
        self.price_provider = price_provider
        self._mode = mode

    # ----------------- Key helpers -----------------

    def _pos_key(self, symbol: str, mode: Optional[str] = None) -> str:
        m = mode or self._mode
        return f"position:{m}:{symbol.upper()}"

    def _index_key(self, mode: Optional[str] = None) -> str:
        m = mode or self._mode
        return f"position:{m}:index"

    def _cooldown_key(self, symbol: str) -> str:
        return f"risk:cooldown:{symbol.upper()}"

    # ----------------- Read paths -----------------

    def get_position(self, symbol: str, mode: Optional[str] = None) -> Optional[PositionRecord]:
        raw = self._store.get_record(self._pos_key(symbol, mode))
        if not raw:
            return None
        try:
            return PositionRecord.from_redis(raw)
        except Exception as e:
            logger.error("Corrupt position record %s: %s", symbol, e)
            return None

    def list_open_positions(self, mode: Optional[str] = None) -> List[PositionRecord]:
        syms = self._store.index_members(self._index_key(mode))
        out: List[PositionRecord] = []
        for s in syms:
            p = self.get_position(s, mode)
            if p and p.state in (PositionState.OPEN, PositionState.TRAILING):
                out.append(p)
        return out

    def has_open_position(self, symbol: str, mode: Optional[str] = None) -> bool:
        return self._store.index_contains(self._index_key(mode), symbol.upper())

    # ----------------- Register (on successful entry fill) -----------------

    def register(
        self,
        *,
        symbol: str,
        entry_price: float,
        qty: int,
        atr_at_entry: float,
        parent_order_id: str,
        sl_order_id: Optional[str],
        sl_price: float,
        mode: Optional[str] = None,
        catalyst_type: Optional[str] = None,
        regime: Optional[str] = None,
        thesis_tags: Optional[List[str]] = None,
        expected_holding_days: Optional[int] = None,
    ) -> PositionRecord:
        """
        Called by entry flow AFTER BracketOrderService.submit + broker fill confirmation.
        Creates the canonical position record. Idempotent on re-call for same symbol (no duplicate).
        """
        sym = symbol.upper()
        m = mode or self._mode
        key = self._pos_key(sym, m)

        existing = self.get_position(sym, m)
        if existing and existing.state != PositionState.CLOSED:
            logger.warning("register called on existing non-CLOSED position %s — returning existing", sym)
            return existing

        now = datetime.now(EST).isoformat()
        rec = PositionRecord(
            symbol=sym,
            mode=m,
            state=PositionState.OPEN,
            entry_price=float(round_dollar(entry_price)),
            entry_at=now,
            qty=int(qty),
            atr_at_entry=float(round_shares(atr_at_entry)),
            sl_price=float(round_dollar(sl_price)),
            sl_order_id=sl_order_id,
            parent_order_id=parent_order_id,
            trail_price=None,
            highest_close_since_entry=entry_price,
            days_held=0,
            pending_action=None,
            catalyst_type_at_entry=catalyst_type,
            regime_at_entry=regime,
            thesis_tags=list(thesis_tags or []),
            expected_holding_days=expected_holding_days,
        )

        self._store.register_atomic(
            key, rec.to_redis(), self._index_key(m), sym
        )

        # journal
        self.journal.append(
            sym,
            mode=m,
            stage="hold",
            decision="OPEN",
            reason_code="ENTRY_FILLED",
            reason_detail=f"qty={qty} @ {entry_price}",
            extra={
                "parent_order_id": parent_order_id,
                "sl_order_id": sl_order_id,
                "sl_price": sl_price,
            },
        )

        notifier.notify_position_state_change(
            symbol=sym, old_state=None, new_state="OPEN",
            entry_price=entry_price, qty=qty
        )

        return rec

    # ----------------- Trailing logic (ratchet only) -----------------

    def _activation_price(self, rec: PositionRecord) -> float:
        activation = getattr(self.limits, "trailing_activation_atr", 1.0)
        return rec.entry_price + (activation * rec.atr_at_entry)

    def maybe_arm_trail(self, rec: PositionRecord, last_close: float) -> bool:
        """Return True if transitioned OPEN -> TRAILING."""
        if rec.state != PositionState.OPEN:
            return False

        # P2-exit-audit: track highest/lowest on every price observation
        rec.highest_close_since_entry = max(rec.highest_close_since_entry or rec.entry_price, last_close)
        rec.lowest_close_since_entry = min(rec.lowest_close_since_entry or rec.entry_price, last_close)

        activation_level = self._activation_price(rec)
        if last_close >= activation_level:
            rec.state = PositionState.TRAILING
            rec.trail_price = self._compute_trail_price(rec)
            self._persist(rec)
            return True
        self._persist(rec)
        return False

    def _compute_trail_price(self, rec: PositionRecord) -> float:
        trail_dist = getattr(self.limits, "trailing_distance_atr", 2.0)
        base = rec.highest_close_since_entry or rec.entry_price
        return float(round_dollar(base - (trail_dist * rec.atr_at_entry)))

    def update_trail_on_tick(self, symbol: str, last_close: float) -> Optional[PositionRecord]:
        """
        Called periodically by trailing_stop_check job (market hours).
        Ratchet highest only. May trigger exit.
        """
        rec = self.get_position(symbol)
        if not rec or rec.state == PositionState.CLOSED:
            return None

        # P2-exit-audit: always track highest/lowest for MFE/MAE, even
        # before trailing is armed (position may be stopped out while OPEN).
        rec.highest_close_since_entry = max(rec.highest_close_since_entry or rec.entry_price, last_close)
        rec.lowest_close_since_entry = min(rec.lowest_close_since_entry or rec.entry_price, last_close)

        # arm if needed
        self.maybe_arm_trail(rec, last_close)

        if rec.state != PositionState.TRAILING:
            self._persist(rec)
            return rec

        new_trail = self._compute_trail_price(rec)
        if rec.trail_price is None or new_trail > rec.trail_price:
            rec.trail_price = new_trail   # ratchet only

        # Check exit
        if last_close <= rec.trail_price:
            return self._close_position(rec, reason="trail", last_price=last_close)

        self._persist(rec)
        return rec

    # ----------------- Exit paths -----------------

    def on_sl_fill(self, symbol: str, fill_price: Optional[float] = None) -> Optional[PositionRecord]:
        """Broker callback path for hard SL hit. Always bypasses min-hold."""
        rec = self.get_position(symbol)
        if not rec or rec.state == PositionState.CLOSED:
            return rec
        return self._close_position(rec, reason="sl", last_price=fill_price)

    def _close_position(
        self, rec: PositionRecord, *, reason: str, last_price: Optional[float] = None
    ) -> PositionRecord:
        rec.state = PositionState.CLOSED
        rec.closed_at = datetime.now(EST).isoformat()
        rec.exit_reason = reason

        # P2-atomicity via PositionStore.close_atomic (srem + setex + set/expire).
        hrs = self.limits.symbol_cooldown_hours
        self._store.close_atomic(
            self._pos_key(rec.symbol, rec.mode),
            rec.to_redis(),
            self._index_key(rec.mode),
            rec.symbol,
            self._cooldown_key(rec.symbol),
            int(hrs * 3600),
            record_ttl_seconds=86400 * 7,
        )

        # P2-exit-audit: compute MFE/MAE/PNL for the journal record.
        # MFE = max favorable excursion (highest close above entry, as pct of entry)
        # MAE = max adverse excursion (lowest close below entry, as pct of entry)
        exit_price = last_price if last_price is not None else rec.entry_price
        highest = rec.highest_close_since_entry or rec.entry_price
        lowest = rec.lowest_close_since_entry or rec.entry_price

        if rec.entry_price > 0:
            pnl_pct = float(round_pct((exit_price - rec.entry_price) / rec.entry_price * 100))
            mfe_pct = float(round_pct((highest - rec.entry_price) / rec.entry_price * 100))
            mae_pct = float(round_pct((lowest - rec.entry_price) / rec.entry_price * 100))
        else:
            pnl_pct = 0.0
            mfe_pct = 0.0
            mae_pct = 0.0

        # Use journal.log_exit() with full MFE/MAE/PNL enrichment
        self.journal.log_exit(
            rec.symbol,
            mode=rec.mode,
            exit_reason=reason,
            pnl_pct=pnl_pct,
            hold_days=rec.days_held,
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            extra={
                "entry_price": rec.entry_price,
                "exit_price": exit_price,
                "regime_at_entry": rec.regime_at_entry,
            },
        )

        notifier.notify_position_state_change(
            symbol=rec.symbol, old_state=rec.state.value if hasattr(rec.state, 'value') else str(rec.state),
            new_state="CLOSED"
        )
        return rec

    def _persist(self, rec: PositionRecord, ttl_seconds: Optional[int] = None) -> None:
        key = self._pos_key(rec.symbol, rec.mode)
        self._store.set_record(key, rec.to_redis(), ttl_seconds=ttl_seconds)

    # ----------------- DailyReview support -----------------

    def apply_pending_action(self, symbol: str, action: Optional[str]) -> Optional[PositionRecord]:
        """DailyReview writes only this. PositionManager applies on next open or immediately if emergency."""
        rec = self.get_position(symbol)
        if not rec or rec.state == PositionState.CLOSED:
            return rec
        rec.pending_action = action
        self._persist(rec)
        return rec

    def trim_position(self, symbol: str, trim_pct: float = 0.5, reason: str = "REVIEW_TRIM") -> Optional[PositionRecord]:
        """Reduce position size (e.g. trim 50%). Called by DailyReview for TRIM decisions."""
        rec = self.get_position(symbol)
        if not rec or rec.state == PositionState.CLOSED or rec.qty <= 0:
            return rec
        trim_pct = max(0.1, min(0.9, trim_pct))
        old_qty = rec.qty
        new_qty = max(1, int(old_qty * (1 - trim_pct)))
        if new_qty >= old_qty:
            return rec

        rec.qty = new_qty
        rec.pending_action = None
        self._persist(rec)

        self.journal.append(
            symbol,
            mode=rec.mode,
            stage="review",
            decision="TRIM",
            reason_code=reason,
            reason_detail=f"trimmed_to_{new_qty}_from_{old_qty}",
            extra={
                "old_qty": old_qty,
                "new_qty": new_qty,
                "trim_pct": trim_pct,
            },
        )
        notifier.notify_position_state_change(
            symbol=symbol, old_state="OPEN", new_state="TRIMMED", qty=new_qty
        )
        return rec

    def close_for_review(self, symbol: str, reason: str = "review_exit") -> Optional[PositionRecord]:
        """DailyReview driven full exit."""
        rec = self.get_position(symbol)
        if not rec or rec.state == PositionState.CLOSED:
            return rec
        return self._close_position(rec, reason=reason)

    def increment_days_held(self, symbol: str) -> Optional[PositionRecord]:
        """Called by DailyReview (16:15) or EOD job once per trading day."""
        rec = self.get_position(symbol)
        if not rec or rec.state == PositionState.CLOSED:
            return rec
        rec.days_held += 1
        self._persist(rec)
        return rec

    # ----------------- Min-hold helper (R-10) -----------------

    def can_discretionary_sell(self, symbol: str) -> Tuple[bool, Optional[str]]:
        """
        Returns (allowed, reason_if_blocked).
        Used by Gatekeeper R-10 + any manual sell path.
        SL/trail always bypass (they go through on_sl_fill / trail exit).
        """
        rec = self.get_position(symbol)
        if not rec or rec.state == PositionState.CLOSED:
            return True, None
        if rec.days_held < self.limits.min_hold_days:
            return False, f"R-10: min_hold_days={self.limits.min_hold_days} not met (held {rec.days_held})"
        return True, None

    # ----------------- Reconciliation (broker is truth for qty) -----------------

    def reconcile_with_broker(self, symbol: str, broker_qty: int) -> Optional[PositionRecord]:
        rec = self.get_position(symbol)
        if not rec:
            return None
        if broker_qty <= 0 and rec.state != PositionState.CLOSED:
            return self._close_position(rec, reason="reconcile_flat")
        if broker_qty != rec.qty:
            logger.warning("qty mismatch %s broker=%s record=%s — keeping record, will journal", symbol, broker_qty, rec.qty)
            # In production we could adjust or force journal RECONCILE_QTY
        return rec

    def process_pending_action(
        self,
        symbol: str,
        broker_reconcile_fn: Optional[Callable[[str], Optional[int]]] = None,
    ) -> Optional[dict]:
        """
        Called at next session open (or premarket) to act on pending_action set by DailyReview.
        Returns action taken or None. In full impl this would submit orders via Bracket/executor.
        For now: returns the action and clears it (simulating execution decision).

        P1-3 (council 2026-07-09): On broker-mutate paths (EXIT/TRIM), if
        broker_reconcile_fn is provided, call it to get the broker's current
        qty. If reconcile raises, returns None, or disagrees with local qty,
        skip the local mutation and log the discrepancy instead of mutating
        on stale data. The entire method is wrapped in try/except so
        unexpected errors don't crash the entry pipeline.
        """
        try:
            rec = self.get_position(symbol)
            if not rec or not rec.pending_action or rec.state == PositionState.CLOSED:
                return None

            action = rec.pending_action
            result: dict = {"symbol": symbol, "action": action, "executed": False}

            # P1-3: reconcile with broker before any qty-affecting mutation
            if action in ("EXIT", "TRIM") and broker_reconcile_fn is not None:
                try:
                    broker_qty = broker_reconcile_fn(symbol)
                except Exception as exc:
                    logger.error(
                        "process_pending_action %s: broker reconcile FAILED — "
                        "skipping %s: %s",
                        symbol, action, exc,
                    )
                    rec.pending_action = None
                    self._persist(rec)
                    result["reason"] = "RECONCILE_FAILED"
                    return result

                if broker_qty is None:
                    logger.warning(
                        "process_pending_action %s: broker reports no position — "
                        "skipping %s",
                        symbol, action,
                    )
                    rec.pending_action = None
                    self._persist(rec)
                    result["reason"] = "BROKER_NO_POSITION"
                    return result

                if broker_qty != rec.qty:
                    logger.warning(
                        "process_pending_action %s: qty mismatch broker=%s "
                        "record=%s — clearing pending_action without local mutation",
                        symbol, broker_qty, rec.qty,
                    )
                    rec.pending_action = None
                    self._persist(rec)
                    result["reason"] = "QTY_MISMATCH"
                    return result

            if action == "EXIT":
                # In real: submit sell via bracket or market, then on fill it closes
                self._close_position(rec, reason="review_exit")
                result["executed"] = True
            elif action == "TRIM":
                # Partial: reduce qty 50%, move SL to breakeven (simplified here)
                new_qty = max(1, rec.qty // 2)
                rec.qty = new_qty
                # Move SL to breakeven approx
                rec.sl_price = rec.entry_price
                self._persist(rec)
                result["executed"] = True
                result["new_qty"] = new_qty
            elif action == "TIGHTEN":
                # Tighten trail (reduce distance) - here just log; real trail logic can read this
                rec.pending_action = None
                self._persist(rec)
                result["executed"] = True
            else:
                rec.pending_action = None
                self._persist(rec)

            rec.pending_action = None
            self._persist(rec)
            return result

        except Exception as exc:
            logger.exception(
                "process_pending_action %s: unexpected error: %s", symbol, exc
            )
            return {
                "symbol": symbol,
                "action": None,
                "executed": False,
                "reason": "INTERNAL_ERROR",
            }
