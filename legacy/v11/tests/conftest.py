"""
tests/conftest.py — shared fixtures for all test modules.
"""

import json
import time

import pytest


# ── Fakeredis fixture ────────────────────────────────────────────────────

@pytest.fixture()
def fake_redis():
    """Synchronous fakeredis instance (isolated per test)."""
    import fakeredis

    r = fakeredis.FakeRedis(decode_responses=True)
    return r


# ── Seed data factories ─────────────────────────────────────────────────

@pytest.fixture()
def tier1_seed():
    """Sample tier1 sorted-set entries and details."""
    entries = [("AAPL", 0.85), ("TSLA", 0.78), ("NVDA", 0.72)]
    details = {
        "AAPL": json.dumps({
            "price": 195.0,
            "gap_percent": 0.02,
            "relative_volume": 1.5,
            "beta": 1.2,
        }),
        "TSLA": json.dumps({
            "price": 250.0,
            "gap_percent": 0.03,
            "relative_volume": 2.1,
            "beta": 2.0,
        }),
        "NVDA": json.dumps({
            "price": 880.0,
            "gap_percent": 0.01,
            "relative_volume": 1.8,
            "beta": 1.7,
        }),
    }
    return entries, details


@pytest.fixture()
def regime_seed():
    """Sample market regime data."""
    return {
        "regime": "BULL_LONG",
        "market_regime": "BULL_LONG",
        "volatility_tier": "NORMAL",
        "vix": 15.0,
        "last_updated": time.time(),
        "trend": "up",
    }


@pytest.fixture()
def seed_tier1(fake_redis, tier1_seed):
    """Populate fakeredis with tier1 data and return the redis client."""
    entries, details = tier1_seed
    for symbol, score in entries:
        fake_redis.zadd("watchlist:tier1", {symbol: score})
    for symbol, raw in details.items():
        fake_redis.hset("watchlist:tier1:details", symbol, raw)
    return fake_redis


@pytest.fixture()
def seed_regime(fake_redis, regime_seed):
    """Populate fakeredis with market regime data."""
    fake_redis.set("market_regime:current", json.dumps(regime_seed))
    return fake_redis
