# tests/test_clock_port.py
from datetime import datetime, date
from zoneinfo import ZoneInfo
from stockapp_v12.contracts.clock import Clock
from stockapp_v12.adapters.clock import FakeClock, SystemClock

EST = ZoneInfo("America/New_York")


def test_fake_clock_today():
    fixed = datetime(2026, 7, 15, 16, 5, tzinfo=EST)
    clock: Clock = FakeClock(fixed)
    assert clock.today_et() == date(2026, 7, 15)
    assert clock.now_et() == fixed


def test_system_clock_returns_et_aware():
    c = SystemClock()
    n = c.now_et()
    assert n.tzinfo is not None
    assert c.today_et() == n.date()
