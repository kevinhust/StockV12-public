"""Task 7 — MarketDataClient injection into SwingScanner.

Proves SwingScanner accepts an injected MarketDataClient and routes
``_fetch_daily_bars`` through it without archive/network init.
"""

from __future__ import annotations

import pandas as pd

from stockapp_v12.adapters.marketdata.fake import FakeMarketData
from stockapp_v12.services.swing_scanner import SwingScanner


def _bars(n: int = 80) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1_000_000,
        },
        index=idx,
    )


def test_scanner_uses_injected_market_data(monkeypatch):
    fake = FakeMarketData(bars={"AAPL": _bars()})
    # Prevent accidental network/archive init when only market_data provided
    s = SwingScanner(config_path=None, redis_client=None, market_data=fake)
    out = s._fetch_daily_bars("AAPL", days=60)
    assert out is not None
    assert len(out) >= 30
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]


def test_injected_market_data_skips_default_providers(monkeypatch):
    """Default LB/Polygon init must not run when market_data is injected."""
    calls: list[str] = []

    def boom_lb():
        calls.append("lb")
        raise AssertionError("should not init longbridge when market_data injected")

    def boom_poly(self):
        calls.append("poly")
        raise AssertionError("should not init polygon when market_data injected")

    monkeypatch.setattr(
        "stockapp_v12.services.swing_scanner._init_longbridge", boom_lb
    )
    monkeypatch.setattr(
        "stockapp_v12.services.swing_scanner.SwingScanner._init_polygon", boom_poly
    )

    fake = FakeMarketData(bars={"MSFT": _bars()})
    s = SwingScanner(config_path=None, redis_client=None, market_data=fake)
    assert s.market_data is fake
    assert s._fetch_daily_bars("MSFT", days=60) is not None
    assert calls == []


def test_fake_market_data_protocol_surface():
    fake = FakeMarketData(bars={"AAPL": _bars(50)}, universe=["AAPL", "MSFT"])
    assert fake.fetch_daily_bars("MISSING") is None
    assert fake.fetch_latest_close("AAPL") == 100.5
    assert fake.universe() == ["AAPL", "MSFT"]
    assert fake.universe("core") == ["AAPL", "MSFT"]


def test_composite_prefers_lb_then_polygon_fallback():
    """Composite mirrors prefer_lb + polygon fallback + min-30-rows gate."""
    from stockapp_v12.adapters.marketdata.composite import CompositeMarketDataClient

    class _LB:
        def get_daily_candles(self, symbol, days=260):
            # Too short → should fall through to polygon
            return _bars(10)

    class _PolyAgg:
        def __init__(self, ts, o, h, l, c, v):
            self.timestamp = ts
            self.open = o
            self.high = h
            self.low = l
            self.close = c
            self.volume = v

    class _Poly:
        def list_aggs(self, *args, **kwargs):
            # 40 business days of synthetic polygon bars
            base = pd.Timestamp("2025-06-01")
            for i in range(40):
                ts = int((base + pd.Timedelta(days=i)).timestamp() * 1000)
                yield _PolyAgg(ts, 10.0, 11.0, 9.0, 10.5, 1000)

    client = CompositeMarketDataClient(
        lb_service=_LB(),
        polygon_client=_Poly(),
        prefer_longbridge=True,
        polygon_fallback=True,
    )
    out = client.fetch_daily_bars("ZZZ", days=30)
    assert out is not None
    assert len(out) >= 30
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert client.universe() == []
    assert client.fetch_latest_close("ZZZ") == float(out["close"].iloc[-1])
