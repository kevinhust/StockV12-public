"""Paper entry composition root + live-safety defaults (2026-07-27 ABC)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from stockapp_v12.app.wiring import (
    build_paper_entry_orchestrator,
    make_entry_window_providers,
)
from stockapp_v12.bridge.live_guard import DEFAULT_ALLOWED_MODES, make_live_order_guard
from stockapp_v12.pipeline.entry_window import run_entry_window
from stockapp_v12.pipeline.types import EntryContext
from tests._fakes import FakeRedis


def test_default_allowed_modes_are_paper_only():
    assert "paper_ibkr" in DEFAULT_ALLOWED_MODES
    assert not any(m.startswith("live") for m in DEFAULT_ALLOWED_MODES)


def test_make_live_order_guard_blocks_live_mode_by_default():
    r = FakeRedis()
    guard = make_live_order_guard(r)
    from stockapp_v12.bridge.live_guard import LiveGuardContext

    res = guard.check(
        LiveGuardContext(
            symbol="AAPL",
            side="BUY",
            mode="live_ibkr",
            price=100.0,
            equity=100_000.0,
            confidence=0.9,
        )
    )
    assert res.allowed is False
    assert res.rule == "MODE_BLOCK"


def test_build_paper_entry_orchestrator_refuses_live():
    with pytest.raises(ValueError, match="refuses live"):
        build_paper_entry_orchestrator(FakeRedis(), mode="live_ibkr")


def test_build_paper_entry_has_live_guard():
    orch = build_paper_entry_orchestrator(
        FakeRedis(), mode="paper_ibkr", load_chronos=False
    )
    assert orch.live_guard is not None


def test_entry_window_with_wired_providers_evaluates_when_bars_present():
    """orch_factory + bars → skipped_no_orchestrator stays 0."""
    r = FakeRedis()
    r.zadd("watchlist:tier2", {"AAPL": 1.0})
    r.set("market_regime:current", "NEUTRAL")

    bars = pd.DataFrame(
        {
            "close": [100.0 + i * 0.1 for i in range(120)],
            "atr_14": [2.0] * 120,
        }
    )

    def orch_factory(*, mode, regime, redis_client):
        return build_paper_entry_orchestrator(
            redis_client, mode=mode, regime=regime, load_chronos=False
        )

    def bars_provider(sym):
        return bars

    result = run_entry_window(
        r,
        mode="paper_ibkr",
        orch_factory=orch_factory,
        bars_provider=bars_provider,
        equity_getter=lambda: 100_000.0,
    )
    assert result.tier2_size == 1
    assert result.skipped_no_orchestrator == 0
    assert result.skipped_no_bars == 0


def test_make_entry_window_providers_returns_callables():
    r = FakeRedis()
    of, bp, eg = make_entry_window_providers(r, load_chronos=False)
    assert callable(of) and callable(bp) and callable(eg)
    assert eg() == 100_000.0
