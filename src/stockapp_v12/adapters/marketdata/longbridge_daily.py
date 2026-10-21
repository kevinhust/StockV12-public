"""Native Longbridge daily OHLCV client (no V11 archive).

Uses official ``longport`` OpenAPI SDK + env credentials:
  LONGBRIDGE_APP_KEY / LONGBRIDGE_APP_SECRET / LONGBRIDGE_ACCESS_TOKEN

This is the production market-data path for feature refresh and scanners
on archive-free hosts (NUC soak).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
_STALE_THRESHOLD = 3600  # reconnect QuoteContext after 1h

try:
    from longport.openapi import AdjustType, Config, Period, QuoteContext

    LONGBRIDGE_SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    LONGBRIDGE_SDK_AVAILABLE = False
    AdjustType = Config = Period = QuoteContext = None  # type: ignore
    logger.debug("longport SDK not installed — LongbridgeDailyClient disabled")


def _to_us_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if s.endswith(".US") or s.endswith(".HK"):
        return s
    return f"{s}.US"


class LongbridgeDailyClient:
    """Synchronous daily (and optional minute) candles via Longbridge QuoteContext."""

    def __init__(self) -> None:
        self._ctx: Any = None
        self._lock = threading.Lock()
        self._connected_at: float = 0.0

    @property
    def available(self) -> bool:
        return LONGBRIDGE_SDK_AVAILABLE and bool(
            os.getenv("LONGBRIDGE_APP_KEY")
            and os.getenv("LONGBRIDGE_APP_SECRET")
            and os.getenv("LONGBRIDGE_ACCESS_TOKEN")
        )

    def _ensure_context(self) -> Any:
        if not LONGBRIDGE_SDK_AVAILABLE:
            return None
        if self._ctx is not None:
            if time.time() - self._connected_at > _STALE_THRESHOLD:
                logger.info("LongbridgeDailyClient: context stale, reconnecting")
                with self._lock:
                    self._ctx = None
                    return self._connect_locked()
            return self._ctx
        with self._lock:
            return self._connect_locked()

    def _connect_locked(self) -> Any:
        app_key = os.getenv("LONGBRIDGE_APP_KEY")
        app_secret = os.getenv("LONGBRIDGE_APP_SECRET")
        access_token = os.getenv("LONGBRIDGE_ACCESS_TOKEN")
        if not (app_key and app_secret and access_token):
            logger.warning("LongbridgeDailyClient: missing LONGBRIDGE_* env credentials")
            return None
        try:
            # longport ≥3/4: Config constructor is not public — use from_apikey
            config = Config.from_apikey(app_key, app_secret, access_token)
            self._ctx = QuoteContext(config)
            self._connected_at = time.time()
            logger.info("LongbridgeDailyClient: QuoteContext connected")
            return self._ctx
        except Exception as e:
            logger.warning("LongbridgeDailyClient: connect failed: %s", e)
            self._ctx = None
            return None

    # OpenAPI rejects oversized kline requests (code 301607 "too many klines").
    # 500 daily bars ≈ 2y trading days — enough for Chronos context + indicators.
    _MAX_KLINES_PER_CALL = 500

    def get_daily_candles(self, symbol: str, days: int = 30) -> Optional[pd.DataFrame]:
        """Return OHLCV with DatetimeIndex (date), columns open/high/low/close/volume.

        Symbol: bare ticker (e.g. ``NVDA``) → ``NVDA.US``.
        ``days`` is capped at ``_MAX_KLINES_PER_CALL`` per API limit.
        """
        ctx = self._ensure_context()
        if ctx is None:
            return None
        lb_symbol = _to_us_symbol(symbol)
        want = min(max(int(days), 20), self._MAX_KLINES_PER_CALL)
        # request a little extra then tail
        count = min(want + 5, self._MAX_KLINES_PER_CALL)
        try:
            candles = ctx.candlesticks(
                lb_symbol, Period.Day, count, AdjustType.ForwardAdjust
            )
            if not candles or len(candles) < 5:
                logger.warning(
                    "LongbridgeDailyClient: insufficient candles for %s (n=%s)",
                    lb_symbol,
                    0 if not candles else len(candles),
                )
                return None
            rows = []
            for c in candles:
                ts = c.timestamp
                # SDK may return unix seconds or datetime
                if isinstance(ts, (int, float)):
                    dt = datetime.fromtimestamp(ts, tz=ET)
                else:
                    dt = pd.Timestamp(ts)
                    if dt.tzinfo is None:
                        dt = dt.tz_localize(ET)
                    else:
                        dt = dt.tz_convert(ET)
                rows.append(
                    {
                        "open": float(c.open),
                        "high": float(c.high),
                        "low": float(c.low),
                        "close": float(c.close),
                        "volume": int(c.volume),
                        "date": dt,
                    }
                )
            df = pd.DataFrame(rows)
            df = df.set_index("date").sort_index()
            return df.tail(want)
        except Exception as e:
            logger.warning(
                "LongbridgeDailyClient: daily candles failed for %s: %s",
                lb_symbol,
                e,
            )
            self._ctx = None
            return None

    def get_daily_candles_frame(
        self, symbol: str, days: int = 500
    ) -> Optional[pd.DataFrame]:
        """Return phase1-style frame: timestamp, open, high, low, close, volume, symbol."""
        df = self.get_daily_candles(symbol, days=days)
        if df is None or df.empty:
            return None
        out = df.reset_index().rename(columns={"date": "timestamp"})
        out["timestamp"] = pd.to_datetime(out["timestamp"])
        if out["timestamp"].dt.tz is None:
            out["timestamp"] = out["timestamp"].dt.tz_localize(ET)
        else:
            out["timestamp"] = out["timestamp"].dt.tz_convert(ET)
        out["symbol"] = symbol.upper()
        out["adj_close"] = out["close"]
        return out[
            ["timestamp", "open", "high", "low", "close", "volume", "adj_close", "symbol"]
        ]


_singleton: Optional[LongbridgeDailyClient] = None
_singleton_lock = threading.Lock()


def get_longbridge_daily_client() -> LongbridgeDailyClient:
    """Process-wide shared client (one QuoteContext)."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = LongbridgeDailyClient()
        return _singleton


__all__ = ["LongbridgeDailyClient", "get_longbridge_daily_client", "LONGBRIDGE_SDK_AVAILABLE"]
