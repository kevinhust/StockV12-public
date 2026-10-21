"""MarketDataClient — passive Protocol for V12 market-data sources.

This is the authoritative seam between V12 (consumer: SwingScanner,
ForecastService future, signal-rule engine future) and any external
data provider (Longbridge, Polygon, Alpaca, IBKR native, …). Today the
only impls are imported via ``src.services.data_loader`` (legacy V11)
and ``services.swing_scanner.SwingScanner._fetch_daily_bars`` (the
in-tree Longbridge/Polygon dispatcher).

Pattern A (mirror ``ChronosBackend``): passive Protocol, no isinstance
checks in V12 production code, no abstract-method enforcement at
runtime. The Protocol exists to *document the seam*, not to gate it.
Adding a 2nd data source (Alpaca, IBKR native, …) becomes a drop-in
Protocol implementation rather than a SwingScanner refactor.

Stability guarantee: ``fetch_daily_bars``, ``fetch_latest_close``,
``universe`` signatures are frozen for v12.x. New optional kwargs may
be added via PEP 3107 keyword-only addition; **positional-arg changes
are a major bump.**
"""

from __future__ import annotations

from typing import List, Optional, Protocol

import pandas as pd


class MarketDataClient(Protocol):
    """Data-source seam for V12 SwingScanner + downstream consumers.

    Implementations MUST:
      - Return ``None`` (not raise) on a soft data-source failure for
        ``fetch_daily_bars`` / ``fetch_latest_close`` — callers handle
        None as "no data, skip symbol" (mirrors SwingScanner._fetch_daily_bars).
      - Return a DataFrame indexed by ``DatetimeIndex`` with columns
        ``open, high, low, close, volume`` (lowercase) for
        ``fetch_daily_bars`` when data is available.
      - Return symbols in uppercase for ``universe`` — callers
        (SR-06, R-05/R-06) uppercase-comparison defensively but the
        canonical form is uppercase.
      - Be pickle-safe if the caller serializes the client (NUC shadow
        harness re-imports per cron run, but future parallel evaluation
        may pickle).
    """

    def fetch_daily_bars(
        self, symbol: str, days: int = 260
    ) -> Optional[pd.DataFrame]:
        """Fetch daily OHLCV bars for ``symbol``.

        :param symbol: Ticker in canonical (uppercase) form.
        :param days: Lookback window in trading days. Default 260
            (~1 calendar year). Must return ≥30 rows on success.
        :return: DataFrame indexed by date with columns
            ``open, high, low, close, volume``, or ``None`` on a soft
            failure (rate limit, no data, both providers down).
        """
        ...

    def fetch_latest_close(self, symbol: str) -> Optional[float]:
        """Fetch the most recent close price for ``symbol``.

        :param symbol: Ticker in canonical (uppercase) form.
        :return: Most recent close as a float, or ``None`` on a soft
            failure. Used by EntryOrchestrator's price-disambiguation
            fallback path when ``forecast.current_price`` is unavailable.
        """
        ...

    def universe(self, tier: str = "default") -> List[str]:
        """Return the symbol universe for a given tier.

        :param tier: One of ``"default"`` (canonical V12 watchlist),
            ``"core"`` (high-conviction subset), or any custom tier
            defined by the implementation.
        :return: List of tickers in canonical uppercase form. May be
            empty if the tier is unknown to this implementation —
            callers treat empty universe as "no candidates" (not an
            error).
        """
        ...


__all__ = ["MarketDataClient"]