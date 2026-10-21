"""In-memory MarketDataClient for tests and offline scans."""

from __future__ import annotations

from typing import Dict, List, Optional

import pandas as pd

_OHLCV = ["open", "high", "low", "close", "volume"]


class FakeMarketData:
    """Duck-typed MarketDataClient backed by preloaded bar frames.

    Soft-failure semantics match production: missing / short series → None.
    """

    def __init__(
        self,
        bars: Optional[Dict[str, pd.DataFrame]] = None,
        universe: Optional[List[str]] = None,
    ) -> None:
        self._bars: Dict[str, pd.DataFrame] = {
            str(k).upper(): v for k, v in (bars or {}).items()
        }
        self._universe: List[str] = [str(s).upper() for s in (universe or [])]

    def fetch_daily_bars(
        self, symbol: str, days: int = 260
    ) -> Optional[pd.DataFrame]:
        df = self._bars.get(str(symbol).upper())
        if df is None or len(df) < 30:
            return None
        out = df[_OHLCV].copy()
        out = out.sort_index()
        if days and len(out) > days:
            out = out.tail(days)
        return out

    def fetch_latest_close(self, symbol: str) -> Optional[float]:
        df = self.fetch_daily_bars(symbol)
        if df is None or df.empty:
            return None
        return float(df["close"].iloc[-1])

    def universe(self, tier: str = "default") -> List[str]:
        # tier ignored in Phase 1 — single static list for tests
        return list(self._universe)


__all__ = ["FakeMarketData"]
