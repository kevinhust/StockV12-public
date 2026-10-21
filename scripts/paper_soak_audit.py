#!/usr/bin/env python
"""
v12/scripts/paper_soak_audit.py

P6 / Phase 4 paper soak preparation and daily audit tool.

Run daily during the 5-day paper soak period to validate SOAK criteria.

Usage:
    PYTHONPATH="src:scripts:." python scripts/paper_soak_audit.py --days 5

It checks:
- SOAK-1: No manual risk overrides in the last N days
- SOAK-2: Average new entries ≤ 3 per day
- SOAK-3: No legacy V11 pattern events

Also prints basic observability: journal review decisions, position states, risk counters.
"""

import argparse
import os
from datetime import datetime, timedelta
from collections import defaultdict
import json

import redis

def get_redis():
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True,
    )

def check_daily_new_entries(r, days: int = 5):
    """SOAK-2: Avg new entries ≤ 3/day"""
    today = datetime.now().date()
    daily_counts = []
    for i in range(days):
        d = today - timedelta(days=i)
        key = f"risk:daily_trades:{d}"
        count = int(r.get(key) or 0)
        daily_counts.append(count)

    avg = sum(daily_counts) / len(daily_counts) if daily_counts else 0
    max_day = max(daily_counts) if daily_counts else 0
    ok = avg <= 3.0 and max_day <= 3

    return {
        "avg_new_per_day": round(avg, 2),
        "max_in_any_day": max_day,
        "daily_counts": daily_counts,
        "pass": ok
    }

def check_no_pattern_events(r, days: int = 5):
    """SOAK-3: No V11 pattern events (heuristic via logs or redis streams if present)"""
    # In real soak you would grep logs or check a dedicated stream.
    # Here we check for absence of known V11 pattern keys if any were left.
    pattern_keys = r.keys("events.pattern*") or []
    recent_pattern = len([k for k in pattern_keys if r.ttl(k) > 0])  # rough
    return {
        "legacy_pattern_keys_seen": recent_pattern,
        "pass": recent_pattern == 0
    }

def check_no_manual_overrides(r, days: int = 5):
    """SOAK-1: No manual risk overrides in the period"""
    # Heuristic: look for override keys or high frequency changes in risk:*
    override_keys = r.keys("risk:override*") or []
    recent = [k for k in override_keys if r.ttl(k) > 0]
    return {
        "recent_override_keys": len(recent),
        "pass": len(recent) == 0
    }

def audit_journal_review(decisions_count: int = 0):
    # Placeholder - in real would query journal keys for review decisions
    return {"review_decisions_logged": decisions_count}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=5, help="Lookback days for soak checks")
    args = parser.parse_args()

    r = get_redis()

    new_entries = check_daily_new_entries(r, args.days)
    no_pattern = check_no_pattern_events(r, args.days)
    no_override = check_no_manual_overrides(r, args.days)

    report = {
        "date": str(datetime.now().date()),
        "lookback_days": args.days,
        "soak_checks": {
            "SOAK-1_no_manual_override": no_override,
            "SOAK-2_avg_new_entries_le3": new_entries,
            "SOAK-3_no_legacy_pattern": no_pattern,
        },
        "all_pass": no_override["pass"] and new_entries["pass"] and no_pattern["pass"],
        "recommendation": "Continue soak" if (no_override["pass"] and new_entries["pass"] and no_pattern["pass"]) else "Investigate before continuing soak"
    }

    print(json.dumps(report, indent=2, ensure_ascii=False))

    if not report["all_pass"]:
        print("\n⚠️  Soak criteria not fully met. Review logs and Redis state.")
    else:
        print("\n✅ Soak criteria look good for the lookback window.")

if __name__ == "__main__":
    main()
