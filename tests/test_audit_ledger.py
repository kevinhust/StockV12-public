"""Tests for AuditLedger (Phase A1)."""

import json
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from stockapp_v12.audit.audit_ledger import (
    SCHEMA_VERSION,
    AuditLedger,
    audit_wrapped_journal,
)
from stockapp_v12.pipeline.journal import TradeJournal

EST = ZoneInfo("America/New_York")


def _fixed_now(date_str="2026-07-07T12:00:00"):
    dt = datetime.fromisoformat(date_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=EST)
    return lambda: dt


def test_record_writes_ndjson(tmp_path):
    ledger = AuditLedger(root=tmp_path, now_provider=_fixed_now())
    assert ledger.record(
        symbol="NVDA",
        mode="paper_ibkr",
        stage="entry",
        decision="BUY",
        reason_code="OK",
        reason_detail="ok",
    )
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    content = files[0].read_text()
    lines = [l for l in content.split("\n") if l.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["schema_version"] == SCHEMA_VERSION
    assert rec["symbol"] == "NVDA"
    assert rec["decision"] == "BUY"


def test_record_appends_multiple(tmp_path):
    ledger = AuditLedger(root=tmp_path, now_provider=_fixed_now())
    for i in range(3):
        ledger.record(
            symbol="AAPL",
            mode="paper_ibkr",
            stage="entry",
            decision="PASS",
            reason_code=f"CODE-{i}",
            reason_detail=f"detail {i}",
        )
    files = list(tmp_path.iterdir())
    content = files[0].read_text()
    lines = [l for l in content.split("\n") if l.strip()]
    assert len(lines) == 3
    decoded = [json.loads(l) for l in lines]
    assert [d["reason_code"] for d in decoded] == ["CODE-0", "CODE-1", "CODE-2"]


def test_replay_returns_records_for_date(tmp_path):
    ledger = AuditLedger(root=tmp_path, now_provider=_fixed_now("2026-07-07T12:00:00"))
    ledger.record(symbol="NVDA", mode="paper_ibkr", stage="entry",
                  decision="BUY", reason_code="OK", reason_detail="")
    ledger.record(symbol="AAPL", mode="paper_ibkr", stage="entry",
                  decision="PASS", reason_code="SR-01", reason_detail="low ret")
    date_str = "2026-07-07"
    records = ledger.replay(date_str)
    assert len(records) == 2
    symbols = {r["symbol"] for r in records}
    assert symbols == {"NVDA", "AAPL"}


def test_replay_returns_empty_for_missing_date(tmp_path):
    ledger = AuditLedger(root=tmp_path, now_provider=_fixed_now())
    assert ledger.replay("1999-01-01") == []


def test_disabled_ledger_records_nothing(tmp_path):
    ledger = AuditLedger(root=tmp_path, enabled=False, now_provider=_fixed_now())
    assert ledger.record(symbol="NVDA", mode="x", stage="x",
                         decision="x", reason_code="x", reason_detail="x") is False
    assert list(tmp_path.iterdir()) == []


def test_corrupt_line_does_not_break_replay(tmp_path):
    ledger = AuditLedger(root=tmp_path, now_provider=_fixed_now())
    ledger.record(symbol="NVDA", mode="x", stage="x",
                  decision="OK", reason_code="x", reason_detail="")
    file_path = list(tmp_path.iterdir())[0]
    with file_path.open("a") as fh:
        fh.write("not-json\n")
    records = ledger.replay("2026-07-07")
    assert len(records) == 1


def test_audit_wrapped_journal_mirrors_calls(tmp_path):
    """The proxy should mirror every TradeJournal write to disk."""
    ledger = AuditLedger(root=tmp_path, now_provider=_fixed_now())
    fake_redis = MagicMock()
    fake_redis.get.return_value = "[]"  # simulate existing journal as empty list
    fake_redis.set.return_value = True
    journal = TradeJournal(fake_redis)
    wrapped = audit_wrapped_journal(journal, ledger)

    wrapped.log_pass("NVDA", "paper_ibkr", "SR-01", "low return")
    wrapped.log_deny("AAPL", "paper_ibkr", "R-05", "daily cap",
                     requested_size_pct=0.05)
    wrapped.log_buy("MSFT", "paper_ibkr",
                    parent_order_id="p1", sl_order_id="s1", size_pct=0.03)
    wrapped.log_exit("GOOG", "paper_ibkr", exit_reason="sl", pnl_pct=-0.5)

    records = ledger.replay("2026-07-07")
    assert len(records) == 4
    decisions = [r["decision"] for r in records]
    assert decisions == ["PASS", "DENY", "BUY", "CLOSED"]
    symbols = [r["symbol"] for r in records]
    assert symbols == ["NVDA", "AAPL", "MSFT", "GOOG"]


def test_make_directory_recursively(tmp_path):
    nested = tmp_path / "deep" / "nest" / "audit"
    AuditLedger(root=nested)
    assert nested.is_dir()
