"""PaperBroker coverage test — hygiene-G.

PaperBroker is a 72-line in-memory BrokerAdapter used by
v12/scripts/backtest_v12_paper.py. It had 0 dedicated unit tests
(coverage 39%). This test pins the 4 public methods so coverage
rises to ~95% and a future refactor cannot silently break backtest
submission/fill semantics.
"""

from __future__ import annotations


from stockapp_v12.core.execution.paper_broker import PaperBroker


# ─── submit_bracket_order ───────────────────────────────────────────────────


def test_submit_bracket_returns_parent_and_sl_ids():
    """submit_bracket_order returns (parent_id, sl_id) tuple."""
    broker = PaperBroker()
    parent, sl = broker.submit_bracket_order(
        symbol="AAPL", side="BUY", qty=10,
        entry_price=150.0, stop_price=145.0, mode="paper_ibkr",
    )
    assert parent.startswith("paper_parent_AAPL_")
    assert sl.startswith("paper_sl_AAPL_")
    assert parent != sl


def test_submit_bracket_records_parent_as_filled_and_sl_as_open():
    """Parent is recorded as filled (paper mode), SL stays open."""
    broker = PaperBroker()
    parent, sl = broker.submit_bracket_order(
        symbol="MSFT", side="BUY", qty=5,
        entry_price=300.0, stop_price=290.0, mode="paper_ibkr",
    )
    assert broker.orders[parent]["status"] == "filled"
    assert broker.orders[sl]["status"] == "open"
    assert broker.orders[sl]["order_type"] == "stop"


def test_submit_bracket_uppercases_symbol_and_appends_submission():
    """Symbol is uppercased; submission is appended for tests/inspection."""
    broker = PaperBroker()
    broker.submit_bracket_order(
        symbol="aapl", side="buy", qty=10,
        entry_price=150.0, stop_price=145.0, mode="paper_ibkr",
    )
    assert len(broker.submissions) == 1
    assert broker.submissions[0]["symbol"] == "AAPL"
    assert broker.submissions[0]["side"] == "buy"  # not uppercased — caller passes canonical


# ─── get_order_status ───────────────────────────────────────────────────────


def test_get_order_status_known_id():
    """Known order_id returns the stored record."""
    broker = PaperBroker()
    parent, _ = broker.submit_bracket_order(
        symbol="AAPL", side="BUY", qty=10,
        entry_price=150.0, stop_price=145.0, mode="paper_ibkr",
    )
    status = broker.get_order_status(parent)
    assert status["status"] == "filled"
    assert status["parent_order_id"] == parent


def test_get_order_status_unknown_id_returns_unknown():
    """Unknown order_id returns sentinel {status: 'unknown', order_id: ...}."""
    broker = PaperBroker()
    status = broker.get_order_status("never_submitted_xyz")
    assert status["status"] == "unknown"
    assert status["order_id"] == "never_submitted_xyz"


# ─── cancel_order ───────────────────────────────────────────────────────────


def test_cancel_order_known_marks_cancelled_and_returns_true():
    """cancel_order on a known id flips status to 'cancelled' and returns True."""
    broker = PaperBroker()
    parent, _ = broker.submit_bracket_order(
        symbol="AAPL", side="BUY", qty=10,
        entry_price=150.0, stop_price=145.0, mode="paper_ibkr",
    )
    assert broker.cancel_order(parent) is True
    assert broker.orders[parent]["status"] == "cancelled"


def test_cancel_order_unknown_returns_false():
    """cancel_order on an unknown id returns False without raising."""
    broker = PaperBroker()
    assert broker.cancel_order("never_submitted_xyz") is False


# ─── reset ──────────────────────────────────────────────────────────────────


def test_reset_clears_submissions_and_orders():
    """reset() empties both submissions and orders dicts."""
    broker = PaperBroker()
    broker.submit_bracket_order(
        symbol="AAPL", side="BUY", qty=10,
        entry_price=150.0, stop_price=145.0, mode="paper_ibkr",
    )
    assert len(broker.submissions) >= 1
    assert len(broker.orders) >= 2
    broker.reset()
    assert broker.submissions == []
    assert broker.orders == {}