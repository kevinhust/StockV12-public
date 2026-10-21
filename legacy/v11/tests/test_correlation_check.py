"""
Tests for V2.2 portfolio correlation risk check.
Verifies size reduction when proposed symbol is highly correlated with existing positions.
"""
import json
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from src.core.risk_controller import HardRiskGatekeeper


class TestCorrelationRiskCheck:
    """_check_correlation_risk: high correlation → size *= 0.60."""

    @pytest.fixture
    def gk(self):
        """Create gatekeeper with fully mocked Redis."""
        fake_redis = MagicMock()
        with patch("redis.Redis", return_value=fake_redis):
            gk = HardRiskGatekeeper(
                redis_host="localhost",
                redis_port=6379,
                initial_capital=100000.0,
                secret_key="test-secret",
            )
        return gk

    def test_no_positions_returns_unchanged_size(self, gk):
        """When portfolio is empty, no correlation check needed."""
        gk._get_existing_positions = MagicMock(return_value=[])
        gk._get_cached_correlation = MagicMock()
        gk._compute_daily_correlation = MagicMock()

        size, meta = gk._check_correlation_risk("AAPL", 0.05)

        assert size == 0.05
        assert meta.get("correlation_reduced") is False

    def test_low_correlation_returns_unchanged_size(self, gk):
        """Correlation < 0.75 threshold → no size reduction."""
        gk._get_existing_positions = MagicMock(return_value=["SPY"])
        gk._get_cached_correlation = MagicMock(return_value=0.3)
        gk._compute_daily_correlation = MagicMock()

        size, meta = gk._check_correlation_risk("AAPL", 0.05)

        assert size == 0.05
        assert meta.get("correlation_reduced") is False

    def test_high_correlation_reduces_size(self, gk):
        """Correlation > 0.75 → base_size *= 0.60."""
        gk._get_existing_positions = MagicMock(return_value=["SPY"])
        gk._get_cached_correlation = MagicMock(return_value=None)  # not cached
        gk._compute_daily_correlation = MagicMock(return_value=0.82)

        size, meta = gk._check_correlation_risk("AAPL", 0.05)

        assert size == pytest.approx(0.03)  # 0.05 * 0.60
        assert meta.get("correlation_reduced") is True
        assert meta.get("max_correlation") == 0.82
        assert len(meta.get("high_corr_positions", [])) == 1

    def test_cached_high_correlation_reduces_size(self, gk):
        """Cached correlation > 0.75 also triggers size reduction."""
        gk._get_existing_positions = MagicMock(return_value=["QQQ"])
        gk._get_cached_correlation = MagicMock(return_value=0.90)
        gk._compute_daily_correlation = MagicMock()

        size, meta = gk._check_correlation_risk("TQQQ", 0.05)

        assert size == pytest.approx(0.03)
        assert meta["correlation_reduced"] is True
        assert meta["max_correlation"] == 0.90

    def test_highest_correlation_used_for_multiple_positions(self, gk):
        """Among multiple positions, highest correlation determines reduction."""
        gk._get_existing_positions = MagicMock(return_value=["SPY", "QQQ", "IWM"])
        gk._get_cached_correlation = MagicMock(return_value=None)
        gk._compute_daily_correlation = MagicMock(
            side_effect=[0.5, 0.85, 0.6]
        )

        size, meta = gk._check_correlation_risk("AAPL", 0.05)

        assert size == pytest.approx(0.03)
        assert meta["max_correlation"] == 0.85

    def test_data_unavailable_skips_pair(self, gk):
        """When correlation data is unavailable, skip (fail open)."""
        gk._get_existing_positions = MagicMock(return_value=["SPY"])
        gk._get_cached_correlation = MagicMock(return_value=None)
        gk._compute_daily_correlation = MagicMock(return_value=None)

        size, meta = gk._check_correlation_risk("AAPL", 0.05)

        assert size == 0.05
        assert meta.get("correlation_reduced") is False

    def test_proposed_symbol_same_as_existing_skipped(self, gk):
        """Don't compare a symbol to itself."""
        gk._get_existing_positions = MagicMock(return_value=["AAPL", "TSLA"])
        gk._get_cached_correlation = MagicMock(return_value=None)
        gk._compute_daily_correlation = MagicMock(return_value=None)

        size, meta = gk._check_correlation_risk("AAPL", 0.05)

        # AAPL vs AAPL skipped; AAPL vs TSLA has no data → no reduction
        assert meta.get("correlation_reduced") is False


