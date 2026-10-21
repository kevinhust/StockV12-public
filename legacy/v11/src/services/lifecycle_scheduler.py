"""
Lifecycle Scheduler -- Container-Internal Cron Jobs

Replaces MacBook launchd -> SSH -> NUC docker exec pattern.
Uses APScheduler AsyncIOScheduler with cron triggers.
All times EST (container TZ=America/New_York).
"""

import os
import sys
import json
import asyncio
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any, Dict, Optional, Tuple

from loguru import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Ensure both project root and src/ are on path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(PROJECT_ROOT))  # project root
sys.path.insert(0, PROJECT_ROOT)  # src/
v12_src = os.path.join(os.path.dirname(PROJECT_ROOT), "v12", "src")
if v12_src not in sys.path:
    sys.path.insert(0, v12_src)  # for v12 components

_scheduler: AsyncIOScheduler | None = None

# ── Redis connection pool (shared across all scheduled tasks) ──────────────────

_redis_pool: "redis.asyncio.ConnectionPool | None" = None


async def _get_redis_pool() -> "redis.asyncio.ConnectionPool":
    """Lazily initialize the async Redis connection pool."""
    global _redis_pool
    if _redis_pool is None:
        import redis.asyncio as redis_asyncio
        _redis_pool = redis_asyncio.ConnectionPool(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            decode_responses=True,
            max_connections=10,
        )
    return _redis_pool


async def _record_success(task_name: str):
    try:
        pool = await _get_redis_pool()
        import redis.asyncio as redis_asyncio
        r = redis_asyncio.Redis(connection_pool=pool)
        await r.set(f"lifecycle:last_run:{task_name}", datetime.now().isoformat())
        await r.incr(f"lifecycle:stats:total:{task_name}")
        await r.aclose()
    except Exception as e:
        logger.warning(f"Failed to record success for {task_name}: {e}")


async def _record_failure(task_name: str, error: str):
    logger.error(f"Lifecycle task '{task_name}' failed:\n{error}")
    try:
        import redis.asyncio as redis_asyncio
        pool = await _get_redis_pool()
        r = redis_asyncio.Redis(connection_pool=pool)
        await r.incr(f"lifecycle:stats:failure:{task_name}")
        await r.set(f"lifecycle:last_error:{task_name}", json.dumps({
            "error": str(error)[:500],
            "timestamp": datetime.now().isoformat(),
        }))
        await r.aclose()
    except Exception as e:
        logger.warning(f"Failed to record failure for {task_name} in Redis: {e}")


# ─────────────────────────────────────────────
#  Task Implementations
# ─────────────────────────────────────────────

async def _run_scanner():
    """Run the stock scanner (L1 — premarket / midday)."""
    from services.selection.run_scanner import build_universe, run_single_scan
    from services.selection.intraday_scanner import IntradayScanner
    import redis as redis_lib

    try:
        r = redis_lib.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            decode_responses=True,
        )
        r.ping()
        universe_set = build_universe(redis_client=r, min_size=50)
        universe = sorted(universe_set)
        r.close()
    except Exception as e:
        logger.warning(f"build_universe failed: {e}, falling back")
        from services.selection.run_scanner import get_default_universe
        universe = get_default_universe()

    scanner = IntradayScanner()
    await scanner.initialize()
    await run_single_scan(scanner, universe)


async def _run_macro_light():
    """
    03:55 AM lightweight macro baseline — VIX waterfall + SPY trend only.
    Produces a regime classification in under 30s so the 04:00 AM scanner
    has fresh regime data without waiting for full news/LLM analysis.
    """
    import redis as redis_lib
    from services.macro_anchor import MacroAnchorService

    def _do():
        r = redis_lib.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            decode_responses=True,
        )
        try:
            anchor = MacroAnchorService(redis_client=r)
            result = anchor.compute_regime_lite()
            # Dual-write regime to Redis so scanner picks it up
            from services.macro_news_analyst import MacroNewsAnalyst
            analyst = MacroNewsAnalyst(redis_client=r)
            analyst.update_from_research(result)
            logger.info(f"macro_light: regime={result.get('market_regime', 'N/A')}")
        finally:
            r.close()

    await asyncio.to_thread(_do)


async def _run_macro_research():
    """Force-refresh macro news analysis (heavy — LLM + economic calendar)."""
    import redis as redis_lib
    from services.macro_news_analyst import MacroNewsAnalyst

    def _do():
        r = redis_lib.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            decode_responses=True,
        )
        analyst = MacroNewsAnalyst(redis_client=r)
        analyst.get_macro_news(force_refresh=True)
        r.close()

    await asyncio.to_thread(_do)


async def _run_self_check():
    """Run full-chain self-check."""
    from scripts.full_chain_self_check import FullChainSelfChecker

    def _do():
        checker = FullChainSelfChecker()
        checker.run()

    await asyncio.to_thread(_do)


async def _run_distillation():
    """Run causal distillation."""
    from services.semantic_distiller import run_distillation
    await asyncio.to_thread(run_distillation)


