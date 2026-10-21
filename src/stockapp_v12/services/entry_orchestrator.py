"""
EntryOrchestrator — wires the full Stage 4 entry path for V12 swing.

Flow (per spec):
EntryGuard (INV-1/2) → ForecastService (TSFM) → SignalRuleEngine (SR-01~05)
    → [optional LLM Analyst narrative] → Gatekeeper (R-01~R-10 + size)
    → BracketOrderService → PositionManager.register → TradeJournal

Wave 9 (YOLO no-trade filter) inserts one extra stage between Forecast and SR:
    ForecastService → YoloNoTradeFilter (optional, soft veto)
        → SignalRuleEngine (SR-01~05, with SR-06 YOLO veto honored via ctx.yolo_decision)
        → ...

If `yolo_filter` is None, the orchestrator behaves exactly as before.
If the YOLO filter is present but degraded (model not loaded / image missing),
SR-06 is a no-op (no false suppression from a missing chart).

This module owns the orchestration and journaling of PASS/DENY/ALLOW/BUY.
It does not own scheduling or data fetching (caller responsibility).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Protocol

logger = logging.getLogger(__name__)


from stockapp_v12.bridge.live_guard import LiveOrderGuard
from stockapp_v12.contracts.notifier import Notifier
from stockapp_v12.core.execution.bracket_order import BracketOrderService
from stockapp_v12.pipeline.analyst import AnalystOutput
from stockapp_v12.pipeline.entry_guard import EntryEvaluationGuard
from stockapp_v12.pipeline.journal import TradeJournal
from stockapp_v12.services.veto_alarm import VetoCounter
from stockapp_v12.services.position_manager import PositionManager
from stockapp_v12.services.signal_rule_engine import (
    SignalRuleEngine,
)
from stockapp_v12.services.yolo_filter import YoloNoTradeFilter


class AnalystFn(Protocol):
    """Callable that produces structured analyst output for a symbol."""
    def __call__(
        self, symbol: str, *, context: Optional[Dict[str, Any]] = None
    ) -> tuple[Optional[AnalystOutput], Optional[str]]: ...


class GatekeeperFn(Protocol):
    """Adapter to HardRiskGatekeeper.validate_order_intent or equivalent."""
    def __call__(
        self,
        symbol: str,
        *,
        confidence: float,
        side: str,
        price: float,
        atr_pct: Optional[float] = None,
        atr_14: Optional[float] = None,
        **kwargs: Any,
    ) -> tuple[bool, str, Dict[str, Any]]: ...


# P2-boundaries (council 2026-07-09): EntryContext/EntryResult are now
# re-exported from stockapp_v12.pipeline.types to break the
# services.entry_orchestrator <-> pipeline.stages circular import.
from stockapp_v12.pipeline.types import EntryContext, EntryResult  # noqa: F401


class EntryOrchestrator:
    """
    Orchestrates one full entry attempt.

    All heavy logic lives in the injected collaborators.
    This class is responsible for sequencing, early exits, and consistent journaling.
    """

    def __init__(
        self,
        *,
        entry_guard: EntryEvaluationGuard,
        forecast_service,  # ForecastService
        sr_engine: SignalRuleEngine,
        analyst_fn: Optional[AnalystFn] = None,
        gatekeeper_fn: GatekeeperFn,
        bracket_service: BracketOrderService,
        position_manager: PositionManager,
        journal: TradeJournal,
        yolo_filter: Optional[YoloNoTradeFilter] = None,  # Wave 9
        live_guard: Optional[LiveOrderGuard] = None,  # A1: LiveOrderGuard (Phase A1 safety)
        veto_counter: Optional[VetoCounter] = None,  # A4: track per-day decision mix
        daily_counter_fn: Optional[Callable[[], int]] = None,  # P0-4 (council 2026-07-09)
        notifier: Optional[Notifier] = None,  # Task 8: inject delivery surface
    ):
        self.entry_guard = entry_guard
        self.forecast = forecast_service
        self.sr = sr_engine
        self.analyst_fn = analyst_fn
        self.gatekeeper = gatekeeper_fn
        self.bracket = bracket_service
        self.pm = position_manager
        self.journal = journal
        self.yolo_filter = yolo_filter  # Wave 9 — None disables SR-06 entirely
        self.live_guard = live_guard  # A1: optional — old call sites pass None
        self.veto_counter = veto_counter  # A4: optional — old call sites pass None
        self.daily_counter_fn = daily_counter_fn  # P0-4 — None means no-op
        # Default keeps production behavior identical (compat singleton).
        from stockapp_v12.services._notifier_compat import notifier as _default_notifier
        self.notifier = notifier if notifier is not None else _default_notifier

    def evaluate_and_maybe_enter(
        self,
        ctx: EntryContext,
        *,
        min_entry_confidence: float = 0.70,
        as_of=None,
    ) -> EntryResult:
        sym = ctx.symbol.upper()
        return self._record_veto(
            self._evaluate_and_maybe_enter_impl(ctx, min_entry_confidence=min_entry_confidence, as_of=as_of)
        )

    def _record_veto(self, res: "EntryResult") -> "EntryResult":
        """A4: count every decision in the per-day veto histogram.

        Cheap (1 HINCRBY + 1 EXPIRE on Redis; no round-trip impact on tests
        that pass `veto_counter=None` since the helper is a no-op then).
        """
        if self.veto_counter is not None:
            try:
                self.veto_counter.record(res.decision)
            except Exception as e:
                logger.warning("veto_counter.record failed for decision=%s: %s: %s",
                               res.decision, type(e).__name__, e)
        return res

    def _evaluate_and_maybe_enter_impl(
        self,
        ctx: EntryContext,
        *,
        min_entry_confidence: float = 0.70,
        as_of=None,
    ) -> EntryResult:
        """B3 stage-chain decomposition.

        Each stage function in `stockapp_v12.pipeline.stages` mirrors
        a contiguous line range of the pre-B3 monolith. Returns on
        first non-None `EntryResult` from a stage. Behavior is
        bit-for-bit identical to the pre-B3 impl (verified by
        `tests/test_entry_orchestrator.py` 5/5 + per-stage unit tests).
        """
        # P2-boundaries: now a top-level import (cycle broken by moving
        # EntryContext/EntryResult to pipeline.types).
        from stockapp_v12.pipeline.stages import (
            extract_price_atr,
            run_stage_1_guard,
            run_stage_2_forecast,
            run_stage_3_yolo_filter,
            run_stage_4_signal_rules,
            run_stage_5a_analyst,
            run_stage_5b_live_guard,
            run_stage_5c_gatekeeper,
            run_stage_6_bracket,
            run_stage_7_register,
        )

        sym = ctx.symbol.upper()

        # Stage 1: begin entry evaluation
        early = run_stage_1_guard(self, ctx, as_of)
        if early is not None:
            return early

        # Stage 2: forecast retrieval
        forecast, early = run_stage_2_forecast(self, ctx, as_of)
        if early is not None:
            return early
        assert forecast is not None  # type narrowing for type-checker

        # Stage 3: YOLO no-trade filter (between Forecast and SR, per B3 spec)
        yolo_decision = run_stage_3_yolo_filter(self, sym, as_of)

        # Stage 4: signal rule evaluation
        sr, early = run_stage_4_signal_rules(self, sym, ctx, forecast, yolo_decision)
        if early is not None:
            return early

        # Stage 5a: LLM Analyst
        analyst, confidence, early = run_stage_5a_analyst(
            self, sym, ctx, forecast, sr, yolo_decision, min_entry_confidence,
        )
        if early is not None:
            return early

        # Price + ATR extraction (used by 5b/6/7)
        current_price, atr_14, atr_pct = extract_price_atr(ctx, forecast)

        # Stage 5b: LiveOrderGuard (A1) — runs STRICTLY BEFORE gatekeeper
        early = run_stage_5b_live_guard(
            self, sym, ctx, analyst, current_price, confidence,
        )
        if early is not None:
            # stage_5b's return value sets forecast=None / sr_result=None;
            # the legacy impl sets forecast=forecast, sr_result=sr. Fix:
            return EntryResult(
                decision=early.decision, reason_code=early.reason_code,
                forecast=forecast, sr_result=sr, analyst=analyst,
                yolo_decision=yolo_decision,
            )

        # Stage 5c: Gatekeeper sizing + log_allow + notify_order_intent
        early, size_pct, _ = run_stage_5c_gatekeeper(
            self, sym, ctx, current_price, atr_14, atr_pct,
            analyst, forecast, sr, yolo_decision, confidence,
        )
        if early is not None:
            return early

        # Stage 6: Bracket submission
        bracket_res, qty, early = run_stage_6_bracket(
            self, sym, ctx, current_price, atr_14, atr_pct,
            size_pct, forecast, sr, analyst, yolo_decision,
        )
        if early is not None:
            return early
        assert bracket_res is not None  # type narrowing

        # Stage 7: Register position + journal BUY
        return run_stage_7_register(
            self, sym, ctx, current_price, atr_14, atr_pct,
            bracket_res, qty, size_pct, forecast, sr, analyst, yolo_decision,
        )
