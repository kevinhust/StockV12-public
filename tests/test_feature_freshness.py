# tests/test_feature_freshness.py
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from scripts.check_feature_freshness import (
    DEFAULT_SYMBOLS,
    check_features_dir,
    expected_last_trading_day,
    last_bar_date_from_parquet,
    previous_trading_day,
)


def test_previous_trading_day_skips_weekend():
    # Monday 2026-07-20 -> Friday 2026-07-17
    assert previous_trading_day(date(2026, 7, 20)) == date(2026, 7, 17)
    # Sunday -> Friday
    assert previous_trading_day(date(2026, 7, 19)) == date(2026, 7, 17)
    # Tuesday -> Monday
    assert previous_trading_day(date(2026, 7, 21)) == date(2026, 7, 20)


def test_expected_last_trading_day_before_close_is_prev():
    # 10:00 ET on a Tuesday — market not closed, expect Monday
    as_of = datetime(2026, 7, 21, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    assert expected_last_trading_day(as_of) == date(2026, 7, 20)


def test_expected_last_trading_day_after_close_is_today_if_weekday():
    # 16:30 ET Tuesday — today's bar should exist
    as_of = datetime(2026, 7, 21, 16, 30, tzinfo=ZoneInfo("America/New_York"))
    assert expected_last_trading_day(as_of) == date(2026, 7, 21)


def test_expected_last_trading_day_weekend_is_friday():
    as_of = datetime(2026, 7, 26, 12, 0, tzinfo=ZoneInfo("America/New_York"))  # Sunday
    assert expected_last_trading_day(as_of) == date(2026, 7, 24)


def test_last_bar_date_from_parquet(tmp_path: Path):
    p = tmp_path / "AAPL.parquet"
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                ["2026-07-22", "2026-07-23", "2026-07-24"]
            ).tz_localize("America/New_York"),
            "close": [1.0, 2.0, 3.0],
        }
    )
    df.to_parquet(p, index=False)
    assert last_bar_date_from_parquet(p) == date(2026, 7, 24)


def test_check_features_dir_flags_stale(tmp_path: Path):
    features = tmp_path / "daily"
    features.mkdir()
    for sym, last in [("AAPL", "2026-07-24"), ("NVDA", "2026-07-20")]:
        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime([last]).tz_localize("America/New_York"),
                "close": [100.0],
            }
        )
        df.to_parquet(features / f"{sym}.parquet", index=False)

    as_of = datetime(2026, 7, 26, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    report = check_features_dir(
        features,
        symbols=["AAPL", "NVDA", "MSFT"],
        as_of=as_of,
        max_staleness_days=0,
    )
    assert report["expected_last"] == "2026-07-24"
    assert report["ok"] is False
    by = {r["symbol"]: r for r in report["symbols"]}
    assert by["AAPL"]["status"] == "ok"
    assert by["NVDA"]["status"] == "stale"
    assert by["MSFT"]["status"] == "missing"
    assert "MSFT" in report["missing"]
    assert "NVDA" in report["stale"]


def test_default_symbols_match_shadow_ten():
    assert "SPY" in DEFAULT_SYMBOLS
    assert len(DEFAULT_SYMBOLS) == 10
