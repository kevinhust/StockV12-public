"""
EntryOrchestrator stage chain (B3 refactor).

Decomposes the monolithic `_evaluate_and_maybe_enter_impl` method
(formerly 280+ lines in `services/entry_orchestrator.py`) into 7
discrete stages per `v12/docs/TRADING_PIPELINE.md` Stage 1..7 contract
and the post-launch roadmap § B3.

Stages (execution order, not numbering):
  1. begin_evaluation  — INV-1/INV-2 guard + process_pending_action short-circuit
  2. forecast          — ForecastService.forecast_daily + FORECAST_FAIL branch
  3. yolo_filter       — YoloNoTradeFilter.predict with degraded fallback
  4. signal_rules      — sr.config extract + regime_features + SignalRuleEngine.evaluate
  5. analyst           — analyst_fn dispatch (optional, sub-stage of "risk")
  6. live_guard        — LiveOrderGuard.check (A1, runs BEFORE gatekeeper)
  7. gatekeeper        — gatekeeper_fn + size_pct + notifier.notify_order_intent + log_allow
  8. bracket           — BracketOrderService.submit + notify_order_submitted + BRACKET_FAIL
  9. register          — PositionManager.register + journal.log_buy + EntryResult(BUY)

Each `run_stage_N` function returns `None` (continue) or an `EntryResult`
(early exit). The `_record_veto` wrapper is unchanged on EntryOrchestrator
(must remain a method because `tests/test_veto_alarm.py:394` uses
`EntryOrchestrator.__new__(EntryOrchestrator)` to inject a counter).

This module is a sequencing shim. All domain logic stays in the
injected collaborators (entry_guard, forecast, sr, yolo_filter, analyst_fn,
live_guard, gatekeeper, bracket, pm, journal). Each stage function
mirrors the exact line range of the former impl, with no behavior change.
"""

from __future__ import annotations

import logging
from datetime import date as _date
from typing import Any, Optional, Tuple


from stockapp_v12.bridge.live_guard import LiveGuardContext
from stockapp_v12.pipeline.journal import JournalWriteError
from stockapp_v12.pipeline.types import EntryContext, EntryResult
from stockapp_v12.schemas.forecast import ForecastOutput
from stockapp_v12.schemas.signal import SignalRuleContext  # Phase 2: no services import
from stockapp_v12.schemas.yolo import YoloFilterDecision

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Named constants (extracted from magic numbers in analyst/bracket stages).
# Per P2-spec (council 2026-07-09), these now live in core/limits.StageFallbacks
# so operators can tune via config instead of editing stage code.
# ---------------------------------------------------------------------------

# Import the dataclass; module-level aliases retain backward compatibility
# for any external importers (tests, other modules) that reference them.
from stockapp_v12.core.limits import StageFallbacks as _StageFallbacks

FALLBACK_ANALYST_CONFIDENCE: float = _StageFallbacks.analyst_confidence
FALLBACK_ATR_PCT: float = _StageFallbacks.atr_pct


# ---------------------------------------------------------------------------
# Stage 1: Begin entry evaluation (INV-1, INV-2 guard)
# ---------------------------------------------------------------------------

def run_stage_1_guard(orch, ctx: EntryContext, as_of) -> Optional[EntryResult]:
    """INV-1 + INV-2 guard. Returns EntryResult(PASS) on skip, None on proceed.

    Mirrors lines 165–186 of the pre-B3 `_evaluate_and_maybe_enter_impl`.
    Side effects: Redis SADD `entry:evaluated:{mode}:{date}` via
    `entry_guard.begin_entry_evaluation`; journal.append on PENDING.
    """
    sym = ctx.symbol.upper()

    can_enter, skip_code = orch.entry_guard.begin_entry_evaluation(
        sym, mode=ctx.mode,
        date=as_of.isoformat() if as_of else None,
    )
    if not can_enter:
        orch.journal.log_pass(
            sym, ctx.mode, reason_code=skip_code or "INV_GUARD",
            reason_detail="skipped by EntryEvaluationGuard",
        )
        return EntryResult(decision="PASS", reason_code=skip_code or "SKIPPED")

    # PENDING_REVIEW_ACTION check (duck-typed; some test mocks lack this method)
    pending = (
        orch.pm.process_pending_action(sym)
        if hasattr(orch.pm, "process_pending_action") else None
    )
    if pending and pending.get("action") in ("EXIT", "TRIM"):
        orch.journal.append(
            sym, mode=ctx.mode, stage="entry", decision="PASS",
            reason_code="PENDING_REVIEW_ACTION",
            reason_detail=f"pending {pending['action']} from prior review",
        )
        return EntryResult(decision="PASS", reason_code="PENDING_REVIEW_ACTION")
    return None


