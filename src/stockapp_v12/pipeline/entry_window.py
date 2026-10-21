"""Live entry-window cron target (P0-3, council 2026-07-09).

The previous production entry path (V11 ``lifecycle_scheduler._run_entry_window``)
read a Redis LIST ``actionable:today`` that was explicitly removed in hygiene-I.
As a result the cron fired, logged ``\"no actionable symbols today\"`` and exited
without ever calling ``EntryOrchestrator.evaluate_and_maybe_enter``. Net effect:
zero entries per day in production, no operator signal.

This module is the V12 replacement. It:

1. Reads ``watchlist:tier2`` (Redis ZSET) — the same ZSET that
   ``scripts/run_catalyst_scorer.sh`` writes, computed by the SCAN → CATALYST
   chain. The ZSET membership is the canonical \"candidate universe for
   entry today\".
2. Reads ``market_regime:current`` (Redis string) for the live regime.
   Pre-fix hardcoded ``BULL_LONG``; we now trust the Redis truth and fall
   back to ``NEUTRAL`` if the key is missing.
3. For each tier-2 symbol, builds an ``EntryContext`` and dispatches it
   through ``EntryOrchestrator.evaluate_and_maybe_enter`` if we have a
   constructor injected via ``orch_factory``. When ``orch_factory`` is
   ``None`` (default — eg. from a smoke-test or a fresh deploy where
   bars are not yet cached), the per-symbol decision is logged but not
   dispatched, so the operator still gets visibility.
4. Never reads ``actionable:today`` — the dead key would 404 / empty-set
   and silently produce \"0 entries per day\" again.

This module is wired from ``scripts/run_entry_window.sh`` which is
registered in ``scripts/install_pipeline_cron.sh`` to fire at 09:35 ET
on NYSE trading days (and every 5 min during market hours).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Set
from zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)

# Redis key names — kept here (not in core/paths) because they're a Redis
# contract, not a filesystem contract. If they ever change, look here.
TIER2_ZSET_KEY: str = "watchlist:tier2"
MARKET_REGIME_KEY: str = "market_regime:current"
DEFAULT_REGIME: str = "NEUTRAL"  # conservative: don't pretend to know regime.


@dataclass
class EntryWindowResult:
    """Outcome of a single entry-window run."""

    regime: str
    tier2_size: int
    evaluated: int = 0
    buys: int = 0
    denies: int = 0
    passes: int = 0
    skipped_no_orchestrator: int = 0
    skipped_no_bars: int = 0
    evaluated_symbols: List[str] = field(default_factory=list)


def read_tier2_symbols(redis_client=None, *, watchlist_store=None) -> List[str]:
    """ZRANGE ``watchlist:tier2`` 0 -1 — return all members as a Python list.

    Phase 3: prefer ``watchlist_store.list_members``; redis_client wrapped for BC.
    Returns an empty list on any error (logged at WARNING).
    """
    try:
        if watchlist_store is not None:
            return list(watchlist_store.list_members(TIER2_ZSET_KEY))
        if redis_client is None:
            return []
        from stockapp_v12.adapters.stores.redis_stores import RedisWatchlistStore

        return RedisWatchlistStore(redis_client).list_members(TIER2_ZSET_KEY)
    except Exception as e:
        logger.warning(
            "[V12-ENTRY-WINDOW] ZRANGE %s failed: %s: %s — treating as empty",
            TIER2_ZSET_KEY, type(e).__name__, e,
        )
        return []


def read_market_regime(redis_client) -> str:
    """Read ``market_regime:current`` from Redis. Default ``NEUTRAL`` on miss/error.

    Pre-fix hardcoded ``BULL_LONG`` which is the wrong default — it told the
    EntryGuard \"this is a buy regime\" even when the regime was actually
    ``BEAR`` or ``CHOP``. Council synthesis P0-3 mandates dynamic read.
    """
    try:
        raw = redis_client.get(MARKET_REGIME_KEY)
        if raw is None:
            logger.info(
                "[V12-ENTRY-WINDOW] %s unset; using default regime=%s",
                MARKET_REGIME_KEY, DEFAULT_REGIME,
            )
            return DEFAULT_REGIME
        val = raw.decode() if isinstance(raw, bytes) else str(raw)
        val = val.strip()
        if not val:
            logger.warning(
                "[V12-ENTRY-WINDOW] %s is empty; using default regime=%s",
                MARKET_REGIME_KEY, DEFAULT_REGIME,
            )
            return DEFAULT_REGIME
        return val
    except Exception as e:
        logger.warning(
            "[V12-ENTRY-WINDOW] GET %s failed: %s: %s — using default regime=%s",
            MARKET_REGIME_KEY, type(e).__name__, e, DEFAULT_REGIME,
        )
        return DEFAULT_REGIME


def _default_orchestrator_factory(*args, **kwargs):
    """Placeholder factory. Real wiring is provided by ``scripts/run_entry_window.sh``.

    If you see this factory hit, the caller did not inject one — which
    means we are in a context that should NOT actually submit orders
    (test, dry-run, fresh deploy without bars). We log + skip instead of
    raising so the cron path is observable but not destructive.
    """
    logger.warning(
        "[V12-ENTRY-WINDOW] no orchestrator_factory supplied — skipping "
        "live evaluation; candidates will be logged only"
    )
    return None


def run_entry_window(
    redis_client,
    *,
    bars_provider: Optional[Callable[[str], Optional[object]]] = None,
    equity_getter: Optional[Callable[[], float]] = None,
    orch_factory: Optional[Callable[..., Optional[object]]] = None,
    mode: str = "paper_ibkr",
    regime_override: Optional[str] = None,
) -> EntryWindowResult:
    """Read tier-2 + regime; build EntryContext per symbol; dispatch to orchestrator.

    Parameters
    ----------
    redis_client : Redis
        Real Redis client (must support ``zrange`` and ``get`` with decode_responses=True
        for clean string handling, or bytes for the existing code path).
    bars_provider : callable, optional
        ``bars_provider(symbol) -> pd.DataFrame | None``. Defaults to ``None``,
        in which case every symbol is logged as ``skipped_no_bars`` (safer than
        guessing bars). Live cron wires this to a real bars loader (D1 cache).
    equity_getter : callable, optional
        ``equity_getter() -> float``. Defaults to ``None`` → $100_000 paper NAV.
    orch_factory : callable, optional
        ``orch_factory(mode, regime, redis_client) -> EntryOrchestrator | None``.
        When ``None`` we log + skip dispatch (operator visibility preserved).
    mode : str
        Passed to ``EntryContext.mode`` — typically ``\"paper_ibkr\"`` or ``\"live\"``.
    regime_override : str, optional
        Test-only escape hatch — overrides the Redis regime read. Production
        code MUST leave this ``None``.

    Returns
    -------
    EntryWindowResult
        Counts + symbols that were dispatched.
    """
    regime = regime_override if regime_override is not None else read_market_regime(redis_client)
    tier2_symbols = read_tier2_symbols(redis_client)

    result = EntryWindowResult(regime=regime, tier2_size=len(tier2_symbols))
    if not tier2_symbols:
        logger.info(
            "[V12-ENTRY-WINDOW] tier2 is empty (0 candidates); entry-window noop. "
            "If this persists past 09:35 ET, check catalyst_scorer.crontab and "
            "the morning swing_scanner.log."
        )
        return result

    # Build (or skip) the orchestrator
    orch = None
    if orch_factory is not None:
        try:
            orch = orch_factory(
                mode=mode, regime=regime, redis_client=redis_client,
            )
        except Exception as e:
            logger.warning(
                "[V12-ENTRY-WINDOW] orch_factory raised: %s: %s — skipping dispatch",
                type(e).__name__, e,
            )

    # Resolve equity + dispatch loop
    equity = float(equity_getter()) if equity_getter is not None else 100_000.0
    actionable: Set[str] = set(tier2_symbols)  # INV-2 style: tier2 == actionable today

    for sym in tier2_symbols:
        sym_u = sym.upper()
        bars = None if bars_provider is None else bars_provider(sym_u)

        if orch is None:
            # Visibility path: log + count, do NOT pretend we did anything.
            logger.info(
                "[V12-ENTRY-WINDOW] candidate %s regime=%s — orchestrator absent, "
                "logged-only (would have evaluated with %d bars)",
                sym_u, regime, 0 if bars is None else len(getattr(bars, "index", [])),
            )
            result.skipped_no_orchestrator += 1
            continue

        if bars is None or (hasattr(bars, "empty") and bars.empty):
            # No bars → can't forecast → can't compute SL → preserve as no-op.
            logger.info(
                "[V12-ENTRY-WINDOW] %s skipped — no bars in cache", sym_u,
            )
            result.skipped_no_bars += 1
            continue

        # Build ctx and dispatch. EntryContext lives in pipeline.types (Phase 2).
        from stockapp_v12.pipeline.types import EntryContext
        ctx = EntryContext(
            symbol=sym_u,
            bars=bars,
            market_regime=regime,
            actionable_symbols=actionable,
            equity=equity,
            mode=mode,
        )
        try:
            entry_res = orch.evaluate_and_maybe_enter(ctx)
        except Exception as e:
            logger.warning(
                "[V12-ENTRY-WINDOW] orchestrator raised for %s: %s: %s — "
                "recording skip, not aborting the run",
                sym_u, type(e).__name__, e,
            )
            continue

        result.evaluated += 1
        result.evaluated_symbols.append(sym_u)
        if entry_res.decision == "BUY":
            result.buys += 1
        elif entry_res.decision == "DENY":
            result.denies += 1
        else:
            result.passes += 1

    logger.info(
        "[V12-ENTRY-WINDOW] DONE regime=%s tier2=%d evaluated=%d buys=%d "
        "denies=%d passes=%d skipped_no_orch=%d skipped_no_bars=%d",
        result.regime, result.tier2_size, result.evaluated,
        result.buys, result.denies, result.passes,
        result.skipped_no_orchestrator, result.skipped_no_bars,
    )
    return result


# ─── Cron CLI entrypoint ─────────────────────────────────────────────────


def main(argv: Optional[Iterable[str]] = None) -> int:
    """``python -m stockapp_v12.pipeline.entry_window`` cron entrypoint.

    This is intentionally thin — real bars-loading / orchestrator wiring
    lives in the production shell wrapper (``scripts/run_entry_window.sh``),
    which imports this module and provides the ``bars_provider``,
    ``equity_getter``, and ``orch_factory`` callables. The CLI here only
    handles a Redis smoke-test (ZRANGE + GET) so a manual ``run`` from
    the shell verifies the Redis contract without standing up the full
    pipeline.
    """
    import argparse
    import os

    import redis

    parser = argparse.ArgumentParser(
        description="V12 entry-window cron — reads tier2 + regime, logs the run.",
    )
    parser.add_argument(
        "--mode", default=os.environ.get("V12_MODE", "paper_ibkr"),
        help="Entry context mode (default paper_ibkr).",
    )
    parser.add_argument(
        "--regime-override", default=None,
        help="Skip Redis regime read; use this value instead (test-only).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Read Redis, log the candidates, but skip evaluation.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    host = os.environ.get("REDIS_HOST", "127.0.0.1")
    port = int(os.environ.get("REDIS_PORT", "6380"))
    try:
        r = redis.Redis(host=host, port=port, decode_responses=True)
        r.ping()
    except Exception as e:
        logger.warning("[V12-ENTRY-WINDOW] Redis ping failed: %s: %s", type(e).__name__, e)
        print(f"ERROR: cannot reach Redis at {host}:{port} — {type(e).__name__}: {e}")
        return 1

    if args.dry_run:
        tier2 = read_tier2_symbols(r)
        regime = args.regime_override or read_market_regime(r)
        print(f"DRY tier2_size={len(tier2)} regime={regime}")
        for sym in tier2[:10]:
            print(f"  candidate: {sym}")
        return 0

    # Visibility-only smoke path. Production cron uses
    # ``python -m stockapp_v12.app.entry_cron`` (composition root in app/).
    result = run_entry_window(
        r, mode=args.mode, regime_override=args.regime_override,
    )
    print(
        f"regime={result.regime} tier2_size={result.tier2_size} "
        f"evaluated={result.evaluated} buys={result.buys} "
        f"skipped_no_orch={result.skipped_no_orchestrator} "
        f"skipped_no_bars={result.skipped_no_bars} wired=0"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
