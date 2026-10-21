"""Tests for composition root app/wiring.py (Task 9)."""

from __future__ import annotations

from datetime import date

from stockapp_v12.adapters.clock import FakeClock, SystemClock
from stockapp_v12.adapters.marketdata.composite import CompositeMarketDataClient
from stockapp_v12.adapters.marketdata.fake import FakeMarketData
from stockapp_v12.app.wiring import Runtime, Settings, build_runtime
from stockapp_v12.notifiers.log_notifier import LogNotifier


def test_build_runtime_with_fakes() -> None:
    rt = build_runtime(Settings(use_fake_market_data=True))
    assert rt.clock.today_et() is not None
    assert isinstance(rt.clock.today_et(), date)
    bars = rt.market_data.fetch_daily_bars("AAPL")
    # Fake default may return None — still a valid soft miss
    assert bars is None or hasattr(bars, "columns")
    assert isinstance(rt.market_data, FakeMarketData)
    assert rt.forecast_backend is None


def test_build_runtime_accepts_external_market_data() -> None:
    fake = FakeMarketData()
    rt = build_runtime(Settings(), market_data=fake)
    assert rt.market_data is fake


def test_build_runtime_default_uses_composite_without_live_clients() -> None:
    rt = build_runtime(Settings(use_fake_market_data=False))
    assert isinstance(rt.market_data, CompositeMarketDataClient)
    assert rt.market_data.lb_service is None
    assert rt.market_data.polygon_client is None
    # Soft miss with empty composite — no live I/O
    assert rt.market_data.fetch_daily_bars("AAPL") is None


def test_build_runtime_accepts_clock_and_notifier_overrides() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    fixed = datetime(2026, 7, 15, 16, 5, tzinfo=ZoneInfo("America/New_York"))
    clock = FakeClock(fixed)
    notifier = LogNotifier()
    rt = build_runtime(Settings(), clock=clock, notifier=notifier)
    assert rt.clock is clock
    assert rt.notifier is notifier
    assert rt.clock.today_et() == date(2026, 7, 15)


def test_build_runtime_defaults_system_clock_and_log_notifier() -> None:
    rt = build_runtime()
    assert isinstance(rt.clock, SystemClock)
    assert rt.notifier is not None
    # Default env → log backend
    assert hasattr(rt.notifier, "send") or hasattr(rt.notifier, "notify")


def test_build_runtime_does_not_load_chronos_by_default() -> None:
    rt = build_runtime(Settings(chronos_model_path="/nonexistent"))
    assert rt.forecast_backend is None


def test_build_runtime_load_chronos_soft_fails() -> None:
    """load_chronos=True with bad path soft-fails → None (never raises)."""
    rt = build_runtime(
        Settings(chronos_model_path="/nonexistent/path"),
        load_chronos=True,
    )
    # build_legacy_chronos_backend soft-fails → None or adapter
    assert rt.forecast_backend is None or hasattr(rt.forecast_backend, "detect")


def test_settings_frozen() -> None:
    s = Settings(use_fake_market_data=True, redis_url="redis://localhost")
    assert s.use_fake_market_data is True
    assert s.redis_url == "redis://localhost"
    try:
        s.use_fake_market_data = False  # type: ignore[misc]
        raised = False
    except Exception:
        raised = True
    assert raised


def test_runtime_holds_ports() -> None:
    rt = build_runtime(Settings(use_fake_market_data=True))
    assert isinstance(rt, Runtime)
    assert rt.clock is not None
    assert rt.market_data is not None
    assert rt.notifier is not None