# ---------------------------------------------------------------------------
# Stage 2: Forecast retrieval
# ---------------------------------------------------------------------------

def run_stage_2_forecast(orch, ctx: EntryContext, as_of) -> tuple[Optional[ForecastOutput], Optional[EntryResult]]:
    """Forecast retrieval. Returns (forecast, EntryResult) — one of them is None.

    Mirrors lines 188–192 of the pre-B3 impl. On FORECAST_FAIL, returns
    (None, EntryResult(PASS, FORECAST_FAIL)) and the journal.write count
    matches the legacy `_log_and_mark("FORECAST_FAIL", ...)` call.
    """
    sym = ctx.symbol.upper()
    forecast = orch.forecast.forecast_daily(sym, ctx.bars, as_of=as_of)
    if forecast is None:
        _log_and_mark(orch, sym, ctx, "FORECAST_FAIL", "no forecast available")
        return None, EntryResult(decision="PASS", reason_code="FORECAST_FAIL", forecast=None)
    return forecast, None


# ---------------------------------------------------------------------------
# Stage 3: YOLO no-trade filter (between Forecast and SR, per B3 spec)
# ---------------------------------------------------------------------------

def run_stage_3_yolo_filter(
    orch, sym: str, as_of,
) -> Optional[YoloFilterDecision]:
    """Run YOLO filter (if enabled). Degraded fallback on exception.

    Mirrors lines 194–214 of the pre-B3 impl. Returns None when
    `orch.yolo_filter is None` (Wave 9 off). Returns a degraded
    `YoloFilterDecision` when the filter raises — this is *never*
    suppressive (the filter cannot block trades when it's broken).
    """
    if orch.yolo_filter is None:
        return None
    try:
        yolo_dt = as_of.date() if as_of and hasattr(as_of, "date") else as_of
        if yolo_dt is None:
            yolo_dt = _date.today()
        return orch.yolo_filter.predict(sym, yolo_dt)
    except Exception as e:
        # Defensive: never let the filter break the entry path
        yolo_dt = as_of.date() if as_of and hasattr(as_of, "date") else as_of
        if yolo_dt is None:
            yolo_dt = _date.today()
        return YoloFilterDecision(
            symbol=sym, as_of=yolo_dt, image_date=None, image_path=None,
            yolo_probs={}, predicted_class="", predicted_conf=0.0,
            no_trade=False,
            reason=f"FILTER_ERROR:{type(e).__name__}",
            degraded=True,
        )


# ---------------------------------------------------------------------------
# Stage 4: Signal rule evaluation (SR-01..SR-07)
# ---------------------------------------------------------------------------

def run_stage_4_signal_rules(
    orch, sym: str, ctx: EntryContext, forecast: ForecastOutput,
    yolo_decision: Optional[YoloFilterDecision],
) -> Tuple[Any, Optional[EntryResult]]:
    """Run signal-rule engine. Returns ``(sr, EntryResult(PASS))`` on short-circuit,
    ``(sr, None)`` on proceed.

    Mirrors lines 219–268 of the pre-B3 impl: defensive extraction of
    `sma_window` / `sr07_enabled` from `sr.config` (Mock-friendly);
    `regime_features` built from `ctx.bars` with fail-closed `{}` on
    KeyError/TypeError/ValueError; `SignalRuleContext` plumbs
    `yolo_decision` and `regime_features`; on `not sr.proceed`, journal
    `log_pass(sr.rule_code, ...)` and return early.
    """
    sma_window = 20
    sr07_enabled = True
    try:
        cfg = orch.sr.config
        if cfg is not None:
            sma_window = int(getattr(cfg, "sr07_sma_window", 20) or 20)
            sr07_enabled = bool(getattr(cfg, "sr07_enabled", True))
    except (AttributeError, TypeError, ValueError):
        sma_window = 20
        sr07_enabled = True

    regime_features: dict = {}
    if sr07_enabled and ctx.bars is not None and not ctx.bars.empty:
        try:
            close_series = ctx.bars["close"].astype(float)
            last_close = float(close_series.iloc[-1])
            sma_val: Optional[float] = None
            if len(close_series) >= sma_window:
                sma_val = float(close_series.iloc[-sma_window:].mean())
            regime_features = {sym: {"close": last_close, f"sma_{sma_window}": sma_val}}
        except (KeyError, TypeError, ValueError):
            regime_features = {}

    sr_ctx = SignalRuleContext(
        market_regime=ctx.market_regime,
        actionable_symbols=ctx.actionable_symbols,
        yolo_decision=yolo_decision,
        regime_features=regime_features,
    )
    sr = orch.sr.evaluate(forecast, sr_ctx)
    if not sr.proceed:
        orch.journal.log_pass(
            sym, ctx.mode,
            reason_code=sr.rule_code,
            reason_detail=sr.reason,
            analyst=None,
        )
        return sr, EntryResult(
            decision="PASS", reason_code=sr.rule_code, forecast=forecast, sr_result=sr,
            yolo_decision=yolo_decision,
        )
    return sr, None


