#!/usr/bin/env python3
"""
v12_soak_daily.py

Daily combined soak audit for StockApp V12 (P6 / Phase 4 + go-live criteria).

Runs two checks:
  1. paper_soak_checklist.py — SOAK-1..6 (manual overrides, daily entries,
     legacy patterns, YOLO firing, model files, env vars)
  2. v12_deployment_health.py — pipeline health, fill-rate expectation,
     Redis observability, data freshness

Output:
  - Always writes JSON to logs/v12_soak_<date>.json (for trend analysis)
  - Prints merged JSON to stdout
  - Sends a Telegram alert ONLY when any check fails (rate-limited to 1/hr)

Exit codes:
  0  all checks pass
  1  one or more checks fail
  2  precondition error (config missing, redis unreachable, scripts not found)

Designed to run as a daily cron at 17:00 ET (after market close).
Can also be run manually for ad-hoc audits.

Usage:
    PYTHONPATH="src:scripts:." python3 scripts/v12_soak_daily.py
    PYTHONPATH="src:scripts:." python3 scripts/v12_soak_daily.py --days 5
    PYTHONPATH="src:scripts:." python3 scripts/v12_soak_daily.py --no-telegram
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
# Post-migration (2026-07-09): v12/ was promoted to project root, so scripts/
# lives directly under repo root rather than under v12/scripts/.
PAPER_SOAK_CHECKLIST = _REPO_ROOT / "scripts/paper_soak_checklist.py"
DEPLOYMENT_HEALTH = _REPO_ROOT / "scripts/v12_deployment_health.py"
DEFAULT_CONFIG = _REPO_ROOT / "config/paper_soak.yaml"
LOG_DIR = _REPO_ROOT / "logs"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("OPERATOR_TELEGRAM_ID") or os.getenv("TELEGRAM_CHAT_ID")
# Rate-limit key for telegram dedup
_TELEGRAM_LAST_SENT_FILE = LOG_DIR / ".v12_soak_telegram_last_sent"


def run_subprocess(script: Path, extra_args: list[str] | None = None) -> tuple[int, dict]:
    """Run a validator script and return (exit_code, parsed_json_output).

    Tolerates non-JSON output by returning a wrapper dict.
    """
    args = ["python3", str(script)] + (extra_args or [])
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(_REPO_ROOT),
        )
        stdout = result.stdout or ""
        # Both validators print a single JSON document, sometimes followed by
        # loguru or print() warnings. Use raw_decode to parse exactly one
        # top-level JSON value from the first '{' position.
        first_brace = stdout.find("{")
        if first_brace >= 0:
            try:
                decoder = json.JSONDecoder()
                obj, _end = decoder.raw_decode(stdout, first_brace)
                return result.returncode, obj
            except json.JSONDecodeError:
                pass
        return result.returncode, {
            "error": "non_json_output",
            "exit_code": result.returncode,
            "stdout_tail": stdout[-500:],
            "stderr_tail": (result.stderr or "")[-200:],
        }
    except subprocess.TimeoutExpired:
        return 124, {"error": "timeout", "script": str(script)}
    except Exception as e:
        return 1, {"error": str(e), "script": str(script)}


def send_telegram_alert(text: str, dedup_hours: int = 1) -> bool:
    """Send telegram alert. Rate-limited via filesystem mtime."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if _TELEGRAM_LAST_SENT_FILE.exists():
        last_mtime = _TELEGRAM_LAST_SENT_FILE.stat().st_mtime
        if (time.time() - last_mtime) < dedup_hours * 3600:
            return False

    try:
        import requests
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text[:4000],
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=8)
        if resp.status_code == 200:
            _TELEGRAM_LAST_SENT_FILE.touch()
            return True
        return False
    except Exception:
        return False


def build_telegram_summary(report: dict) -> str:
    """Build a compact Telegram alert listing all issues."""
    issues_soak = report.get("soak_checklist", {}).get("issues", [])
    issues_dep = report.get("deployment_health", {}).get("issues_found", [])
    today = report.get("date", datetime.now().date().isoformat())
    all_issues = issues_soak + issues_dep
    if not all_issues:
        return ""

    lines = [
        f"⚠️ *V12 Soak Audit — {today}*",
        "",
        f"*{len(all_issues)} issue(s):*",
    ]
    for i, issue in enumerate(all_issues[:10], 1):
        lines.append(f"{i}. {issue}")
    if len(all_issues) > 10:
        lines.append(f"... +{len(all_issues) - 10} more (see JSON log)")
    lines.append("")
    lines.append("Run `scripts/v12_deployment_health.py` for details.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="V12 daily soak audit (combined)")
    parser.add_argument("--days", type=int, default=5, help="Lookback days (default 5)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--no-telegram", action="store_true", help="Skip telegram alerts")
    args = parser.parse_args()

    # Pre-check: scripts exist
    if not PAPER_SOAK_CHECKLIST.exists():
        print(f"ERROR: missing {PAPER_SOAK_CHECKLIST}", file=sys.stderr)
        return 2
    if not DEPLOYMENT_HEALTH.exists():
        print(f"ERROR: missing {DEPLOYMENT_HEALTH}", file=sys.stderr)
        return 2

    # Run both validators
    soak_rc, soak_report = run_subprocess(
        PAPER_SOAK_CHECKLIST,
        ["--days", str(args.days), "--config", str(args.config)],
    )
    dep_rc, dep_report = run_subprocess(DEPLOYMENT_HEALTH, [])

    # Merge
    today = datetime.now().date().isoformat()
    merged = {
        "date": today,
        "timestamp_utc": datetime.now().astimezone().isoformat(),
        "lookback_days": args.days,
        "soak_checklist": {
            "exit_code": soak_rc,
            "all_pass": soak_report.get("all_pass", False),
            "issues": [
                f"SOAK: {k} failed"
                for k, v in soak_report.get("soak_checks", {}).items()
                if isinstance(v, dict) and v.get("pass") is False
            ],
            "raw_summary": {
                k: v.get("pass") if isinstance(v, dict) else None
                for k, v in soak_report.get("soak_checks", {}).items()
            },
        },
        "deployment_health": {
            "exit_code": dep_rc,
            "healthy": dep_report.get("healthy", False),
            "issues_found": dep_report.get("issues_found", []),
            "lifecycle_job_runs": {
                k: v.get("total_runs", 0)
                for k, v in dep_report.get("checks", {}).get("lifecycle_jobs", {}).items()
            },
            "fill_rate_expectation": dep_report.get("checks", {}).get("fill_rate_expectation", {}),
            "model_artifacts_ok": (
                dep_report.get("checks", {}).get("model_artifacts", {}).get("chronos_exists", False)
                and dep_report.get("checks", {}).get("model_artifacts", {}).get("yolo_exists", False)
            ),
            "env_vars_ok": dep_report.get("checks", {}).get("env_vars", {}).get("pass", False),
        },
        "all_pass": (soak_rc == 0 and dep_rc == 0),
    }

    # Always write JSON log
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"v12_soak_{today}.json"
    with open(log_file, "w") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    # Print merged report
    print(json.dumps(merged, indent=2, ensure_ascii=False))

    # Telegram alert (only on issues)
    if not merged["all_pass"] and not args.no_telegram:
        msg = build_telegram_summary(merged)
        if msg:
            sent = send_telegram_alert(msg)
            print(f"\n[telegram] alert sent={sent} (rate-limited 1/hr)")

    return 0 if merged["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())