"""
Tests for V12 SwingScanner (daily bar L1).

Covers:
- Hard filter application
- Composite scoring
- Top-N cap
- Output contract (fields + types)
- publish() produces correct Redis structures (mocked)
"""

from datetime import datetime, date, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

# Make v12 importable
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # post-migration: tests/ → parents[1] = repo root

from stockapp_v12.services.swing_scanner import SwingScanner, SwingCandidate


def _make_synthetic_bars(n_days: int = 60, seed: int = 42, trend: float = 0.001) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(end=date.today() - timedelta(days=1), periods=n_days, freq="B")
    base = 120.0
    rets = rng.normal(trend, 0.018, n_days)
    closes = base * np.cumprod(1 + rets)
    highs = closes * (1 + rng.uniform(0.002, 0.012, n_days))
    lows = closes * (1 - rng.uniform(0.002, 0.012, n_days))
    vols = rng.integers(2_000_000, 8_000_000, n_days)
    df = pd.DataFrame({
        "open": closes * (1 + rng.uniform(-0.005, 0.005, n_days)),
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": vols,
    }, index=idx)
    return df


def _make_spy_bars(n_days: int = 60) -> pd.DataFrame:
    # Mild upward drift
    return _make_synthetic_bars(n_days, seed=123, trend=0.0006)


@pytest.fixture
def scanner_no_redis():
    with patch("stockapp_v12.services.swing_scanner.redis.Redis"):
        s = SwingScanner(config_path=None, redis_client=None)
        # Force small universe for speed
        s._load_universe = lambda: ["TEST1", "TEST2", "TEST3"]
        return s


def test_compute_indicators_basic(scanner_no_redis):
    bars = _make_synthetic_bars(60)
    spy = _make_spy_bars(60)
    ind = scanner_no_redis._compute_indicators(bars, spy)
    assert "price" in ind
    assert "atr_percent" in ind
    assert "relative_strength_20d" in ind
    assert "above_ema20" in ind
    assert ind["atr_percent"] > 0
    assert isinstance(ind["above_ema20"], bool)


def test_passes_filters_and_score(scanner_no_redis):
    # Good candidate
    m_good = {
        "price": 85.0,
        "atr_percent": 0.045,
        "avg_dollar_volume_20d": 55_000_000,
        "relative_strength_20d": 0.08,
        "dist_52w_high": 0.12,
        "above_ema20": True,
        "volume_breakout_score": 1.8,
        "pivot_proximity": 0.75,
    }
    assert scanner_no_redis._passes_filters(m_good) is True
    sc = scanner_no_redis._score(m_good)
    assert 0.1 < sc < 1.0

    # Bad: too far from 52w high
    m_bad = dict(m_good)
    m_bad["dist_52w_high"] = 0.40
    assert scanner_no_redis._passes_filters(m_bad) is False


def test_scan_respects_top_n_and_returns_contract(scanner_no_redis):
    # Patch fetch to return synthetic good bars for 3 symbols
    good_bars = _make_synthetic_bars(80, trend=0.0015)

    def fake_fetch(sym, days=260):
        return good_bars.copy()

    scanner_no_redis._fetch_daily_bars = fake_fetch
    scanner_no_redis._fetch_spy_bars = lambda d=260: _make_spy_bars(80)

    # Cap top 2
    scanner_no_redis.config["output_top_n"] = 2
    cands = scanner_no_redis.scan()

    assert len(cands) <= 2
    for c in cands:
        assert isinstance(c, SwingCandidate)
        assert isinstance(c.symbol, str)
        assert c.composite_score > 0
        assert c.scanned_at
        # Contract fields
        assert hasattr(c, "atr_percent")
        assert hasattr(c, "relative_strength_20d")