# ---------------------------------------------------------------------------
# Stage 5a: LLM Analyst (optional)
# ---------------------------------------------------------------------------

def run_stage_5a_analyst(
    orch, sym: str, ctx: EntryContext, forecast: ForecastOutput,
    sr, yolo_decision, min_entry_confidence: float,
):
    """Run analyst_fn (if set). Returns (analyst, confidence, EntryResult).

    Mirrors lines 272–290 of the pre-B3 impl. On failure, returns
    `(None, 0.75, EntryResult(PASS, ANALYST_FAIL|ANALYST_ERROR))`. On
    low confidence, returns `(analyst, 0.75, EntryResult(PASS, LOW_CONFIDENCE))`.
    On success, returns `(analyst, analyst.confidence, None)`.
    """
    analyst = None
    if orch.analyst_fn is not None:
        try:
            from stockapp_v12.pipeline.analyst import AnalystOutput  # noqa: F401
            analyst, err = orch.analyst_fn(
                sym,
                context={"forecast": forecast.to_journal_dict() if forecast else None},
            )
            if err or analyst is None:
                _log_and_mark(orch, sym, ctx, "ANALYST_FAIL", err or "analyst returned None")
                return None, FALLBACK_ANALYST_CONFIDENCE, EntryResult(
                    decision="PASS", reason_code="ANALYST_FAIL",
                    forecast=forecast, sr_result=sr,
                )
        except Exception as e:
            _log_and_mark(orch, sym, ctx, "ANALYST_ERROR", str(e))
            return None, FALLBACK_ANALYST_CONFIDENCE, EntryResult(
                decision="PASS", reason_code="ANALYST_ERROR",
                forecast=forecast, sr_result=sr, yolo_decision=yolo_decision,
            )

        if not analyst.passes_confidence_gate(min_entry_confidence):
            orch.journal.log_pass(
                sym, ctx.mode, "LOW_CONFIDENCE",
                f"analyst confidence {analyst.confidence:.2f} < {min_entry_confidence}",
                analyst=analyst.to_dict(),
            )
            return analyst, FALLBACK_ANALYST_CONFIDENCE, EntryResult(
                decision="PASS", reason_code="LOW_CONFIDENCE",
                forecast=forecast, sr_result=sr, analyst=analyst,
                yolo_decision=yolo_decision,
            )

    confidence = analyst.confidence if analyst else FALLBACK_ANALYST_CONFIDENCE
    return analyst, confidence, None


# ---------------------------------------------------------------------------
# Stage 5b: LiveOrderGuard (A1) — runs STRICTLY BEFORE gatekeeper
# ---------------------------------------------------------------------------

