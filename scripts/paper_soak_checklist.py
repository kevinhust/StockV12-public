#!/usr/bin/env python3
"""
scripts/paper_soak_checklist.py

P6 / Phase 4 paper soak checklist runner.

Extends scripts/paper_soak_audit.py with the Wave 9 / Task 11 SOAK checks:
  SOAK-4: YOLO no-trade filter is firing (lifecycle:sr06_yolo_rejects:* > 0)
  SOAK-5: Chronos + YOLO model files resolve on disk (per config/paper_soak.yaml)
  SOAK-6: Scheduler boots with required env vars (STOCKAPP_V12_RISK,
          STOCKAPP_V12_ENTRY_ENABLED)

Reuses SOAK-1/2/3 from paper_soak_audit (no override / avg entries / no legacy).

Usage:
    PYTHONPATH="src:scripts:." python scripts/paper_soak_checklist.py
    PYTHONPATH="src:scripts:." python scripts/paper_soak_checklist.py --days 5
    PYTHONPATH="src:scripts:." python scripts/paper_soak_checklist.py --config config/paper_soak.yaml

Exit codes:
    0  all SOAK checks pass
    1  one or more SOAK checks fail (review the printed report)
    2  precondition error (config missing, redis unreachable)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Resolve repo root so config/paper_soak.yaml is found regardless of CWD.
# scripts/paper_soak_checklist.py → parents[1] is repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = _REPO_ROOT / "config" / "paper_soak.yaml"


def load_config(path: Path) -> dict:
    """Load YAML config; fall back to minimal defaults if PyYAML is missing."""
    if not path.exists():
        raise FileNotFoundError(f"Soak config not found: {path}")
    try:
        import yaml  # PyYAML is a project dep via the existing audit script
    except ImportError as e:
        raise RuntimeError(
            "PyYAML is required for paper_soak_checklist.py "
            "(already used by paper_soak_audit.py dependencies)."
        ) from e
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_redis():
    """Synchronous redis client (matches paper_soak_audit.py)."""
    import redis
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "redis"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


# ── SOAK-7 sign-agreement pure helpers (added 2026-06-29) ───────────────
# These exist so SOAK-7 has a unit-testable core that does not depend on
# Redis. The lifecycle_scheduler writes one Redis key per entry attempt:
#   - soak:forecast:chronos:<YYYY-MM-DD>:<SYM>  → signed forecast_mean (str)
#   - soak:trade:side:<YYYY-MM-DD>:<SYM>         → "BUY" | "SELL"
# Both keys have a 7-day TTL. SOAK-7 reads them back, joins by (date, sym),
# and computes sign-agreement rate over the lookback window.


def sign(x: float) -> int:
    """Return -1, 0, or +1 based on the sign of `x`.

    Zero and exact-zero return 0 (flat). Strictly positive returns +1,
    strictly negative returns -1.
    """
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def side_to_sign(side: str | None) -> int:
    """Map a trade-side string to a sign.

    "BUY" → +1, "SELL" → -1, anything else (None, "", "HOLD", unknown)
    → 0 (flat / no signal).
    """
    if side == "BUY":
        return 1
    if side == "SELL":
        return -1
    return 0


def sign_match(forecast: float, side: str) -> bool:
    """True if sign(forecast) == side_to_sign(side).

    Both flat (0 vs 0) counts as a match by convention — neither disagreed
    with the other. A flat forecast with a BUY/SELL side does NOT match,
    because in that case the entry direction came from something other
    than Chronos (YOLO pass-through, regime override, etc.).
    """
    return sign(forecast) == side_to_sign(side)


def sign_agreement_rate(
    pairs: list[tuple[float, str]],
    floor: float = 0.5,
) -> dict:
    """Aggregate sign-agreement over a list of (forecast, side) pairs.

    Returns:
      - n:               total pairs evaluated (incl. flat-forecast pairs)
      - n_agree:         count where sign_match(fc, side) is True
      - n_flat_fc:       count where sign(fc) == 0 (forecast was flat)
      - rate:            n_agree / n  (None if n == 0)
      - pass:            rate is not None and rate >= floor
                         (None if n == 0 — no data, no verdict)
    """
    n = len(pairs)
    if n == 0:
        return {"n": 0, "n_agree": 0, "n_flat_fc": 0, "rate": None, "pass": None}

    n_agree = 0
    n_flat_fc = 0
    for fc, side in pairs:
        if sign(fc) == 0:
            n_flat_fc += 1
        if sign_match(fc, side):
            n_agree += 1

    rate = round(n_agree / n, 4)
    return {
        "n": n,
        "n_agree": n_agree,
        "n_flat_fc": n_flat_fc,
        "rate": rate,
        "pass": rate >= floor,
    }


def _scan_dated_keys(r, prefix: str, today: datetime.date, days: int) -> list[str]:
    """Return all `prefix:<YYYY-MM-DD>` keys in [today-days+1 .. today].

    Uses SCAN-style pattern matching via KEYS (paper-soak is low-volume,
    not a hot path). Filters to date-suffixed entries within the lookback.
    """
    import fnmatch
    pattern = f"{prefix}:*"
    try:
        keys = r.keys(pattern) or []
    except Exception:
        return []
    cutoff = today - timedelta(days=days - 1)
    out = []
    for k in keys:
        # Expect keys like prefix:YYYY-MM-DD:SYM (SYM may contain alnum + -)
        parts = k.split(":")
        if len(parts) < 4:
            continue
        date_str = parts[-2]
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if cutoff <= d <= today:
            out.append(k)
    return out


def check_sign_agreement(r, cfg: dict, days: int = 5) -> dict:
    """SOAK-7: Sign-agreement rate between Chronos forecast and trade side.

    Reads two Redis key families written by the lifecycle_scheduler on every
    entry attempt:
      - <chronos_signal_key_prefix>:<date>:<sym>  → signed forecast_mean
      - <trade_side_key_prefix>:<date>:<sym>       → "BUY" | "SELL"

    Joins them by (date, sym), evaluates sign_match, and aggregates over the
    lookback window. The check is warn-only by design — a low agreement
    rate during P6 is consistent with the known OOS finding (v2 hit_rate
    0.30 on the 10-symbol panel) and we want to *measure* live behavior,
    not block the soak on it. See v12/docs/H2H_V3_REPORT.md "Directional
    (sign-correctness) metrics" for the OOS finding.

    Floor defaults to 0.5 (coin flip). Configure via cfg["sign_agreement_floor"].
    """
    redis_cfg = cfg.get("redis", {}) or {}
    fc_prefix = redis_cfg.get("chronos_signal_key_prefix", "soak:forecast:chronos")
    sd_prefix = redis_cfg.get("trade_side_key_prefix", "soak:trade:side")
    floor = float(cfg.get("sign_agreement_floor", 0.5))

    today = datetime.now().date()
    fc_keys = _scan_dated_keys(r, fc_prefix, today, days)
    sd_keys = _scan_dated_keys(r, sd_prefix, today, days)

    # Build side lookup: (date, sym) → side string
    side_by_pair: dict[tuple[str, str], str] = {}
    for k in sd_keys:
        parts = k.split(":")
        if len(parts) < 4:
            continue
        side_by_pair[(parts[-2], parts[-1])] = r.get(k) or ""

    pairs: list[tuple[float, str]] = []
    n_skipped_no_side = 0
    n_skipped_unparseable = 0
    for k in fc_keys:
        parts = k.split(":")
        if len(parts) < 4:
            continue
        date_str, sym = parts[-2], parts[-1]
        side = side_by_pair.get((date_str, sym))
        if side is None or side == "":
            # Forecast recorded but no trade side — entry was filtered out
            # (YOLO SR-06, regime override, cooldown, etc.). Skip — we only
            # evaluate cases where Chronos actually led to a trade.
            n_skipped_no_side += 1
            continue
        raw = r.get(k)
        try:
            fc = float(raw)
        except (TypeError, ValueError):
            n_skipped_unparseable += 1
            continue
        pairs.append((fc, side))

    agg = sign_agreement_rate(pairs, floor=floor)

    if agg["n"] == 0:
        note = (
            f"no entries in last {days}-day window; cannot evaluate sign agreement. "
            "This is expected on T-1d or if YOLO/SR-06 has filtered every "
            "Chronos signal."
        )
    else:
        note = (
            f"sign-agreement rate = {agg['rate']:.3f} over {agg['n']} entries "
            f"(floor={floor}). Compares to OOS hit_rate=0.30 for phase2_v2 on "
            "the 10-symbol panel (v12/docs/H2H_V3_REPORT.md). A live rate near "
            "or below 0.5 confirms the OOS finding; a live rate consistently "
            "above 0.5 would suggest the OOS panel underestimates live edge."
        )

    return {
        "n_entries": agg["n"],
        "n_agree": agg["n_agree"],
        "n_flat_fc": agg["n_flat_fc"],
        "rate": agg["rate"],
        "floor": floor,
        "pass": agg["pass"],  # None if n==0 (no data), True/False otherwise
        "skipped_no_side": n_skipped_no_side,
        "skipped_unparseable": n_skipped_unparseable,
        "lookback_days": days,
        "note": note,
    }


# ── SOAK-4: YOLO filter is firing ───────────────────────────────────────────
def check_yolo_filter_firing(r, cfg: dict, days: int = 5) -> dict:
    """SOAK-4: at least one SR-06 reject in the lookback window.

    Reads `lifecycle:sr06_yolo_rejects:<date>` keys populated by
    src/services/lifecycle_scheduler.py whenever SR-06 fires.
    """
    today = datetime.now().date()
    sr06_key_pattern = cfg.get("redis", {}).get(
        "sr06_yolo_rejects_key", "lifecycle:sr06_yolo_rejects"
    ) + ":*"
    # Date-suffixed keys (YYYY-MM-DD); scheduler writes <key>:<date>
    daily_counts: dict[str, int] = {}
    total = 0
    for i in range(days):
        d = today - timedelta(days=i)
        key = f"{sr06_key_pattern.rstrip('*')}{d.isoformat()}"
        try:
            val = r.get(key)
            count = int(val) if val is not None else 0
        except Exception:
            count = 0
        daily_counts[d.isoformat()] = count
        total += count

    return {
        "sr06_daily_counts": daily_counts,
        "sr06_total_in_lookback": total,
        "pass": total > 0,
        "note": (
            "pass = SR-06 has fired at least once in the lookback window, "
            "confirming YoloNoTradeFilter is loaded and making predictions."
        ),
    }


# ── SOAK-5: model files resolve on scheduler path ──────────────────────────
def check_model_files_resolve(cfg: dict) -> dict:
    """SOAK-5: Chronos + YOLO model paths from config exist on disk."""
    models_cfg = cfg.get("models", {})
    chronos_rel = models_cfg.get("chronos", "")
    yolo_rel = models_cfg.get("yolo", "")

    chronos_path = _REPO_ROOT / chronos_rel
    yolo_path = _REPO_ROOT / yolo_rel

    chronos_ok = chronos_path.exists()
    yolo_ok = yolo_path.exists()

    return {
        "chronos_path": str(chronos_path),
        "chronos_exists": chronos_ok,
        "yolo_path": str(yolo_path),
        "yolo_exists": yolo_ok,
        "pass": chronos_ok and yolo_ok,
        "note": (
            "Both model paths from config/paper_soak.yaml must resolve on "
            "the scheduler container's filesystem before T+0."
        ),
    }


# ── SOAK-6: required env vars set ───────────────────────────────────────────
def check_required_env_vars(cfg: dict) -> dict:
    """SOAK-6: All env vars required by scheduler are set to non-empty values."""
    required = cfg.get("required_env_vars", [])
    missing = [name for name in required if not os.getenv(name)]
    return {
        "required": required,
        "missing": missing,
        "present": [name for name in required if os.getenv(name)],
        "pass": len(missing) == 0,
        "note": (
            "These env vars must be set in the scheduler container environment "
            "before _run_entry_window is invoked. They are typically set in "
            "scripts/run_paper_soak.sh and/or docker-compose."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="P6 paper soak checklist runner")
    parser.add_argument(
        "--days", type=int, default=5, help="Lookback days for SOAK-1..4 (default: 5)"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Path to soak config (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--skip-redis",
        action="store_true",
        help="Skip Redis-dependent checks (SOAK-1..4); use only for offline model/env verification.",
    )
    args = parser.parse_args()

    # 1. Load config
    try:
        cfg = load_config(args.config)
    except (FileNotFoundError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    # 2. SOAK-5 (model files) and SOAK-6 (env vars) — no redis needed
    soak5 = check_model_files_resolve(cfg)
    soak6 = check_required_env_vars(cfg)

    # 3. SOAK-1..3 from paper_soak_audit + SOAK-4 (YOLO) — redis needed
    if args.skip_redis:
        report_soak_1234 = {
            "skipped": True,
            "reason": "--skip-redis flag set; SOAK-1..4 not evaluated",
        }
        soak4 = {"skipped": True}
        all_pass = soak5["pass"] and soak6["pass"]
    else:
        try:
            r = get_redis()
            r.ping()
        except Exception as e:
            print(f"ERROR: redis unreachable: {e}", file=sys.stderr)
            return 2

        # Reuse existing SOAK-1..3 by importing the audit script directly.
        # paper_soak_audit.py prints to stdout; we re-run its checks in-process
        # by importing its module so we can capture the structured return values.
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
        try:
            import paper_soak_audit as _audit
        except Exception as e:
            print(f"ERROR: failed to import paper_soak_audit: {e}", file=sys.stderr)
            return 2

        soak1 = _audit.check_no_manual_overrides(r, args.days)
        soak2 = _audit.check_daily_new_entries(r, args.days)
        soak3 = _audit.check_no_pattern_events(r, args.days)
        soak4 = check_yolo_filter_firing(r, cfg, args.days)
        soak7 = check_sign_agreement(r, cfg, args.days)

        report_soak_1234 = {
            "SOAK-1_no_manual_override": soak1,
            "SOAK-2_avg_new_entries_le3": soak2,
            "SOAK-3_no_legacy_pattern": soak3,
            "SOAK-4_yolo_filter_firing": soak4,
            "SOAK-7_sign_agreement": soak7,
        }
        # SOAK-7 is warn-only by design — it MEASURES a known-bad metric,
        # not gates the soak. So it does NOT contribute to all_pass.
        all_pass = (
            soak1["pass"]
            and soak2["pass"]
            and soak3["pass"]
            and soak4["pass"]
            and soak5["pass"]
            and soak6["pass"]
        )

    report = {
        "date": str(datetime.now().date()),
        "config_path": str(args.config),
        "lookback_days": args.days,
        "soak_checks": {
            **(
                report_soak_1234
                if isinstance(report_soak_1234, dict) and "SOAK-1" in report_soak_1234
                else {"SOAK-1..4": report_soak_1234}
            ),
            "SOAK-5_model_files_resolve": soak5,
            "SOAK-6_env_vars_set": soak6,
        },
        "all_pass": all_pass,
        "recommendation": (
            "Continue soak" if all_pass else "Investigate failing checks before continuing soak"
        ),
    }

    print(json.dumps(report, indent=2, ensure_ascii=False))

    if all_pass:
        print("\n✅ All SOAK checks pass for the lookback window.")
        return 0
    else:
        print("\n⚠️  One or more SOAK checks failed. Review above and remediate.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