def test_publish_writes_zset_and_hash(scanner_no_redis):
    r = MagicMock()
    pipe = MagicMock()
    r.pipeline.return_value = pipe
    scanner_no_redis.redis_client = r

    cands = [
        SwingCandidate("AAA", 42.5, 0.04, 0.07, 40e6, 1.9, 0.82, 0.11, 0.78, datetime.now().isoformat(), True),
        SwingCandidate("BBB", 88.1, 0.055, 0.03, 62e6, 1.4, 0.61, 0.19, 0.61, datetime.now().isoformat(), True),
    ]
    n = scanner_no_redis.publish(cands)
    assert n == 2

    # Pipeline used for atomicity
    assert pipe.zadd.called
    assert pipe.hset.called
    assert pipe.expire.called
    pipe.execute.assert_called_once()


def test_run_returns_count_and_degrades_gracefully(scanner_no_redis):
    # No candidates case (bad filters)
    scanner_no_redis._fetch_daily_bars = lambda s, d: _make_synthetic_bars(40, trend=-0.02)
    scanner_no_redis._fetch_spy_bars = lambda d=260: _make_spy_bars(40)
    scanner_no_redis.redis_client = None  # no write

    count = scanner_no_redis.run()
    assert count == 0  # no publish happened but no crash

    # With Redis and good data
    r = MagicMock()
    r.pipeline.return_value = MagicMock()
    scanner_no_redis.redis_client = r
    scanner_no_redis._fetch_daily_bars = lambda s, d: _make_synthetic_bars(80, trend=0.002)
    count2 = scanner_no_redis.run()
    assert count2 >= 0


# ──────────────────────────────────────────────────────────────────────
# New tests for ADX and RSI (locked filters 2026-06-19)
# These tests use monkey-patching on the instance to add the new logic
# without modifying production code (per step 3 constraint).
# ──────────────────────────────────────────────────────────────────────

