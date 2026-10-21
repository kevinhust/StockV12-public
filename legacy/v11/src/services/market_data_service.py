"""
Market Data Service — ATR-based volatility helpers.

Provides cached ATR% calculations using Polygon daily bars.
Used by the post-mortem pipeline for dynamic (volatility-adjusted) thresholds.

ATR% = (ATR_14 / entry_price) * 100

Redis cache: market_data:atr_pct:{symbol}:{YYYY-MM-DD}  TTL 48h
"""

import os
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import redis

from src.services.polygon_historical_fetcher import PolygonHistoricalFetcher

logger = logging.getLogger("MarketDataService")

# Default ATR period (standard)
ATR_PERIOD = 14
# Lookback buffer in calendar days to guarantee ATR_PERIOD valid trading days
ATR_LOOKBACK_BUFFER_DAYS = 20
# Redis TTL for cached ATR% values (48 hours covers weekends)
ATR_CACHE_TTL = 172800
# Max age for cached ATR values before re-fetch (1 hour)
ATR_CACHE_MAX_AGE = 3600


def _get_redis_client() -> redis.Redis:
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True,
    )


def _calculate_atr(bars: list, period: int = ATR_PERIOD) -> float:
    """
    Calculate Average True Range from a list of bar dicts.

    Each bar must have keys: high, low, close (from PolygonHistoricalFetcher).
    Returns the ATR as a float, or 0.0 if insufficient data.
    """
    if len(bars) < period + 1:
        logger.warning(
            f"Insufficient bars for ATR calculation: {len(bars)} < {period + 1}"
        )
        return 0.0

    highs = np.array([b["high"] for b in bars], dtype=float)
    lows = np.array([b["low"] for b in bars], dtype=float)
    closes = np.array([b["close"] for b in bars], dtype=float)

    # True Range = max(H-L, |H-prevC|, |L-prevC|)
    prev_closes = closes[:-1]
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - prev_closes),
            np.abs(lows[1:] - prev_closes),
        ),
    )

    if len(tr) < period:
        return float(np.mean(tr))

    # Simple moving average of the last `period` true ranges
    atr = float(np.mean(tr[-period:]))
    return atr


class MarketDataService:
    """
    Provides volatility metrics (ATR%) with Redis caching.
    Designed for batch use in the post-mortem pipeline — one call per
    unique symbol per day, cached for 48 hours.
    """

    def __init__(
        self,
        fetcher: Optional[PolygonHistoricalFetcher] = None,
        redis_client: Optional[redis.Redis] = None,
    ):
        self._fetcher = fetcher
        self._redis = redis_client

    @property
    def fetcher(self) -> PolygonHistoricalFetcher:
        if self._fetcher is None:
            from src.services.polygon_historical_fetcher import create_polygon_fetcher
            self._fetcher = create_polygon_fetcher()
        return self._fetcher

    @property
    def redis_client(self) -> redis.Redis:
        if self._redis is None:
            self._redis = _get_redis_client()
        return self._redis

    def get_atr_pct(
        self,
        symbol: str,
        target_date: datetime,
        entry_price: float,
    ) -> float:
        """
        Get ATR% for a symbol on a given date, using Redis cache.

        Args:
            symbol: Ticker symbol (e.g. "TSLA")
            target_date: The date to calculate ATR for
            entry_price: Price used to normalize ATR into a percentage

        Returns:
            ATR as a percentage of entry_price (e.g. 3.5 means 3.5%)
        """
        date_str = target_date.strftime("%Y-%m-%d")
        cache_key = f"market_data:atr_pct:{symbol}:{date_str}"

        # 1. Cache hit with freshness check
        try:
            cached = self.redis_client.get(cache_key)
            if cached is not None:
                try:
                    import json
                    cached_data = json.loads(cached)
                    # New format: {"value": float, "computed_at": float}
                    if isinstance(cached_data, dict) and "computed_at" in cached_data:
                        age_sec = time.time() - cached_data["computed_at"]
                        if age_sec <= ATR_CACHE_MAX_AGE:
                            return float(cached_data["value"])
                        else:
                            logger.debug(
                                f"ATR cache stale for {symbol}: age={age_sec:.0f}s "
                                f"> max={ATR_CACHE_MAX_AGE}s, re-fetching"
                            )
                    elif isinstance(cached_data, dict) and "value" in cached_data:
                        # Cached but no timestamp — accept (backward compat)
                        return float(cached_data["value"])
                    else:
                        # Legacy format: plain float string — accept
                        return float(cached)
                except (json.JSONDecodeError, ValueError, TypeError):
                    # Legacy plain float string
                    return float(cached)
        except Exception:
            logger.debug("Redis cache read failed, fetching from Polygon")

        # 2. Cache miss — fetch daily bars
        from_date = (target_date - timedelta(days=ATR_LOOKBACK_BUFFER_DAYS)).strftime(
            "%Y-%m-%d"
        )
        to_date = date_str

        bars = self.fetcher.fetch_bars(
            symbol=symbol,
            multiplier=1,
            timespan="day",
            from_date=from_date,
            to_date=to_date,
        )

        if not bars or len(bars) < ATR_PERIOD + 1:
            logger.warning(
                f"Insufficient bars for {symbol} ATR (got {len(bars) if bars else 0}), "
                f"falling back to default 2.0%"
            )
            return 2.0  # Safe default — equivalent to old static -1.5% / -1.8% range

        # 3. Calculate ATR and normalize
        atr_value = _calculate_atr(bars, ATR_PERIOD)

        if entry_price <= 0:
            logger.warning(f"Invalid entry_price ({entry_price}) for {symbol}")
            return 2.0

        atr_pct = (atr_value / entry_price) * 100

        # 4. Cache with timestamp and return
        try:
            import json
            cache_payload = json.dumps({
                "value": round(atr_pct, 4),
                "computed_at": time.time(),
            })
            self.redis_client.setex(cache_key, ATR_CACHE_TTL, cache_payload)
        except Exception:
            logger.debug("Redis cache write failed (non-critical)")

        return round(atr_pct, 4)
