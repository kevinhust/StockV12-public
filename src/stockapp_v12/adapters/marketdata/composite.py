"""Composite MarketDataClient: Longbridge prefer + Polygon fallback.

Behavioral equivalent of the historical SwingScanner._fetch_daily_bars
routing (prefer_longbridge, polygon_fallback, min 30 rows, OHLCV filter).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_OHLCV = ["open", "high", "low", "close", "volume"]
_MIN_ROWS = 30


class CompositeMarketDataClient:
    """Behavioral equivalent of current SwingScanner._fetch_daily_bars routing."""

    def __init__(
        self,
        *,
        lb_service: Any = None,
        polygon_client: Any = None,
        prefer_longbridge: bool = True,
        polygon_fallback: bool = True,
    ) -> None:
        self.lb_service = lb_service
        self.polygon_client = polygon_client
        self.prefer_longbridge = prefer_longbridge
        self.polygon_fallback = polygon_fallback

    def fetch_daily_bars(
        self, symbol: str, days: int = 260
    ) -> Optional[pd.DataFrame]:
        """Prefer Longbridge (no rate limit) then Polygon. OHLCV, date index."""
        df: Optional[pd.DataFrame] = None

        if self.prefer_longbridge and self.lb_service:
            try:
                df = self.lb_service.get_daily_candles(symbol, days=days)
            except Exception as e:
                logger.debug("LB daily fail %s: %s", symbol, e)

        if df is None or len(df) < _MIN_ROWS:
            if self.polygon_fallback and self.polygon_client:
                df = self._fetch_polygon_daily(symbol, days)

        if df is None or len(df) < _MIN_ROWS:
            return None

        df = df[_OHLCV].copy()
        df = df.sort_index()
        return df

    def _fetch_polygon_daily(
        self, symbol: str, days: int
    ) -> Optional[pd.DataFrame]:
        if not self.polygon_client:
            return None
        end = date.today() - timedelta(days=1)  # previous close for 16:30 job
        start = end - timedelta(days=days + 30)
        try:
            aggs = list(
                self.polygon_client.list_aggs(
                    symbol,
                    1,
                    "day",
                    from_=start.isoformat(),
                    to=end.isoformat(),
                    limit=50000,
                    adjusted=True,
                )
            )
            if not aggs:
                return None
            rows = []
            for a in aggs:
                dt = datetime.fromtimestamp(a.timestamp / 1000).date()
                rows.append(
                    {
                        "date": dt,
                        "open": float(a.open),
                        "high": float(a.high),
                        "low": float(a.low),
                        "close": float(a.close),
                        "volume": int(a.volume),
                    }
                )
            df = pd.DataFrame(rows).set_index(
                pd.to_datetime([r["date"] for r in rows])
            )
            df = df[_OHLCV]
            return df.tail(days)
        except Exception as e:
            logger.warning("Polygon daily fail %s: %s", symbol, e)
            return None

    def fetch_latest_close(self, symbol: str) -> Optional[float]:
        df = self.fetch_daily_bars(symbol)
        if df is None or df.empty:
            return None
        return float(df["close"].iloc[-1])

    def universe(self, tier: str = "default") -> List[str]:
        # Phase 1: scanner still loads universe from yaml — composite returns [].
        return []


__all__ = ["CompositeMarketDataClient"]