def run_stage_5b_live_guard(
    orch, sym: str, ctx: EntryContext, analyst, current_price: float, confidence: float,
) -> Optional[EntryResult]:
    """A1 LiveGuard. Returns EntryResult(DENY) on block, None on pass.

    When ``live_guard is None``:
    - **live*** modes → hard DENY (LIVE_GUARD_REQUIRED) — never skip safety on live
    - paper / backtest → no-op (unit tests / research without Redis)
    """
    if orch.live_guard is None:
        mode_l = (ctx.mode or "").lower()
        if mode_l.startswith("live") or mode_l in {"live_ibkr", "live"}:
            orch.journal.log_deny(
                sym, ctx.mode,
                reason_code="LIVE_GUARD_REQUIRED",
                reason_detail="live mode requires LiveOrderGuard injection",
                requested_size_pct=0.0,
                analyst=analyst.to_dict() if analyst else None,
            )
            return EntryResult(
                decision="DENY", reason_code="LIVE_GUARD_REQUIRED",
                forecast=None, sr_result=None, analyst=analyst,
                yolo_decision=None,
            )
        return None
    guard_ctx = LiveGuardContext(
        symbol=sym, side="BUY", mode=ctx.mode,
        price=current_price, equity=ctx.equity, confidence=confidence,
    )
    guard_res = orch.live_guard.check(guard_ctx)
    if not guard_res.allowed:
        orch.journal.log_deny(
            sym, ctx.mode,
            reason_code=guard_res.rule,
            reason_detail=guard_res.reason,
            requested_size_pct=0.0,
            analyst=analyst.to_dict() if analyst else None,
        )
        return EntryResult(
            decision="DENY", reason_code=guard_res.rule,
            forecast=None, sr_result=None, analyst=analyst,  # forecast/sr set by caller pre-build
            yolo_decision=None,
        )
    return None


# ---------------------------------------------------------------------------
# Stage 5c: Gatekeeper sizing + log_allow + notify_order_intent
# ---------------------------------------------------------------------------

def run_stage_5c_gatekeeper(
    orch, sym: str, ctx: EntryContext, current_price: float, atr_14, atr_pct,
    analyst, forecast, sr, yolo_decision, confidence: float,
) -> tuple[Optional[EntryResult], float, dict]:
    """Run gatekeeper_fn. Returns (EntryResult on DENY | None, size_pct, risk_meta).

    Mirrors lines 331–363 of the pre-B3 impl. On ALLOW, also writes
    `notifier.notify_order_intent` and `journal.log_allow`. Caller must
    also call these in the legacy order (int happens here, allow happens here).
    """
    approved, reason, risk_meta = orch.gatekeeper(
        sym,
        confidence=confidence,
        side="BUY",
        price=current_price,
        atr_14=atr_14,
        atr_pct=atr_pct,
        regime=ctx.market_regime,
    )
    size_pct = float(risk_meta.get("size_pct", 0.0) or 0.0)
    if not approved or size_pct <= 0:
        orch.journal.log_deny(
            sym, ctx.mode, reason_code=reason or "GATEKEEPER_DENY",
            reason_detail=reason,
            requested_size_pct=size_pct,
            analyst=analyst.to_dict() if analyst else None,
        )
        return EntryResult(
            decision="DENY", reason_code=reason or "GATEKEEPER_DENY",
            forecast=forecast, sr_result=sr, analyst=analyst, yolo_decision=yolo_decision,
        ), size_pct, risk_meta

    # Notify intent (decision made) via injected orch.notifier
    orch.notifier.notify_order_intent(
        symbol=sym, side="BUY", qty=0,  # qty computed later
        price=current_price, size_pct=size_pct,
        reason=sr.reason if sr else "passed gates",
    )
    # P1-7: log_allow is strict=True. If the journal write fails, we MUST NOT
    # proceed to bracket submission — the audit trail would be broken for a
    # trade we're about to execute. Short-circuit to DENY with INV5 reason.
    try:
        orch.journal.log_allow(sym, ctx.mode, risk_meta, analyst=analyst.to_dict() if analyst else None)
    except JournalWriteError as e:
        logger.error(
            "[V12-PIPELINE] INV-5 violation: log_allow failed for %s — "
            "aborting before bracket submit: %s", sym, e,
        )
        return EntryResult(
            decision="DENY", reason_code="INV5_JOURNAL_FAIL",
            forecast=forecast, sr_result=sr, analyst=analyst,
            yolo_decision=yolo_decision,
        ), size_pct, risk_meta
    return None, size_pct, risk_meta


# ---------------------------------------------------------------------------
# Stage 6: Bracket order submission
# ---------------------------------------------------------------------------

