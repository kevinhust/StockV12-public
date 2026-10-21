"""Task 8 — Notifier injection into EntryOrchestrator + stages.

Proves:
1. stages.py no longer imports services._notifier_compat / notification_service
2. EntryOrchestrator accepts optional notifier and defaults to compat shim
3. gatekeeper / bracket stages call orch.notifier methods
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from stockapp_v12.core.execution.bracket_order import BracketResult
from stockapp_v12.pipeline import stages
from stockapp_v12.pipeline.entry_guard import EntryEvaluationGuard
from stockapp_v12.pipeline.journal import TradeJournal
from stockapp_v12.pipeline.types import EntryContext
from stockapp_v12.services.entry_orchestrator import EntryOrchestrator


class RecordingNotifier:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def notify_order_intent(self, **kwargs):
        self.calls.append(("intent", kwargs))

    def notify_order_submitted(self, **kwargs):
        self.calls.append(("submitted", kwargs))

    def __getattr__(self, name):
        return lambda **kwargs: None


def test_stages_source_has_no_notifier_compat_import():
    src = inspect.getsource(stages)
    assert "services._notifier_compat" not in src
    assert "notification_service" not in src


def test_entry_orchestrator_defaults_to_compat_notifier():
    from stockapp_v12.services._notifier_compat import notifier as default_notifier

    orch = EntryOrchestrator(
        entry_guard=MagicMock(),
        forecast_service=MagicMock(),
        sr_engine=MagicMock(),
        gatekeeper_fn=MagicMock(),
        bracket_service=MagicMock(),
        position_manager=MagicMock(),
        journal=MagicMock(),
    )
    assert orch.notifier is default_notifier


def test_entry_orchestrator_accepts_injected_notifier():
    rec = RecordingNotifier()
    orch = EntryOrchestrator(
        entry_guard=MagicMock(),
        forecast_service=MagicMock(),
        sr_engine=MagicMock(),
        gatekeeper_fn=MagicMock(),
        bracket_service=MagicMock(),
        position_manager=MagicMock(),
        journal=MagicMock(),
        notifier=rec,
    )
    assert orch.notifier is rec


def test_stage_5c_uses_orch_notifier_for_order_intent(fake_redis):
    rec = RecordingNotifier()
    journal = TradeJournal(fake_redis)
    orch = SimpleNamespace(
        gatekeeper=MagicMock(
            return_value=(True, "", {"size_pct": 0.025, "risk_amount": 1000})
        ),
        journal=journal,
        notifier=rec,
    )
    ctx = EntryContext(
        symbol="NVDA",
        bars=pd.DataFrame({"close": [100.0], "atr_14": [5.0]}),
        market_regime="BULL_LONG",
        actionable_symbols={"NVDA"},
        equity=100_000.0,
        mode="paper_ibkr",
    )
    early, size_pct, _meta = stages.run_stage_5c_gatekeeper(
        orch,
        "NVDA",
        ctx,
        current_price=100.0,
        atr_14=5.0,
        atr_pct=0.05,
        analyst=None,
        forecast=None,
        sr=None,
        yolo_decision=None,
        confidence=0.82,
    )
    assert early is None
    assert size_pct == 0.025
    assert any(c[0] == "intent" for c in rec.calls)
    intent = next(c for c in rec.calls if c[0] == "intent")[1]
    assert intent["symbol"] == "NVDA"
    assert intent["side"] == "BUY"
    assert intent["size_pct"] == 0.025


def test_stage_6_uses_orch_notifier_for_order_submitted():
    rec = RecordingNotifier()
    bracket = MagicMock()
    bracket.submit_entry_bracket.return_value = BracketResult(
        parent_order_id="p_123",
        sl_order_id="s_456",
        sl_price=94.0,
        submitted_qty=25,
        meta={"tier": "high_vol"},
    )
    orch = SimpleNamespace(bracket=bracket, notifier=rec, journal=MagicMock())
    ctx = EntryContext(
        symbol="NVDA",
        bars=pd.DataFrame({"close": [100.0], "atr_14": [5.0]}),
        market_regime="BULL_LONG",
        actionable_symbols={"NVDA"},
        equity=100_000.0,
        mode="paper_ibkr",
    )
    bracket_res, qty, early = stages.run_stage_6_bracket(
        orch,
        "NVDA",
        ctx,
        current_price=100.0,
        atr_14=5.0,
        atr_pct=0.05,
        size_pct=0.025,
        forecast=None,
        sr=None,
        analyst=None,
        yolo_decision=None,
    )
    assert early is None
    assert bracket_res is not None
    assert qty >= 1
    assert any(c[0] == "submitted" for c in rec.calls)
    submitted = next(c for c in rec.calls if c[0] == "submitted")[1]
    assert submitted["symbol"] == "NVDA"
    assert submitted["parent_order_id"] == "p_123"
