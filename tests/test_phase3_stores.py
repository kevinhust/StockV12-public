"""Phase 3: store ports + in-memory adapters + domain zero-redis path."""

from __future__ import annotations

from datetime import date

from stockapp_v12.adapters.stores.memory import (
    InMemoryEntryEvalStore,
    InMemoryJournalStore,
    InMemoryPositionStore,
    InMemoryWatchlistStore,
)
from stockapp_v12.core.limits import V12RiskLimits
from stockapp_v12.pipeline.entry_guard import EntryEvaluationGuard
from stockapp_v12.pipeline.journal import TradeJournal
from stockapp_v12.services.position_manager import PositionManager, PositionState
from stockapp_v12.services.swing_scanner import SwingCandidate, SwingScanner


def test_position_manager_register_list_close_with_memory_store():
    store = InMemoryPositionStore()
    journal = TradeJournal(store=InMemoryJournalStore())
    pm = PositionManager(
        store=store,
        journal=journal,
        limits=V12RiskLimits(min_hold_days=1, symbol_cooldown_hours=48),
        mode="paper_ibkr",
    )
    rec = pm.register(
        symbol="NVDA",
        entry_price=100.0,
        qty=10,
        atr_at_entry=2.0,
        parent_order_id="p1",
        sl_order_id="s1",
        sl_price=95.0,
    )
    assert rec.state == PositionState.OPEN
    assert pm.has_open_position("NVDA")
    assert len(pm.list_open_positions()) == 1

    closed = pm._close_position(rec, reason="review_exit", last_price=105.0)
    assert closed.state == PositionState.CLOSED
    assert not pm.has_open_position("NVDA")
    assert store.strings.get("risk:cooldown:NVDA") == "1"


def test_trade_journal_append_read_memory():
    j = TradeJournal(store=InMemoryJournalStore())
    j.append(
        "AAPL",
        mode="paper_ibkr",
        stage="entry",
        decision="PASS",
        reason_code="SR-01",
        reason_detail="low return",
        strict=False,
    )
    entries = j.read_entries("AAPL")
    assert len(entries) == 1
    assert entries[0]["reason_code"] == "SR-01"


def test_entry_guard_try_mark_memory():
    store = InMemoryEntryEvalStore()
    # open-index for INV-2
    store.sets["position:paper_ibkr:index"] = set()
    guard = EntryEvaluationGuard(store=store, mode="paper_ibkr")
    ok, reason = guard.begin_entry_evaluation("MSFT", date="20260726")
    assert ok is True and reason is None
    ok2, reason2 = guard.begin_entry_evaluation("MSFT", date="20260726")
    assert ok2 is False and reason2 == "INV-1_ALREADY_EVALUATED"


def test_watchlist_memory_publish_and_list():
    wl = InMemoryWatchlistStore()
    scanner = SwingScanner(
        config_path=None,
        redis_client=None,
        market_data=object(),  # skip network init
        watchlist_store=wl,
    )
    c = SwingCandidate(
        symbol="AAA",
        price=10.0,
        atr_percent=0.02,
        relative_strength_20d=1.0,
        avg_dollar_volume_20d=1e7,
        volume_breakout_score=1.0,
        pivot_proximity=0.5,
        dist_52w_high=0.1,
        composite_score=0.9,
        scanned_at="2026-07-26T16:00:00-04:00",
        above_ema20=True,
    )
    n = scanner.publish([c])
    assert n == 1
    assert "AAA" in wl.zsets["watchlist:tier1"]
    assert "AAA" in wl.hashes["watchlist:tier1:details"]
