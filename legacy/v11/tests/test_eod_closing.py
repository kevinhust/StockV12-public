"""
Unit tests for EOD Closing tiered decision logic.

Uses fakeredis + mocked DB to test classification and Redis coordination
without external dependencies.
"""

import json
import os
import pytest
from unittest.mock import MagicMock, patch

# Ensure project root on path
import sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Fakeredis setup — must be imported before the module under test
# ---------------------------------------------------------------------------

try:
    import fakeredis
    HAS_FAKEREDIS = True
except ImportError:
    HAS_FAKEREDIS = False


@pytest.fixture
def fake_redis():
    """Provide a fakeredis instance with standard test data."""
    if not HAS_FAKEREDIS:
        pytest.skip("fakeredis not installed")
    r = fakeredis.FakeRedis(decode_responses=True)

    # Default regime
    r.set("market_regime:current", json.dumps({"regime": "NEUTRAL"}))
    yield r


def _make_service(fake_redis, regime="NEUTRAL"):
    """Build an EODClosingService with mocked dependencies."""
    from scripts.eod_closing import EODClosingService

    # Patch get_redis_client to return our fake
    with patch("scripts.eod_closing.get_redis_client", return_value=fake_redis):
        with patch("scripts.eod_closing.RedpandaEventBus"):
            with patch("scripts.eod_closing.ExecutionRouter"):
                with patch("scripts.eod_closing.ConfigManager"):
                    svc = EODClosingService.__new__(EODClosingService)
                    svc.dry_run = True
                    svc.redis = fake_redis
                    svc.db_url = "postgresql://fake"
                    svc.eod_config = {
                        "exempt_enabled": True,
                        "exempt_regimes": ["BULL_LONG", "NEUTRAL"],
                        "exempt_profit_pct": 0.08,
                        "exempt_score_threshold": 0.75,
                        "exempt_sentiment_threshold": 0.70,
                        "exempt_leave_ratio": 0.30,
                        "exempt_max_per_stock_pct": 0.05,
                        "exempt_max_total_pct": 0.20,
                        "exempt_max_symbols_per_day": 5,
                        "exempt_max_consecutive_days": 3,
                    }
                    svc.router = MagicMock()
                    svc.bus = MagicMock()

    # Update regime
    fake_redis.set("market_regime:current", json.dumps({"regime": regime}))
    return svc


def _pos(symbol, qty, entry, current, pct=None, mode="shadow_v8"):
    """Helper to build a position dict."""
    if pct is None:
        pct = (current - entry) / entry if entry > 0 else 0
    return {
        "symbol": symbol,
        "quantity": qty,
        "remaining_quantity": qty,
        "avg_price": entry,
        "current_price": current,
        "unrealized_pct": pct,
        "mode": mode,
    }


# ===========================================================================
# Tier Classification Tests
# ===========================================================================

class TestForceTier:
    """PANIC / BEAR_LONG / RANGE_BEARISH → close ALL."""

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_panic_forces_all(self, fake_redis):
        svc = _make_service(fake_redis, regime="PANIC")
        pos = _pos("TSLA", 10, 100, 105, pct=0.05)
        tier, action, order_type = svc.classify_position(pos, "PANIC", {})
        assert tier == "FORCE"
        assert action == "SELL_ALL"
        assert order_type == "MKT"

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_bear_long_forces_all(self, fake_redis):
        svc = _make_service(fake_redis, regime="BEAR_LONG")
        pos = _pos("AAPL", 20, 150, 160, pct=0.067)
        tier, action, _ = svc.classify_position(pos, "BEAR_LONG", {})
        assert tier == "FORCE"
        assert action == "SELL_ALL"

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_range_bearish_forces_all(self, fake_redis):
        svc = _make_service(fake_redis, regime="RANGE_BEARISH")
        pos = _pos("NVDA", 5, 400, 420, pct=0.05)
        tier, action, _ = svc.classify_position(pos, "RANGE_BEARISH", {})
        assert tier == "FORCE"
        assert action == "SELL_ALL"


