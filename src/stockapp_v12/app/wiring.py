"""Composition root — wire ports to adapters for Runtime + paper entry.

Phase 1: clock + market_data + notifier + optional chronos + stores.
Phase 5 / 2026-07-27: ``build_paper_entry_orchestrator`` for NUC paper cron.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from stockapp_v12.adapters.clock import SystemClock
from stockapp_v12.adapters.marketdata.fake import FakeMarketData
from stockapp_v12.contracts.chronos_backend import ChronosBackend
from stockapp_v12.contracts.clock import Clock
from stockapp_v12.contracts.market_data_client import MarketDataClient
from stockapp_v12.contracts.notifier import Notifier
from stockapp_v12.notifiers import make_notifier_from_env

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Settings:
    """Runtime settings for composition root (lightweight Phase 1)."""

    redis_url: Optional[str] = None
    chronos_model_path: Optional[str] = None
    use_fake_market_data: bool = False


@dataclass
class Runtime:
    """Wired application ports. Heavy pieces left None until needed."""

    clock: Clock
    market_data: MarketDataClient
    notifier: Notifier
    # optional heavy pieces left None until needed
    forecast_backend: Optional[ChronosBackend] = None
    # Phase 3 stores (optional; None unless redis_url / use_memory_stores)
    position_store: Optional[object] = None
    journal_store: Optional[object] = None
    entry_eval_store: Optional[object] = None
    watchlist_store: Optional[object] = None


def build_runtime(
    settings: Optional[Settings] = None,
    *,
    clock: Optional[Clock] = None,
    market_data: Optional[MarketDataClient] = None,
    notifier: Optional[Notifier] = None,
    forecast_backend: Optional[ChronosBackend] = None,
    load_chronos: bool = False,
    use_memory_stores: bool = False,
    redis_client=None,
) -> Runtime:
    """Build a lightweight Runtime from settings and optional overrides.

    Default market_data is Composite with no live clients (unit-safe).
    Pass ``use_fake_market_data=True`` or ``market_data=...`` for tests.
    Chronos loads only when ``load_chronos=True`` (soft-fails to None).
    Phase 3: ``use_memory_stores=True`` or ``redis_client=`` wires store ports.
    """
    settings = settings or Settings()
    clock = clock or SystemClock()
    if market_data is None:
        if settings.use_fake_market_data:
            market_data = FakeMarketData()
        else:
            from stockapp_v12.adapters.marketdata.composite import (
                CompositeMarketDataClient,
            )

            # Composite default: no live clients unless caller supplies —
            # for unit safety
            market_data = CompositeMarketDataClient(
                lb_service=None, polygon_client=None
            )
    notifier = notifier or make_notifier_from_env()
    if forecast_backend is None and load_chronos:
        from stockapp_v12.adapters.legacy_v11.chronos import (
            build_legacy_chronos_backend,
        )

        forecast_backend = build_legacy_chronos_backend(
            model_path=settings.chronos_model_path
        )

    position_store = journal_store = entry_eval_store = watchlist_store = None
    if use_memory_stores:
        from stockapp_v12.adapters.stores.memory import (
            InMemoryEntryEvalStore,
            InMemoryJournalStore,
            InMemoryPositionStore,
            InMemoryWatchlistStore,
        )

        position_store = InMemoryPositionStore()
        journal_store = InMemoryJournalStore()
        entry_eval_store = InMemoryEntryEvalStore()
        watchlist_store = InMemoryWatchlistStore()
    elif redis_client is not None:
        from stockapp_v12.adapters.stores.redis_stores import (
            RedisEntryEvalStore,
            RedisJournalStore,
            RedisPositionStore,
            RedisWatchlistStore,
        )

        position_store = RedisPositionStore(redis_client)
        journal_store = RedisJournalStore(redis_client)
        entry_eval_store = RedisEntryEvalStore(redis_client)
        watchlist_store = RedisWatchlistStore(redis_client)

    return Runtime(
        clock=clock,
        market_data=market_data,
        notifier=notifier,
        forecast_backend=forecast_backend,
        position_store=position_store,
        journal_store=journal_store,
        entry_eval_store=entry_eval_store,
        watchlist_store=watchlist_store,
    )


def _paper_gatekeeper_fn(
    redis_client: Any = None,
    *,
    default_equity: float = 100_000.0,
) -> Callable[..., Any]:
    """Sizing-only gatekeeper for paper path (no archive HardRisk required)."""
    from stockapp_v12.bridge.gatekeeper import (
        check_daily_trade_cap,
        compute_position_size,
        get_daily_trade_count,
    )
    from stockapp_v12.core.limits import load_v12_risk_limits

    limits = load_v12_risk_limits()
    nav = float(default_equity)

    def gatekeeper_fn(
        symbol: str,
        *,
        confidence: float,
        side: str,
        price: float,
        atr_pct: Optional[float] = None,
        atr_14: Optional[float] = None,
        regime: str = "NEUTRAL",
        equity: Optional[float] = None,
        **kwargs: Any,
    ) -> tuple[bool, str, dict]:
        if side.upper() != "BUY":
            return False, "SIDE_NOT_BUY", {}
        if redis_client is not None:
            over, why = check_daily_trade_cap(redis_client, limits)
            if over:
                return False, why or "R-05_DAILY_CAP", {}
        eq = float(equity if equity is not None else nav)
        size_pct, _risk_scaling, audit = compute_position_size(
            equity=eq,
            price=float(price),
            atr_pct=atr_pct,
            atr_14=atr_14,
            regime=regime or "NEUTRAL",
            confidence=float(confidence),
            kelly_override=None,
            risk_scaling_override=None,
            is_degraded=False,
            limits=limits,
        )
        if size_pct <= 0:
            return False, "ZERO_SIZE", dict(audit)
        meta = {"size_pct": size_pct, **audit}
        if redis_client is not None:
            meta["daily_trade_count"] = get_daily_trade_count(redis_client)
        return True, "", meta

    return gatekeeper_fn


def build_paper_entry_orchestrator(
    redis_client,
    *,
    mode: str = "paper_ibkr",
    regime: str = "NEUTRAL",
    features_dir: Optional[str | Path] = None,
    equity: float = 100_000.0,
    load_chronos: bool = True,
):
    """Assemble a paper-only EntryOrchestrator with LiveOrderGuard fail-closed.

    Used by NUC ``run_entry_window`` composition. Live modes are rejected by
    the guard whitelist (paper_ibkr / paper only).
    """
    from stockapp_v12.bridge.gatekeeper import daily_trades_key
    from stockapp_v12.bridge.live_guard import DEFAULT_ALLOWED_MODES, make_live_order_guard
    from stockapp_v12.core.execution.bracket_order import BracketOrderService
    from stockapp_v12.core.execution.paper_broker import PaperBroker
    from stockapp_v12.pipeline.entry_guard import EntryEvaluationGuard
    from stockapp_v12.pipeline.journal import TradeJournal
    from stockapp_v12.services.entry_orchestrator import EntryOrchestrator
    from stockapp_v12.services.forecast_service import ForecastService
    from stockapp_v12.services.position_manager import PositionManager
    from stockapp_v12.services.signal_rule_engine import SignalRuleEngine

    mode_l = (mode or "paper_ibkr").lower()
    if mode_l.startswith("live"):
        raise ValueError(
            f"build_paper_entry_orchestrator refuses live mode {mode!r}; "
            "use an explicit live composition root when ready"
        )

    rt = build_runtime(
        redis_client=redis_client,
        load_chronos=load_chronos,
    )
    live_guard = make_live_order_guard(
        redis_client,
        allowed_modes=list(DEFAULT_ALLOWED_MODES),
        fail_closed_on_halt_check=True,
    )
    journal = TradeJournal(redis_client, store=rt.journal_store)
    entry_guard = EntryEvaluationGuard(
        redis_client, mode=mode, store=rt.entry_eval_store
    )
    pm = PositionManager(
        redis_client=redis_client,
        journal=journal,
        mode=mode,
        store=rt.position_store,
    )
    forecast = ForecastService(backend=rt.forecast_backend)
    sr = SignalRuleEngine()
    broker = PaperBroker()
    bracket = BracketOrderService(broker=broker)
    gatekeeper_fn = _paper_gatekeeper_fn(redis_client, default_equity=float(equity))

    def daily_counter_fn() -> int:
        key = daily_trades_key()
        try:
            return int(redis_client.incr(key))
        except Exception as e:
            logger.warning("daily_counter_fn incr failed: %s", e)
            return 0

    orch = EntryOrchestrator(
        entry_guard=entry_guard,
        forecast_service=forecast,
        sr_engine=sr,
        analyst_fn=None,  # forecast-only paper path
        gatekeeper_fn=gatekeeper_fn,
        bracket_service=bracket,
        position_manager=pm,
        journal=journal,
        yolo_filter=None,
        live_guard=live_guard,
        veto_counter=None,
        daily_counter_fn=daily_counter_fn,
        notifier=rt.notifier,
    )
    # stash for factory consumers (bars path / equity)
    orch._paper_equity = float(equity)  # type: ignore[attr-defined]
    orch._paper_features_dir = features_dir  # type: ignore[attr-defined]
    orch._paper_regime = regime  # type: ignore[attr-defined]
    return orch


def make_entry_window_providers(
    redis_client,
    *,
    mode: str = "paper_ibkr",
    features_dir: Optional[str | Path] = None,
    equity: Optional[float] = None,
    load_chronos: bool = True,
):
    """Callables for ``run_entry_window`` (orch_factory, bars_provider, equity_getter)."""
    root = Path(os.environ.get("STOCKAPP_ROOT", Path.cwd()))
    feat = Path(features_dir or root / "data" / "features" / "daily")
    nav = float(equity if equity is not None else os.environ.get("V12_PAPER_EQUITY", "100000"))

    def orch_factory(*, mode: str, regime: str, redis_client=redis_client):
        return build_paper_entry_orchestrator(
            redis_client,
            mode=mode,
            regime=regime,
            features_dir=feat,
            equity=nav,
            load_chronos=load_chronos,
        )

    def bars_provider(symbol: str):
        from stockapp_v12.services.feature_loader import load_single_symbol

        try:
            return load_single_symbol(symbol, features_dir=feat, require_min_rows=50)
        except Exception as e:
            logger.warning("bars_provider %s failed: %s", symbol, e)
            return None

    def equity_getter() -> float:
        return nav

    return orch_factory, bars_provider, equity_getter


__all__ = [
    "Settings",
    "Runtime",
    "build_runtime",
    "build_paper_entry_orchestrator",
    "make_entry_window_providers",
]