class TestDailyCorrelationComputation:
    """_compute_daily_correlation: TimescaleDB → Polygon fallback."""

    @pytest.fixture
    def gk(self):
        fake_redis = MagicMock()
        with patch("redis.Redis", return_value=fake_redis):
            gk = HardRiskGatekeeper(
                redis_host="localhost",
                redis_port=6379,
                initial_capital=100000.0,
                secret_key="test",
            )
        return gk

    @patch("psycopg2.connect")
    def test_timescale_returns_correlation(self, mock_connect, gk):
        """TimescaleDB query returns valid correlation."""
        aapl_prices = [150.0 + i for i in range(20)]
        spy_prices = [400.0 + i * 0.5 for i in range(20)]

        mock_cursor = MagicMock()
        # Return our data regardless of how fetchall is called
        mock_cursor.fetchall.return_value = (
            [(s, p) for s, p in zip(["AAPL"] * 20, aapl_prices)]
            + [(s, p) for s, p in zip(["SPY"] * 20, spy_prices)]
        )
        mock_conn = MagicMock()
        # Configure `with psycopg2.connect(...) as conn:` → conn = mock_conn
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False
        # Configure `with conn.cursor() as cur:` → cur = mock_cursor
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_conn.cursor.return_value.__exit__.return_value = False
        mock_connect.return_value = mock_conn

        corr = gk._compute_correlation_from_timescale("AAPL", "SPY")
        assert corr is not None
        assert -1.0 <= corr <= 1.0

    @patch("psycopg2.connect")
    def test_timescale_insufficient_data_returns_none(self, mock_connect, gk):
        """Returns None when fewer than 10 price pairs."""
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("AAPL", 150.0),
            ("AAPL", 151.0),
            ("SPY", 400.0),
            ("SPY", 401.0),
        ]
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.__exit__.return_value = False
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        mock_conn.cursor.return_value.__exit__.return_value = False
        mock_connect.return_value = mock_conn

        corr = gk._compute_correlation_from_timescale("AAPL", "SPY")
        assert corr is None

    @patch("psycopg2.connect")
    def test_timescale_exception_falls_back(self, mock_connect, gk):
        """DB exception → falls back to Polygon."""
        mock_connect.side_effect = Exception("Connection refused")

        with patch.object(
            HardRiskGatekeeper,
            "_compute_correlation_from_polygon",
            return_value=0.45,
        ) as mock_poly:
            corr = gk._compute_daily_correlation("AAPL", "SPY")
            mock_poly.assert_called_once()
            assert corr == 0.45

    @patch("services.polygon_historical_fetcher.create_polygon_fetcher")
    def test_polygon_fallback_success(self, mock_factory, gk):
        """Polygon returns valid correlation."""
        mock_fetcher = MagicMock()
        mock_fetcher.fetch_bars.side_effect = [
            [{"close": 150.0 + i} for i in range(20)],
            [{"close": 400.0 + i * 0.5} for i in range(20)],
        ]
        mock_factory.return_value = mock_fetcher

        corr = gk._compute_correlation_from_polygon("AAPL", "SPY")
        assert corr is not None
        assert -1.0 <= corr <= 1.0

    @patch("services.polygon_historical_fetcher.create_polygon_fetcher")
    def test_polygon_no_data_returns_none(self, mock_factory, gk):
        """Polygon returns empty bars → None."""
        mock_fetcher = MagicMock()
        mock_fetcher.fetch_bars.side_effect = [[], []]
        mock_factory.return_value = mock_fetcher

        corr = gk._compute_correlation_from_polygon("AAPL", "SPY")
        assert corr is None


