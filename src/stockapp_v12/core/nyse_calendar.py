"""
NYSE trading calendar guard — prevents cron jobs from firing on holidays
and adjusts for early-close (half-day) sessions.

Tries ``exchange_calendars.XNYS`` if installed; falls back to a built-in
holiday table covering 2024–2028.

Usage from shell (cron wrapper):
    if python -m stockapp_v12.core.nyse_calendar; then
        # proceed with job
    else
        exit 0  # skip silently
    fi

Usage from Python:
    from stockapp_v12.core.nyse_calendar import is_trading_day, is_early_close
    if not is_trading_day():
        return
"""

from __future__ import annotations

import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")

# ─── Built-in NYSE holiday fallback ───────────────────────────────────────
# These are the full-day closures (no trading at all).
# Source: NYSE holiday calendar (https://www.nyse.com/markets/hours-calendars).
# Updated: 2026-07-09.

_FULL_DAY_HOLIDAYS: set[date] = set()

# 2024
for _d in [
    "2024-01-01",  # New Year's Day
    "2024-01-15",  # MLK Day
    "2024-02-19",  # Presidents' Day
    "2024-03-29",  # Good Friday
    "2024-05-27",  # Memorial Day
    "2024-06-19",  # Juneteenth
    "2024-07-04",  # Independence Day
    "2024-09-02",  # Labor Day
    "2024-11-28",  # Thanksgiving
    "2024-12-25",  # Christmas
]:
    _FULL_DAY_HOLIDAYS.add(date.fromisoformat(_d))

# 2025
for _d in [
    "2025-01-01",  # New Year's Day
    "2025-01-20",  # MLK Day
    "2025-02-17",  # Presidents' Day
    "2025-04-18",  # Good Friday
    "2025-05-26",  # Memorial Day
    "2025-06-19",  # Juneteenth
    "2025-07-04",  # Independence Day
    "2025-09-01",  # Labor Day
    "2025-11-27",  # Thanksgiving
    "2025-12-25",  # Christmas
]:
    _FULL_DAY_HOLIDAYS.add(date.fromisoformat(_d))

# 2026
for _d in [
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents' Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed — Jul 4 is Saturday)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
]:
    _FULL_DAY_HOLIDAYS.add(date.fromisoformat(_d))

# 2027
for _d in [
    "2027-01-01",  # New Year's Day
    "2027-01-18",  # MLK Day
    "2027-02-15",  # Presidents' Day
    "2027-03-26",  # Good Friday
    "2027-05-31",  # Memorial Day
    "2027-06-18",  # Juneteenth (observed — Jun 19 is Saturday)
    "2027-07-05",  # Independence Day (observed — Jul 4 is Sunday)
    "2027-09-06",  # Labor Day
    "2027-11-25",  # Thanksgiving
    "2027-12-24",  # Christmas (observed — Dec 25 is Saturday)
]:
    _FULL_DAY_HOLIDAYS.add(date.fromisoformat(_d))

# 2028
for _d in [
    "2028-01-01",  # New Year's Day (Saturday — no observance per NYSE rules)
    "2028-01-17",  # MLK Day
    "2028-02-21",  # Presidents' Day
    "2028-04-14",  # Good Friday
    "2028-05-29",  # Memorial Day
    "2028-06-19",  # Juneteenth
    "2028-07-04",  # Independence Day
    "2028-09-04",  # Labor Day
    "2028-11-23",  # Thanksgiving
    "2028-12-25",  # Christmas
]:
    _FULL_DAY_HOLIDAYS.add(date.fromisoformat(_d))

# ─── Early-close days (1:00 PM ET) ────────────────────────────────────────
# Typically: day after Thanksgiving, Christmas Eve.
# On early-close days, entry_window should stop after 12:30 ET.

_EARLY_CLOSE_DAYS: set[date] = set()

for _d in [
    "2024-07-03",  # Day before Independence Day
    "2024-11-29",  # Day after Thanksgiving
    "2024-12-24",  # Christmas Eve
    "2025-07-03",  # Day before Independence Day
    "2025-11-28",  # Day after Thanksgiving
    "2025-12-24",  # Christmas Eve
    "2026-11-27",  # Day after Thanksgiving
    "2026-12-24",  # Christmas Eve
    "2027-11-26",  # Day after Thanksgiving
    "2027-12-23",  # Christmas Eve (Dec 24 is Friday but Christmas observed Dec 24)
    "2028-11-24",  # Day after Thanksgiving
    "2028-12-22",  # Christmas Eve (Dec 25 is Monday, Christmas Eve is Sunday → no early close expected but included for safety)
]:
    _EARLY_CLOSE_DAYS.add(date.fromisoformat(_d))

# Remove early-close days that are also full holidays (no double-count)
_EARLY_CLOSE_DAYS -= _FULL_DAY_HOLIDAYS


def _today_et() -> date:
    """Return today's date in Eastern Time."""
    return datetime.now(EST).date()


def is_trading_day(d: date | None = None) -> bool:
    """Return True if the NYSE is open for regular trading on the given date.

    Uses ``exchange_calendars.XNYS`` if available; otherwise falls back
    to the built-in holiday table.
    """
    d = d or _today_et()

    # Weekend check (always applies)
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False

    # Try exchange_calendars first
    try:
        import exchange_calendars as xcals  # type: ignore[import-untyped]

        cal = xcals.get_calendar("XNYS")
        return bool(cal.is_session(d))
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: built-in holiday table
    return d not in _FULL_DAY_HOLIDAYS


def is_early_close(d: date | None = None) -> bool:
    """Return True if the NYSE has an early close (1:00 PM ET) on the given date."""
    d = d or _today_et()
    return d in _EARLY_CLOSE_DAYS


def _main() -> int:
    """CLI entry point: exit 0 if trading day, exit 1 if not."""
    today = _today_et()
    if not is_trading_day(today):
        print(f"SKIP: {today.isoformat()} is not an NYSE trading day", file=sys.stderr)
        return 1
    if is_early_close(today):
        print(f"WARN: {today.isoformat()} is an NYSE early-close day (1:00 PM ET)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
