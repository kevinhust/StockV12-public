#!/usr/bin/env python3
"""
v12/scripts/v12_deployment_health.py

Deployment health validator for StockApp V12.

What this script answers:
  1. Is the production scheduler actually running paper-soak trades?
  2. Are YOLO + Chronos models loaded and being invoked?
  3. What's the actual fill rate vs the 3.7b baseline expectation?
  4. Is the lifecycle entry_window job firing?

Reads:
  - Redis (lifecycle:* keys, SR-06 counters, entry counters)
  - Models directory (chronos, yolo)
  - config/paper_soak.yaml
  - /tmp/backtest_p0_3_7b.json (3.7b baseline for fill-rate comparison)

Writes JSON to stdout and exits 0 if healthy, 1 if degraded.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = _REPO_ROOT / "config" / "paper_soak.yaml"
BASELINE_3_7B = "/tmp/backtest_p0_3_7b.json"


def get_redis():
    import redis
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        db=int(os.getenv("REDIS_DB", 0)),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


# ── 1. Redis health ─────────────────────────────────────────────────────────
def check_redis_alive(r) -> dict:
    try:
        r.ping()
        return {"alive": True}
    except Exception as e:
        return {"alive": False, "error": str(e)}


# ── 2. Lifecycle job health ──────────────────────────────────────────────────
def check_lifecycle_jobs(r, days: int = 5) -> dict:
    """Check that the key jobs have run in the lookback window."""
    jobs = [
        "entry_window_close", "daily_review", "post_mortem",
        "swing_scanner", "trailing_stop_check", "telegram_heartbeat",
        "job_health_monitor", "rag_integrity",
    ]
    today = datetime.now().date()
    out = {}
    for job in jobs:
        # last_run timestamp
        last_run_key = f"lifecycle:last_run:{job}"
        last_run = r.get(last_run_key)
        # total runs (all-time)
        total = r.get(f"lifecycle:stats:total:{job}")
        # failures
        failures = r.get(f"lifecycle:stats:failure:{job}")
        # last error
        last_err = r.get(f"lifecycle:last_error:{job}")
        out[job] = {
            "last_run": last_run,
            "total_runs": int(total) if total else 0,
            "failures": int(failures) if failures else 0,
            "last_error": last_err[:100] if last_err else None,
        }
    return out


# ── 3. YOLO filter observability ────────────────────────────────────────────
def check_yolo_filter_observability(r, days: int = 5) -> dict:
    """SOAK-4: SR-06 (YOLO no-trade) rejects must be tracked in Redis.

    The lifecycle scheduler SHOULD write lifecycle:sr06_yolo_rejects:<date>
    for each day SR-06 fires. If those keys don't exist, the scheduler is
    not actually invoking the YOLO filter.
    """
    today = datetime.now().date()
    daily = {}
    total = 0
    for i in range(days):
        d = today - timedelta(days=i)
        key = f"lifecycle:sr06_yolo_rejects:{d.isoformat()}"
        v = r.get(key)
        cnt = int(v) if v else 0
        daily[d.isoformat()] = cnt
        total += cnt

    # Also check the legacy key shape (lifecycle:stats:*)
    legacy_keys = r.keys("lifecycle:sr06*")
    legacy_keys += r.keys("*yolo*")
    return {
        "sr06_daily_counts": daily,
        "sr06_total_in_lookback": total,
        "legacy_or_alternate_keys_found": list(set(legacy_keys)),
        "filter_invoked_in_lookback": total > 0,
        "pass": total > 0,
        "note": "If pass=false, either (a) SR-06 never fires (model issue), or (b) the scheduler doesn't write these keys (observability gap).",
    }


# ── 4. Trade entry observability ─────────────────────────────────────────────
def check_trade_entry_observability(r, days: int = 5) -> dict:
    """Are trade entries being recorded at all in Redis?"""
    today = datetime.now().date()
    daily = {}
    for i in range(days):
        d = today - timedelta(days=i)
        key = f"lifecycle:entries:{d.isoformat()}"
        v = r.get(key)
        daily[d.isoformat()] = int(v) if v else 0

    # Search for any entries keys
    entry_keys = r.keys("*entries*") + r.keys("*trades*") + r.keys("*entry_count*")
    entry_keys = list(set(entry_keys))

    return {
        "daily_entry_counts": daily,
        "entry_related_keys_in_redis": entry_keys,
        "any_paper_fills_recorded": sum(daily.values()) > 0,
    }


# ── 5. Model artifacts ──────────────────────────────────────────────────────
def check_model_artifacts(cfg: dict) -> dict:
    models_cfg = cfg.get("models", {})
    chronos_rel = models_cfg.get("chronos", "")
    yolo_rel = models_cfg.get("yolo", "")

    chronos_path = _REPO_ROOT / chronos_rel if chronos_rel else None
    yolo_path = _REPO_ROOT / yolo_rel if yolo_rel else None

    out = {}
    if chronos_path:
        out["chronos_path"] = str(chronos_path)
        out["chronos_exists"] = chronos_path.exists()
        if chronos_path.exists():
            # Count checkpoints
            ckpts = list(chronos_path.rglob("*.ckpt")) + list(chronos_path.rglob("*.bin")) + list(chronos_path.rglob("*.safetensors"))
            out["chronos_checkpoints"] = [str(p) for p in ckpts[:5]]
    if yolo_path:
        out["yolo_path"] = str(yolo_path)
        out["yolo_exists"] = yolo_path.exists()
        if yolo_path.exists():
            out["yolo_size_mb"] = round(yolo_path.stat().st_size / 1024 / 1024, 1)

    # Also check intraday image dir for YOLO input
    cfg_paths = cfg.get("paths", {}) or {}
    yolo_img_rel = cfg_paths.get("yolo_image_dir", "")
    if yolo_img_rel:
        img_dir = _REPO_ROOT / yolo_img_rel
        out["yolo_image_dir"] = str(img_dir)
        out["yolo_image_dir_exists"] = img_dir.exists()
        if img_dir.exists():
            # Count images for today
            today_str = datetime.now().strftime("%Y%m%d")
            today_dir = img_dir / today_str
            if today_dir.exists():
                images = list(today_dir.rglob("*.png")) + list(today_dir.rglob("*.jpg"))
                out["yolo_images_today"] = len(images)
    return out


# ── 6. Data availability ────────────────────────────────────────────────────
def check_data_availability(cfg: dict) -> dict:
    cfg_paths = cfg.get("paths", {}) or {}
    features_rel = cfg_paths.get("features_dir", "data/features/daily")
    features_dir = _REPO_ROOT / features_rel
    out = {"features_dir": str(features_dir), "exists": features_dir.exists()}
    if features_dir.exists():
        parquets = sorted(features_dir.glob("*.parquet"))
        out["symbol_count"] = len(parquets)
        # Check freshness of last 5 parquets
        latest_dates = []
        for pq in parquets[:5]:
            try:
                import pandas as pd
                # Try both 'date' and 'timestamp' column names
                df = pd.read_parquet(pq)
                date_col = "date" if "date" in df.columns else ("timestamp" if "timestamp" in df.columns else None)
                if date_col and not df.empty:
                    latest_dates.append({
                        "symbol": pq.stem,
                        "max_date": str(df[date_col].max()),
                        "rows": len(df),
                    })
                else:
                    latest_dates.append({"symbol": pq.stem, "error": "no date/timestamp col", "cols": list(df.columns)[:5]})
            except Exception as e:
                latest_dates.append({"symbol": pq.stem, "error": str(e)[:80]})
        out["sample_freshness"] = latest_dates
    return out


# ── 7. Env vars ─────────────────────────────────────────────────────────────
def check_required_env_vars(cfg: dict) -> dict:
    required = cfg.get("required_env_vars", [])
    missing = [n for n in required if not os.getenv(n)]
    return {
        "required": required,
        "present": [n for n in required if os.getenv(n)],
        "missing": missing,
        "pass": len(missing) == 0,
    }


# ── 8. Fill-rate vs 3.7b baseline ───────────────────────────────────────────
def compute_fill_rate_expectation(baseline_path: str) -> dict:
    """Compute expected daily entries from the 3.7b baseline.

    The 3.7b baseline JSON has structure: {overall: {total_trades, ...}, risk_gates: {...}}.
    We know from CLI: data window 2018-01-02 to 2026-06-18 (~8.46y × 252 = ~2132 trading days).
    """
    p = Path(baseline_path)
    if not p.exists():
        return {"baseline_available": False, "note": f"{baseline_path} not found"}
    try:
        with open(p) as f:
            data = json.load(f)
        overall = data.get("overall", data)
        trades = overall.get("total_trades", 0)
        # Hardcode the data window (3.7b ran 2018-01-02 to 2026-06-18)
        from datetime import date
        s = date.fromisoformat("2018-01-02")
        e = date.fromisoformat("2026-06-18")
        days = int(((e - s).days / 365.25) * 252)  # 252 trading days/year
        if trades and days:
            per_day = trades / days
            per_5d = per_day * 5
            return {
                "baseline_available": True,
                "baseline_source": baseline_path,
                "data_window": "2018-01-02..2026-06-18",
                "data_trading_days": days,
                "total_trades_3_7b": trades,
                "trades_per_day": round(per_day, 3),
                "trades_per_5d_window_expected": round(per_5d, 1),
                "pass_band_low": round(per_5d * 0.5, 1),
                "pass_band_high": round(per_5d * 1.5, 1),
                "note": "Healthy V12 paper-soak should hit 0.5x-1.5x of expected over time",
            }
    except Exception as e:
        return {"baseline_available": False, "error": str(e)}
    return {"baseline_available": False}


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    import yaml
    if not DEFAULT_CONFIG.exists():
        print(f"ERROR: config not found: {DEFAULT_CONFIG}", file=sys.stderr)
        return 2

    with open(DEFAULT_CONFIG) as f:
        cfg = yaml.safe_load(f) or {}

    report = {"date": str(datetime.now().date()), "checks": {}}

    try:
        r = get_redis()
        report["checks"]["redis_alive"] = check_redis_alive(r)
        if not report["checks"]["redis_alive"]["alive"]:
            print(json.dumps(report, indent=2))
            return 2
        report["checks"]["lifecycle_jobs"] = check_lifecycle_jobs(r)
        report["checks"]["yolo_filter_observability"] = check_yolo_filter_observability(r)
        report["checks"]["trade_entry_observability"] = check_trade_entry_observability(r)
    except Exception as e:
        report["checks"]["redis_error"] = str(e)
        print(json.dumps(report, indent=2))
        return 2

    report["checks"]["model_artifacts"] = check_model_artifacts(cfg)
    report["checks"]["data_availability"] = check_data_availability(cfg)
    report["checks"]["env_vars"] = check_required_env_vars(cfg)
    report["checks"]["fill_rate_expectation"] = compute_fill_rate_expectation(BASELINE_3_7B)

    # Verdict
    issues = []
    ewc = report["checks"]["lifecycle_jobs"].get("entry_window_close", {})
    ewc_runs = ewc.get("total_runs", 0)
    pipeline_ran = ewc_runs > 0
    if not report["checks"]["yolo_filter_observability"]["filter_invoked_in_lookback"]:
        if not pipeline_ran:
            issues.append("YOLO filter not invoked in last 5d (entry pipeline never ran)")
        else:
            issues.append("YOLO SR-06 counter never incremented in last 5d — pipeline ran but SR-06 never fired (acceptable if all forecasts weak; check log for SR-01 vs SR-06 split)")
    if not report["checks"]["env_vars"]["pass"]:
        issues.append(f"Missing required env vars: {report['checks']['env_vars']['missing']}")
    if ewc_runs == 0:
        issues.append("entry_window_close has never run — production entry path is disabled")
    elif not report["checks"]["trade_entry_observability"]["any_paper_fills_recorded"]:
        # Soft warning: pipeline ran but 0 fills. Most likely cause is
        # all forecasts blocked by SR-01 (forecast too weak — correct
        # discipline). Note: actionable:today Redis key is no longer
        # populated (hygiene-I removed the premarket_review cron);
        # the live entry path uses in-process ctx.actionable_symbols.
        issues.append(
            f"entry_window_close ran {ewc_runs}× with 0 fills — likely all blocked by "
            "SR-01 (forecast too weak); check log for decision codes"
        )

    report["issues_found"] = issues
    report["healthy"] = len(issues) == 0

    print(json.dumps(report, indent=2, ensure_ascii=False))

    return 0 if report["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())