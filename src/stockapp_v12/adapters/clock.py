"""Clock adapters — SystemClock (production) and FakeClock (tests)."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")


class SystemClock:
    """Wall-clock time in America/New_York."""

    def now_et(self) -> datetime:
        return datetime.now(EST)

    def today_et(self) -> date:
        return self.now_et().date()


class FakeClock:
    """Fixed-time clock for deterministic tests.

    Naive ``fixed`` values are treated as ET (America/New_York).
    """

    def __init__(self, fixed: datetime) -> None:
        if fixed.tzinfo is None:
            fixed = fixed.replace(tzinfo=EST)
        self._fixed = fixed

    def now_et(self) -> datetime:
        return self._fixed

    def today_et(self) -> date:
        return self._fixed.astimezone(EST).date()


__all__ = ["SystemClock", "FakeClock", "EST"]