class TestLossTiers:
    """LOSS_SEVERE (< -3%) and LOSS_MILD (< -1%) logic."""

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_severe_loss_any_regime(self, fake_redis):
        """Severe loss forces close regardless of regime."""
        svc = _make_service(fake_redis, regime="NEUTRAL")
        pos = _pos("TSLA", 10, 100, 95, pct=-0.05)
        tier, action, _ = svc.classify_position(pos, "NEUTRAL", {})
        assert tier == "LOSS_SEVERE"
        assert action == "SELL_ALL"

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_severe_loss_even_in_bull(self, fake_redis):
        """Severe loss forces close even in bull regime."""
        svc = _make_service(fake_redis, regime="BULL_LONG")
        pos = _pos("AAPL", 10, 100, 96, pct=-0.04)
        tier, action, _ = svc.classify_position(pos, "BULL_LONG", {})
        assert tier == "LOSS_SEVERE"

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_mild_loss_non_bull(self, fake_redis):
        """Mild loss in NEUTRAL → close."""
        svc = _make_service(fake_redis, regime="NEUTRAL")
        pos = _pos("MSFT", 10, 100, 98, pct=-0.02)
        tier, action, _ = svc.classify_position(pos, "NEUTRAL", {})
        assert tier == "LOSS_MILD"
        assert action == "SELL_ALL"

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_mild_loss_in_bull_holds(self, fake_redis):
        """Mild loss in BULL regime → held (not LOSS_MILD)."""
        svc = _make_service(fake_redis, regime="BULL_LONG")
        pos = _pos("MSFT", 10, 100, 98.5, pct=-0.015)
        tier, action, _ = svc.classify_position(pos, "BULL_LONG", {})
        # Should NOT be LOSS_MILD since BULL_LONG is in BULL_REGIMES
        assert tier != "LOSS_MILD"


class TestProfitTiers:
    """PROFIT_HOLD and PROFIT_TAKE logic."""

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_profit_take_above_3pct(self, fake_redis):
        """Profit >= 3% but < 8% → PROFIT_TAKE."""
        svc = _make_service(fake_redis, regime="NEUTRAL")
        pos = _pos("AAPL", 10, 100, 105, pct=0.05)
        tier, action, _ = svc.classify_position(pos, "NEUTRAL", {})
        assert tier == "PROFIT_TAKE"
        assert action == "SELL_ALL"

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_profit_hold_exempt(self, fake_redis):
        """Profit >= 8% with qualifying watchlist data → PROFIT_HOLD."""
        svc = _make_service(fake_redis, regime="BULL_LONG")
        # Set up watchlist data for exemption
        fake_redis.hset("watchlist:current", "NVDA",
                        json.dumps({"fused_score": 0.85, "sentiment_score": 0.80}))

        pos = _pos("NVDA", 10, 100, 112, pct=0.12)
        tier, action, order_type = svc.classify_position(pos, "BULL_LONG", {})
        assert tier == "PROFIT_HOLD"
        assert action == "SELL_PARTIAL"
        assert order_type == "LIMIT"

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_profit_hold_no_watchlist_falls_to_profit_take(self, fake_redis):
        """Profit >= 8% but no watchlist data → PROFIT_TAKE (fallback)."""
        svc = _make_service(fake_redis, regime="BULL_LONG")
        # No watchlist data set
        pos = _pos("NVDA", 10, 100, 112, pct=0.12)
        tier, action, _ = svc.classify_position(pos, "BULL_LONG", {})
        assert tier == "PROFIT_TAKE"
        assert action == "SELL_ALL"

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_profit_hold_low_score_falls_to_profit_take(self, fake_redis):
        """Profit >= 8% but low score → PROFIT_TAKE."""
        svc = _make_service(fake_redis, regime="BULL_LONG")
        fake_redis.hset("watchlist:current", "NVDA",
                        json.dumps({"fused_score": 0.50, "sentiment_score": 0.80}))
        pos = _pos("NVDA", 10, 100, 112, pct=0.12)
        tier, action, _ = svc.classify_position(pos, "BULL_LONG", {})
        assert tier == "PROFIT_TAKE"


class TestFlatTier:
    """FLAT / FLAT_HOLD logic for -1% <= pct < 3%."""

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_flat_non_bull_closes(self, fake_redis):
        svc = _make_service(fake_redis, regime="NEUTRAL")
        pos = _pos("MSFT", 10, 100, 101, pct=0.01)
        tier, action, _ = svc.classify_position(pos, "NEUTRAL", {})
        assert tier == "FLAT"
        assert action == "SELL_ALL"

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_flat_in_bull_holds(self, fake_redis):
        svc = _make_service(fake_redis, regime="BULL_LONG")
        pos = _pos("MSFT", 10, 100, 101, pct=0.01)
        tier, action, _ = svc.classify_position(pos, "BULL_LONG", {})
        assert tier == "FLAT_HOLD"
        assert action == "HOLD"