class TestCorrelationCaching:
    """Redis TTL caching of computed correlations."""

    @pytest.fixture
    def gk(self):
        fake_redis = MagicMock()
        with patch("redis.Redis", return_value=fake_redis):
            gk = HardRiskGatekeeper(
                redis_host="localhost",
                redis_port=6379,
                initial_capital=100000.0,
                secret_key="test",
            )
        return gk

    def test_cache_writes_with_ttl(self, gk):
        """Computed correlation written to Redis with 480s TTL and JSON payload."""
        gk._cache_correlation("AAPL", "SPY", 0.75)

        gk.r.setex.assert_called()
        call_args = gk.r.setex.call_args
        assert call_args[0][0] == "corr:AAPL:SPY"
        assert call_args[0][1] == 480
        import json
        data = json.loads(call_args[0][2])
        assert data["corr"] == 0.75
        assert "ts" in data

    def test_cache_read_returns_float(self, gk):
        """_get_cached_correlation returns float from Redis JSON payload."""
        import json, time
        gk.r.get.return_value = json.dumps({"corr": 0.8234, "ts": time.time()})
        gk.r.ttl.return_value = 300  # plenty of TTL remaining

        corr = gk._get_cached_correlation("AAPL", "SPY")
        assert corr == 0.8234

    def test_cache_read_returns_none_when_missing(self, gk):
        """Returns None when key is absent."""
        gk.r.get.return_value = None

        corr = gk._get_cached_correlation("AAPL", "SPY")
        assert corr is None

    def test_cache_key_normalizes_order(self, gk):
        """Cache key is alphabetical regardless of symbol order."""
        gk._cache_correlation("SPY", "AAPL", 0.5)

        gk.r.setex.assert_called()
        call_args = gk.r.setex.call_args
        assert call_args[0][0] == "corr:AAPL:SPY"
        assert call_args[0][1] == 480

    def test_cache_near_expiry_triggers_async_refresh(self, gk):
        """When TTL < 96s, async refresh is scheduled."""
        import json, time, threading
        gk.r.get.return_value = json.dumps({"corr": 0.8, "ts": time.time()})
        gk.r.ttl.return_value = 50  # within refresh window (< 96s)

        original_threads = threading.active_count()

        corr = gk._get_cached_correlation("AAPL", "SPY")
        assert corr == 0.8

        # A background thread should have been spawned
        assert threading.active_count() == original_threads + 1

    def test_cache_legacy_float_format_still_works(self, gk):
        """Legacy plain float string format is still readable (backward compat)."""
        gk.r.get.return_value = "0.8234"
        gk.r.ttl.return_value = 300

        corr = gk._get_cached_correlation("AAPL", "SPY")
        assert corr == 0.8234


