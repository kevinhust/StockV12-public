"""Regression tests for P1-6 (council 2026-07-09): AuditLedger must flush +
fsync after every write so a crash doesn't lose buffered records.

The docstring claimed "atomicity via single write+flush" but the code only
relied on the `with` block's implicit close-flush — which buffers in the OS
page cache.

Post-fix: explicit `fh.flush()` + `os.fsync(fh.fileno())` after every write.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


from stockapp_v12.audit.audit_ledger import AuditLedger


def test_record_writes_and_returns_true(tmp_path):
    """Happy path: record persists to the NDJSON file."""
    ledger = AuditLedger(root=tmp_path)
    ok = ledger.record(
        symbol="AAPL", mode="paper", stage="entry",
        decision="BUY", reason_code="FILLED", reason_detail="test",
    )
    assert ok is True
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "AAPL" in content
    assert "BUY" in content


def test_record_calls_fsync(tmp_path):
    """P1-6: os.fsync must be called after every write."""
    ledger = AuditLedger(root=tmp_path)
    with patch("stockapp_v12.audit.audit_ledger.os.fsync") as mock_fsync:
        ledger.record(
            symbol="AAPL", mode="paper", stage="entry",
            decision="BUY", reason_code="FILLED", reason_detail="test",
        )
        mock_fsync.assert_called_once()


def test_record_calls_flush_before_fsync(tmp_path):
    """P1-6: fh.flush() must happen before fsync (Python buffer → OS, then OS → disk)."""
    ledger = AuditLedger(root=tmp_path)
    call_order = []
    real_open = Path.open

    def tracking_open(self, *args, **kwargs):
        fh = real_open(self, *args, **kwargs)
        real_flush = fh.flush
        real_write = fh.write

        def tracking_write(data):
            call_order.append("write")
            return real_write(data)

        def tracking_flush():
            call_order.append("flush")
            return real_flush()

        fh.write = tracking_write
        fh.flush = tracking_flush
        return fh

    with patch("pathlib.Path.open", tracking_open), \
         patch("stockapp_v12.audit.audit_ledger.os.fsync", side_effect=lambda fd: call_order.append("fsync")):
        ledger.record(
            symbol="MSFT", mode="paper", stage="entry",
            decision="DENY", reason_code="R-05", reason_detail="cap",
        )

    # The `with` block's implicit close also flushes, so we check the
    # critical sequence appears in the first 3 positions.
    assert call_order[:3] == ["write", "flush", "fsync"], \
        f"Expected [write, flush, fsync] at start but got {call_order[:3]}"


def test_multiple_records_each_fsync(tmp_path):
    """P1-6: every record triggers its own fsync — no batching."""
    ledger = AuditLedger(root=tmp_path)
    with patch("stockapp_v12.audit.audit_ledger.os.fsync") as mock_fsync:
        for i in range(5):
            ledger.record(
                symbol=f"S{i}", mode="paper", stage="entry",
                decision="PASS", reason_code="SR-01", reason_detail="test",
            )
        assert mock_fsync.call_count == 5


def test_disabled_ledger_does_not_write(tmp_path):
    """enabled=False → no file writes, returns False."""
    ledger = AuditLedger(root=tmp_path, enabled=False)
    ok = ledger.record(
        symbol="AAPL", mode="paper", stage="entry",
        decision="BUY", reason_code="FILLED", reason_detail="test",
    )
    assert ok is False
    assert list(tmp_path.glob("*.jsonl")) == []
