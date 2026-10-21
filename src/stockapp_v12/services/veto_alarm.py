"""
Veto-rate alarm + minimum-trade-floor escape (Phase A4).

Problem: if every trade attempt is vetoed (R-05, R-06, blacklist, …) and the
system silently records 0 buys, there is no operator-visible signal that
"the rules have eaten our edge".  Symptom is identical to "model is broken"
but the fix is parameter loosening, not retraining.

This module supplies:

1. **VetoCounter** — Redis-backed counts of decisions per day (BUY / PASS /
   DENY / ALLOW).  Cheap to write on every orchestrator call, cheap to read.

2. **VetoRateAlarm** — fired on demand (daily review / preflight) and writes
   ``system:alarm:veto_high:<date>`` if veto_rate > threshold for the day.
   The preflight report surfaces this as a `warn` so the operator knows.

3. **MinimumTradeFloorEscape** — if the rolling N day buy count is 0 AND the
   current regime is not PANIC, emit a floor-escape advisory.  This is
   informational only (it never relaxes a guard automatically — humans decide).

Key layout:
    metrics:veto:<YYYY-MM-DD>            HASH {buy, pass, deny, allow, total}
    metrics:buys:rolling:<window>        LIST of dates with >=1 buy
    system:alarm:veto_high:<YYYY-MM-DD>  STRING (anything non-empty) when alarm fires

All writes are best-effort with try/except — Redis blips never block the
trading path.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
EST = ZoneInfo("America/New_York")

METRICS_KEY = "metrics:veto"
ALARM_KEY_PREFIX = "system:alarm:veto_high"
BUYS_ROLLING_KEY = "metrics:buys:rolling"


@dataclass
class VetoThresholds:
    """Operator-tunable thresholds for the alarm and floor-escape checks."""
    veto_rate_max: float = 0.80       # > 80% non-BUY => alarm
    min_alarm_n_total: int = 5         # don't fire alarm on a tiny sample
    rolling_window_days: int = 5
    floor_escape_min_days: int = 5    # if 0 buys for N days, escape advisory
    panic_regimes: tuple = ("PANIC", "BEAR_LONG")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class VetoSnapshot:
    date: str
    buy: int = 0
    pass_: int = 0
    deny: int = 0
    allow: int = 0
    total: int = 0
    veto_rate: float = 0.0
    alarm: bool = False
    alarm_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FloorEscapeAdvisory:
    active: bool
    days_with_zero_buys: int
    window_days: int
    current_regime: str
    panic_regime: bool
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class VetoCounter:
    """Increment-only counter backed by a Redis HASH per day."""

    def __init__(self, redis_client, *, now_provider=None):
        self._r = redis_client
        self._now = now_provider or (lambda: datetime.now(EST))

    def _key(self, date: Optional[str] = None) -> str:
        d = date or self._now().strftime("%Y-%m-%d")
        return f"{METRICS_KEY}:{d}"

    def record(self, decision: str, *, date: Optional[str] = None, n: int = 1) -> bool:
        """Record a decision.  decision ∈ {"BUY","PASS","DENY","ALLOW"}."""
        bucket = _bucket_for(decision)
        if not bucket:
            return False
        try:
            pipe = self._r.pipeline()
            pipe.hincrby(self._key(date), bucket, n)
            pipe.hincrby(self._key(date), "total", n)
            # 7-day TTL — we only ever look at today, but keep a little cushion
            pipe.expire(self._key(date), 7 * 86400)
            pipe.execute()
            return True
        except Exception as e:
            logger.warning("VetoCounter.record failed: %s", e)
            return False

    def snapshot(self, *, date: Optional[str] = None) -> VetoSnapshot:
        d = date or self._now().strftime("%Y-%m-%d")
        try:
            raw = self._r.hgetall(self._key(d)) or {}
            decoded = {
                (k.decode() if isinstance(k, (bytes, bytearray)) else k):
                int(v.decode() if isinstance(v, (bytes, bytearray)) else v)
                for k, v in raw.items()
            }
        except Exception as e:
            logger.warning("VetoCounter.snapshot failed: %s", e)
            decoded = {}

        buy = decoded.get("buy", 0)
        pass_ = decoded.get("pass", 0)
        deny = decoded.get("deny", 0)
        allow = decoded.get("allow", 0)
        total = decoded.get("total", buy + pass_ + deny + allow)
        veto_rate = 0.0
        if total > 0:
            veto_rate = (total - buy) / total
        return VetoSnapshot(
            date=d,
            buy=buy,
            pass_=pass_,
            deny=deny,
            allow=allow,
            total=total,
            veto_rate=veto_rate,
        )


class VetoRateAlarm:
    """Computed-on-demand alarm.  Does NOT auto-fire; called from preflight / review."""

    def __init__(self, counter: VetoCounter, *, thresholds: Optional[VetoThresholds] = None):
        self.counter = counter
        self.thresholds = thresholds or VetoThresholds()

    def evaluate(self, *, date: Optional[str] = None) -> VetoSnapshot:
        snap = self.counter.snapshot(date=date)
        thr = self.thresholds
        if snap.total < thr.min_alarm_n_total:
            snap.alarm = False
            snap.alarm_reason = (
                f"sample too small ({snap.total} < {thr.min_alarm_n_total}); "
                "skipping alarm check"
            )
        elif snap.veto_rate > thr.veto_rate_max:
            snap.alarm = True
            snap.alarm_reason = (
                f"veto_rate {snap.veto_rate:.2f} > {thr.veto_rate_max:.2f} on "
                f"{snap.total} decisions — rules may be too tight"
            )
        else:
            snap.alarm = False
            snap.alarm_reason = (
                f"veto_rate {snap.veto_rate:.2f} within {thr.veto_rate_max:.2f}"
            )
        return snap

    def raise_alarm(self, snap: VetoSnapshot, *, redis_client=None) -> bool:
        """Write the alarm key if `snap.alarm` is True.  Returns True on write."""
        if not snap.alarm:
            return False
        if redis_client is None:
            return False  # counter doesn't expose redis directly
        try:
            pipe = redis_client.pipeline()
            pipe.set(f"{ALARM_KEY_PREFIX}:{snap.date}", snap.alarm_reason)
            pipe.expire(f"{ALARM_KEY_PREFIX}:{snap.date}", 14 * 86400)
            pipe.execute()
            logger.warning("VetoRateAlarm fired for %s: %s", snap.date, snap.alarm_reason)
            return True
        except Exception as e:
            logger.warning("VetoRateAlarm.raise_alarm failed: %s", e)
            return False

    def is_alarmed(self, *, redis_client, date: Optional[str] = None) -> bool:
        """Check if a previous alarm is still active for the date."""
        from datetime import datetime as _dt
        d = date or _dt.now(EST).strftime("%Y-%m-%d")
        try:
            return bool(redis_client.get(f"{ALARM_KEY_PREFIX}:{d}"))
        except Exception as e:
            logger.warning(
                "[veto_alarm] is_alarmed redis read failed for date=%s: "
                "%s: %s — returning False (operator cannot distinguish "
                "'no alarm raised' from 'alarm check unreachable')",
                d, type(e).__name__, e,
            )
            return False


class MinimumTradeFloorEscape:
    """Advisory-only: if N consecutive days have 0 buys AND we're not in PANIC,
    emit a recommendation to the operator.

    NEVER bypasses a guard automatically — humans decide.
    """

    def __init__(self, counter: VetoCounter, *, thresholds: Optional[VetoThresholds] = None):
        self.counter = counter
        self.thresholds = thresholds or VetoThresholds()

    def evaluate(
        self,
        *,
        current_regime: str,
        today: Optional[str] = None,
    ) -> FloorEscapeAdvisory:
        thr = self.thresholds
        today_str = today or datetime.now(EST).strftime("%Y-%m-%d")
        # Walk the last N days
        days = []
        try:
            base = datetime.strptime(today_str, "%Y-%m-%d")
        except (TypeError, ValueError):
            base = datetime.now(EST)
        zero_streak = 0
        for offset in range(thr.floor_escape_min_days):
            dt = base - timedelta(days=offset)
            ds = dt.strftime("%Y-%m-%d")
            snap = self.counter.snapshot(date=ds)
            days.append(ds)
            if snap.buy == 0:
                zero_streak += 1
            else:
                break

        in_panic = current_regime.upper() in (
            r.upper() for r in thr.panic_regimes
        )
        active = (zero_streak >= thr.floor_escape_min_days) and not in_panic
        if active:
            msg = (
                f"0 buys for {zero_streak} consecutive trading days "
                f"in regime {current_regime!r} (not PANIC). "
                f"Consider whether a guard (R-05, R-06, min_confidence) "
                f"is over-tight and review the VetoRateAlarm."
            )
        elif in_panic and zero_streak >= thr.floor_escape_min_days:
            msg = (
                f"0 buys for {zero_streak} days, but regime={current_regime!r} "
                f"is PANIC — floor escape DOES NOT apply; no action needed."
            )
        else:
            msg = (
                f"buys present in recent {thr.floor_escape_min_days} days OR "
                f"zero_streak={zero_streak} below threshold; no advisory."
            )
        return FloorEscapeAdvisory(
            active=active,
            days_with_zero_buys=zero_streak,
            window_days=thr.floor_escape_min_days,
            current_regime=current_regime,
            panic_regime=in_panic,
            message=msg,
        )


# ----- helpers -----

def _bucket_for(decision: str) -> Optional[str]:
    d = (decision or "").upper()
    if d == "BUY":
        return "buy"
    if d == "PASS":
        return "pass"
    if d == "DENY":
        return "deny"
    if d == "ALLOW":
        return "allow"
    return None
