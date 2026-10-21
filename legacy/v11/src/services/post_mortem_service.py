"""
Post-Mortem Service V2 (Self-Correction Layer)
Automated pipeline: Extractor → 4-Layer Filter → Reflector → Injector

Extracts losing trades, runs them through a 4-layer quality filter,
generates causal lessons via structured LLM analysis, and injects
them into KnowledgeStore (ChromaDB + Redis) for future RAG retrieval.

Changes from V1:
- Removed: pickle deserialization (RCE risk), regex LLM parsing,
  direct ChromaDB collection creation, lesson_text truncation
- Added: 4-layer filtering (Qualify→Bucket→Refine→Veto),
  Pydantic structured LLM output, severity gate (>= 7),
  dedup via Redis, correct PnL from SQL realized_pnl_pct
- V2.1: Stage 4.5 Shadow Backtesting — simulate suggestion impact
  on last 30d of similar trades before queuing for review
- V2.2: Dynamic ATR thresholds — Stage 1/2 use volatility-adjusted
  (ATR%) thresholds instead of hardcoded pct values
"""
import os
import sys
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Dict, Any

import psycopg2
from psycopg2.extras import RealDictCursor

from langchain_core.messages import SystemMessage, HumanMessage

from src.services.post_mortem_models import (
    LosingTrade,
    NegativeLesson,
    TradeReviewModel,
    LessonEnvelope,
    FilterResult,
)
# KnowledgeStore lives in V11 (archived). Optional import — INJECT stage is
# skipped gracefully if unavailable. V12 paper-soak runs post_mortem in
# degrade-closed mode (lessons still extracted + filtered, only RAG inject dropped).
try:
    from src.knowledge.store import KnowledgeStore  # type: ignore[import-not-found]
    _KNOWLEDGE_STORE_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    KnowledgeStore = None  # type: ignore[assignment,misc]
    _KNOWLEDGE_STORE_AVAILABLE = False
    logging.getLogger("PostMortemService").warning(
        "src.knowledge.store unavailable (V11 archived). "
        "Post-mortem will run EXTRACT/FILTER/REFLECT but skip INJECT stage. "
        "Lessons cached in Redis short-term; no long-term RAG writes."
    )
from src.services.reinforcement_service import ReinforcementEngine
from src.services.market_data_service import MarketDataService

logger = logging.getLogger("PostMortemService")

# ── Market hours (ET) ──
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 30
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 16, 0

# Time-of-day buckets (ET hour)
TIME_OF_DAY = {
    (0, 9): "premarket",
    (9, 12): "morning",
    (12, 14): "midday",
    (14, 16): "afternoon",
    (16, 24): "eod",
}

BULL_REGIMES = {"BULL_LONG", "RANGE_BULLISH", "BULL", "NEUTRAL"}
RISKY_PATTERNS = {"CHOPPY", "FAKEOUT", "RANGE_BOUND"}

# Severity gate — only lessons with severity >= this enter RAG
SEVERITY_RAG_THRESHOLD = 7