async def _run_eod_closing():
    """Run EOD closing pipeline."""
    from scripts.eod_closing import main as eod_main
    await eod_main()


async def _run_liquidation():
    """Run EOD liquidation."""
    from services.liquidation_service import LiquidationService

    def _do():
        service = LiquidationService()
        service.liquidate_all()

    await asyncio.to_thread(_do)


async def _run_postmortem():
    """Run daily post-mortem."""
    from services.post_mortem_service import PostMortemService

    def _do():
        service = PostMortemService()
        service.run_daily_postmortem()

    await asyncio.to_thread(_do)


async def _run_rag_integrity():
    """Run RAG integrity check."""
    from scripts.maintenance.rag_integrity_check import check_rag_integrity
    await asyncio.to_thread(check_rag_integrity)


async def _run_v12_soak_daily():
    """V12 daily combined soak audit (paper_soak_checklist + deployment_health).

    Runs at 17:00 EST weekdays — after market close, after daily_review, before post_mortem.
    Writes logs/v12_soak_<date>.json; Telegram alert only on anomalies (1/hr rate-limit).
    """
    import importlib.util
    from pathlib import Path as _Path

    def _do():
        # v12/scripts has no __init__.py — load the module by absolute path.
        # PROJECT_ROOT in this scheduler is <repo>/src, so go up one level to
        # reach the repo root before joining v12/scripts.
        soak_script = _Path(PROJECT_ROOT).parent / "v12" / "scripts" / "v12_soak_daily.py"
        if not soak_script.exists():
            raise FileNotFoundError(f"v12_soak_daily.py not found at {soak_script}")
        spec = importlib.util.spec_from_file_location("v12_soak_daily", soak_script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Pass --no-telegram so the scheduler-driven run doesn't spam Telegram when
        # the wrapper is also re-invoked by humans during the same hour.
        # Manual invocations still get alerts by default.
        import sys as _sys
        argv_backup = _sys.argv
        try:
            _sys.argv = ["v12_soak_daily.py", "--no-telegram"]
            rc = mod.main()
            logger.info(f"[V12] soak_daily completed rc={rc}")
        finally:
            _sys.argv = argv_backup

    await asyncio.to_thread(_do)


async def _run_morning_risk_load():
    """Load approved post-mortem overrides into HardRiskGatekeeper."""
    import redis as redis_lib

    def _do():
        from core.risk_controller import HardRiskGatekeeper
        # Constructor calls _load_approved_overrides() internally,
        # which writes overrides to Redis keys.
        HardRiskGatekeeper()

        # Publish reload signal for any running gatekeeper instances
        date_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        r = redis_lib.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            decode_responses=True,
        )
        r.publish("risk:overrides:reload", json.dumps({"date": date_str}))
        r.close()
        logger.info("Morning risk override loading complete")

    await asyncio.to_thread(_do)


async def _run_telegram_heartbeat():
    """Run Telegram health heartbeat check."""
    from scripts.maintenance.telegram_heartbeat import main as hb_main
    await asyncio.to_thread(hb_main)


async def _run_catalyst_scorer():
    """
    L2: Catalyst scoring — reads tier1, scores via LLM + Polygon news + hash cache.
    Writes to watchlist:tier2. Expels symbols with no catalyst.
    """
    from services.selection.catalyst_scorer import CatalystScorer

    def _do():
        scorer = CatalystScorer()
        return scorer.run()

    count = await asyncio.to_thread(_do)
    if count is not None:
        await _record_success("catalyst_scorer")


async def _run_catalyst_scorer_pm():
    """L2: Afternoon catalyst scoring — incremental re-score after midday scan."""
    await _run_catalyst_scorer()


async def _run_carryover_builder():
    """
    Independent EOD carryover — populates carryover:7d from tier2 + tier3.
    Runs at 16:05 (after eod_closing at 15:45) to ensure robustness.
    """
    import redis.asyncio as aioredis

    pool = await _get_redis_pool()
    r = aioredis.Redis(connection_pool=pool)

    try:
        tier2_raw = await r.zrevrange("watchlist:tier2", 0, 14, withscores=True)
        tier3_raw = await r.zrevrange("watchlist:tier3", 0, 7, withscores=True)

        tier2_top = [s for s, _ in tier2_raw]
        tier3_top = [s for s, _ in tier3_raw]
        combined = set(tier2_top) | set(tier3_top)

        if not combined:
            logger.warning("carryover_builder: tier2 + tier3 empty, skipping")
            await _record_failure("carryover_builder", "tier2 + tier3 empty")
            await r.aclose()
            return

        await r.sadd("carryover:7d", *combined)

        # Unified notification for stock pool (main system)
        from src.services.notification_service import notifier
        notifier.notify_stock_pool_updated(
            tier="tier2+3_carryover",
            symbols=list(combined)[:20],  # full list cap for message
            source="carryover_builder",
        )
        await r.expire("carryover:7d", 604800)   # 7 days
        logger.info(f"carryover_builder: {len(combined)} symbols → carryover:7d")
        await _record_success("carryover_builder")

    except Exception as e:
        logger.error(f"carryover_builder failed: {e}")
        await _record_failure("carryover_builder", str(e))
    finally:
        await r.aclose()


