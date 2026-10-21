"""Market-data adapters implementing the MarketDataClient Protocol."""

from stockapp_v12.adapters.marketdata.composite import CompositeMarketDataClient
from stockapp_v12.adapters.marketdata.fake import FakeMarketData

__all__ = ["CompositeMarketDataClient", "FakeMarketData"]