class TestAIForceOverride:
    """Gemini urgency >= 8 forces immediate exit."""

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_high_urgency_forces_exit(self, fake_redis):
        svc = _make_service(fake_redis, regime="BULL_LONG")
        pos = _pos("TSLA", 10, 100, 110, pct=0.10)
        priorities = {"TSLA": {"urgency": 9, "reason": "causal conflict", "method": "MARKET"}}
        tier, action, order_type = svc.classify_position(pos, "BULL_LONG", priorities)
        assert tier == "AI_FORCE"
        assert action == "SELL_ALL"
        assert order_type == "MKT"


# ===========================================================================
# Redis Coordination Tests
# ===========================================================================

class TestRedisCoordination:
    """Verify eod:closed_today is populated correctly."""

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_closed_symbols_added_to_redis_set(self, fake_redis):
        svc = _make_service(fake_redis, regime="PANIC")
        # Mock get_open_positions to return test data
        svc.get_open_positions = MagicMock(return_value=[
            _pos("TSLA", 10, 100, 95, pct=-0.05),
            _pos("AAPL", 20, 150, 140, pct=-0.067),
        ])
        svc._run_gemini_audit = MagicMock(return_value={})

        result = svc.execute_closing(mode="shadow_v8")
        assert result["closed"] == 2

        # Verify Redis set
        members = fake_redis.smembers("eod:closed_today")
        assert "TSLA" in members
        assert "AAPL" in members

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_held_symbols_not_in_closed_set(self, fake_redis):
        svc = _make_service(fake_redis, regime="BULL_LONG")
        # Position that should be held (flat in bull)
        svc.get_open_positions = MagicMock(return_value=[
            _pos("MSFT", 10, 100, 101, pct=0.01),
        ])
        svc._run_gemini_audit = MagicMock(return_value={})

        result = svc.execute_closing(mode="shadow_v8")
        assert result["held"] == 1
        assert result["closed"] == 0

        members = fake_redis.smembers("eod:closed_today")
        assert "MSFT" not in members

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_audit_written_to_redis(self, fake_redis):
        svc = _make_service(fake_redis, regime="PANIC")
        svc.get_open_positions = MagicMock(return_value=[
            _pos("TSLA", 10, 100, 95, pct=-0.05),
        ])
        svc._run_gemini_audit = MagicMock(return_value={})

        svc.execute_closing(mode="shadow_v8")

        # Check audit key exists
        keys = [k for k in fake_redis.keys() if k.startswith("eod:closing_audit:")]
        assert len(keys) == 1
        audit = json.loads(fake_redis.get(keys[0]))
        assert audit["regime"] == "PANIC"
        assert audit["positions_evaluated"] == 1


# ===========================================================================
# Edge Cases
# ===========================================================================

class TestEdgeCases:
    """No positions, missing price data, etc."""

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_no_positions(self, fake_redis):
        svc = _make_service(fake_redis, regime="NEUTRAL")
        svc.get_open_positions = MagicMock(return_value=[])
        svc._run_gemini_audit = MagicMock(return_value={})

        result = svc.execute_closing(mode="shadow_v8")
        assert result["evaluated"] == 0
        assert result["closed"] == 0

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_zero_quantity_skipped(self, fake_redis):
        svc = _make_service(fake_redis, regime="PANIC")
        pos = _pos("TSLA", 0, 100, 95, pct=-0.05)
        pos["quantity"] = 0
        svc.get_open_positions = MagicMock(return_value=[pos])
        svc._run_gemini_audit = MagicMock(return_value={})

        result = svc.execute_closing(mode="shadow_v8")
        assert result["closed"] == 0

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_unknown_regime(self, fake_redis):
        svc = _make_service(fake_redis, regime="UNKNOWN")
        pos = _pos("TSLA", 10, 100, 95, pct=-0.05)
        tier, action, _ = svc.classify_position(pos, "UNKNOWN", {})
        # Unknown is not in FORCE_REGIMES, so check LOSS path
        assert tier == "LOSS_SEVERE"
        assert action == "SELL_ALL"

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_missing_regime_data(self, fake_redis):
        """No regime in Redis → UNKNOWN."""
        fake_redis.delete("market_regime:current")
        svc = _make_service(fake_redis, regime="UNKNOWN")
        # get_regime should return UNKNOWN
        regime = svc.get_regime()
        assert regime == "UNKNOWN"

    @pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis required")
    def test_db_connection_failure_returns_empty(self, fake_redis):
        svc = _make_service(fake_redis, regime="NEUTRAL")
        svc.db_url = "postgresql://invalid:invalid@nonexistent:5432/fake"
        positions = svc.get_open_positions()
        assert positions == []
