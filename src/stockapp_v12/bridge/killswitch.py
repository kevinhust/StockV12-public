"""
KillSwitch — emergency halt for V12 trading pipeline.

Spec: Phase A1 (council verdict 2026-07-07) — single source of truth for
"is the system allowed to enter a position right now?".

Backed by Redis key `system:halt`:
- present (any value)  -> halted
- absent               -> not halted

Halts can be:
- indefinite (no TTL) — must be cleared by explicit `resume()`
- timed (TTL seconds) — auto-expires

Reasons are stored at `system:halt:reason` (string).  Reason is intentionally
side-channel (does NOT affect gate) so logs / audits always know *why*.

This module must be import-safe even if Redis is unreachable.  All Redis
calls are wrapped in try/except, returning a "fail-open" (or "fail-closed")
default per call site — see `fail_closed_on_error`.

Keep this file tiny and dependency-free so cron scripts can pipe through it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


HALT_KEY = "system:halt"
HALT_REASON_KEY = "system:halt:reason"
HALT_AT_KEY = "system:halt:at"
HALTED_BY_KEY = "system:halt:by"


@dataclass(frozen=True)
class HaltState:
    is_halted: bool
    reason: str
    halted_at: Optional[str] = None
    halted_by: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "is_halted": self.is_halted,
            "reason": self.reason,
            "halted_at": self.halted_at,
            "halted_by": self.halted_by,
        }


class KillSwitch:
    """Redis-backed emergency halt flag.

    Constructor default remains ``fail_closed_on_error=False`` for
    backward-compatible ad-hoc use. **Production paper path** (LiveOrderGuard /
    ``make_live_order_guard``) passes ``True`` so Redis outages fail closed
    (prefer no-trade). See ``docs/SAFETY_LIVE_GUARD.md`` (2026-07-28 ABC).
    """

    def __init__(self, redis_client, fail_closed_on_error: bool = False):
        self._r = redis_client
        self._fail_closed = fail_closed_on_error

    # ----- introspection -----

    def status(self) -> HaltState:
        """Read the current halt state. Redis error respects ``fail_closed_on_error``."""
        try:
            raw = self._r.get(HALT_KEY)
            if not raw:
                return HaltState(is_halted=False, reason="")
            reason = self._r.get(HALT_REASON_KEY) or ""
            decoded = reason.decode() if isinstance(reason, (bytes, bytearray)) else str(reason or "")
            at = self._r.get(HALT_AT_KEY)
            at_str = at.decode() if isinstance(at, (bytes, bytearray)) else (str(at) if at else None)
            by = self._r.get(HALTED_BY_KEY)
            by_str = by.decode() if isinstance(by, (bytes, bytearray)) else (str(by) if by else None)
            return HaltState(
                is_halted=True,
                reason=decoded,
                halted_at=at_str,
                halted_by=by_str,
            )
        except Exception as e:
            logger.warning("KillSwitch.status() redis error: %s — fail_closed=%s", e, self._fail_closed)
            if self._fail_closed:
                return HaltState(
                    is_halted=True,
                    reason=f"redis_unreachable:{type(e).__name__}",
                )
            return HaltState(is_halted=False, reason="")

    def is_halted(self) -> bool:
        return self.status().is_halted

    def get_halt_reason(self) -> str:
        return self.status().reason

    # ----- control -----

    def halt(
        self,
        reason: str,
        *,
        halted_by: str = "operator",
        ttl_seconds: Optional[int] = None,
    ) -> HaltState:
        """Set the halt flag.  `ttl_seconds` is optional (indefinite if None).

        Overwrites any existing halt (callers can re-arm with extended TTL).
        Reason is required and surfaced in logs / `status()`.
        """
        reason = (reason or "").strip() or "halted"
        ts = datetime.now(timezone.utc).isoformat()
        try:
            pipe = self._r.pipeline()
            if ttl_seconds and ttl_seconds > 0:
                pipe.setex(HALT_KEY, int(ttl_seconds), "1")
            else:
                pipe.set(HALT_KEY, "1")
            pipe.set(HALT_REASON_KEY, reason)
            pipe.set(HALT_AT_KEY, ts)
            pipe.set(HALTED_BY_KEY, halted_by)
            pipe.execute()
        except Exception as e:
            logger.error("KillSwitch.halt() redis error: %s", e)
        logger.warning(
            "KillSwitch HALT engaged by %s reason=%s ttl=%s",
            halted_by, reason, ttl_seconds,
        )
        return HaltState(is_halted=True, reason=reason, halted_at=ts, halted_by=halted_by)

    def resume(self, *, resumed_by: str = "operator") -> bool:
        """Clear the halt flag.  Returns True if a halt was cleared."""
        cleared = False
        try:
            existed = bool(self._r.get(HALT_KEY))
            pipe = self._r.pipeline()
            pipe.delete(HALT_KEY)
            pipe.delete(HALT_REASON_KEY)
            pipe.delete(HALT_AT_KEY)
            pipe.delete(HALTED_BY_KEY)
            pipe.execute()
            cleared = existed
        except Exception as e:
            logger.error("KillSwitch.resume() redis error: %s", e)
        if cleared:
            logger.warning("KillSwitch RESUME by %s", resumed_by)
        return cleared

    # ----- helpers -----

    @staticmethod
    def require_not_halted(state: HaltState) -> None:
        """Convenience guard for cron entry points."""
        if state.is_halted:
            raise SystemExit(
                f"[KillSwitch] system halted: {state.reason or 'unknown reason'}"
            )
