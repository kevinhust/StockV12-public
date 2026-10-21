"""
V12 extensions for HardRiskGatekeeper (sizing, counters, red-line thresholds).
Bridge layer: V11 gatekeeper in src/core calls into this package.

Phase A5: `compute_position_size` arithmetic uses `decimal.Decimal`
internally for boundary determinism. Public return type stays `float`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

from stockapp_v12.core.limits import V12RiskLimits
from stockapp_v12.core.precision import D, round_pct, to_float
from stockapp_v12.core.sizing import calculate_risk_based_size_pct

EST = ZoneInfo("America/New_York")


def red_line_thresholds(limits: V12RiskLimits) -> Dict[str, float]:
    return {
        "daily_loss_thresh": -limits.daily_loss_limit_pct,
        "dd_thresh": limits.max_drawdown_pct,
        "single_cap": limits.max_single_position_pct,
        "exposure_cap": limits.max_portfolio_exposure_pct,
    }


def compute_position_size(
    *,
    equity: float,
    price: float,
    atr_pct: Optional[float],
    atr_14: Optional[float],
    regime: str,
    confidence: float,
    kelly_override: Optional[float],
    risk_scaling_override: Optional[float],
    is_degraded: bool,
    limits: V12RiskLimits,
) -> Tuple[float, float, Dict[str, Any]]:
    risk_scaling_d = D(risk_scaling_override if risk_scaling_override is not None else 1.0)
    DEGRADED_SIZE_FACTOR = D("0.5")
    DEGRADED_SCALING_FACTOR = D("0.7")

    if price > 0:
        try:
            result = calculate_risk_based_size_pct(
                nav=equity,
                entry_price=price,
                atr_14=atr_14,
                atr_pct=atr_pct,
                regime=regime,
                limits=limits,
                confidence=confidence if limits.confidence_cap_discount_enabled else None,
            )
            base_size_d = D(result.size_pct)
            audit = {
                "sizing_model": "v12_risk",
                "risk_amount": result.risk_amount,
                "stop_distance": result.stop_distance,
                "atr_14": result.atr_14,
                "sl_atr_multiple": result.sl_atr_multiple,
                "regime_multiplier": result.regime_multiplier,
                "regime": regime,
                "capped_by_single_position": result.capped_by_single_position,
            }
        except (ValueError, ZeroDivisionError) as e:
            fallback_d = D("0.01") * D(limits.regime_sizing.get(regime, 0.8))
            base_size_d = round_pct(fallback_d)
            audit = {"sizing_model": "v12_fallback", "error": str(e)}
    else:
        base_size_d = D("0.01")
        audit = {"sizing_model": "v12_fallback", "error": "no_price"}

    if is_degraded:
        base_size_d = base_size_d * DEGRADED_SIZE_FACTOR
        risk_scaling_d = risk_scaling_d * DEGRADED_SCALING_FACTOR

    base_size_d = round_pct(base_size_d * risk_scaling_d)

    return to_float(base_size_d), to_float(risk_scaling_d), audit


def daily_trades_key() -> str:
    return f"risk:daily_trades:{datetime.now(EST).strftime('%Y-%m-%d')}"


def get_daily_trade_count(redis_client) -> int:
    """Return today's trade count from Redis, or fail-closed on Redis errors.

    **Fail-closed contract (hygiene-D H1 fix):** on any ``Exception``
    from Redis (connection drop, timeout, decode error), we return
    ``max_daily_trades`` (i.e. "at the cap") and log at ``EXCEPTION``
    level. This prevents the R-05 daily-trade cap from being silently
    bypassed during a Redis outage — the caller (``check_daily_trade_cap``)
    will deny every BUY until Redis recovers.

    The previous behavior (return 0 on error) silently disabled the
    R-05 cap: ``count < cap`` → no deny → unbounded trades.
    """
    try:
        return int(redis_client.get(daily_trades_key()) or 0)
    except Exception:
        from stockapp_v12.core.limits import load_v12_risk_limits

        try:
            cap = load_v12_risk_limits().max_daily_trades
        except Exception:
            # If we can't even load the limit file, use a conservative
            # hard-coded floor (matches the R-05 spec default).
            cap = 4
        logger.exception(
            "[gatekeeper] R-05 daily-trade count read failed; "
            "failing closed (returning cap=%d)", cap,
        )
        return cap


def increment_daily_trade_count(redis_client, mode: Optional[str] = None) -> int:
    """Atomically increment today's trade counter (hygiene R-05).

    P0-4 (council 2026-07-09): the prior signature had no ``mode`` and the
    function was only called from backtest / test paths — production entries
    (orchestrator ``run_stage_7_register``) silently bypassed R-05. We now
    accept the optional ``mode`` arg and **fail-closed**: any Redis
    exception propagates and the caller MUST decide whether to abort
    the position registration. The trade counter is keyed by date only
    (not mode), so cross-mode totals remain comparable.

    NB: this function does NOT itself fail-closed-recover — that is the
    caller's job. The stages.py call site wraps it in try/except and
    raises ``EntryResult(BUY, reason_code=R05_INCR_FAIL)`` so that the
    bracket has already been submitted but the position is unregistered
    in software. (Bracket rollback is handled separately by the
    bracket service for safety.)

    :param redis_client: Redis-like client with ``pipeline()``.
    :param mode: Optional mode label (e.g. ``"paper_ibkr"``, ``"live"``).
        Reserved for future per-mode split; currently informational only.
    :return: New daily-trade count (post-incr).
    """
    key = daily_trades_key()
    pipe = redis_client.pipeline()
    pipe.incr(key)
    pipe.expire(key, 86400)
    return int(pipe.execute()[0])


def check_daily_trade_cap(redis_client, limits: V12RiskLimits) -> Tuple[bool, str]:
    count = get_daily_trade_count(redis_client)
    cap = limits.max_daily_trades
    if count >= cap:
        return True, f"R-05: Daily trade limit reached ({count}/{cap})"
    return False, ""


def check_symbol_cooldown(redis_client, symbol: str) -> Tuple[bool, str]:
    """Check if a symbol is in R-06 cooldown.

    **Fail-closed contract (P1-2, council 2026-07-09):** on any
    ``Exception`` from Redis (connection drop, timeout, decode error),
    we return ``(True, "R-06: ... failing closed")`` — i.e. treat the
    symbol as in-cooldown so the BUY is denied. This mirrors the H1
    pattern in ``get_daily_trade_count`` and prevents R-06 from being
    silently bypassed during a Redis outage.

    The previous behavior caught only ``(TypeError, ValueError)``, so a
    ``redis.exceptions.ConnectionError`` propagated uncaught — callers
    that swallowed it at a higher level would allow the trade through
    with no cooldown check at all.
    """
    key = f"risk:cooldown:{symbol.upper()}"
    try:
        if redis_client.get(key):
            ttl = int(redis_client.ttl(key) or 0)
            return True, f"R-06: Symbol {symbol} in cooldown ({ttl}s remaining)"
    except Exception:
        logger.exception(
            "[gatekeeper] R-06 cooldown check failed for %s; "
            "failing closed (treating as in-cooldown)", symbol,
        )
        return True, f"R-06: Symbol {symbol} cooldown check failed (Redis error) — failing closed"
    return False, ""


def set_symbol_cooldown(redis_client, symbol: str, limits: V12RiskLimits, hours: Optional[int] = None) -> None:
    hrs = hours if hours is not None else limits.symbol_cooldown_hours
    key = f"risk:cooldown:{symbol.upper()}"
    redis_client.setex(key, int(hrs * 3600), "1")


# ---------------------------------------------------------------------------
# P1-9 (council 2026-07-09): R-01 / R-02 / R-09 runtime enforcement.
#
# red_line_thresholds() was defined but never called. These three functions
# implement the actual checks with the same fail-closed posture as H1.
# Callers (gatekeeper_fn builders) should wire them BEFORE compute_position_size
# so a red-line hit denies the trade without wasting the sizing computation.
# ---------------------------------------------------------------------------


def _daily_pnl_key() -> str:
    return f"risk:daily_pnl:{datetime.now(EST).strftime('%Y-%m-%d')}"


def check_daily_loss_limit(
    redis_client,
    limits: V12RiskLimits,
    equity: Optional[float] = None,
) -> Tuple[bool, str]:
    """R-01: block all entries if today's realized loss exceeds the daily limit.

    Reads ``risk:daily_pnl:{date}`` from Redis (a running total of today's
    realized P&L in dollars, maintained by DailyReview / position close paths).
    If daily_pnl <= -(daily_loss_limit_pct * equity), blocks all new entries.

    **Fail-closed:** on Redis error, returns ``(True, "R-01: ...")`` — block.
    """
    try:
        raw = redis_client.get(_daily_pnl_key())
        if raw is None:
            return False, ""  # no losses today → allow
        daily_pnl = float(raw)
        eq = equity if equity is not None else 100_000.0
        threshold = -(limits.daily_loss_limit_pct / 100.0) * eq
        if daily_pnl <= threshold:
            return True, (
                f"R-01: Daily loss limit hit (pnl={daily_pnl:.2f}, "
                f"threshold={threshold:.2f}, limit={limits.daily_loss_limit_pct}%)"
            )
        return False, ""
    except Exception as e:
        logger.exception(
            "[gatekeeper] R-01 daily loss check failed; failing closed: %s", e,
        )
        return True, f"R-01: Daily loss check failed (Redis error) — failing closed"


def check_drawdown_limit(
    redis_client,
    limits: V12RiskLimits,
) -> Tuple[bool, str]:
    """R-02: block all entries if current drawdown from peak exceeds the limit.

    Reads ``risk:peak_equity`` and ``risk:current_equity`` from Redis
    (maintained by the daily_review / equity tracking path).
    If (peak - current) / peak >= max_drawdown_pct / 100, blocks entries.

    **Fail-closed:** on Redis error, returns ``(True, "R-02: ...")`` — block.
    """
    try:
        peak_raw = redis_client.get("risk:peak_equity")
        curr_raw = redis_client.get("risk:current_equity")
        if peak_raw is None or curr_raw is None:
            return False, ""  # no equity tracking → allow (cold start)
        peak = float(peak_raw)
        curr = float(curr_raw)
        if peak <= 0:
            return False, ""
        dd_pct = ((peak - curr) / peak) * 100.0
        if dd_pct >= limits.max_drawdown_pct:
            return True, (
                f"R-02: Max drawdown exceeded (dd={dd_pct:.2f}%, "
                f"limit={limits.max_drawdown_pct}%, peak={peak:.2f}, curr={curr:.2f})"
            )
        return False, ""
    except Exception as e:
        logger.exception(
            "[gatekeeper] R-02 drawdown check failed; failing closed: %s", e,
        )
        return True, f"R-02: Drawdown check failed (Redis error) — failing closed"


def check_entry_window(now: Optional[datetime] = None) -> Tuple[bool, str]:
    """R-09: only allow entries during the NYSE market window (09:35–15:30 ET).

    Pure clock check — no Redis needed. Weekends are always blocked.
    The window is [09:35, 15:30] ET per RISK_MANAGEMENT.md.

    Args:
        now: Optional datetime for testability. If None, uses ``datetime.now(EST)``.

    Returns:
        ``(True, "R-09: ...")`` if outside the window, ``(False, "")`` if inside.
    """
    ts = now or datetime.now(EST)
    # Weekend check (Mon=0, Sun=6)
    if ts.weekday() >= 5:
        return True, f"R-09: Outside entry window (weekend, {ts.strftime('%A')})"
    # Time window: 09:35–15:30 ET
    minutes_from_midnight = ts.hour * 60 + ts.minute
    window_start = 9 * 60 + 35   # 09:35
    window_end = 15 * 60 + 30    # 15:30
    if minutes_from_midnight < window_start:
        return True, (
            f"R-09: Before entry window ({ts.strftime('%H:%M')} ET, "
            f"opens at 09:35)"
        )
    if minutes_from_midnight > window_end:
        return True, (
            f"R-09: After entry window ({ts.strftime('%H:%M')} ET, "
            f"closed at 15:30)"
        )
    return False, ""