async def _run_tier3_forced_inspection():
    """
    L3: 10:30 AM forced patrol — prevents top tier2 symbols from
    being ignored just because no YOLO pattern formed yet.
    Reads tier2 top 8, forces an event into the ShadowRunner pipeline.
    """
    import redis.asyncio as aioredis
    from kafka import KafkaProducer

    pool = await _get_redis_pool()
    r = aioredis.Redis(connection_pool=pool)

    try:
        top_symbols = await r.zrevrange("watchlist:tier2", 0, 7)
        if not top_symbols:
            logger.warning("tier3_inspection: tier2 empty, skipping")
            await r.aclose()
            return

        kafka_bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
        producer = KafkaProducer(
            bootstrap_servers=kafka_bootstrap,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
            acks="all",
        )

        for symbol in top_symbols:
            # --- Price Resolution (3-tier fallback) ---
            price_raw = await r.get(f"price:{symbol}:current")
            price = 0
            price_source = "none"

            if price_raw:
                price_data = json.loads(price_raw)
                price = price_data.get("last", 0)
                price_source = "live"
            else:
                # Fallback: use L2 scanner price (cached by intraday_scanner)
                l2_detail_raw = await r.hget("watchlist:tier2:details", symbol)
                l2_detail = json.loads(l2_detail_raw) if l2_detail_raw else {}
                if isinstance(l2_detail, dict) and l2_detail.get("price"):
                    price = l2_detail.get("price", 0)
                    price_source = "tier2_scan"
                else:
                    logger.warning(f"tier3_inspection: {symbol} has no price (live or scan), skipping")
                    continue

            # Fetch L2 detail context for the event payload
            if price_source != "tier2_scan":
                l2_detail_raw = await r.hget("watchlist:tier2:details", symbol)
                l2_detail = json.loads(l2_detail_raw) if l2_detail_raw else {}

            event = {
                "symbol": symbol,
                "type": "forced_patrol",
                "price": price,
                "timestamp": datetime.now().isoformat(),
                "source": "tier3_forced_patrol",
                "l2_context": {
                    "catalyst_score": l2_detail.get("catalyst_score", 0),
                    "catalyst_type": l2_detail.get("catalyst_type", "none"),
                    "has_catalyst": l2_detail.get("has_catalyst", False),
                    "llm_summary": l2_detail.get("llm_summary", ""),
                },
            }
            producer.send("events.pattern", event)
            logger.info(f"tier3_inspection: forced → {symbol} @ {price} (source: {price_source})")

        producer.flush()
        producer.close()
        logger.info(f"tier3_inspection: {len(top_symbols)} symbols dispatched")
        await _record_success("tier3_forced_inspection")

    except Exception as e:
        logger.error(f"tier3_forced_inspection failed: {e}")
        await _record_failure("tier3_forced_inspection", str(e))
    finally:
        await r.aclose()


async def _run_beta_calculator():
    """P1: Weekly Beta recalculation — 120-day rolling Beta from Polygon → TimescaleDB."""
    from scripts.maintenance.calculate_weekly_beta import run as beta_run

    def _do():
        beta_run()

    await asyncio.to_thread(_do)


async def _run_fred_fetch():
    """Weekly FRED macro covariates — CPI, unemployment, treasury yields, etc."""
    from services.fred_macro_fetcher import run_fred_fetch

    await asyncio.to_thread(run_fred_fetch)


# ─────────────────────────────────────────────
#  V12 Swing Scheduling Rhythm (new, full replacement)
# ─────────────────────────────────────────────

async def _run_swing_scanner():
    """V12 daily bar swing scan (16:30 previous close) → tier1 (pure V12)."""
    from v12.src.stockapp_v12.services.swing_scanner import SwingScanner
    import redis as redis_lib

    # Minimal integration of Locked Hard Filter Set (ADX + RSI)
    # Rollback: set USE_LOCKED_V12_SWING=0 (filters still driven by swing_scanner.yaml for quick revert)
    use_locked = os.getenv("USE_LOCKED_V12_SWING", "1").lower() not in ("0", "false", "no")
    mode = "Locked Hard Filter Set (ADX(14)>20 + RSI(14) 40-75)" if use_locked else "Legacy V12 filters (fallback)"
    logger.info(f"[V12] Running swing_scanner (daily bar mode - {mode})")

    def _do():
        r = redis_lib.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            decode_responses=True,
        )
        try:
            scanner = SwingScanner(redis_client=r)
            count = scanner.run()
            return count
        finally:
            try:
                r.close()
            except Exception:
                pass

    try:
        count = await asyncio.to_thread(_do)
        if count is not None:
            await _record_success("swing_scanner")
            logger.info(f"[V12] swing_scanner published {count} candidates (mode: {'locked' if use_locked else 'legacy'})")
    except Exception as e:
        logger.error(f"[V12] swing_scanner failed: {e}")
        await _record_failure("swing_scanner", str(e))


