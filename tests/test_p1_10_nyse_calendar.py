"""Regression tests for P1-10 (council 2026-07-09): NYSE calendar guard.

Pre-fix: cron jobs fire on `* * 1-5` — weekends are skipped but holidays
(July 4, Thanksgiving, Christmas, etc.) are not. On holidays, jobs would
write stale data, trigger Redis writes on non-trading days, and pollute
the journal with phantom entries.

Post-fix: ``core/nyse_calendar.py`` checks the date against the NYSE
holiday table. Shell scripts source ``nyse_guard.sh`` and exit 0 if it's
not a trading day.
"""

from __future__ import annotations

from datetime import date

import pytest

from stockapp_v12.core.nyse_calendar import (
    is_trading_day,
    is_early_close,
    _FULL_DAY_HOLIDAYS,
    _EARLY_CLOSE_DAYS,
)


# ─── Weekend check ────────────────────────────────────────────────────────


def test_saturday_is_not_trading_day():
    """P1-10: Saturdays are never trading days."""
    assert is_trading_day(date(2026, 7, 4)) is False  # Sat (also Independence Day)


def test_sunday_is_not_trading_day():
    """P1-10: Sundays are never trading days."""
    assert is_trading_day(date(2026, 7, 5)) is False


# ─── Holiday checks (2026) ────────────────────────────────────────────────


@pytest.mark.parametrize("holiday_date", [
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents' Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
])
def test_holiday_is_not_trading_day(holiday_date):
    """P1-10: All 10 NYSE full-day holidays in 2026 must be non-trading days."""
    assert is_trading_day(holiday_date) is False


# ─── Normal weekday checks ────────────────────────────────────────────────


@pytest.mark.parametrize("weekday_date", [
    date(2026, 1, 5),   # Monday
    date(2026, 3, 13),  # Friday
    date(2026, 7, 13),  # Monday (summer)
    date(2026, 10, 15), # Thursday (fall)
])
def test_normal_weekday_is_trading_day(weekday_date):
    """P1-10: Regular weekdays that are not holidays must be trading days."""
    assert is_trading_day(weekday_date) is True


# ─── Early close checks ───────────────────────────────────────────────────


def test_day_after_thanksgiving_2026_is_early_close():
    """P1-10: Day after Thanksgiving is an early-close day."""
    assert is_early_close(date(2026, 11, 27)) is True


def test_christmas_eve_2026_is_early_close():
    """P1-10: Christmas Eve is an early-close day."""
    assert is_early_close(date(2026, 12, 24)) is True


def test_normal_weekday_is_not_early_close():
    """P1-10: A regular Wednesday is not an early-close day."""
    assert is_early_close(date(2026, 7, 8)) is False


# ─── Holiday table integrity ──────────────────────────────────────────────


def test_holiday_table_covers_2026_through_2028():
    """P1-10: Built-in holiday table must span at least 2026–2028."""
    years_covered = {d.year for d in _FULL_DAY_HOLIDAYS}
    assert 2026 in years_covered
    assert 2027 in years_covered
    assert 2028 in years_covered


def test_no_early_close_is_also_full_holiday():
    """P1-10: A date can't be both a full holiday and an early-close day."""
    overlap = _EARLY_CLOSE_DAYS & _FULL_DAY_HOLIDAYS
    assert len(overlap) == 0, f"Overlap found: {overlap}"


def test_each_year_has_at_least_8_holidays():
    """P1-10: NYSE has at least 8 market holidays per year."""
    for year in (2026, 2027, 2028):
        count = sum(1 for d in _FULL_DAY_HOLIDAYS if d.year == year)
        assert count >= 8, f"Year {year} has only {count} holidays (expected >= 8)"