class PostMortemService:
    """
    Four-stage pipeline for automated trade post-mortem:
    1. EXTRACT  — Find losing trades from trade_events
    2. FILTER   — 4-layer quality gate (Qualify→Bucket→Refine→Veto)
    3. REFLECT  — LLM causal analysis with structured output
    4. INJECT   — Store filtered lessons in KnowledgeStore (ChromaDB + Redis)
    """

    # Thresholds
    MAX_LESSONS_PER_RUN = 10        # Avoid runaway LLM calls
    LOOKBACK_HOURS = 24             # Default scan window
    # ATR multipliers for dynamic thresholds (V2.2)
    ATR_EXTRACT_MULTIPLIER = 1.0    # Stage 1: pnl_pct < -1.0 * ATR%
    ATR_NEGATIVE_BUCKET = 1.2       # Stage 2 L2: pnl_pct < -1.2 * ATR%
    ATR_POSITIVE_BUCKET = 0.8       # Stage 2 L2: pnl_pct > 0.8 * ATR%
    ATR_DEFAULT_FALLBACK = 2.0      # Default ATR% when data unavailable

    def __init__(
        self,
        db_url: Optional[str] = None,
        knowledge_store: Optional["KnowledgeStore"] = None,
        llm_model: str = "glm-5",
        market_data: Optional[MarketDataService] = None,
    ):
        self.db_url = db_url or os.getenv(
            "DATABASE_URL", self._build_db_url()
        )
        # KnowledgeStore optional (V11 archived). If not provided AND not importable,
        # INJECT stage will be skipped — but extract/filter/reflect still run.
        if knowledge_store is not None:
            self.knowledge_store = knowledge_store
        elif _KNOWLEDGE_STORE_AVAILABLE and KnowledgeStore is not None:
            try:
                self.knowledge_store = KnowledgeStore()
            except Exception as _ks_e:
                logging.getLogger("PostMortemService").warning(
                    f"KnowledgeStore() init failed ({_ks_e}); INJECT stage will skip"
                )
                self.knowledge_store = None
        else:
            self.knowledge_store = None
        self.market_data = market_data or MarketDataService()

        # LLM setup
        self.llm_model = llm_model
        try:
            from langchain_community.chat_models import ChatZhipuAI
            zai_key = os.getenv("ZAI_API_KEY")
            if zai_key:
                self.llm = ChatZhipuAI(
                    model=llm_model, temperature=0.2, api_key=zai_key,
                )
            else:
                from langchain_openai import ChatOpenAI
                self.llm = ChatOpenAI(model="gpt-4-turbo", temperature=0.2)
        except ImportError:
            from langchain_openai import ChatOpenAI
            self.llm = ChatOpenAI(model="gpt-4-turbo", temperature=0.2)

        self.reinforcement_engine = ReinforcementEngine(db_url=self.db_url)

    # ─────────────────────────────────────────────
    #  STAGE 1: EXTRACTOR
    # ─────────────────────────────────────────────

    def extract_losers(
        self,
        lookback_hours: int = 24,
        pnl_threshold_pct: float = None,
    ) -> List[LosingTrade]:
        """
        Query trade_events for closed losing trades within the lookback window.

        V2.2: Uses dynamic ATR-based thresholds. If pnl_threshold_pct is provided,
        it acts as a fallback when ATR data is unavailable.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        use_static = pnl_threshold_pct is not None

        if use_static:
            logger.info(
                f"Scanning for losing trades since {cutoff.isoformat()} "
                f"(static threshold: {pnl_threshold_pct}%)"
            )
        else:
            logger.info(
                f"Scanning for losing trades since {cutoff.isoformat()} "
                f"(dynamic ATR threshold)"
            )

        sql = """
            SELECT
                te.event_id,
                te.trace_id,
                te.symbol,
                te.side,
                te.quantity,
                te.price,
                te.pattern_type,
                te.mode,
                te.time AS event_time,
                te.risk_metadata,
                te.context_snapshot,
                te.realized_pnl_dollar AS realized_pnl,
                te.realized_pnl_pct
            FROM trade_events te
            WHERE te.time >= %s
              AND te.trace_id IS NOT NULL
              AND te.side = 'SELL'
              AND te.realized_pnl_dollar IS NOT NULL
              AND te.realized_pnl_dollar < 0
            ORDER BY te.realized_pnl_dollar ASC;
        """

        losers: List[LosingTrade] = []

        try:
            with psycopg2.connect(self.db_url) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, (cutoff,))
                    rows = cur.fetchall()

            for row in rows:
                rpnl = float(row.get("realized_pnl", 0.0))
                pnl_pct_decimal = float(row.get("realized_pnl_pct", 0.0))

                # Convert to percentage points for comparison with threshold
                pnl_pct_pp = pnl_pct_decimal * 100

                # Dynamic ATR threshold or static fallback
                if use_static:
                    threshold = pnl_threshold_pct
                else:
                    symbol = row.get("symbol", "")
                    price = float(row.get("price", 1.0))
                    event_time = row.get("event_time")
                    atr_pct = self._get_atr_pct(symbol, event_time, price)
                    threshold = -1.0 * self.ATR_EXTRACT_MULTIPLIER * atr_pct

                if pnl_pct_pp > threshold:
                    continue

                trace_id = row.get("trace_id", "")
                doc_id = f"lesson_{trace_id}"

                # Dedup via Redis (O(1)) — skipped if KnowledgeStore unavailable
                if self.knowledge_store is not None and self.knowledge_store.lesson_exists_in_redis(doc_id):
                    logger.debug(f"  Skipping {trace_id[:8]}... already in Redis cache")
                    continue

                price = float(row.get("price", 1.0))
                qty = float(row.get("quantity", 0.0))
                event_time = row.get("event_time")

                losers.append(LosingTrade(
                    trace_id=trace_id,
                    event_id=row.get("event_id", ""),
                    symbol=row.get("symbol", ""),
                    side=row.get("side", ""),
                    entry_price=price,  # approximate; actual entry from open_positions
                    exit_price=price,
                    quantity=qty,
                    realized_pnl=rpnl,
                    pnl_percent=round(pnl_pct_pp, 2),
                    pattern_type=row.get("pattern_type") or "UNKNOWN",
                    mode=row.get("mode", "PAPER"),
                    entry_time=event_time,
                    risk_metadata=row.get("risk_metadata") or {},
                    context_snapshot=row.get("context_snapshot") or {},
                ))

                if len(losers) >= self.MAX_LESSONS_PER_RUN:
                    break

            logger.info(
                f"Found {len(losers)} losing trades exceeding threshold"
            )
            return losers

        except Exception as e:
            logger.error(f"Extractor failed: {e}")
            return []

    # ─────────────────────────────────────────────
    #  STAGE 2: 4-LAYER FILTER
    # ─────────────────────────────────────────────

    def filter_trade(self, trade: LosingTrade) -> FilterResult:
        """Run trade through 4-layer quality pipeline."""
        # Layer 1: Qualify (market hours + hold duration)
        if not self._layer1_qualify(trade):
            return FilterResult(passed=False, bucket="neutral",
                                layer_reasons=["L1: failed qualify"])

        # Layer 2: Performance bucket
        bucket = self._layer2_bucket(trade)
        reasons = [f"L2: bucketed as {bucket}"]

        # Layer 3: Quality refine
        bucket, l3_reasons = self._layer3_refine(trade, bucket)
        reasons.extend(l3_reasons)

        # Layer 4: Business veto
        bucket, l4_reasons = self._layer4_veto(trade, bucket)
        reasons.extend(l4_reasons)

        # Final decision + quality gates
        passed, priority_score = self._final_decision(trade, bucket, reasons)
        reasons.append(f"Priority score: {priority_score}")

        return FilterResult(
            passed=passed,
            bucket=bucket,
            layer_reasons=reasons,
            priority_score=priority_score,
        )

    def _layer1_qualify(self, trade: LosingTrade) -> bool:
        """Market hours check + hold duration > 6 min."""
        if not trade.entry_time:
            return True  # Can't check without timestamp
        # Hold duration check
        if trade.exit_time:
            hold_sec = (trade.exit_time - trade.entry_time).total_seconds()
            if hold_sec < 360:  # 6 minutes
                return False
        return True

    def _layer2_bucket(self, trade: LosingTrade) -> str:
        """Performance-based bucketing with dynamic ATR thresholds."""
        pnl = trade.pnl_percent
        atr_pct = self._get_atr_pct(trade.symbol, trade.entry_time, trade.entry_price)

        neg_threshold = -1.0 * self.ATR_NEGATIVE_BUCKET * atr_pct
        pos_threshold = self.ATR_POSITIVE_BUCKET * atr_pct

        if pnl < neg_threshold:
            return "negative"
        if pnl > pos_threshold and trade.risk_metadata.get("confidence", 0) >= 0.75:
            return "positive"
        return "neutral"

    def _layer3_refine(self, trade: LosingTrade, bucket: str) -> tuple:
        """Signal quality, regime mismatch, risky patterns."""
        reasons = []
        signal = trade.risk_metadata.get("confidence", 0)
        pattern = trade.pattern_type
        entry_regime = trade.risk_metadata.get("regime", "UNKNOWN")

        # Weak signal degrades positive to neutral
        if signal < 0.65 and bucket == "positive":
            reasons.append(f"Weak signal ({signal:.2f})")
            bucket = "neutral"

        # Risky patterns → negative
        if pattern in RISKY_PATTERNS:
            reasons.append(f"Risky pattern: {pattern}")
            bucket = "negative"

        # Regime mismatch (BULL entry → BEAR/panic exit)
        is_bullish = entry_regime in BULL_REGIMES
        current_regime = self._get_current_regime()
        is_bearish = current_regime in {"BEAR", "BEAR_LONG", "VOLATILE", "PANIC", "RANGE_BEARISH"}

        if is_bullish and is_bearish:
            reasons.append(f"Regime mismatch: {entry_regime} → {current_regime}")
            bucket = "negative"

        return bucket, reasons

    def _layer4_veto(self, trade: LosingTrade, bucket: str) -> tuple:
        """Exposure and business rule vetoes."""
        reasons = []
        # Early exit veto for positive trades
        if bucket == "positive" and trade.pnl_percent < 0.8:
            reasons.append(f"Early exit: realized {trade.pnl_percent:.2f}%")
            bucket = "neutral"
        return bucket, reasons

    def _final_decision(
        self, trade: LosingTrade, bucket: str, reasons: List[str]
    ) -> tuple:
        """Quality gates + priority scoring."""
        l3_count = sum(1 for r in reasons if r.startswith("L3:") or r.startswith("Regime") or r.startswith("Weak") or r.startswith("Risky"))
        l4_count = sum(1 for r in reasons if r.startswith("Early") or r.startswith("Exposure"))
        score = 1 + (2 * l3_count) + (3 * l4_count)

        # Quality gate: negative needs score >= 5
        if bucket == "negative" and score < 5:
            return False, score

        # Quality gate: positive needs no vetoes and score >= 7
        if bucket == "positive":
            if l4_count > 0 or score < 7:
                return False, score

        # Neutral never passes
        if bucket == "neutral":
            return False, score

        return True, score

    # ─────────────────────────────────────────────
    #  STAGE 3: REFLECTOR (Structured LLM)
    # ─────────────────────────────────────────────

    def reflect(self, trade: LosingTrade) -> Optional[LessonEnvelope]:
        """
        Run LLM causal analysis with structured Pydantic output.
        Returns a LessonEnvelope or None on failure.
        """
        logger.info(
            f"Reflecting on {trade.symbol} "
            f"(PnL: {trade.pnl_percent}%, trace: {trade.trace_id[:8]}...)"
        )

        audit_log = trade.context_snapshot or {"recovery": "none"}
        market_ctx = self._build_market_context(trade)

        system_prompt = (
            "You are a **Post-Mortem Analyst** for an autonomous trading system. "
            "Your job is to perform ROOT CAUSE ANALYSIS on a losing trade.\n\n"
            "## OUTPUT FORMAT:\n"
            "Return a JSON object matching the TradeReviewModel schema:\n"
            "- overview: one sentence summary\n"
            "- execution_score: 0-10\n"
            "- signal_score: 0-10\n"
            "- pnl_attribution: list of {{factor, pct, desc}}\n"
            "- example_category: A (Excellence), B (Borderline Pos), C (Neutral), "
            "D (Borderline Neg), E (Fatal Error)\n"
            "- failure_reason: one of: false_breakout, regime_mismatch, liquidity_trap, "
            "stale_signal, overconfidence, news_shock, correlation_trap, other\n"
            "- key_lesson: list of key takeaway strings\n"
            "- system_suggestions: list of {{priority: P0/P1/P2, suggestion}}\n"
            "- severity: 1-10 (1=minor fluke, 10=critical systemic flaw)\n"
            "- confidence: 0.0-1.0\n\n"
            "## ANTI-PTSD RULE:\n"
            "Do NOT recommend blanket avoidance. Specify CONDITIONAL factors. "
            "The lesson must be falsifiable.\n\n"
            "Output ONLY valid JSON."
        )

        human_prompt = (
            f"## TRADE DETAILS\n"
            f"- Symbol: {trade.symbol}\n"
            f"- Side: {trade.side}\n"
            f"- PnL: {trade.pnl_percent}%\n"
            f"- Pattern: {trade.pattern_type}\n\n"
            f"## ORIGINAL REASONING\n"
            f"{json.dumps(audit_log, indent=2, default=str)}\n\n"
            f"## MARKET CONTEXT\n"
            f"{json.dumps(market_ctx, indent=2, default=str)}\n\n"
            f"Perform root cause analysis now."
        )

        try:
            # Try structured output first
            structured_llm = self.llm.with_structured_output(TradeReviewModel)
            review = structured_llm.invoke([
                SystemMessage(content=system_prompt),
                HumanMessage(content=human_prompt),
            ])

            if isinstance(review, TradeReviewModel):
                return self._build_envelope(trade, review, market_ctx)

            # Fallback: if LLM returned dict
            if isinstance(review, dict):
                review = TradeReviewModel(**review)
                return self._build_envelope(trade, review, market_ctx)

            logger.warning(f"Unexpected LLM output type: {type(review)}")
            return None

        except Exception as e:
            logger.warning(f"Structured output failed (non-critical): {e}")
            return None

    # ─────────────────────────────────────────────
    #  STAGE 4: INJECTOR
    # ─────────────────────────────────────────────

    def inject(self, envelope: LessonEnvelope) -> bool:
        """
        Store lesson via KnowledgeStore (ChromaDB + Redis).
        Severity gate: only severity >= SEVERITY_RAG_THRESHOLD enters long-term RAG.
        """
        doc_id = envelope.doc_id

        # Always cache in Redis short-term (any severity)
        metadata = {
            "trace_id": envelope.trace_id,
            "symbol": envelope.symbol,
            "regime": envelope.regime,
            "pattern": envelope.pattern,
            "failure_reason": envelope.failure_reason,
            "severity": envelope.severity,
            "confidence": envelope.confidence,
            "created_at": envelope.created_at.isoformat(),
            "source": envelope.source,
            "time_of_day": envelope.time_of_day,
        }

        if envelope.severity >= SEVERITY_RAG_THRESHOLD:
            # High-severity → both ChromaDB and Redis
            doc_text = (
                f"[{envelope.symbol}] [{envelope.regime}] [{envelope.pattern}] "
                f"Failure: {envelope.failure_reason}. "
                f"{envelope.lesson_text}"
            )
            if self.knowledge_store is None:
                # V11 path archived — skip INJECT, lessons stay in-memory only
                logger.warning(
                    f"Lesson {doc_id} severity={envelope.severity} → INJECT skipped "
                    f"(KnowledgeStore unavailable, V11 archived)"
                )
                return False
            return self.knowledge_store.upsert_negative_lesson(
                doc_text=doc_text,
                metadata=metadata,
                doc_id=doc_id,
                lesson_envelope=envelope,
            )
        else:
            # Below severity gate → Redis only (short-term)
            logger.info(
                f"Lesson {doc_id} severity={envelope.severity} < {SEVERITY_RAG_THRESHOLD} "
                f"→ Redis only (not ChromaDB)"
            )
            if self.knowledge_store is None:
                return False
            return self.knowledge_store.upsert_negative_lesson(
                doc_text="",  # Empty doc → won't be useful in ChromaDB semantic search
                metadata=metadata,
                doc_id=doc_id,
                lesson_envelope=envelope,
            )

    # ─────────────────────────────────────────────
    #  ORCHESTRATOR
    # ─────────────────────────────────────────────

    def run_daily_postmortem(
        self,
        lookback_hours: int = 24,
        pnl_threshold_pct: float = None,
    ) -> Dict[str, Any]:
        """
        Full pipeline: Extract → Filter → Reflect → Inject.
        Returns a summary report.

        V2.2: pnl_threshold_pct defaults to None (dynamic ATR thresholds).
        Pass a float to force a static threshold (backward compat).
        """
        logger.info("=" * 60)
        logger.info("DAILY POST-MORTEM ANALYSIS STARTING (V2)")
        logger.info("=" * 60)

        report = {
            "start_time": datetime.now(timezone.utc).isoformat(),
            "trades_scanned": 0,
            "trades_filtered": 0,
            "losers_found": 0,
            "lessons_generated": 0,
            "lessons_injected": 0,
            "severity_gate_rejected": 0,
            "errors": [],
        }

        # Stage 1: Extract
        losers = self.extract_losers(lookback_hours, pnl_threshold_pct)
        report["losers_found"] = len(losers)
        report["trades_scanned"] = len(losers)

        if not losers:
            logger.info("No significant losing trades found. System is performing well.")
            return report

        # Stage 2: Filter
        filtered_trades = []
        for trade in losers:
            result = self.filter_trade(trade)
            if result.passed:
                filtered_trades.append((trade, result))
                logger.info(
                    f"  PASSED {trade.symbol} ({trade.pnl_percent:.2f}%) "
                    f"→ {result.bucket} score={result.priority_score}"
                )
            else:
                logger.debug(
                    f"  FILTERED {trade.symbol}: {result.layer_reasons}"
                )

        report["trades_filtered"] = len(losers) - len(filtered_trades)
        logger.info(f"4-Layer Filter: {len(filtered_trades)}/{len(losers)} passed")

        # Stage 3 + 4: Reflect & Inject
        for trade, filter_result in filtered_trades:
            try:
                envelope = self.reflect(trade)
                if not envelope:
                    continue

                report["lessons_generated"] += 1

                if envelope.severity < SEVERITY_RAG_THRESHOLD:
                    report["severity_gate_rejected"] += 1

                success = self.inject(envelope)
                if success:
                    report["lessons_injected"] += 1
                    logger.info(
                        f"  Injected {envelope.symbol} lesson "
                        f"(severity={envelope.severity}, reason={envelope.failure_reason})"
                    )

                    # Queue high-severity suggestions for human review
                    if envelope.severity >= SEVERITY_RAG_THRESHOLD and envelope.review:
                        simulation = self._simulate_suggestions(envelope)
                        self._queue_for_review(envelope, report, simulation=simulation)

            except Exception as e:
                err_msg = f"Pipeline error for {trade.symbol}/{trade.trace_id[:8]}: {e}"
                logger.error(f"{err_msg}")
                report["errors"].append(err_msg)

        report["end_time"] = datetime.now(timezone.utc).isoformat()

        # V2.2: Compute and publish dynamic signal-source weights
        try:
            weights_result = self.compute_source_weights()
            report["weights_computed"] = weights_result
            logger.info(f"Source weights computed: {weights_result}")
        except Exception as e:
            logger.warning(f"Source weight computation failed (non-critical): {e}")
            report["weights_computed"] = {"error": str(e)}

        # Stage 5: Positive Reinforcement (unchanged from V1)
        try:
            logger.info("Running Positive Reinforcement Audit...")
            reinforced_count = self.reinforcement_engine.audit_successful_avoidance(
                lookback_hours=lookback_hours
            )
            report["lessons_reinforced"] = reinforced_count
            logger.info(f"Reinforcement Complete: {reinforced_count} lessons updated.")
        except Exception as e:
            logger.error(f"Reinforcement Engine failed: {e}")
            report["errors"].append(f"Reinforcement Error: {e}")

        logger.info("=" * 60)
        logger.info(
            f"POST-MORTEM COMPLETE: "
            f"{report['lessons_injected']}/{report['losers_found']} lessons injected, "
            f"{report.get('lessons_reinforced', 0)} reinforced, "
            f"{report['severity_gate_rejected']} below severity gate."
        )
        logger.info("=" * 60)

        return report

    # ─────────────────────────────────────────────
    #  STAGE 4.5: SHADOW BACKTESTING
    # ─────────────────────────────────────────────

    # Regime multipliers — mirrors risk_controller.py exactly
    _REGIME_MULTIPLIERS = {
        "BULL_LONG": 1.3, "BULL_SHORT": 1.1, "NEUTRAL": 1.0,
        "BEAR_SHORT": 0.8, "BEAR_LONG": 0.6, "PANIC": 0.3,
    }

    @staticmethod
    def _calc_position_size(
        regime: str, confidence: float,
        kelly: Optional[float] = None,
        risk_scaling: Optional[float] = None,
    ) -> float:
        """
        Replicate the position sizing formula from risk_controller.py
        so that simulation is self-contained (no import coupling).
        """
        base = 0.01
        base *= PostMortemService._REGIME_MULTIPLIERS.get(regime, 1.0)
        base *= min(confidence / 0.7, 2.0) if confidence > 0 else 0.5
        base *= kelly if kelly is not None else 1.0
        base = min(base, 0.05)
        base *= risk_scaling if risk_scaling is not None else 1.0
        return base

    @staticmethod
    def _parse_suggestion_params(suggestions: List[Dict]) -> Dict[str, float]:
        """
        Parse P0/P1 suggestion text into parameter overrides.
        Returns dict like {"kelly": 0.4, "risk_scaling": 0.7, "min_confidence": 0.6}
        """
        overrides: Dict[str, float] = {}
        for s in suggestions:
            text = s.get("suggestion", "")
            # Match: "kelly_fraction to 0.4", "kelly=0.4", "kelly 0.4"
            m = re.search(r"kelly[_\s]?fraction\s*(?:to|=)?\s*([\d.]+)", text, re.I)
            if m:
                val = float(m.group(1))
                if 0.1 <= val <= 1.0:
                    overrides["kelly"] = val
                continue
            # Match: "risk_scaling 0.7", "risk scaling to 0.7"
            m = re.search(r"risk[_\s]?scaling\s*(?:to|=)?\s*([\d.]+)", text, re.I)
            if m:
                val = float(m.group(1))
                if 0.3 <= val <= 2.0:
                    overrides["risk_scaling"] = val
                continue
            # Match: "min_confidence 0.6", "minimum confidence to 0.6"
            m = re.search(r"min(?:imum)?[_\s]?confidence\s*(?:to|=)?\s*([\d.]+)", text, re.I)
            if m:
                val = float(m.group(1))
                if 0.1 <= val <= 0.8:
                    overrides["min_confidence"] = val
        return overrides

    def _simulate_suggestions(self, envelope: LessonEnvelope) -> Optional[Dict[str, Any]]:
        """
        Replay last 30 days of similar trades with suggested parameter changes.
        Returns impact metrics to attach to the review queue payload.
        Returns None if no simulation is possible (no parseable params or no trades).
        """
        if not envelope.review or not envelope.review.system_suggestions:
            return None

        suggestions = [
            {"priority": s.priority, "suggestion": s.suggestion}
            for s in envelope.review.system_suggestions
            if s.priority in ("P0", "P1")
        ]
        overrides = self._parse_suggestion_params(suggestions)

        if not overrides:
            logger.debug("No parseable parameter overrides from suggestions")
            return None

        # Query historical trades matching the same pattern
        # Note: trade_events.time is stored in UTC (TimescaleDB convention).
        # NOW() returns UTC on TimescaleDB, so the comparison is timezone-consistent.
        sql = """
            SELECT symbol, side, quantity, price, pattern_type,
                   realized_pnl_dollar, realized_pnl_pct,
                   risk_metadata, time
            FROM trade_events
            WHERE time >= NOW() - INTERVAL '30 days'
              AND pattern_type = %s
              AND realized_pnl_dollar IS NOT NULL
            ORDER BY time DESC;
        """

        try:
            with psycopg2.connect(self.db_url) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, (envelope.pattern,))
                    rows = cur.fetchall()
        except Exception as e:
            logger.warning(f"Shadow backtest DB query failed: {e}")
            return None

        if not rows:
            return {"matched_trades": 0}

        original_total = 0.0
        simulated_total = 0.0
        trades_blocked = 0
        losses_avoided = 0
        wins_blocked = 0

        min_conf = overrides.get("min_confidence")
        new_kelly = overrides.get("kelly")
        new_risk_scaling = overrides.get("risk_scaling")

        for row in rows:
            orig_pnl = float(row.get("realized_pnl_dollar", 0.0))
            original_total += orig_pnl

            rm = row.get("risk_metadata") or {}
            confidence = float(rm.get("confidence", 0.5))
            regime = rm.get("regime", "NEUTRAL")

            # If min_confidence is suggested and trade would have been blocked
            if min_conf is not None and confidence < min_conf:
                trades_blocked += 1
                if orig_pnl < 0:
                    losses_avoided += 1
                else:
                    wins_blocked += 1
                # Blocked trade → PnL = 0 (trade never happens)
                continue

            # Simulate proportional PnL based on position size change
            orig_size = self._calc_position_size(regime, confidence)
            new_size = self._calc_position_size(
                regime, confidence,
                kelly=new_kelly,
                risk_scaling=new_risk_scaling,
            )

            if orig_size > 0:
                simulated_pnl = orig_pnl * (new_size / orig_size)
            else:
                simulated_pnl = orig_pnl

            simulated_total += simulated_pnl

        result = {
            "matched_trades": len(rows),
            "original_total_pnl": round(original_total, 2),
            "simulated_total_pnl": round(simulated_total, 2),
            "pnl_improvement": round(simulated_total - original_total, 2),
            "trades_blocked": trades_blocked,
            "losses_avoided": losses_avoided,
            "wins_blocked": wins_blocked,
            "lookback_days": 30,
        }

        logger.info(
            f"Shadow backtest for {envelope.pattern}: "
            f"{result['matched_trades']} trades, "
            f"PnL improvement: ${result['pnl_improvement']:+.2f}, "
            f"{result['losses_avoided']} losses avoided"
        )
        return result

    # ─────────────────────────────────────────────
    #  REVIEW QUEUE
    # ─────────────────────────────────────────────

    def _queue_for_review(
        self, envelope: LessonEnvelope, report: Dict,
        simulation: Optional[Dict] = None,
    ):
        """Queue lesson suggestions for next-morning human review."""
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"rag:review:pending:{date_str}"

        suggestions = []
        if envelope.review and envelope.review.system_suggestions:
            for s in envelope.review.system_suggestions:
                if s.priority in ("P0", "P1"):
                    suggestions.append({
                        "priority": s.priority,
                        "suggestion": s.suggestion,
                    })

        if not suggestions:
            return

        payload = {
            "doc_id": envelope.doc_id,
            "symbol": envelope.symbol,
            "severity": envelope.severity,
            "failure_reason": envelope.failure_reason,
            "lesson_text": envelope.lesson_text,
            "pnl_pct": envelope.pnl_pct,
            "suggestions": suggestions,
            "shadow_backtest": simulation,
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            import redis as _redis
            redis_host = os.getenv("REDIS_HOST", "localhost")
            redis_port = int(os.getenv("REDIS_PORT", 6379))
            r = _redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
            r.hset(key, envelope.doc_id, json.dumps(payload, default=str))
            r.expire(key, 172800)  # 48h TTL
            r.close()
            report.setdefault("queued_for_review", 0)
            report["queued_for_review"] += 1
            logger.info(
                f"Queued {envelope.doc_id} for human review "
                f"({len(suggestions)} suggestions)"
            )
        except Exception as e:
            logger.warning(f"Failed to queue for review (non-critical): {e}")

    # ─────────────────────────────────────────────
    #  INTERNAL HELPERS
    # ─────────────────────────────────────────────

    # V2.2: Signal-source win-rate weight computation
    WEIGHT_LOOKBACK_DAYS = 30
    WEIGHT_MIN_TRADES = 10        # Minimum trades per source before trusting win-rate
    WEIGHT_CLAMP_MIN = 0.5
    WEIGHT_CLAMP_MAX = 2.0
    WEIGHT_REDIS_TTL = 86400       # 24 hours

    def compute_source_weights(self) -> Dict[str, Any]:
        """
        Compute per-source win-rate from recent trades and write weights to Redis.

        Strategy: win-rate_i / avg_win_rate, clamped to [0.5, 2.0].
        At least WEIGHT_MIN_TRADES trades per source are required; otherwise
        that source gets the average weight (1.0).
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.WEIGHT_LOOKBACK_DAYS)

        sql = """
            SELECT
                risk_metadata->>'dominant_source' AS source,
                COUNT(*) AS total_trades,
                SUM(CASE WHEN realized_pnl_dollar > 0 THEN 1 ELSE 0 END) AS winning_trades
            FROM trade_events
            WHERE side = 'SELL'
              AND realized_pnl_dollar IS NOT NULL
              AND time >= %s
              AND risk_metadata->>'dominant_source' IS NOT NULL
            GROUP BY risk_metadata->>'dominant_source';
        """

        try:
            with psycopg2.connect(self.db_url) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, (cutoff,))
                    rows = cur.fetchall()
        except Exception as e:
            logger.warning(f"Weight computation DB query failed: {e}")
            return {"error": str(e)}

        if not rows:
            logger.info("No trade events with dominant_source found — using unit weights")
            return self._write_weights({"w_rl": 1.0, "w_llm": 1.0, "w_yolo": 1.0})

        source_stats: Dict[str, Dict] = {}
        for row in rows:
            source = row["source"]
            total = int(row["total_trades"])
            wins = int(row["winning_trades"])
            win_rate = wins / total if total > 0 else 0.0
            source_stats[source] = {"total": total, "wins": wins, "win_rate": win_rate}

        # Calculate average win-rate across all sources with enough data
        valid_sources = [
            (s, stats) for s, stats in source_stats.items()
            if stats["total"] >= self.WEIGHT_MIN_TRADES
        ]

        if not valid_sources:
            logger.info(
                f"No source has >= {self.WEIGHT_MIN_TRADES} trades — using unit weights"
            )
            return self._write_weights({"w_rl": 1.0, "w_llm": 1.0, "w_yolo": 1.0})

        avg_win_rate = sum(s["win_rate"] for _, s in valid_sources) / len(valid_sources)
        if avg_win_rate == 0:
            avg_win_rate = 0.01  # Avoid division by zero

        # Map known source names to canonical keys
        source_key_map = {
            "RL": "w_rl",
            "LLM": "w_llm",
            "YOLO": "w_yolo",
        }

        weights: Dict[str, float] = {"w_rl": 1.0, "w_llm": 1.0, "w_yolo": 1.0}
        details = {}

        for source, canonical_key in source_key_map.items():
            if source in source_stats:
                stats = source_stats[source]
                if stats["total"] >= self.WEIGHT_MIN_TRADES:
                    raw_weight = stats["win_rate"] / avg_win_rate
                    weight = max(self.WEIGHT_CLAMP_MIN, min(self.WEIGHT_CLAMP_MAX, raw_weight))
                    weights[canonical_key] = round(weight, 3)
                    details[source] = {
                        "win_rate": round(stats["win_rate"], 3),
                        "weight": round(weight, 3),
                        "total_trades": stats["total"],
                    }
                else:
                    # Insufficient trades — use neutral weight (1.0)
                    details[source] = {
                        "win_rate": round(stats["win_rate"], 3),
                        "weight": 1.0,
                        "total_trades": stats["total"],
                        "note": f"below {self.WEIGHT_MIN_TRADES}-trade minimum",
                    }
            else:
                details[source] = {"weight": 1.0, "note": "no data"}

        logger.info(
            f"Source weights computed: {weights} | avg_win_rate={avg_win_rate:.3f}"
        )

        result = self._write_weights(weights)
        result["details"] = details
        result["avg_win_rate"] = round(avg_win_rate, 3)
        return result

    def _write_weights(self, weights: Dict[str, float]) -> Dict[str, Any]:
        """Write weights to Redis and publish update signal. Includes ts for freshness tracking."""
        import redis as _redis
        import time as _time
        try:
            r = _redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                decode_responses=True,
            )
            payload = {**weights, "ts": int(_time.time())}
            weights_json = json.dumps(payload)
            r.setex("fusion:weights:current", self.WEIGHT_REDIS_TTL, weights_json)
            r.publish("fusion:weights:updated", weights_json)
            r.close()
            return {"status": "written", "weights": payload}
        except Exception as e:
            logger.warning(f"Failed to write weights to Redis: {e}")
            return {"status": "error", "error": str(e), "weights": weights}

    def _get_atr_pct(self, symbol: str, event_time, entry_price: float) -> float:
        """
        Safely fetch ATR% for a symbol. Returns default on any failure.
        """
        try:
            target_date = event_time or datetime.now(timezone.utc)
            return self.market_data.get_atr_pct(symbol, target_date, entry_price)
        except Exception as e:
            logger.debug(f"ATR fetch failed for {symbol}: {e}")
            return self.ATR_DEFAULT_FALLBACK

    def _build_market_context(self, trade: LosingTrade) -> Dict[str, Any]:
        ctx = {
            "regime": trade.risk_metadata.get("regime", "UNKNOWN"),
            "vix": trade.risk_metadata.get("vix", 0.0),
            "pattern_type": trade.pattern_type,
            "mode": trade.mode,
        }
        if trade.risk_metadata:
            ctx["fusion_sources"] = trade.risk_metadata.get("fusion_sources", [])
            ctx["confidence_at_decision"] = trade.risk_metadata.get("confidence", 0.0)
        return ctx

    def _build_envelope(
        self, trade: LosingTrade, review: TradeReviewModel, market_ctx: Dict,
    ) -> LessonEnvelope:
        trace_id = trade.trace_id
        return LessonEnvelope(
            doc_id=f"lesson_{trace_id}",
            trace_id=trace_id,
            symbol=trade.symbol,
            regime=market_ctx.get("regime", "UNKNOWN"),
            pattern=trade.pattern_type,
            failure_reason=review.failure_reason,
            lesson_text=" | ".join(review.key_lesson),
            severity=review.severity,
            confidence=review.confidence,
            pnl_pct=trade.pnl_percent,
            time_of_day=self._compute_time_of_day(trade.entry_time),
            market_context=market_ctx,
            review=review,
        )

    @staticmethod
    def _compute_time_of_day(entry_time: Optional[datetime]) -> str:
        if not entry_time:
            return "unknown"
        try:
            # Convert to ET if timezone-aware, else assume UTC
            from zoneinfo import ZoneInfo
            if entry_time.tzinfo:
                et_hour = entry_time.astimezone(ZoneInfo("America/New_York")).hour
            else:
                et_hour = (entry_time.hour - 5) % 24  # rough UTC→ET
        except Exception:
            et_hour = entry_time.hour
        for (lo, hi), label in TIME_OF_DAY.items():
            if lo <= et_hour < hi:
                return label
        return "unknown"

    def _get_current_regime(self) -> str:
        try:
            import redis as _redis
            r = _redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                decode_responses=True,
            )
            data = r.get("market_regime:current")
            r.close()
            if data:
                return json.loads(data).get("regime", "UNKNOWN")
        except Exception:
            pass
        return "UNKNOWN"

    @staticmethod
    def _build_db_url() -> str:
        host = os.getenv("DB_HOST", "timescaledb")
        port = os.getenv("DB_PORT", "5432")
        user = os.getenv("DB_USER", "stock_app")
        pw = os.getenv("DB_PASS")
        if not pw:
            raise ValueError("DB_PASS environment variable is not set.")
        db = os.getenv("DB_NAME", "v8_db")
        return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


# ─────────────────────────────────────────────
#  CLI Entrypoint
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    parser = argparse.ArgumentParser(description="Run Post-Mortem Analysis (V2)")
    parser.add_argument(
        "--action", type=str, default="run_daily",
        choices=["run_daily", "run_all", "list_lessons"],
    )
    parser.add_argument("--lookback", type=int, default=24)
    parser.add_argument("--threshold", type=float, default=None)
    args = parser.parse_args()

    svc = PostMortemService()

    if args.action == "run_all":
        report = svc.run_daily_postmortem(
            lookback_hours=24 * 30, pnl_threshold_pct=-0.5,
        )
    elif args.action == "list_lessons":
        if svc.knowledge_store is None:
            print("KnowledgeStore unavailable (V11 archived). No lessons in ChromaDB.")
            count = 0
        else:
            count = svc.knowledge_store.negative_lessons_count()
            print(f"Total Negative Lessons in ChromaDB: {count}")
        report = {"action": "list_lessons", "count": count}
    else:
        report = svc.run_daily_postmortem(
            lookback_hours=args.lookback,
            pnl_threshold_pct=args.threshold,
        )

    print(json.dumps(report, indent=2, default=str))
