"""
LongbridgeDataService — Centralised Longbridge data access for scanner, macro, capital flow.

Wraps longport QuoteContext to provide:
- Daily OHLCV candles (identical format to Polygon output)
- Minute OHLCV candles
- Real-time quote snapshots

All methods return None on error, allowing callers to fall back to Polygon.
Auto-reconnects on stale/dropped connections.
"""

import os
import time
import threading
from datetime import datetime
from typing import Optional

import pandas as pd
from loguru import logger

try:
    from longport.openapi import Config, QuoteContext, Period, AdjustType
    LONGBRIDGE_AVAILABLE = True
except ImportError:
    LONGBRIDGE_AVAILABLE = False
    logger.warning("longport SDK not installed — LongbridgeDataService disabled")

# Maximum age (seconds) before we consider the QuoteContext stale and reconnect
_STALE_THRESHOLD = 3600  # 1 hour


class LongbridgeDataService:
    """Centralised Longbridge data access — shared across scanner, macro, capital flow.

    Note: The LongbridgeRealtimeStreamer intentionally creates its own separate
    QuoteContext because it needs set_on_quote() for WebSocket push callbacks with
    its own reconnect lifecycle. This service is for synchronous pull calls only.
    """

    def __init__(self):
        self._ctx: Optional[QuoteContext] = None
        self._lock = threading.Lock()
        self._connected_at: float = 0.0

    def _ensure_context(self) -> Optional[QuoteContext]:
        """Lazy-init QuoteContext (thread-safe). Reconnects if stale."""
        if self._ctx is not None:
            # Reconnect if connection is stale
            if time.time() - self._connected_at > _STALE_THRESHOLD:
                logger.info("LongbridgeDataService: QuoteContext stale, reconnecting")
                self._reconnect()
            if self._ctx is not None:
                return self._ctx
        with self._lock:
            return self._connect_locked()

    def _connect_locked(self) -> Optional[QuoteContext]:
        """Create QuoteContext (caller must hold self._lock)."""
        if not LONGBRIDGE_AVAILABLE:
            return None
        app_key = os.getenv("LONGBRIDGE_APP_KEY")
        app_secret = os.getenv("LONGBRIDGE_APP_SECRET")
        access_token = os.getenv("LONGBRIDGE_ACCESS_TOKEN")
        if not (app_key and app_secret and access_token):
            logger.debug("LongbridgeDataService: missing env credentials")
            return None
        try:
            config = Config(app_key, app_secret, access_token)
            self._ctx = QuoteContext(config)
            self._connected_at = time.time()
            logger.info("LongbridgeDataService: QuoteContext connected")
            return self._ctx
        except Exception as e:
            logger.warning(f"LongbridgeDataService: QuoteContext init failed: {e}")
            self._ctx = None
            return None

    def _reconnect(self):
        """Force reconnect — clears stale context and creates a fresh one."""
        with self._lock:
            self._ctx = None
            self._connect_locked()

    def get_daily_candles(self, symbol: str, days: int = 30) -> Optional[pd.DataFrame]:
        """
        Fetch daily OHLCV candles. Returns DataFrame with columns
        [open, high, low, close, volume, date] and date as index.

        Symbol format: bare ticker (e.g. "NVDA"), will be converted to "NVDA.US".
        """
        ctx = self._ensure_context()
        if ctx is None:
            return None
        lb_symbol = f"{symbol}.US"
        try:
            candles = ctx.candlesticks(
                lb_symbol, Period.Day, days + 10, AdjustType.ForwardAdjust
            )
            if not candles or len(candles) < 5:
                return None
            rows = []
            for c in candles:
                rows.append({
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": int(c.volume),
                    "date": datetime.fromtimestamp(c.timestamp),
                })
            df = pd.DataFrame(rows)
            df.set_index("date", inplace=True)
            df = df.tail(days)
            return df
        except Exception as e:
            logger.debug(f"Longbridge daily candles failed for {symbol}: {e}")
            # Connection may be dead — trigger reconnect on next call
            self._ctx = None
            return None

    def get_minute_candles(self, symbol: str, limit: int = 390) -> Optional[pd.DataFrame]:
        """
        Fetch 1-minute OHLCV candles. Returns DataFrame with columns
        [open, high, low, close, volume, date] and date as index.
        """
        ctx = self._ensure_context()
        if ctx is None:
            return None
        lb_symbol = f"{symbol}.US"
        try:
            candles = ctx.candlesticks(
                lb_symbol, Period.Min_1, limit, AdjustType.ForwardAdjust
            )
            if not candles or len(candles) < 5:
                return None
            rows = []
            for c in candles:
                rows.append({
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": int(c.volume),
                    "date": datetime.fromtimestamp(c.timestamp),
                })
            df = pd.DataFrame(rows)
            df.set_index("date", inplace=True)
            return df
        except Exception as e:
            logger.debug(f"Longbridge minute candles failed for {symbol}: {e}")
            self._ctx = None
            return None

    def get_quote(self, symbol: str) -> Optional[dict]:
        """
        Fetch real-time quote snapshot.

        Returns dict with keys: symbol, price, volume, timestamp, source.
        """
        ctx = self._ensure_context()
        if ctx is None:
            return None
        lb_symbol = f"{symbol}.US"
        try:
            quotes = ctx.quote([lb_symbol])
            if not quotes:
                return None
            q = quotes[0]
            price = float(q.last_done) if q.last_done else None
            if price is None or price <= 0:
                return None
            return {
                "symbol": symbol,
                "price": price,
                "volume": int(q.volume) if q.volume else None,
                "timestamp": datetime.now().isoformat(),
                "source": "longbridge",
            }
        except Exception as e:
            logger.debug(f"Longbridge quote failed for {symbol}: {e}")
            self._ctx = None
            return None


# ── Module-level singleton ────────────────────────────────────────────────────
_instance: Optional[LongbridgeDataService] = None
_instance_lock = threading.Lock()


def get_longbridge_service() -> LongbridgeDataService:
    """Factory: returns the module-level LongbridgeDataService singleton."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = LongbridgeDataService()
    return _instance