async def _run_premarket_review():
    """V12 premarket review (08:30) → actionable:today.

    When STOCKAPP_V12_ENTRY_ENABLED=1 use the real V12 PreMarketReview (fail-closed).
    Otherwise keep legacy/placeholder behavior (no breakage).
    """
    logger.info("[V12] Running premarket_review")

    if os.getenv("STOCKAPP_V12_ENTRY_ENABLED") == "1":
        try:
            import redis as redis_sync

            from v12.src.stockapp_v12.services.premarket_review import run_premarket_review

            r = redis_sync.Redis(
                host=os.getenv("REDIS_HOST", "redis"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                decode_responses=True,
            )
            count = run_premarket_review(redis_client=r)
            logger.info(f"[V12] premarket_review (V12 path) completed — actionable count={count}")
            r.close()
            return
        except Exception as e:
            # The V12 impl itself does fail-closed. Log here for scheduler visibility.
            logger.error(f"[V12] V12 premarket_review integration error (fail-closed should be applied inside): {e}")

    # Legacy / placeholder path (unchanged behavior when switch is off)
    logger.info("[V12] premarket_review using legacy/placeholder path")
    try:
        from services.selection import premarket_review  # if exists
        # await premarket_review.run()
    except Exception:
        logger.info("[V12] premarket_review placeholder - implement V12 version")
    # no-op otherwise (matches previous placeholder)


async def _run_daily_review():
    """V12 DailyReview (16:15) for multi-day positions: HOLD/TRIM/EXIT etc.

    Protected by STOCKAPP_V12_ENTRY_ENABLED (same as entry window).
    Uses real PositionManager (with journal) + TradeJournal.
    """
    if os.getenv("STOCKAPP_V12_ENTRY_ENABLED", "0") != "1":
        logger.info("[V12] daily_review skipped (STOCKAPP_V12_ENTRY_ENABLED != 1)")
        return

    from v12.src.stockapp_v12.services.daily_review import DailyReview
    from v12.src.stockapp_v12.services.position_manager import PositionManager
    from v12.src.stockapp_v12.pipeline.journal import TradeJournal
    import redis

    logger.info("[V12] Running daily_review (V12 path)")

    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True,
    )
    journal = TradeJournal(r)
    # IMPORTANT: PositionManager requires journal= kwarg
    pm = PositionManager(r, journal=journal, mode="paper_ibkr")

    # Price provider stub — in real env would come from market data
    def price_provider(symbol: str):
        # For verification/demo we can override in tests
        # Return a value above entry to allow HOLD decisions etc.
        return None

    # For paper soak / verification we apply immediately. In strict mode this can be False.
    review = DailyReview(pm, journal, price_provider, mode="paper_ibkr", apply_immediately=True)
    decisions = review.run()
    for d in decisions:
        logger.info(f"[V12] daily_review {d.symbol} -> {d.action} ({d.reason_code}) pnl={d.unrealized_pnl_pct:.1f}%")
    logger.info(f"[V12] daily_review completed, decisions={len(decisions)}")
    r.close()


async def _run_trailing_stop_check():
    """V12 periodic trailing (every 5min market hours)."""
    from v12.src.stockapp_v12.services.position_manager import PositionManager
    from v12.src.stockapp_v12.pipeline.journal import TradeJournal
    import redis

    logger.info("[V12] Running trailing_stop_check")
    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True,
    )
    # IMPORTANT: PositionManager requires journal= kwarg (see line 501 pattern).
    # Fix for P6 paper soak (Task 12): trailing_stop_check was failing every
    # 5 min with TypeError because the journal kwarg was missing.
    journal = TradeJournal(r)
    pm = PositionManager(r, journal=journal, mode="paper_ibkr")
    # For all open positions, update trail
    for pos in pm.list_open_positions():
        last_close = None  # TODO: fetch real price
        pm.update_trail_on_tick(pos.symbol, last_close or pos.entry_price)
    r.close()


async def _run_entry_window_open():
    """V12 entry window open (09:35 EST) — protected placeholder for EntryOrchestrator."""
    await _run_entry_window(phase="open")


async def _run_entry_window_close():
    """V12 entry window close (15:30 EST) — protected placeholder for EntryOrchestrator."""
    await _run_entry_window(phase="close")


