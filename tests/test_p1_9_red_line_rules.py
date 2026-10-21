"""Regression tests for P1-9 (council 2026-07-09): R-01/R-02/R-09 runtime
enforcement.

``red_line_thresholds()`` was defined but never called. These tests verify
the three newly implemented check functions:

  - ``check_daily_loss_limit`` (R-01): blocks entries when today's realized
    loss exceeds the daily limit. Fail-closed on Redis errors.
  - ``check_drawdown_limit`` (R-02): blocks entries when current drawdown
    from peak exceeds the max drawdown. Fail-closed on Redis errors.
  - ``check_entry_window`` (R-09): pure clock check — only 09:35–15:30 ET
    on weekdays. No Redis needed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo


from stockapp_v12.bridge.gatekeeper import (
    check_daily_loss_limit,
    check_drawdown_limit,
    check_entry_window,
)
from stockapp_v12.core.limits import V12RiskLimits

EST = ZoneInfo("America/New_York")


# ─── R-01: daily loss limit ───────────────────────────────────────────────


class TestR01DailyLossLimit:
    def test_no_pnl_key_allows(self):
        """No losses recorded today → allow."""
        r = MagicMock()
        r.get.return_value = None
        blocked, reason = check_daily_loss_limit(r, V12RiskLimits())
        assert blocked is False

    def test_small_loss_allows(self):
        """Loss within limit → allow."""
        r = MagicMock()
        r.get.return_value = "-500.0"  # -$500 on $100k = -0.5% (limit is 2%)
        blocked, reason = check_daily_loss_limit(r, V12RiskLimits(), equity=100_000.0)
        assert blocked is False

    def test_loss_at_limit_blocks(self):
        """Loss at exactly the limit (2% of $100k = -$2000) → block."""
        r = MagicMock()
        r.get.return_value = "-2000.0"
        blocked, reason = check_daily_loss_limit(r, V12RiskLimits(), equity=100_000.0)
        assert blocked is True
        assert "R-01" in reason
        assert "2000" in reason or "2.0%" in reason

    def test_loss_exceeding_limit_blocks(self):
        """Loss exceeds limit → block."""
        r = MagicMock()
        r.get.return_value = "-3500.0"  # -3.5%
        blocked, reason = check_daily_loss_limit(r, V12RiskLimits(), equity=100_000.0)
        assert blocked is True
        assert "R-01" in reason

    def test_profit_allows(self):
        """Positive P&L → allow."""
        r = MagicMock()
        r.get.return_value = "1500.0"
        blocked, reason = check_daily_loss_limit(r, V12RiskLimits())
        assert blocked is False

    def test_redis_error_fails_closed(self, caplog):
        """Redis error → fail-closed (block)."""
        caplog.set_level(logging.ERROR)
        r = MagicMock()
        r.get.side_effect = ConnectionError("redis down")
        blocked, reason = check_daily_loss_limit(r, V12RiskLimits())
        assert blocked is True
        assert "R-01" in reason
        assert "failing closed" in reason.lower()


# ─── R-02: drawdown limit ─────────────────────────────────────────────────


class TestR02DrawdownLimit:
    def test_no_equity_keys_allows(self):
        """Cold start (no equity tracking) → allow."""
        r = MagicMock()
        r.get.return_value = None
        blocked, reason = check_drawdown_limit(r, V12RiskLimits())
        assert blocked is False

    def test_small_drawdown_allows(self):
        """Drawdown within limit (8%) → allow."""
        r = MagicMock()
        r.get.side_effect = lambda k: {
            "risk:peak_equity": "105000.0",
            "risk:current_equity": "102000.0",
        }.get(k)
        blocked, reason = check_drawdown_limit(r, V12RiskLimits())
        # DD = (105k - 102k) / 105k = 2.86% < 8% → allow
        assert blocked is False

    def test_drawdown_exceeds_limit_blocks(self):
        """Drawdown exceeds 8% → block."""
        r = MagicMock()
        r.get.side_effect = lambda k: {
            "risk:peak_equity": "110000.0",
            "risk:current_equity": "100000.0",
        }.get(k)
        blocked, reason = check_drawdown_limit(r, V12RiskLimits())
        # DD = (110k - 100k) / 110k = 9.09% > 8% → block
        assert blocked is True
        assert "R-02" in reason

    def test_drawdown_at_limit_blocks(self):
        """Drawdown at exactly the limit → block (>= comparison)."""
        r = MagicMock()
        r.get.side_effect = lambda k: {
            "risk:peak_equity": "100000.0",
            "risk:current_equity": "92000.0",
        }.get(k)
        blocked, reason = check_drawdown_limit(r, V12RiskLimits())
        # DD = (100k - 92k) / 100k = 8.0% >= 8% → block
        assert blocked is True
        assert "R-02" in reason

    def test_redis_error_fails_closed(self, caplog):
        """Redis error → fail-closed (block)."""
        caplog.set_level(logging.ERROR)
        r = MagicMock()
        r.get.side_effect = ConnectionError("redis down")
        blocked, reason = check_drawdown_limit(r, V12RiskLimits())
        assert blocked is True
        assert "R-02" in reason
        assert "failing closed" in reason.lower()

    def test_zero_peak_equity_allows(self):
        """Peak equity is 0 (invalid) → allow (don't divide by zero)."""
        r = MagicMock()
        r.get.side_effect = lambda k: {
            "risk:peak_equity": "0",
            "risk:current_equity": "0",
        }.get(k)
        blocked, reason = check_drawdown_limit(r, V12RiskLimits())
        assert blocked is False


# ─── R-09: entry window ───────────────────────────────────────────────────


class TestR09EntryWindow:
    def test_midday_weekday_allows(self):
        """Wednesday 12:00 ET → inside window → allow."""
        wed_noon = datetime(2026, 7, 8, 12, 0, tzinfo=EST)  # Wednesday
        blocked, reason = check_entry_window(wed_noon)
        assert blocked is False
        assert reason == ""

    def test_market_open_allows(self):
        """Monday 09:35 ET → exactly at open → allow."""
        mon_open = datetime(2026, 7, 6, 9, 35, tzinfo=EST)  # Monday
        blocked, reason = check_entry_window(mon_open)
        assert blocked is False

    def test_market_close_allows(self):
        """Friday 15:30 ET → exactly at close → allow."""
        fri_close = datetime(2026, 7, 10, 15, 30, tzinfo=EST)  # Friday
        blocked, reason = check_entry_window(fri_close)
        assert blocked is False

    def test_before_open_blocks(self):
        """Monday 09:34 ET → 1 minute before open → block."""
        mon_early = datetime(2026, 7, 6, 9, 34, tzinfo=EST)
        blocked, reason = check_entry_window(mon_early)
        assert blocked is True
        assert "R-09" in reason
        assert "Before" in reason

    def test_after_close_blocks(self):
        """Monday 15:31 ET → 1 minute after close → block."""
        mon_late = datetime(2026, 7, 6, 15, 31, tzinfo=EST)
        blocked, reason = check_entry_window(mon_late)
        assert blocked is True
        assert "R-09" in reason
        assert "After" in reason

    def test_weekend_blocks(self):
        """Saturday → block regardless of time."""
        sat = datetime(2026, 7, 11, 12, 0, tzinfo=EST)  # Saturday
        blocked, reason = check_entry_window(sat)
        assert blocked is True
        assert "R-09" in reason
        assert "weekend" in reason.lower()

    def test_sunday_blocks(self):
        """Sunday → block."""
        sun = datetime(2026, 7, 12, 10, 0, tzinfo=EST)
        blocked, reason = check_entry_window(sun)
        assert blocked is True
        assert "weekend" in reason.lower()

    def test_all_weekdays_in_window_pass(self):
        """Mon-Fri at 12:00 → all pass."""
        for day in range(6, 11):  # July 6-10, 2026 = Mon-Fri
            noon = datetime(2026, 7, day, 12, 0, tzinfo=EST)
            blocked, _ = check_entry_window(noon)
            assert blocked is False, f"Day {day} should be allowed"


# ─── Integration: all three checks together ───────────────────────────────


def test_all_three_checks_exported_from_bridge():
    """P1-9: the three new functions must be exported from the bridge package."""
    from stockapp_v12.bridge import (
        check_daily_loss_limit,
        check_drawdown_limit,
        check_entry_window,
    )
    assert callable(check_daily_loss_limit)
    assert callable(check_drawdown_limit)
    assert callable(check_entry_window)
