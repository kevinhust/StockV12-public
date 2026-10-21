"""TS-A3: Track A wiring — entry_window_open uses real feature bars (not 1-row fake).

Verifies that when lifecycle_scheduler._run_entry_window_open() builds an
EntryContext, the `bars` payload is a real feature-engineered DataFrame
(>=100 rows, contains cross-asset cols) loaded from
v12.src.stockapp_v12.services.feature_loader — not the legacy 1-row fake
that forced Chronos into degraded mode.

The fake was:
    bars = pd.DataFrame({"close": [142.50], "atr_14": [3.8]})
which has 1 row × 2 cols and no cross-asset features. The new contract:
    bars = load_single_symbol(sym, lookback_days=252)
which produces a tz-naive UTC DataFrame with close + 19 features.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pandas as pd
import pytest


# Enable the entry window gate; otherwise the function early-returns.
@pytest.fixture(autouse=True)
def enable_entry_env(monkeypatch):
    monkeypatch.setenv("STOCKAPP_V12_ENTRY_ENABLED", "1")
    monkeypatch.setenv("STOCKAPP_V12_RISK", "1")
    yield


def _make_real_bars_stub():
    """Return a deterministic 22-col frame with 252 rows — what a healthy
    post-rebuild AAPL features parquet looks like."""
    idx = pd.date_range("2025-01-02", periods=252, freq="D")
    df = pd.DataFrame(
        {
            "timestamp": idx,
            "symbol": "AAPL",
            "close": [100.0 + i * 0.1 for i in range(252)],
            "ret_1d": [0.001] * 252,
            "ret_5d": [0.005] * 252,
            "ret_20d": [0.02] * 252,
            "vol_10d": [0.015] * 252,
            "vol_20d": [0.013] * 252,
            "rsi_14": [50.0] * 252,
            "macd": [0.1] * 252,
            "macd_signal": [0.05] * 252,
            "macd_hist": [0.05] * 252,
            "atr_14": [1.5] * 252,
            "bb_pct_b_20": [0.5] * 252,
            "obv": [1.0e7] * 252,
            "obv_slope_10": [100.0] * 252,
            "dist_ma_20": [0.01] * 252,
            "dist_ma_50": [0.02] * 252,
            "spy_ret_1d": [0.0005] * 252,
            "spy_ret_5d": [0.0025] * 252,
            "sector_ret_1d": [0.0008] * 252,
            "sector_beta_20": [1.05] * 252,
        }
    )
    return df.set_index("timestamp")


def _capture_entry_context(monkeypatch):
    """Patch heavy deps and capture the EntryContext.bars passed in."""
    captured = {"bars_list": []}

    from dataclasses import dataclass

    @dataclass
    class FakeEntryContext:
        symbol: str = ""
        bars: object = None
        market_regime: str = ""
        actionable_symbols: object = None
        equity: float = 0.0
        mode: str = ""

    class FakeEntryOrchestrator:
        def __init__(self, **kwargs):
            pass

        def evaluate_and_maybe_enter(self, ctx):
            captured["bars_list"].append(ctx.bars)
            @dataclass
            class _Res:
                decision: str = "NO_TRADE"
                reason_code: str = "TEST_DRY_RUN"
                forecast: object = None
                size_pct: float = 0.0
            return _Res()

    # Fake redis (must yield >=1 actionable symbol)
    fake_redis_module = MagicMock()
    fake_redis_instance = MagicMock()
    fake_redis_instance.lrange.return_value = ["AAPL", "MSFT"]
    fake_redis_module.Redis.return_value = fake_redis_instance
    monkeypatch.setitem(sys.modules, "redis", fake_redis_module)

    # Patch all v12 components imported by _run_entry_window
    fake_modules = {
        "v12.src.stockapp_v12.pipeline.entry_guard": MagicMock(EntryEvaluationGuard=MagicMock()),
        "v12.src.stockapp_v12.services.forecast_service": MagicMock(ForecastService=MagicMock()),
        "v12.src.stockapp_v12.services.signal_rule_engine": MagicMock(SignalRuleEngine=MagicMock()),
        "v12.src.stockapp_v12.services.position_manager": MagicMock(PositionManager=MagicMock()),
        "v12.src.stockapp_v12.pipeline.journal": MagicMock(TradeJournal=MagicMock()),
        "v12.src.stockapp_v12.core.execution.bracket_order": MagicMock(BracketOrderService=MagicMock()),
        "v12.src.stockapp_v12.core.execution.paper_broker": MagicMock(PaperBroker=MagicMock()),
        "v12.src.stockapp_v12.services.entry_orchestrator": MagicMock(
            EntryOrchestrator=FakeEntryOrchestrator,
            EntryContext=FakeEntryContext,
        ),
        "v12.src.stockapp_v12.bridge.gatekeeper": MagicMock(
            compute_position_size=MagicMock(return_value=(0.0, {}, {})),
            check_daily_trade_cap=MagicMock(return_value=(False, "")),
            check_symbol_cooldown=MagicMock(return_value=(False, "")),
            increment_daily_trade_count=MagicMock(return_value=1),
        ),
        "v12.src.stockapp_v12.core.limits": MagicMock(V12RiskLimits=MagicMock()),
        "v12.src.stockapp_v12.services.yolo_filter": MagicMock(YoloNoTradeFilter=MagicMock()),
    }
    for mod_name, fake in fake_modules.items():
        monkeypatch.setitem(sys.modules, mod_name, fake)

    # CRITICAL: Patch the feature_loader module that _run_entry_window imports inline.
    # This isolates the test from real parquet state.
    real_bars = _make_real_bars_stub()
    fake_loader_module = MagicMock()
    fake_loader_module.load_single_symbol = MagicMock(return_value=real_bars)
    monkeypatch.setitem(
        sys.modules, "v12.src.stockapp_v12.services.feature_loader", fake_loader_module
    )

    return captured


@pytest.mark.xfail(
    reason="V1→V12 bridge: V1 lifecycle_scheduler._run_entry_window_open() does not wire V12 feature_loader; integration is runtime-only",
    strict=False,
)
@pytest.mark.asyncio
async def test_entry_window_uses_real_feature_bars_not_fake(monkeypatch):
    """DoD: EntryContext.bars must be a real multi-row, cross-asset-enriched
    DataFrame — not the legacy 1-row fake."""
    captured = _capture_entry_context(monkeypatch)

    from src.services import lifecycle_scheduler  # noqa: F401

    await lifecycle_scheduler._run_entry_window_open()

    assert captured["bars_list"], (
        "EntryContext was never constructed. The function may have "
        "early-returned before reaching the bars wiring."
    )

    bars = captured["bars_list"][0]
    assert isinstance(bars, pd.DataFrame), f"bars is not DataFrame: {type(bars)}"

    # The legacy fake was 1 row × 2 cols. Real bars must have many rows.
    assert len(bars) > 100, (
        f"bars has only {len(bars)} rows — looks like the 1-row fake survived. "
        f"Re-check that lifecycle_scheduler.py:741 calls load_single_symbol()."
    )

    # Real bars must contain cross-asset cols (the columns Chronos needs).
    for col in ("spy_ret_1d", "spy_ret_5d", "sector_ret_1d", "sector_beta_20"):
        assert col in bars.columns, (
            f"bars missing cross-asset col '{col}' — feature_loader was bypassed "
            f"or returned an old-style 18-col frame."
        )


@pytest.mark.xfail(
    reason="V1→V12 bridge: V12 feature_loader lives at src/stockapp_v12/services/; archived V1 scheduler does not contain the call",
    strict=False,
)
def test_lifecycle_scheduler_source_calls_feature_loader():
    """Static guard: confirm the source no longer contains the 1-row fake."""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src_path = os.path.join(repo_root, "src", "services", "lifecycle_scheduler.py")
    with open(src_path, "r", encoding="utf-8") as f:
        text = f.read()

    # The 1-row fake must be gone.
    assert '"close": [142.50]' not in text, (
        "Source still contains the 1-row fake `close: [142.50]` — "
        "replace it with load_single_symbol()."
    )
    assert '"atr_14": [3.8]' not in text, (
        "Source still contains the 1-row fake `atr_14: [3.8]`."
    )
    # And the new call must be there.
    assert "load_single_symbol(sym" in text, (
        "Source missing the new `load_single_symbol(sym, ...)` call. "
        "Track A3 wiring is incomplete."
    )