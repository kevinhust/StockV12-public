#!/usr/bin/env python3
"""E-DATA — feature freshness preflight (ADR-010).

Checks data/features/daily/<SYM>.parquet last bar date against the expected
last US equity trading day (America/New_York).

Usage:
  PYTHONPATH=src:scripts:. python scripts/check_feature_freshness.py
  PYTHONPATH=src:scripts:. python scripts/check_feature_freshness.py \\
      --features-dir data/features/daily --max-staleness-days 0

Exit codes:
  0 — all symbols OK (within max_staleness_days)
  1 — one or more missing or stale
  2 — usage / hard failure (dir missing, etc.)

Wire into shadow cron optionally:
  check_feature_freshness.py || { echo stale features; exit 1; }
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
DEFAULT_FEATURES_DIR = REPO / "data" / "features" / "daily"
DEFAULT_SYMBOLS = [
    "AAPL",
    "NVDA",
    "TSLA",
    "JPM",
    "MSFT",
    "AMZN",
    "AMD",
    "META",
    "GOOGL",
    "SPY",
]
ET = ZoneInfo("America/New_York")
# After this local time on a weekday, expect today's daily bar to exist.
MARKET_CLOSE_HHMM = (16, 5)


def previous_trading_day(d: date) -> date:
    """Strict previous Mon–Fri calendar day before d."""
    cur = d - timedelta(days=1)
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur


def expected_last_trading_day(as_of: Optional[datetime] = None) -> date:
    """Expected last complete daily bar date in America/New_York.

    - Weekend → last Friday
    - Weekday before market close → previous trading day
    - Weekday at/after 16:05 ET → today
    """
    if as_of is None:
        as_of = datetime.now(ET)
    elif as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=ET)
    else:
        as_of = as_of.astimezone(ET)

    d = as_of.date()
    if d.weekday() >= 5:
        return previous_trading_day(d)

    close = as_of.replace(
        hour=MARKET_CLOSE_HHMM[0],
        minute=MARKET_CLOSE_HHMM[1],
        second=0,
        microsecond=0,
    )
    if as_of < close:
        return previous_trading_day(d)
    return d


def last_bar_date_from_parquet(path: Path) -> Optional[date]:
    """Return calendar date of max timestamp in a features parquet."""
    import pandas as pd

    df = pd.read_parquet(path)
    if "timestamp" not in df.columns:
        if isinstance(df.index, pd.DatetimeIndex) and len(df.index):
            ts = pd.to_datetime(df.index.max())
        else:
            return None
    else:
        if len(df) == 0:
            return None
        ts = pd.to_datetime(df["timestamp"]).max()
    if getattr(ts, "tzinfo", None) is not None or (
        hasattr(ts, "tz") and ts.tz is not None
    ):
        try:
            ts = ts.tz_convert(ET)
        except (TypeError, AttributeError):
            pass
    return ts.date() if hasattr(ts, "date") else date.fromisoformat(str(ts)[:10])


def check_features_dir(
    features_dir: Path,
    symbols: list[str],
    as_of: Optional[datetime] = None,
    max_staleness_days: int = 0,
) -> dict[str, Any]:
    """Check each symbol parquet against expected last trading day.

    max_staleness_days: allow last_bar to be this many *trading* days
    behind expected (0 = must match expected_last).
    """
    expected = expected_last_trading_day(as_of)
    # Build set of acceptable last dates (expected and up to N trading days back)
    acceptable: set[date] = {expected}
    cur = expected
    for _ in range(max_staleness_days):
        cur = previous_trading_day(cur)
        acceptable.add(cur)

    results: list[dict[str, Any]] = []
    missing: list[str] = []
    stale: list[str] = []
    ok_list: list[str] = []

    for sym in symbols:
        path = features_dir / f"{sym}.parquet"
        rec: dict[str, Any] = {"symbol": sym, "path": str(path)}
        if not path.exists():
            rec["status"] = "missing"
            rec["last_bar"] = None
            missing.append(sym)
            results.append(rec)
            continue
        try:
            last = last_bar_date_from_parquet(path)
        except Exception as e:
            rec["status"] = "error"
            rec["error"] = str(e)
            rec["last_bar"] = None
            missing.append(sym)
            results.append(rec)
            continue
        rec["last_bar"] = last.isoformat() if last else None
        if last is None:
            rec["status"] = "missing"
            missing.append(sym)
        elif last in acceptable or last > expected:
            rec["status"] = "ok"
            ok_list.append(sym)
        else:
            rec["status"] = "stale"
            stale.append(sym)
        results.append(rec)

    return {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "features_dir": str(features_dir),
        "expected_last": expected.isoformat(),
        "max_staleness_days": max_staleness_days,
        "ok": len(stale) == 0 and len(missing) == 0,
        "n_ok": len(ok_list),
        "n_stale": len(stale),
        "n_missing": len(missing),
        "ok_symbols": ok_list,
        "stale": stale,
        "missing": missing,
        "symbols": results,
        "adr": "ADR-010 E-DATA",
    }


def main() -> int:
    p = argparse.ArgumentParser(description="ADR-010 E-DATA feature freshness check")
    p.add_argument(
        "--features-dir",
        type=Path,
        default=DEFAULT_FEATURES_DIR,
        help=f"default: {DEFAULT_FEATURES_DIR}",
    )
    p.add_argument(
        "--symbols",
        nargs="+",
        default=DEFAULT_SYMBOLS,
        help="symbols to check (default: shadow main 10)",
    )
    p.add_argument(
        "--max-staleness-days",
        type=int,
        default=0,
        help="allow last bar this many trading days behind expected (default 0)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write JSON report path",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="print full JSON to stdout",
    )
    args = p.parse_args()

    if not args.features_dir.exists():
        print(f"ERROR: features dir missing: {args.features_dir}", file=sys.stderr)
        return 2

    report = check_features_dir(
        args.features_dir,
        symbols=args.symbols,
        max_staleness_days=args.max_staleness_days,
    )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2))
        print(f"Wrote {args.out}")

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(
            f"E-DATA feature freshness  expected_last={report['expected_last']}  "
            f"max_staleness_days={report['max_staleness_days']}"
        )
        print(
            f"  ok={report['n_ok']}  stale={report['n_stale']}  "
            f"missing={report['n_missing']}  overall_ok={report['ok']}"
        )
        if report["stale"]:
            print(f"  STALE: {', '.join(report['stale'])}")
        if report["missing"]:
            print(f"  MISSING: {', '.join(report['missing'])}")
        for rec in report["symbols"]:
            if rec["status"] != "ok":
                print(
                    f"    {rec['symbol']}: {rec['status']} last_bar={rec.get('last_bar')}"
                )

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
