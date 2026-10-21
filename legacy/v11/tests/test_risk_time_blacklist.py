"""
tests/test_risk_time_blacklist.py — Unit tests for tail protection (尾盘保护).

Verifies that _check_time_blacklist_softened:
- Blocks BUY/open during 15:40-16:00 closing window
- Allows SELL/close during 15:40-16:00
- Blocks all sides during 9:30-9:40 opening window (regime-dependent)
"""

import json
from datetime import time as dtime
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture()
def gatekeeper(fake_redis):
    """HardRiskGatekeeper wired to fakeredis."""
    from src.core.risk_controller import HardRiskGatekeeper

    gk = HardRiskGatekeeper.__new__(HardRiskGatekeeper)
    gk.r = fake_redis
    gk.initial_capital = 100000.0
    gk._lock = __import__("threading").Lock()
    gk.blacklist_symbols = set()
    gk.blacklist_reasons = {}
    gk.blacklist_metadata = {}
    gk.blacklist_expires = {}
    gk.kelly_fraction_override = None
    gk.min_confidence_override = None
    gk.risk_scaling_override = None
    gk.market_regime = "NEUTRAL"
    gk.rejection_cooldowns = {}
    gk.current_equity = 100000.0
    gk.current_pnl = 0.0
    gk._state_updated_at = 0.0
    gk.secret_key = "test-secret"
    gk.token_version = "v1"
    return gk


def _set_regime(redis_client, regime="NEUTRAL"):
    redis_client.set("market_regime:current", json.dumps({
        "regime": regime,
        "market_regime": regime,
    }))


def _mock_time(hour, minute):
    """Return a mock for _now_est() that returns a fixed time."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    EST = ZoneInfo("America/New_York")
    return datetime(2026, 3, 30, hour, minute, tzinfo=EST)


# ── Closing window (15:40-16:00): side-aware ────────────────────────────

class TestClosingWindow:
    """15:40-16:00 should block new entries only, allow exits."""

    @patch("src.core.risk_controller._now_est", return_value=_mock_time(15, 45))
    def test_buy_blocked_at_1545(self, mock_now, gatekeeper):
        _set_regime(gatekeeper.r, "BULL_LONG")
        blocked, reason = gatekeeper._check_time_blacklist_softened(0.85, side="BUY")
        assert blocked is True
        assert "Closing Blacklist" in reason

    @patch("src.core.risk_controller._now_est", return_value=_mock_time(15, 45))
    def test_open_blocked_at_1545(self, mock_now, gatekeeper):
        _set_regime(gatekeeper.r, "BULL_LONG")
        blocked, reason = gatekeeper._check_time_blacklist_softened(0.85, side="open")
        assert blocked is True
        assert "Closing Blacklist" in reason

    @patch("src.core.risk_controller._now_est", return_value=_mock_time(15, 45))
    def test_sell_allowed_at_1545(self, mock_now, gatekeeper):
        _set_regime(gatekeeper.r, "BULL_LONG")
        blocked, reason = gatekeeper._check_time_blacklist_softened(0.85, side="SELL")
        assert blocked is False
        assert reason == ""

    @patch("src.core.risk_controller._now_est", return_value=_mock_time(15, 45))
    def test_close_allowed_at_1545(self, mock_now, gatekeeper):
        _set_regime(gatekeeper.r, "BULL_LONG")
        blocked, reason = gatekeeper._check_time_blacklist_softened(0.85, side="close")
        assert blocked is False

    @patch("src.core.risk_controller._now_est", return_value=_mock_time(15, 59))
    def test_buy_blocked_at_1559(self, mock_now, gatekeeper):
        _set_regime(gatekeeper.r, "NEUTRAL")
        blocked, reason = gatekeeper._check_time_blacklist_softened(0.9, side="BUY")
        assert blocked is True

    @patch("src.core.risk_controller._now_est", return_value=_mock_time(15, 59))
    def test_sell_allowed_at_1559(self, mock_now, gatekeeper):
        _set_regime(gatekeeper.r, "NEUTRAL")
        blocked, reason = gatekeeper._check_time_blacklist_softened(0.9, side="SELL")
        assert blocked is False


# ── Opening window (9:30-9:40): regime-dependent ────────────────────────

class TestOpeningWindow:

    @patch("src.core.risk_controller._now_est", return_value=_mock_time(9, 35))
    def test_panic_blocks_regardless_of_side(self, mock_now, gatekeeper):
        _set_regime(gatekeeper.r, "PANIC")
        blocked, _ = gatekeeper._check_time_blacklist_softened(0.9, side="BUY")
        assert blocked is True

    @patch("src.core.risk_controller._now_est", return_value=_mock_time(9, 35))
    def test_bear_long_blocks(self, mock_now, gatekeeper):
        _set_regime(gatekeeper.r, "BEAR_LONG")
        blocked, _ = gatekeeper._check_time_blacklist_softened(0.85, side="BUY")
        assert blocked is True

    @patch("src.core.risk_controller._now_est", return_value=_mock_time(9, 35))
    def test_bull_low_confidence_blocks(self, mock_now, gatekeeper):
        _set_regime(gatekeeper.r, "BULL_LONG")
        blocked, _ = gatekeeper._check_time_blacklist_softened(0.6, side="BUY")
        assert blocked is True

    @patch("src.core.risk_controller._now_est", return_value=_mock_time(9, 35))
    def test_bull_high_confidence_passes(self, mock_now, gatekeeper):
        _set_regime(gatekeeper.r, "BULL_LONG")
        blocked, _ = gatekeeper._check_time_blacklist_softened(0.8, side="BUY")
        assert blocked is False


# ── Normal hours: no blocking ────────────────────────────────────────────

class TestNormalHours:

    @patch("src.core.risk_controller._now_est", return_value=_mock_time(10, 30))
    def test_no_block_during_normal_hours(self, mock_now, gatekeeper):
        _set_regime(gatekeeper.r, "NEUTRAL")
        blocked, reason = gatekeeper._check_time_blacklist_softened(0.5, side="BUY")
        assert blocked is False
        assert reason == ""