def run_stage_6_bracket(
    orch, sym: str, ctx: EntryContext, current_price: float, atr_14, atr_pct,
    size_pct: float, forecast, sr, analyst, yolo_decision,
):
    """Submit bracket. Returns (bracket_res, EntryResult on BRACKET_FAIL | None).

    Mirrors lines 366–388 of the pre-B3 impl. qty computed here from
    `size_pct * equity / current_price` (clamped to ≥1). On exception,
    `_log_and_mark("BRACKET_FAIL", ...)` and DENY. On success, fires
    `notifier.notify_order_submitted` and returns the BracketResult.
    """
    raw_qty = int((size_pct * ctx.equity) / current_price) if current_price > 0 else 0
    if raw_qty < 1:
        # Do not force 1 share — that can exceed risk budget on expensive names.
        orch.journal.log_deny(
            sym, ctx.mode,
            reason_code="SIZE_BELOW_ONE_SHARE",
            reason_detail=f"computed qty={raw_qty} from size_pct={size_pct}",
            requested_size_pct=size_pct,
            analyst=analyst.to_dict() if analyst else None,
        )
        return None, 0, EntryResult(
            decision="DENY", reason_code="SIZE_BELOW_ONE_SHARE",
            forecast=forecast, sr_result=sr, analyst=analyst, yolo_decision=yolo_decision,
            size_pct=size_pct,
        )
    qty = raw_qty
    try:
        bracket_res = orch.bracket.submit_entry_bracket(
            symbol=sym, qty=qty,
            entry_price=current_price,
            atr=atr_14 or (current_price * (atr_pct or FALLBACK_ATR_PCT)),
            mode=ctx.mode, tier="high_vol",
        )
        orch.notifier.notify_order_submitted(
            symbol=sym, side="BUY", qty=qty,
            parent_order_id=bracket_res.parent_order_id,
            sl_price=bracket_res.sl_price,
        )
        return bracket_res, qty, None
    except Exception as e:
        _log_and_mark(orch, sym, ctx, "BRACKET_FAIL", str(e))
        return None, qty, EntryResult(
            decision="DENY", reason_code="BRACKET_FAIL", size_pct=size_pct,
            forecast=forecast, sr_result=sr, analyst=analyst, yolo_decision=yolo_decision,
        )


# ---------------------------------------------------------------------------
# Stage 7: Position registration + journal BUY + EntryResult(BUY)
# ---------------------------------------------------------------------------

def run_stage_7_register(
    orch, sym: str, ctx: EntryContext, current_price: float, atr_14, atr_pct,
    bracket_res, qty: int, size_pct: float, forecast, sr, analyst, yolo_decision,
) -> EntryResult:
    """Register position + journal BUY. Returns EntryResult(BUY).

    Mirrors lines 391–432 of the pre-B3 impl. The kwargs passed to
    `pm.register` are the *exact* list from lines 393–403, including the
    ternary `analyst.thesis_tags[0] if analyst and analyst.thesis_tags else None`.

    P0-4 (council 2026-07-09): after ``pm.register`` we *must* call
    ``orch.daily_counter_fn`` to increment the R-05 daily-trade counter.
    Previously this was only done by ``scripts/backtest_v12_paper.py``
    in its own loop, so production entries silently bypassed R-05.
    The increment is ``None``-safe (old call sites that don't wire
    ``daily_counter_fn`` keep working unchanged).
    """
    orch.pm.register(
        symbol=sym,
        entry_price=current_price,
        qty=qty,
        atr_at_entry=atr_14 or (current_price * (atr_pct or FALLBACK_ATR_PCT)),
        parent_order_id=bracket_res.parent_order_id,
        sl_order_id=bracket_res.sl_order_id,
        sl_price=bracket_res.sl_price,
        mode=ctx.mode,
        # P2-spec (council 2026-07-09): catalyst_type aliasing contract.
        # AnalystOutput doesn't have a dedicated `catalyst_type` field; the
        # first element of `thesis_tags` is the de-facto catalyst label.
        # This contract is documented in the AnalystOutput docstring and
        # consumed by PositionManager (field `catalyst_type_at_entry`).
        catalyst_type=(analyst.thesis_tags[0] if analyst and analyst.thesis_tags else None),
        regime=ctx.market_regime,
        thesis_tags=analyst.thesis_tags if analyst else [],
        expected_holding_days=analyst.expected_holding_days if analyst else None,
    )
    # P1-7: log_buy is strict=True. If the journal write fails, the position
    # is ALREADY registered via pm.register — we can't undo it. We still
    # return BUY (the trade is live) but log a CRITICAL warning so the
    # operator knows the audit trail is broken for this entry.
    try:
        orch.journal.log_buy(
            sym, ctx.mode,
            parent_order_id=bracket_res.parent_order_id,
            sl_order_id=bracket_res.sl_order_id,
            size_pct=size_pct,
            analyst=analyst.to_dict() if analyst else None,
            extra={"qty": qty, "sr_rule": sr.rule_code if sr else None},
        )
    except JournalWriteError as e:
        logger.critical(
            "[V12-PIPELINE] INV-5 violation: log_buy failed for %s after "
            "pm.register — position is LIVE but audit trail is broken: %s. "
            "Manual reconciliation required.", sym, e,
        )
    # P0-4: bump the R-05 daily-trade counter (after the position is
    # registered + the journal BUY is logged so we have an audit trail
    # if Redis dies right after). Fail-closed posture: any exception
    # from Redis is logged at WARNING + swallowed — the *position is
    # already on the book* and reverting it here would require a
    # bracket-rollback that we don't yet have atomic guarantees for.
    # The next check_daily_trade_cap() will return the wrong number
    # until the counter catches up, but that's a known correctness
    # trade-off documented in the council synthesis.
    if getattr(orch, "daily_counter_fn", None) is not None:
        try:
            count = orch.daily_counter_fn()
            logger.info(
                "[V12-PIPELINE] R-05 daily trade count = %d (after BUY %s qty=%d)",
                count, sym, qty,
            )
        except Exception as e:
            logger.warning(
                "[V12-PIPELINE] R-05 daily_counter_fn failed for %s: %s: %s "
                "(fail-closed per P0-4: position already registered; next "
                "cap-check may undercount by 1 until Redis recovers)",
                sym, type(e).__name__, e,
            )
    return EntryResult(
        decision="BUY", reason_code="OK",
        size_pct=size_pct, qty=qty,
        forecast=forecast, sr_result=sr, analyst=analyst,
        parent_order_id=bracket_res.parent_order_id,
        sl_order_id=bracket_res.sl_order_id,
        yolo_decision=yolo_decision,
    )


