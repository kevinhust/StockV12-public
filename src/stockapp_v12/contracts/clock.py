"""Clock — passive Protocol for V12 time access (America/New_York).

Injected into orchestration so session windows, EOD, and journal keys can
be tested without wall-clock coupling. Production uses ``SystemClock``;
tests use ``FakeClock``.

Stability: method signatures frozen for v12.x.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol


class Clock(Protocol):
    """Time source in America/New_York (ET)."""

    def now_et(self) -> datetime:
        """Current datetime with ET timezone info."""
        ...

    def today_et(self) -> date:
        """Calendar date in ET."""
        ...


__all__ = ["Clock"]