class TestPortfolioMetrics:
    """_get_portfolio_metrics: drawdown_pct and total_exposure_pct from Redis snapshot."""

    @pytest.fixture
    def gk(self):
        fake_redis = MagicMock()
        with patch("redis.Redis", return_value=fake_redis):
            gk = HardRiskGatekeeper(
                redis_host="localhost",
                redis_port=6379,
                initial_capital=100000.0,
                secret_key="test",
            )
        return gk

    def test_no_snapshot_returns_zeros(self, gk):
        """When no portfolio snapshot exists, defaults to 0.0 for both metrics."""
        gk.r.get.return_value = None
        metrics = gk._get_portfolio_metrics()
        assert metrics["drawdown_pct"] == 0.0
        assert metrics["total_exposure_pct"] == 0.0

    def test_drawdown_computed_from_peak_nav(self, gk):
        """Drawdown = (peak - current) / peak * 100 when NAV below peak."""
        # Peak NAV is 100000 (initial_capital), current NAV is 90000
        snapshot = {"nav": 90000.0, "market_value": 45000.0}
        gk.r.get.side_effect = lambda k: {
            "portfolio:snapshot:paper": None,
            "portfolio:snapshot:live": json.dumps(snapshot),
        }.get(k)

        metrics = gk._get_portfolio_metrics()
        assert metrics["drawdown_pct"] == pytest.approx(10.0)  # (100000-90000)/100000*100
        assert metrics["total_exposure_pct"] == pytest.approx(50.0)  # 45000/90000*100

    def test_new_peak_nav_updates_drawdown_to_zero(self, gk):
        """When NAV is below stored peak, drawdown = (peak-nav)/peak * 100."""
        # Stored peak 110000, current NAV 105000 → 4.54% drawdown
        snapshot = {"nav": 105000.0, "market_value": 50000.0}
        gk.r.get.side_effect = lambda k: {
            "portfolio:peak_nav": "110000",
            "portfolio:snapshot:paper": None,
            "portfolio:snapshot:live": json.dumps(snapshot),
        }.get(k)

        metrics = gk._get_portfolio_metrics()
        assert metrics["drawdown_pct"] == pytest.approx(4.545, rel=1e-2)

    def test_total_exposure_pct_from_snapshot(self, gk):
        """total_exposure_pct = market_value / nav * 100."""
        snapshot = {"nav": 100000.0, "market_value": 30000.0}
        gk.r.get.side_effect = lambda k: {
            "portfolio:peak_nav": "100000",
            "portfolio:snapshot:paper": None,
            "portfolio:snapshot:live": json.dumps(snapshot),
        }.get(k)

        metrics = gk._get_portfolio_metrics()
        assert metrics["total_exposure_pct"] == pytest.approx(30.0)

    def test_update_account_state_tracks_peak_nav(self, gk):
        """update_account_state records new peak NAV to Redis."""
        gk.r.get.side_effect = lambda k: {
            "portfolio:peak_nav": "100000",
        }.get(k)

        # New equity exceeds peak → should update Redis
        gk.update_account_state(equity=110000.0, pnl=0.0)
        gk.r.set.assert_called_once()
        call_args = gk.r.set.call_args[0]
        assert call_args[0] == "portfolio:peak_nav"
        assert float(call_args[1]) == pytest.approx(110000.0)

    def test_update_account_state_skips_update_when_below_peak(self, gk):
        """update_account_state does NOT overwrite peak NAV when equity is lower."""
        gk.r.get.side_effect = lambda k: {
            "portfolio:peak_nav": "100000",
        }.get(k)
        gk.r.set.reset_mock()

        gk.update_account_state(equity=90000.0, pnl=0.0)
        # Should NOT have called set to update the peak
        gk.r.set.assert_not_called()


class TestDrawdownR02Blocking:
    """R-02 (Max Drawdown 10%) now fires when drawdown_pct > 10% from portfolio snapshot."""

    @pytest.fixture
    def gk(self):
        fake_redis = MagicMock()
        with patch("redis.Redis", return_value=fake_redis):
            gk = HardRiskGatekeeper(
                redis_host="localhost",
                redis_port=6379,
                initial_capital=100000.0,
                secret_key="test",
            )
        return gk

    def test_r02_blocks_buy_when_drawdown_exceeds_10pct(self, gk):
        """BUY order rejected when portfolio drawdown > 10%."""
        # Peak NAV 100000, current NAV 85000 → 15% drawdown
        snapshot = {"nav": 85000.0, "market_value": 30000.0}
        gk.r.get.side_effect = lambda k: {
            "portfolio:peak_nav": "100000",
            "portfolio:snapshot:paper": None,
            "portfolio:snapshot:live": json.dumps(snapshot),
        }.get(k)

        gk.update_account_state(equity=100000.0, pnl=0.0)
        approved, reason, meta = gk.validate_order_intent(
            symbol="AAPL", confidence=0.8, side="BUY", price=195.0
        )
        assert approved is False
        assert "R-02" in reason or "Drawdown" in reason

    def test_r02_allows_sell_in_drawdown(self, gk):
        """R-02 only blocks BUY, not SELL, even in drawdown."""
        snapshot = {"nav": 85000.0, "market_value": 30000.0}
        gk.r.get.side_effect = lambda k: {
            "portfolio:peak_nav": "100000",
            "portfolio:snapshot:paper": None,
            "portfolio:snapshot:live": json.dumps(snapshot),
        }.get(k)

        gk.update_account_state(equity=100000.0, pnl=0.0)
        approved, reason, meta = gk.validate_order_intent(
            symbol="AAPL", confidence=0.8, side="SELL", price=195.0
        )
        # SELL should not be blocked by drawdown
        assert "R-02" not in reason