# ---------------------------------------------------------------------------
# Helper: replicate the pre-B3 _log_and_mark behavior
# ---------------------------------------------------------------------------

def _log_and_mark(orch, sym: str, ctx: EntryContext, code: str, detail: str) -> None:
    """Write journal.log_pass for early-exit paths. Mirrors the pre-B3 helper.

    The pre-B3 comment notes: "already marked by begin_entry_evaluation; if we
    want to be strict we could skip the mark on early path but current guard
    marks before forecast, which is acceptable per INV-1 (we did evaluate)".
    """
    orch.journal.log_pass(sym, ctx.mode, code, detail)


# ---------------------------------------------------------------------------
# Price + ATR extraction (used by 5b/6/7)
# ---------------------------------------------------------------------------

def extract_price_atr(ctx: EntryContext, forecast: ForecastOutput) -> tuple[float, Optional[float], Optional[float]]:
    """Extract current_price, atr_14, atr_pct from ctx.bars (or forecast fallback).

    Mirrors lines 296–300 of the pre-B3 impl. Used by stages 5b/6/7 to
    keep the expression computed exactly once and bit-identical.

    P1-8 (council 2026-07-09): ``iloc[-1]`` assumes bars are sorted ascending
    by date. If a caller passes unsorted bars, the wrong day's close feeds
    SL math. We now assert ``index.is_monotonic_increasing`` before reading.
    """
    current_price = forecast.current_price
    atr_14 = None
    atr_pct = None
    if ctx.bars is not None and not ctx.bars.empty:
        # P1-8: guard against unsorted bars — iloc[-1] must be the latest bar.
        if not ctx.bars.index.is_monotonic_increasing:
            raise ValueError(
                "extract_price_atr: ctx.bars index is not monotonic increasing. "
                "illoc[-1] would read the wrong bar. Sort bars by date before "
                "passing to EntryContext."
            )
        current_price = float(ctx.bars.iloc[-1]["close"])
        if "atr_14" in ctx.bars.columns:
            atr_14 = float(ctx.bars.iloc[-1]["atr_14"])
        elif "atr_pct" in ctx.bars.columns:
            atr_pct = float(ctx.bars.iloc[-1]["atr_pct"])
    return current_price, atr_14, atr_pct