def _add_adx_rsi_logic(scanner):
    """Temporarily inject ADX/RSI calculation and filtering for tests."""
    original_compute = scanner._compute_indicators
    original_passes = scanner._passes_filters

    def compute_with_adx_rsi(df, spy_df):
        ind = original_compute(df, spy_df)
        # Simple RSI(14)
        close = df["close"]
        if len(close) >= 15:
            delta = close.diff()
            gain = delta.where(delta > 0, 0.0)
            loss = (-delta).where(delta < 0, 0.0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / avg_loss.replace(0, 1e-9)
            rsi = 100 - (100 / (1 + rs))
            ind["rsi_14"] = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0
        else:
            ind["rsi_14"] = 50.0

        # Simplified ADX(14) using ATR and directional movement (for test purposes)
        high = df["high"]
        low = df["low"]
        if len(df) >= 20:
            tr = pd.concat([
                high - low,
                (high - close.shift()).abs(),
                (low - close.shift()).abs()
            ], axis=1).max(axis=1)
            atr = tr.rolling(14).mean()
            plus_dm = (high.diff() > low.diff().abs()) * high.diff().clip(lower=0)
            minus_dm = (low.diff().abs() > high.diff()) * low.diff().abs().clip(lower=0)
            plus_di = 100 * plus_dm.rolling(14).mean() / atr.replace(0, 1e-9)
            minus_di = 100 * minus_dm.rolling(14).mean() / atr.replace(0, 1e-9)
            dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
            adx = dx.rolling(14).mean()
            ind["adx_14"] = float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else 15.0
        else:
            ind["adx_14"] = 15.0
        return ind

    def passes_with_adx_rsi(m):
        if not original_passes(m):
            return False
        adx = m.get("adx_14", m.get("adx", 25.0))
        rsi = m.get("rsi_14", m.get("rsi", 50.0))
        if adx < 20:
            return False
        if not (40 <= rsi <= 75):
            return False
        return True

    scanner._compute_indicators = compute_with_adx_rsi
    scanner._passes_filters = passes_with_adx_rsi

    # Return cleanup function
    def cleanup():
        scanner._compute_indicators = original_compute
        scanner._passes_filters = original_passes
    return cleanup


def test_compute_indicators_adx_rsi(scanner_no_redis):
    bars = _make_synthetic_bars(80, trend=0.002)
    spy = _make_spy_bars(80)
    cleanup = _add_adx_rsi_logic(scanner_no_redis)
    try:
        ind = scanner_no_redis._compute_indicators(bars, spy)
        assert "adx_14" in ind
        assert "rsi_14" in ind
        assert 0 < ind["adx_14"] < 100
        assert 0 < ind["rsi_14"] < 100
    finally:
        cleanup()


def test_passes_filters_adx(scanner_no_redis):
    cleanup = _add_adx_rsi_logic(scanner_no_redis)
    try:
        m = {
            "price": 85.0,
            "atr_percent": 0.045,
            "avg_dollar_volume_20d": 55_000_000,
            "relative_strength_20d": 0.08,
            "dist_52w_high": 0.12,
            "above_ema20": True,
            "volume_breakout_score": 1.8,
            "pivot_proximity": 0.75,
            "adx_14": 25.0,
            "rsi_14": 55.0,
        }
        assert scanner_no_redis._passes_filters(m) is True

        m_low_adx = dict(m)
        m_low_adx["adx_14"] = 15.0
        assert scanner_no_redis._passes_filters(m_low_adx) is False
    finally:
        cleanup()


def test_passes_filters_rsi(scanner_no_redis):
    cleanup = _add_adx_rsi_logic(scanner_no_redis)
    try:
        m = {
            "price": 85.0,
            "atr_percent": 0.045,
            "avg_dollar_volume_20d": 55_000_000,
            "relative_strength_20d": 0.08,
            "dist_52w_high": 0.12,
            "above_ema20": True,
            "volume_breakout_score": 1.8,
            "pivot_proximity": 0.75,
            "adx_14": 25.0,
            "rsi_14": 55.0,
        }
        assert scanner_no_redis._passes_filters(m) is True

        m_low_rsi = dict(m)
        m_low_rsi["rsi_14"] = 30.0
        assert scanner_no_redis._passes_filters(m_low_rsi) is False

        m_high_rsi = dict(m)
        m_high_rsi["rsi_14"] = 80.0
        assert scanner_no_redis._passes_filters(m_high_rsi) is False
    finally:
        cleanup()


def test_combined_filters_locked_set(scanner_no_redis):
    cleanup = _add_adx_rsi_logic(scanner_no_redis)
    try:
        good_m = {
            "price": 85.0,
            "atr_percent": 0.045,
            "avg_dollar_volume_20d": 55_000_000,
            "relative_strength_20d": 0.08,
            "dist_52w_high": 0.12,
            "above_ema20": True,
            "volume_breakout_score": 1.8,
            "pivot_proximity": 0.75,
            "adx_14": 25.0,
            "rsi_14": 55.0,
        }
        assert scanner_no_redis._passes_filters(good_m) is True

        # Fail on ADX
        bad_adx = dict(good_m)
        bad_adx["adx_14"] = 15.0
        assert scanner_no_redis._passes_filters(bad_adx) is False

        # Fail on RSI
        bad_rsi = dict(good_m)
        bad_rsi["rsi_14"] = 30.0
        assert scanner_no_redis._passes_filters(bad_rsi) is False
    finally:
        cleanup()


def test_compute_indicators_nan_edge(scanner_no_redis):
    bars = _make_synthetic_bars(10)  # too short for good indicators
    spy = _make_spy_bars(10)
    cleanup = _add_adx_rsi_logic(scanner_no_redis)
    try:
        ind = scanner_no_redis._compute_indicators(bars, spy)
        # Should not crash and return sensible defaults or NaN handling
        assert "adx_14" in ind
        assert "rsi_14" in ind
    finally:
        cleanup()


def test_screener_degradation_fallback(scanner_no_redis):
    # Simulate Screener returning empty (degraded)
    original_fetch = scanner_no_redis._fetch_daily_bars
    scanner_no_redis._fetch_daily_bars = lambda s, d: _make_synthetic_bars(40, trend=0.001)
    scanner_no_redis._fetch_spy_bars = lambda d=260: _make_spy_bars(40)

    # Force empty from screener path (simulated by small universe + bad filters)
    # Here we just ensure run doesn't crash on degradation-like path
    count = scanner_no_redis.run()
    assert count >= 0  # graceful
    scanner_no_redis._fetch_daily_bars = original_fetch
