"""Regression tests for P1-7 (council 2026-07-09): TradeJournal.append
must be fail-closed for post-execution records (ALLOW, BUY) to prevent
INV-5 violations (every decision silently drops from audit trail on
Redis errors).

Strategy:
  - ``append(strict=True)`` (default): raises ``JournalWriteError``
  - ``append(strict=False)``: logs + swallows (for PASS/DENY)
  - ``log_allow`` / ``log_buy``: strict=True (post-execution — MUST be audited)
  - ``log_pass`` / ``log_deny``: strict=False (pre-execution — trade isn't happening)
  - stages.py ``run_stage_5c_gatekeeper``: catches JournalWriteError → DENY
  - stages.py ``run_stage_7_register``: catches JournalWriteError → CRITICAL log + BUY
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from stockapp_v12.pipeline.journal import TradeJournal, JournalWriteError


# ─── append strict mode ───────────────────────────────────────────────────


def test_append_strict_raises_on_redis_error():
    """P1-7: append(strict=True) raises JournalWriteError on Redis failure."""
    r = MagicMock()
    r.rpush.side_effect = ConnectionError("redis down")
    journal = TradeJournal(r)

    with pytest.raises(JournalWriteError, match="Journal append failed"):
        journal.append(
            "AAPL", mode="paper_ibkr", stage="entry",
            decision="BUY", reason_code="FILLED", reason_detail="test",
        )


def test_append_lenient_swallows_on_redis_error(caplog):
    """P1-7: append(strict=False) logs but does NOT raise."""
    caplog.set_level(logging.ERROR)
    r = MagicMock()
    r.rpush.side_effect = ConnectionError("redis down")
    journal = TradeJournal(r)

    # Should NOT raise
    journal.append(
        "AAPL", mode="paper_ibkr", stage="entry",
        decision="PASS", reason_code="SR-01", reason_detail="test",
        strict=False,
    )
    assert any("TradeJournal append failed" in r.message for r in caplog.records)


def test_append_happy_path_does_not_raise():
    """Normal append succeeds without exception."""
    r = MagicMock()
    journal = TradeJournal(r)
    journal.append(
        "AAPL", mode="paper_ibkr", stage="entry",
        decision="BUY", reason_code="FILLED", reason_detail="test",
    )
    r.rpush.assert_called_once()


# ─── log_allow / log_buy are strict ───────────────────────────────────────


def test_log_allow_raises_on_redis_error():
    """P1-7: log_allow uses strict=True (post-execution audit record)."""
    r = MagicMock()
    r.rpush.side_effect = ConnectionError("redis down")
    journal = TradeJournal(r)

    with pytest.raises(JournalWriteError):
        journal.log_allow("AAPL", "paper_ibkr", {"size_pct": 0.05})


def test_log_buy_raises_on_redis_error():
    """P1-7: log_buy uses strict=True (post-execution audit record)."""
    r = MagicMock()
    r.rpush.side_effect = ConnectionError("redis down")
    journal = TradeJournal(r)

    with pytest.raises(JournalWriteError):
        journal.log_buy(
            "AAPL", "paper_ibkr",
            parent_order_id="P1", sl_order_id="SL1",
            size_pct=0.05,
        )


# ─── log_pass / log_deny are lenient ──────────────────────────────────────


def test_log_pass_does_not_raise_on_redis_error(caplog):
    """P1-7: log_pass uses strict=False (pre-execution — trade isn't happening)."""
    caplog.set_level(logging.ERROR)
    r = MagicMock()
    r.rpush.side_effect = ConnectionError("redis down")
    journal = TradeJournal(r)

    # Should NOT raise — PASS is non-critical
    journal.log_pass("AAPL", "paper_ibkr", "SR-01", "low return")
    assert any("TradeJournal append failed" in r.message for r in caplog.records)


def test_log_deny_does_not_raise_on_redis_error(caplog):
    """P1-7: log_deny uses strict=False (pre-execution — trade was denied)."""
    caplog.set_level(logging.ERROR)
    r = MagicMock()
    r.rpush.side_effect = ConnectionError("redis down")
    journal = TradeJournal(r)

    # Should NOT raise — DENY is non-critical
    journal.log_deny("AAPL", "paper_ibkr", "R-05", "daily cap")
    assert any("TradeJournal append failed" in r.message for r in caplog.records)


# ─── stages.py integration: log_allow failure → DENY ──────────────────────


def test_stage_5c_log_allow_failure_returns_deny():
    """P1-7: if log_allow raises in run_stage_5c_gatekeeper, the stage
    must short-circuit to DENY with reason INV5_JOURNAL_FAIL — do NOT
    proceed to bracket submission.
    """
    from stockapp_v12.pipeline import stages
    from stockapp_v12.services.entry_orchestrator import EntryContext

    # Build mocks: gatekeeper approves, but journal.log_allow raises
    orch = MagicMock()
    orch.gatekeeper.return_value = (True, "", {"size_pct": 0.05})
    orch.journal.log_allow.side_effect = JournalWriteError("redis down")

    ctx = MagicMock(spec=EntryContext)
    ctx.mode = "paper_ibkr"
    ctx.market_regime = "BULL_LONG"

    analyst = MagicMock()
    analyst.to_dict.return_value = {}

    result, size_pct, risk_meta = stages.run_stage_5c_gatekeeper(
        orch, sym="AAPL", ctx=ctx,
        current_price=150.0, atr_14=2.0, atr_pct=0.013,
        analyst=analyst, forecast=MagicMock(), sr=MagicMock(),
        yolo_decision=MagicMock(), confidence=0.85,
    )

    # Must have returned a DENY result, not None
    assert result is not None
    assert result.decision == "DENY"
    assert result.reason_code == "INV5_JOURNAL_FAIL"


# ─── stages.py integration: log_buy failure → CRITICAL log + BUY ──────────


def test_stage_7_log_buy_failure_still_returns_buy(caplog):
    """P1-7: if log_buy raises in run_stage_7_register, the position is
    ALREADY registered — we can't undo it. Return BUY but log CRITICAL.
    """
    from stockapp_v12.pipeline import stages
    from stockapp_v12.services.entry_orchestrator import EntryContext

    caplog.set_level(logging.CRITICAL)

    orch = MagicMock()
    orch.pm = MagicMock()
    orch.journal.log_buy.side_effect = JournalWriteError("redis down")
    orch.daily_counter_fn = MagicMock(return_value=1)

    ctx = MagicMock(spec=EntryContext)
    ctx.mode = "paper_ibkr"
    ctx.market_regime = "BULL_LONG"

    bracket_res = MagicMock()
    bracket_res.parent_order_id = "P1"
    bracket_res.sl_order_id = "SL1"
    bracket_res.sl_price = 100.0

    analyst = MagicMock()
    analyst.thesis_tags = ["tag"]
    analyst.expected_holding_days = 5
    analyst.to_dict.return_value = {}

    result = stages.run_stage_7_register(
        orch, sym="AAPL", ctx=ctx,
        current_price=150.0, atr_14=2.0, atr_pct=0.013,
        bracket_res=bracket_res, qty=10, size_pct=0.05,
        forecast=MagicMock(), sr=MagicMock(rule_code="OK"),
        analyst=analyst, yolo_decision=MagicMock(),
    )

    # Position is live — must still return BUY
    assert result.decision == "BUY"
    # But the critical log must be present
    assert any(
        "INV-5" in r.message and "log_buy failed" in r.message
        for r in caplog.records
    )
