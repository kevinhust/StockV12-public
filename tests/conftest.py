from __future__ import annotations

import pytest
from unittest.mock import MagicMock

# V11 bridge: gatekeeper fixture loads HardRiskGatekeeper via legacy cage.
_V11_PATH_INJECTED = False


# K-cleanup (council 2026-07-09 tail): the simple "register a position with
# sensible defaults" helper previously lived in three test files
# (test_j1_close_atomicity, test_p2_exit_audit, test_p1_3_broker_reconcile).
# The signatures diverged only in the optional `regime` default and whether
# the caller wanted to set pending_action. Consolidated here as a single
# helper to keep the three test files honest about which kwargs they pass
# (tests that need regime override it explicitly; tests that need a
# pending_action post-mutation call this then mutate the record).
def register_test_position(pm, **kwargs):
    """Register a position with sensible defaults. Returns the PositionRecord.

    Defaults (override via kwargs):
        symbol="TEST", entry_price=100.00, qty=100, atr_at_entry=3.0,
        parent_order_id="p1", sl_order_id="s1", sl_price=90.00
    """
    defaults = dict(
        symbol="TEST",
        entry_price=100.00,
        qty=100,
        atr_at_entry=3.0,
        parent_order_id="p1",
        sl_order_id="s1",
        sl_price=90.00,
    )
    defaults.update(kwargs)
    pm.register(**defaults)
    return pm.get_position(defaults["symbol"])


def _ensure_v11_path():
    """Lazily inject V11 archive via Phase-4 legacy cage (idempotent)."""
    global _V11_PATH_INJECTED
    if _V11_PATH_INJECTED:
        return
    from stockapp_v12.core._v11_compat import ensure_v11_archive_on_path

    ensure_v11_archive_on_path()
    _V11_PATH_INJECTED = True


@pytest.fixture
def fake_redis():
    r = MagicMock()
    r.get.return_value = None
    r.exists.return_value = False
    r.ttl.return_value = -1
    r.sismember.return_value = False
    pipe = MagicMock()
    pipe.incr.return_value = pipe
    pipe.expire.return_value = pipe
    pipe.sadd.return_value = pipe
    pipe.execute.return_value = [1]
    r.pipeline.return_value = pipe
    r.smembers.return_value = set()
    r.srem.return_value = True
    r.set.return_value = True
    r.setex.return_value = True
    # P1-1: entry_guard now uses EVAL for atomic SADD+EXPIRE.
    # Default: SADD returns 1 (first caller — proceed).
    r.eval.return_value = 1
    return r


@pytest.fixture
def gatekeeper(fake_redis):
    from unittest.mock import patch

    from stockapp_v12.adapters.legacy_v11.risk import load_hard_risk_gatekeeper_class

    HardRiskGatekeeper = load_hard_risk_gatekeeper_class()
    if HardRiskGatekeeper is None:
        pytest.skip("V11 archive unavailable or STOCKAPP_DISABLE_V11_ARCHIVE=1")

    with patch("redis.Redis", return_value=fake_redis):
        gk = HardRiskGatekeeper(
            redis_host="localhost",
            redis_port=6379,
            initial_capital=100_000.0,
            secret_key="test-secret",
        )
    gk.use_v12_risk = True
    gk._state_updated_at = __import__("time").time()
    gk.current_equity = 100_000.0
    gk.current_pnl = 0.0
    gk._get_current_regime = MagicMock(return_value="BULL_LONG")
    gk._get_portfolio_metrics = MagicMock(
        return_value={"drawdown_pct": 0.0, "total_exposure_pct": 0.0}
    )
    gk._get_existing_positions = MagicMock(return_value=[])
    gk._check_correlation_risk = MagicMock(side_effect=lambda s, sz: (sz, {}))
    return gk
