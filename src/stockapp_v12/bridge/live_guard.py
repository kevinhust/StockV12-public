"""
LiveOrderGuard — final pre-trade gate composing system-level safety checks.

Spec: Phase A1 (council verdict 2026-07-07).

Plugs into EntryOrchestrator between ``evaluate_and_maybe_enter()`` start
and the gatekeeper call.  Returns ``(allowed, reason, meta)`` so callers
can log a single PASS / DENY / ALLOW line into TradeJournal.

The guard is **composable** — each rule is a small method.  Keep them
pure (no side effects beyond logging) so unit tests can exercise each
rule independently and so future additions (sector caps, news blackout,
Earnings blackout) drop in without rewiring.

Order of evaluation (veto on first failure):
    1. KillSwitch (system halt)
    2. Mode guard (live vs paper)
    3. Blacklist (system: blacklisted symbols)
    4. Daily trade cap (R-05)
    5. Symbol cooldown (R-06)
    6. Confidence floor (gatekeeper computes size; we just sanity check)
    7. Single position cap (R-03 — semantic check)
    8. Portfolio exposure cap (R-04 — semantic check)
    9. Min-hold days (R-10 — only checked when closing an existing position)

This module depends only on ``stockapp_v12.bridge.killswitch.KillSwitch``
and ``stockapp_v12.bridge.gatekeeper`` to avoid adding a new layer to the
existing dependency graph.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from zoneinfo import ZoneInfo

from stockapp_v12.bridge.gatekeeper import (
    check_daily_trade_cap,
    check_symbol_cooldown,
)
from stockapp_v12.bridge.killswitch import KillSwitch
from stockapp_v12.core.limits import V12RiskLimits

logger = logging.getLogger(__name__)
EST = ZoneInfo("America/New_York")


BLACKLIST_KEY = "system:blacklist"
ALLOWED_MODES_KEY = "system:allowed_modes"
EXPOSURE_KEY_PREFIX = "risk:exposure:"

# Default mode whitelist — paper only. Live modes must be explicit.
DEFAULT_ALLOWED_MODES: List[str] = ["paper_ibkr", "paper"]


@dataclass
class LiveGuardContext:
    """Inputs needed to evaluate one BUY intent."""
    symbol: str
    side: str                       # "BUY" | "SELL"
    mode: str                       # "paper_ibkr" | "live_ibkr" | ...
    price: float
    equity: float
    confidence: Optional[float] = None
    requested_size_pct: Optional[float] = None
    current_total_exposure_pct: Optional[float] = None  # from PM if available
    has_open_position: bool = False
    entry_at: Optional[str] = None  # ISO timestamp of existing position (for min-hold on SELL)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LiveGuardResult:
    allowed: bool
    reason: str
    rule: str                       # which rule decided (or "OK" / "ALLOWED")
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "rule": self.rule,
            "meta": self.meta,
        }


class LiveOrderGuard:
    """Composable safety gate.  Per-instance config + injectable deps."""

    def __init__(
        self,
        *,
        redis_client,
        limits: V12RiskLimits,
        kill_switch: Optional[KillSwitch] = None,
        allowed_modes: Optional[List[str]] = None,
        now_provider=None,
    ):
        self._r = redis_client
        self.limits = limits
        # Production default: fail-closed on Redis errors (prefer no-trade).
        self.kill_switch = kill_switch or KillSwitch(
            redis_client, fail_closed_on_error=True
        )
        # None → paper defaults. Explicit [] is treated as paper defaults too
        # (empty whitelist must never mean "allow all modes").
        if allowed_modes is None or len(allowed_modes) == 0:
            self.allowed_modes = set(DEFAULT_ALLOWED_MODES)
        else:
            self.allowed_modes = set(allowed_modes)
        self._now_provider = now_provider or (lambda: datetime.now(EST))

    # ----- public API -----

    def check(self, ctx: LiveGuardContext) -> LiveGuardResult:
        """Run all rules in order.  First veto wins."""
        sym = ctx.symbol.upper()

        # 1. KillSwitch (highest priority)
        halt = self.kill_switch.status()
        if halt.is_halted:
            return self._deny(
                rule="KILL_SWITCH",
                reason=f"system halted: {halt.reason or 'unknown'}",
                meta=halt.to_dict(),
            )

        # 2. Mode guard (e.g. block live trades if mode not whitelisted)
        if ctx.side == "BUY":
            mode_res = self._check_mode(ctx.mode)
            if not mode_res.allowed:
                return mode_res

        # 3. Symbol blacklist (system-level) — fail-closed on Redis error
        try:
            if self._r.sismember(BLACKLIST_KEY, sym):
                return self._deny(
                    rule="BLACKLIST",
                    reason=f"symbol {sym} is system-blacklisted",
                    meta={"key": BLACKLIST_KEY},
                )
        except Exception as e:
            logger.warning("LiveGuard blacklist check redis error: %s — fail closed", e)
            return self._deny(
                rule="BLACKLIST_CHECK_FAILED",
                reason=f"blacklist redis unreachable: {type(e).__name__}",
                meta={"error": str(e)},
            )

        # 4. Daily trade cap (R-05)
        over_cap, why = check_daily_trade_cap(self._r, self.limits)
        if over_cap:
            return self._deny(
                rule="R-05_DAILY_CAP",
                reason=why,
                meta={"max_daily_trades": self.limits.max_daily_trades},
            )

        # 5. Symbol cooldown (R-06) — only for BUY intents
        if ctx.side == "BUY":
            in_cd, why = check_symbol_cooldown(self._r, sym)
            if in_cd:
                return self._deny(rule="R-06_COOLDOWN", reason=why)

        # 6. Confidence floor
        if (
            ctx.side == "BUY"
            and ctx.confidence is not None
            and ctx.confidence < self.limits.min_entry_confidence
        ):
            return self._deny(
                rule="R-09_CONF_FLOOR",
                reason=f"confidence {ctx.confidence:.2f} < floor {self.limits.min_entry_confidence:.2f}",
                meta={
                    "confidence": ctx.confidence,
                    "min": self.limits.min_entry_confidence,
                },
            )

        # 7. Single position cap (R-02) — semantic check on requested size
        if (
            ctx.side == "BUY"
            and ctx.requested_size_pct is not None
            and ctx.requested_size_pct > self.limits.max_single_position_pct + 1e-9
        ):
            return self._deny(
                rule="R-03_SINGLE_CAP",
                reason=(
                    f"requested_size_pct {ctx.requested_size_pct:.4f} > "
                    f"max_single_position_pct {self.limits.max_single_position_pct:.4f}"
                ),
                meta={
                    "requested": ctx.requested_size_pct,
                    "cap": self.limits.max_single_position_pct,
                },
            )

        # 8. Portfolio exposure cap (R-08)
        if ctx.side == "BUY" and ctx.current_total_exposure_pct is not None:
            prospective = ctx.current_total_exposure_pct + (ctx.requested_size_pct or 0.0)
            if prospective > self.limits.max_portfolio_exposure_pct + 1e-9:
                return self._deny(
                    rule="R-04_EXPOSURE_CAP",
                    reason=(
                        f"prospective exposure {prospective:.4f} > "
                        f"cap {self.limits.max_portfolio_exposure_pct:.4f}"
                    ),
                    meta={
                        "current": ctx.current_total_exposure_pct,
                        "requested": ctx.requested_size_pct,
                        "cap": self.limits.max_portfolio_exposure_pct,
                    },
                )

        # 9. Min-hold days (R-10) — only when closing an existing position
        if ctx.side == "SELL" and ctx.has_open_position and ctx.entry_at:
            min_hold = self.limits.min_hold_days
            if min_hold > 0:
                try:
                    opened = datetime.fromisoformat(ctx.entry_at)
                    if opened.tzinfo is None:
                        opened = opened.replace(tzinfo=EST)
                    now = self._now_provider()
                    held = (now - opened).total_seconds() / 86400.0
                    if held < min_hold - 1e-9:
                        return self._deny(
                            rule="R-10_MIN_HOLD",
                            reason=(
                                f"position held {held:.2f}d < min_hold {min_hold}d"
                            ),
                            meta={
                                "held_days": held,
                                "min_hold_days": min_hold,
                            },
                        )
                except (TypeError, ValueError) as e:
                    logger.warning(
                        "LiveGuard min-hold parse failed for %s: %s — skipping",
                        sym, e,
                    )

        return LiveGuardResult(
            allowed=True,
            reason="all checks passed",
            rule="OK",
            meta={
                "halt": halt.to_dict(),
                "mode": ctx.mode,
                "side": ctx.side,
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )

    # ----- internal rules -----

    def _check_mode(self, mode: str) -> LiveGuardResult:
        # allowed_modes is always non-empty after __init__ (paper defaults).
        if mode in self.allowed_modes:
            return LiveGuardResult(
                allowed=True, reason=f"mode {mode} whitelisted", rule="MODE_OK",
            )
        return self._deny(
            rule="MODE_BLOCK",
            reason=f"mode {mode!r} not in allowed_modes",
            meta={"requested": mode, "allowed": sorted(self.allowed_modes)},
        )

    @staticmethod
    def _deny(rule: str, reason: str, meta: Optional[Dict[str, Any]] = None) -> LiveGuardResult:
        return LiveGuardResult(
            allowed=False,
            reason=reason,
            rule=rule,
            meta=meta or {},
        )


# ----- convenience builder -----

def make_live_order_guard(
    redis_client,
    *,
    allowed_modes: Optional[List[str]] = None,
    fail_closed_on_halt_check: bool = True,
) -> LiveOrderGuard:
    """Build a guard using V12 defaults loaded from config.

    Defaults (2026-07-27 code review):
    - KillSwitch fail-closed on Redis errors
    - Mode whitelist paper-only unless ``allowed_modes`` explicitly set
    """
    from stockapp_v12.core.limits import load_v12_risk_limits  # local import for testability
    limits = load_v12_risk_limits()
    ks = KillSwitch(redis_client, fail_closed_on_error=fail_closed_on_halt_check)
    modes = list(allowed_modes) if allowed_modes is not None else list(DEFAULT_ALLOWED_MODES)
    return LiveOrderGuard(
        redis_client=redis_client,
        limits=limits,
        kill_switch=ks,
        allowed_modes=modes,
    )