async def _run_entry_window(phase: str):
    """
    V12 Entry Window (real wiring when enabled).

    - Reads actionable:today (LIST)
    - Wires real EntryOrchestrator with live V12 components
    - Paper-only execution (no real orders)
    - Full protected by STOCKAPP_V12_ENTRY_ENABLED=1
    """
    if os.getenv("STOCKAPP_V12_ENTRY_ENABLED", "0") != "1":
        logger.info(f"[V12] entry_window_{phase} skipped (STOCKAPP_V12_ENTRY_ENABLED != 1)")
        return

    logger.info(f"[V12] Running entry_window_{phase} (real wiring)")

    import redis
    from datetime import datetime, timezone, date as dt_date
    from typing import Any, Dict, Optional, Tuple

    try:
        # --- Setup redis (same pattern as other V12 jobs) ---
        r = redis.Redis(
            host=os.getenv("REDIS_HOST", "redis"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            decode_responses=True,
        )

        # --- Read actionable (from PreMarketReview) ---
        actionable_raw = r.lrange("actionable:today", 0, 4) or []
        actionable = [s.upper() for s in actionable_raw]
        if not actionable:
            logger.info("[V12] entry_window: no actionable symbols today")
            r.close()
            return

        logger.info(f"[V12] entry_window: processing actionable symbols = {actionable}")

        # --- Real components wiring ---
        from v12.src.stockapp_v12.pipeline.entry_guard import EntryEvaluationGuard
        from v12.src.stockapp_v12.services.forecast_service import ForecastService
        from v12.src.stockapp_v12.services.signal_rule_engine import SignalRuleEngine
        from v12.src.stockapp_v12.services.position_manager import PositionManager
        from v12.src.stockapp_v12.pipeline.journal import TradeJournal
        from v12.src.stockapp_v12.core.execution.bracket_order import BracketOrderService
        from v12.src.stockapp_v12.core.execution.paper_broker import PaperBroker
        from v12.src.stockapp_v12.services.entry_orchestrator import EntryOrchestrator, EntryContext
        from v12.src.stockapp_v12.bridge.gatekeeper import (
            compute_position_size,
            check_daily_trade_cap,
            check_symbol_cooldown,
            increment_daily_trade_count,
        )
        from v12.src.stockapp_v12.core.limits import V12RiskLimits

        # Guard (real)
        entry_guard = EntryEvaluationGuard(r, mode="paper_ibkr")

        # ForecastService: use real implementation.
        # It will attempt to resolve ChronosService from main src/ (via PYTHONPATH in scheduler).
        # On missing model/backend it gracefully degrades (returns ForecastOutput with degraded=True).
        # This is the desired real TSFM path per Phase 0.2.
        #
        # Round 3 closure (2026-07-01): when CHRONOS_ENSEMBLE_MODEL_PATHS is set
        # (colon-separated list of model dirs), ForecastService is wired with
        # EnsembleChronosBackend instead of the single-model ChronosService.
        # The ensemble averages Variant A + Variant B forecasts; falls back to
        # the first model when per-model forecast_ret_pct spread > 3%
        # (validated in v12/scripts/validate_ensemble_gate.py).
        from v12.src.stockapp_v12.services.ensemble_chronos_backend import (
            make_ensemble_backend_from_env,
        )
        ensemble_backend = make_ensemble_backend_from_env()
        if ensemble_backend is not None:
            forecast_service = ForecastService(backend=ensemble_backend)
            logger.info(
                f"[V12] entry_window: ForecastService using ENSEMBLE backend "
                f"(n_models={ensemble_backend.n_models}, "
                f"fallback_spread_pct={ensemble_backend.fallback_spread_pct})"
            )
        else:
            forecast_service = ForecastService()
            logger.info("[V12] entry_window: ForecastService using real backend resolution (ChronosService or degrade)")

        # SR (real)
        sr_engine = SignalRuleEngine()

        # V12 Gatekeeper adapter: sole sizing authority using V12 risk model (ATR-based).
        # - confidence explicitly None so it NEVER enters core position formula.
        # - R-05 (daily cap) and R-06 (cooldown) runtime checks integrated before sizing.
        limits = V12RiskLimits(confidence_cap_discount_enabled=False)
        def _v12_gatekeeper_fn(
            symbol: str,
            *,
            confidence: float,
            side: str,
            price: float,
            atr_14: Optional[float] = None,
            atr_pct: Optional[float] = None,
            regime: str = "BULL_LONG",
            **kwargs: Any,
        ) -> Tuple[bool, str, Dict[str, Any]]:
            sym = symbol.upper()

            # R-05: daily new trade cap
            try:
                capped, r05_reason = check_daily_trade_cap(r, limits)
                if capped:
                    logger.info(f"[V12] R-05 reject for {sym}: {r05_reason}")
                    return False, "R05_DAILY_CAP", {"size_pct": 0.0, "risk_rule": "R-05", "reason": r05_reason}
            except Exception as e:
                logger.warning(f"[V12] R-05 check error (safe default: allow) : {e}")

            # R-06: symbol cooldown
            try:
                in_cd, r06_reason = check_symbol_cooldown(r, sym)
                if in_cd:
                    logger.info(f"[V12] R-06 reject for {sym}: {r06_reason}")
                    return False, "R06_COOLDOWN", {"size_pct": 0.0, "risk_rule": "R-06", "reason": r06_reason}
            except Exception as e:
                logger.warning(f"[V12] R-06 check error (safe default: allow) : {e}")

            try:
                # confidence=None guarantees it does not affect the core ATR risk sizing calculation.
                size, scaling, audit = compute_position_size(
                    equity=100_000.0,
                    price=price,
                    atr_pct=atr_pct,
                    atr_14=atr_14,
                    regime=regime or "BULL_LONG",
                    confidence=None,
                    kelly_override=None,
                    risk_scaling_override=None,
                    is_degraded=False,
                    limits=limits,
                )
                approved = size > 0
                reason = "OK" if approved else "SIZE_ZERO"
                meta = {"size_pct": float(size), **audit, "sizing_model": "v12_risk"}
                return approved, reason, meta
            except Exception as e:
                return False, f"GK_ERR:{e}", {"size_pct": 0.0}

        # Bracket + Paper only (never real orders)
        paper_broker = PaperBroker()
        bracket_service = BracketOrderService(paper_broker)

        # Position + Journal (real)
        journal = TradeJournal(r)
        position_manager = PositionManager(r, journal=journal, mode="paper_ibkr")

        # Orchestrator (real wiring, analyst optional = None for minimal)
        # Wave 9 → Task 11 (4.1): wire YOLO no-trade filter (SR-06) into scheduler.
        # Soft veto — fail-soft inside YoloNoTradeFilter.predict(); only fires
        # when the model is loadable AND a high-confidence CHOP/NORMAL is
        # predicted for the symbol's 5min bar.
        from v12.src.stockapp_v12.services.yolo_filter import YoloNoTradeFilter
        orch = EntryOrchestrator(
            entry_guard=entry_guard,
            forecast_service=forecast_service,
            sr_engine=sr_engine,
            analyst_fn=None,
            gatekeeper_fn=_v12_gatekeeper_fn,
            bracket_service=bracket_service,
            position_manager=position_manager,
            journal=journal,
            yolo_filter=YoloNoTradeFilter(),  # Task 11 (4.1)
        )

        # --- Process actionable symbols (real ctx) ---
        for sym in actionable[:2]:  # limit for window
            # Real feature-engineered daily bars from data/features/daily/<sym>.parquet.
            # Replaces the earlier 1-row fake that caused Chronos to degrade to 0.
            from v12.src.stockapp_v12.services.feature_loader import load_single_symbol
            bars = load_single_symbol(sym, lookback_days=252)
            if bars is None or bars.empty:
                logger.warning(
                    f"[V12] {sym} no daily features available (parquet missing or "
                    f"< 100 rows); skipping. Re-run feature_engineering to rebuild."
                )
                continue

            ctx = EntryContext(
                symbol=sym,
                bars=bars,
                market_regime="BULL_LONG",
                actionable_symbols=set(actionable),
                equity=100_000.0,
                mode="paper_ibkr",
            )

            logger.info(f"[V12] entry_window: evaluating {sym} (real components, real ForecastService)")
            try:
                res = orch.evaluate_and_maybe_enter(ctx)
                fc = getattr(res, 'forecast', None)
                if fc:
                    logger.info(
                        f"[V12] {sym} forecast (real path): mean={getattr(fc, 'forecast_mean', None)}, "
                        f"degraded={getattr(fc, 'degraded', None)}, uncertainty={getattr(fc, 'uncertainty_pct', None)}"
                    )
                logger.info(
                    f"[V12] entry_window {sym} -> decision={res.decision} "
                    f"reason={res.reason_code} size_pct={getattr(res, 'size_pct', None)}"
                )

                # SOAK-7 (added 2026-06-29 per v3 postmortem): record the Chronos
                # forecast at every entry attempt and the actual trade side if
                # one fired. The P6 paper soak checklist (SOAK-7) reads these
                # keys to compute sign-agreement rate live, against the known
                # OOS finding (v2 hit_rate=0.30 on 10-symbol panel). Both keys
                # are 7-day-TTL date-suffixed per (date, sym) and feed
                # v12/scripts/paper_soak_checklist.check_sign_agreement.
                try:
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    fc_key = f"soak:forecast:chronos:{today_str}:{sym}"
                    sd_key = f"soak:trade:side:{today_str}:{sym}"
                    pipe = r.pipeline()
                    if fc is not None:
                        fc_mean = getattr(fc, "forecast_mean", None)
                        if fc_mean is not None:
                            pipe.setex(fc_key, 86400 * 7, str(float(fc_mean)))
                    decision = getattr(res, "decision", None)
                    if decision in ("BUY", "SELL"):
                        pipe.setex(sd_key, 86400 * 7, decision)
                    pipe.execute()
                except Exception as se:
                    logger.warning(f"[V12] failed to write SOAK-7 forecast/side keys: {se}")

                # Task 11 (4.0): SR-06 YOLO no-trade reject counter (Wave 9 → Task 11)
                # The P6 paper soak checklist reads lifecycle:sr06_yolo_rejects to verify
                # the filter is loaded and firing. Date-suffixed so daily rate is computable.
                if "SR-06" in (res.reason_code or ""):
                    try:
                        today_str = datetime.now().strftime("%Y-%m-%d")
                        sr06_key = f"lifecycle:sr06_yolo_rejects:{today_str}"
                        pipe = r.pipeline()
                        pipe.incr(sr06_key)
                        pipe.expire(sr06_key, 86400 * 7)  # 7-day retention for soak audit
                        pipe.execute()
                        logger.info(f"[V12] {sym} SR-06 YOLO no-trade reject counter incremented")
                    except Exception as se:
                        logger.warning(f"[V12] failed to increment SR-06 counter: {se}")

                if getattr(res, "decision", None) == "BUY":
                    try:
                        increment_daily_trade_count(r)
                        logger.info(f"[V12] {sym} R-05 daily trade count incremented after BUY")
                    except Exception as ie:
                        logger.warning(f"[V12] failed to increment daily trade count: {ie}")
            except Exception as inner_e:
                logger.error(f"[V12] entry_window {sym} orchestrator error: {inner_e}")

        r.close()

    except Exception as e:
        logger.error(f"[V12] entry_window_{phase} real wiring failed (protected): {e}")


# ─────────────────────────────────────────────
#  Scheduler Health Monitoring (4E)
# ─────────────────────────────────────────────

# Job SLA: (job_name, expected_completion_minutes_from_midnight, alert_label)
# Updated for V12 rhythm
JOB_SLAS = [
    ("swing_scanner", 16 * 60 + 40, "16:40"),  # 16:30 start
    ("catalyst_scorer", 8 * 60 + 10, "8:10"),
    ("premarket_review", 8 * 60 + 40, "8:40"),
    ("entry_window_open", 9 * 60 + 40, "9:40"),   # 09:35 V12 entry
    ("entry_window_close", 15 * 60 + 35, "15:35"), # 15:30 V12 entry close
    ("daily_review", 16 * 60 + 25, "16:25"),
]


async def _check_job_health():
    """
    Check if critical jobs completed within their SLA windows.
    Runs every 5 minutes during trading hours.
    Each alert fires at most once per job per day (dedup via Redis key).
    """
    import redis.asyncio as aioredis

    pool = await _get_redis_pool()
    r = aioredis.Redis(connection_pool=pool)
    try:
        now_est = datetime.now(ZoneInfo("America/New_York"))
        today = now_est.strftime("%Y-%m-%d")
        for job_name, expected_min, label in JOB_SLAS:
            # Dedup: skip if already alerted for this job today
            dedup_key = f"lifecycle:alerted:{job_name}:{today}"
            if await r.exists(dedup_key):
                continue

            last_run = await r.get(f"lifecycle:last_run:{job_name}")
            if not last_run:
                continue
            last_dt = datetime.fromisoformat(last_run)
            # Only check if we're past the expected completion time today
            if now_est.hour * 60 + now_est.minute < expected_min:
                continue
            if last_dt.date() != now_est.date():
                msg = f"\u26a0\ufe0f Scheduler Health: {job_name} has not run today (expected by {label} EST)"
                await _send_health_alert(r, msg, job_name, dedup_key)
            elif last_dt.hour * 60 + last_dt.minute > expected_min:
                msg = f"\u26a0\ufe0f Scheduler Health: {job_name} completed late ({last_dt.strftime('%H:%M')} vs expected {label} EST)"
                await _send_health_alert(r, msg, job_name, dedup_key)
    except Exception as e:
        logger.error(f"Job health check failed: {e}")
    finally:
        await r.aclose()


async def _send_health_alert(r, message: str, job_name: str, dedup_key: str = ""):
    """Send health alert via Telegram + Redis stream. Dedup once per day per job."""
    # Set dedup marker so we don't re-alert every 5 minutes
    if dedup_key:
        await r.setex(dedup_key, 86400, "1")  # 24h TTL

    logger.warning(message)
    # Telegram
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try:
            import urllib.request
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            data = json.dumps({"chat_id": chat_id, "text": message}).encode()
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
    # Redis stream
    try:
        await r.xadd("alerts:scheduler_health", {"message": message, "job": job_name})
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Job Definitions (EST, weekdays only)
# ─────────────────────────────────────────────

# (name, hour, minute, func, day_of_week)
# day_of_week defaults to "mon-fri" if omitted
JOBS = [
    # ── V12 Swing New Scheduling Rhythm (full replacement) ───────────────────────
    # Aligned with v12/docs/SCHEDULER.md and time-node flow (swing, not intraday)
    ("swing_scanner",     "16", "30",  _run_swing_scanner),      # Previous close, daily bar for tier1
    ("catalyst_scorer",    "8",  "0",  _run_catalyst_scorer),    # 08:00 L2
    ("premarket_review",   "8", "30",  _run_premarket_review),  # 08:30 actionable
    ("entry_window_open",  "9", "35",  _run_entry_window_open),  # 09:35 V12 entry window (STOCKAPP_V12_ENTRY_ENABLED)
    ("entry_window_close", "15", "30", _run_entry_window_close), # 15:30 V12 entry window close
    ("self_check",         "8", "55",  _run_self_check),
    ("morning_risk_load",  "9", "15",  _run_morning_risk_load), # Load overrides
    ("daily_review",      "16", "15",  _run_daily_review),      # 16:15 V12 matrix for multi-day

    # ── Feedback & Observability (adapted for V12) ──────────────────────────────
    ("v12_soak_daily",    "17",  "0",  _run_v12_soak_daily),    # 17:00 V12 combined soak audit (SOAK + deployment_health)
    ("post_mortem",       "19", "30",  _run_postmortem),
    ("rag_integrity",     "20",  "0",  _run_rag_integrity),

    # ── Weekly (keep if relevant for V12 risk/ macro) ───────────────────────────
    ("calculate_beta",     "2",  "0",  _run_beta_calculator, "sun"),
    ("fred_macro_fetch",   "3",  "0",  _run_fred_fetch, "mon"),
]


def _wrap_task(task_name: str, coro_func):
    """Wrap a task with error handling and Redis status tracking."""
    async def wrapper():
        logger.info(f"[LifecycleScheduler] task '{task_name}' starting...")
        try:
            await coro_func()
            await _record_success(task_name)
            logger.info(f"[LifecycleScheduler] task '{task_name}' completed")
        except Exception:
            await _record_failure(task_name, traceback.format_exc())
    return wrapper


def start_lifecycle_scheduler() -> AsyncIOScheduler:
    """
    Start the APScheduler with all lifecycle cron jobs.
    Must be called from within an async context (e.g. FastAPI lifespan).
    """
    global _scheduler

    _scheduler = AsyncIOScheduler(timezone="America/New_York")

    for entry in JOBS:
        name, hour, minute, func = entry[:4]
        dow = entry[4] if len(entry) > 4 else "mon-fri"
        _scheduler.add_job(
            _wrap_task(name, func),
            CronTrigger(
                day_of_week=dow,
                hour=hour,
                minute=minute,
                timezone="America/New_York",
            ),
            id=name,
            name=name,
            misfire_grace_time=300,
            max_instances=1,
            coalesce=True,
        )

    # Telegram heartbeat: every 5 minutes on weekdays 04:00–20:05 EST
    # Idle on weekends; system runs Mon 04:00 → Fri ~20:00.
    _scheduler.add_job(
        _wrap_task("telegram_heartbeat", _run_telegram_heartbeat),
        CronTrigger(
            day_of_week="mon-fri",
            hour="4-20",
            minute="*/5",
            timezone="America/New_York",
        ),
        id="telegram_heartbeat",
        name="telegram_heartbeat",
        misfire_grace_time=60,
        max_instances=1,
        coalesce=True,
    )

    # Job health monitor: every 5 min during trading hours (4E)
    _scheduler.add_job(
        _wrap_task("job_health_monitor", _check_job_health),
        CronTrigger(
            day_of_week="mon-fri",
            hour="4-16",
            minute="*/5",
            timezone="America/New_York",
        ),
        id="job_health_monitor",
        name="job_health_monitor",
        misfire_grace_time=60,
        max_instances=1,
        coalesce=True,
    )

    # V12 periodic trailing stop check (every 5 min during market hours)
    _scheduler.add_job(
        _wrap_task("trailing_stop_check", _run_trailing_stop_check),
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-16",
            minute="*/5",
            timezone="America/New_York",
        ),
        id="trailing_stop_check",
        name="trailing_stop_check",
        misfire_grace_time=60,
        max_instances=1,
        coalesce=True,
    )

    _scheduler.start()
    logger.info(f"LifecycleScheduler started -- {len(JOBS)} cron jobs + heartbeat + health monitor")
    logger.info(f"  Active window: Mon 04:00 → Fri ~20:00 EST")
    logger.info(f"  misfire_grace_time: JOBS=300s, periodic=60s")
    logger.info(f"  ⚠️  Late-start: morning jobs (8:00-9:35) only fire if scheduler is up by 7:55am EST")
    logger.info(f"     Daily restart mechanism: scripts/paper_soak_daily_restart.sh (cron @ 07:50am)")

    # Per-job registration log with next_fire_time (B-task fix: makes "only 3 jobs firing" myth debunkable)
    for entry in JOBS:
        name, hour, minute = entry[0], entry[1], entry[2]
        dow = entry[4] if len(entry) > 4 else "mon-fri"
        job = _scheduler.get_job(name)
        next_fire = job.next_run_time.isoformat() if job and job.next_run_time else "?"
        logger.info(f"  registered {name:20s} cron={hour:>2}:{minute:0>2} ({dow:7s})  next={next_fire}")
    for pname in ("telegram_heartbeat", "job_health_monitor", "trailing_stop_check"):
        job = _scheduler.get_job(pname)
        next_fire = job.next_run_time.isoformat() if job and job.next_run_time else "?"
        logger.info(f"  registered {pname:20s} cron=*/5min  next={next_fire}")

    return _scheduler


def get_scheduler():
    """Return the active scheduler (for testing / inspection)."""
    return _scheduler
