"""Tests for LiveOrderGuard (Phase A1).

Each rule is exercised independently and the orchestration order is
verified (KillSwitch short-circuits everything else, blacklist short-
circuits before R-05 etc.).
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from stockapp_v12.bridge.killswitch import KillSwitch
from stockapp_v12.bridge.live_guard import (
    BLACKLIST_KEY,
    LiveGuardContext,
    LiveOrderGuard,
    make_live_order_guard,
)
from stockapp_v12.core.limits import V12RiskLimits
from ._fakes import FakeRedis


EST = ZoneInfo("America/New_York")


def _ctx(**overrides):
    base = dict(
        symbol="NVDA",
        side="BUY",
        mode="paper_ibkr",
        price=100.0,
        equity=100_000.0,
        confidence=0.85,
        requested_size_pct=0.02,
        current_total_exposure_pct=0.10,
        has_open_position=False,
        entry_at=None,
    )
    base.update(overrides)
    return LiveGuardContext(**base)


# ----- KillSwitch short-circuits everything -----


def test_killswitch_halt_blocks_buy():
    r = FakeRedis()
    ks = KillSwitch(r)
    ks.halt("market_crash", halted_by="scheduler")
    guard = LiveOrderGuard(redis_client=r, limits=V12RiskLimits(), kill_switch=ks)
    res = guard.check(_ctx())
    assert res.allowed is False
    assert res.rule == "KILL_SWITCH"
    assert "market_crash" in res.reason


def test_killswitch_pass_allows_progression():
    r = FakeRedis()
    guard = LiveOrderGuard(
        redis_client=r, limits=V12RiskLimits(), kill_switch=KillSwitch(r)
    )
    res = guard.check(_ctx())
    assert res.allowed is True
    assert res.rule == "OK"


# ----- Blacklist -----


def test_blacklist_blocks_symbol():
    r = FakeRedis()
    r.sadd(BLACKLIST_KEY, "NVDA")
    guard = LiveOrderGuard(redis_client=r, limits=V12RiskLimits(), kill_switch=KillSwitch(r))
    res = guard.check(_ctx(symbol="NVDA"))
    assert res.allowed is False
    assert res.rule == "BLACKLIST"


def test_blacklist_allows_unlisted_symbol():
    r = FakeRedis()
    r.sadd(BLACKLIST_KEY, "GME")
    guard = LiveOrderGuard(redis_client=r, limits=V12RiskLimits(), kill_switch=KillSwitch(r))
    res = guard.check(_ctx(symbol="AAPL"))
    assert res.allowed is True


# ----- Mode whitelist -----


def test_mode_whitelist_blocks_disallowed_mode():
    r = FakeRedis()
    guard = LiveOrderGuard(
        redis_client=r,
        limits=V12RiskLimits(),
        kill_switch=KillSwitch(r),
        allowed_modes=["live_ibkr"],
    )
    res = guard.check(_ctx(mode="paper_ibkr"))
    assert res.allowed is False
    assert res.rule == "MODE_BLOCK"
    assert res.meta["allowed"] == ["live_ibkr"]


def test_mode_whitelist_passes_when_allowed():
    r = FakeRedis()
    guard = LiveOrderGuard(
        redis_client=r,
        limits=V12RiskLimits(),
        kill_switch=KillSwitch(r),
        allowed_modes=["paper_ibkr", "live_ibkr"],
    )
    res = guard.check(_ctx(mode="paper_ibkr"))
    assert res.allowed is True


def test_mode_whitelist_empty_defaults_to_paper_only():
    """Empty whitelist must NOT allow-all (2026-07-27 safety default)."""
    r = FakeRedis()
    guard = LiveOrderGuard(
        redis_client=r,
        limits=V12RiskLimits(),
        kill_switch=KillSwitch(r),
        allowed_modes=[],
    )
    res = guard.check(_ctx(mode="experimental_mode"))
    assert res.allowed is False
    assert res.rule == "MODE_BLOCK"
    # paper still allowed under defaults
    res_paper = guard.check(_ctx(mode="paper_ibkr"))
    assert res_paper.allowed is True


# ----- Daily trade cap (R-05) -----


def test_daily_trade_cap_blocks_when_reached():
    r = FakeRedis()
    # Simulate already at cap (3 of 3 used)
    today = datetime.now(EST).strftime("%Y-%m-%d")
    r.set(f"risk:daily_trades:{today}", "3")
    guard = LiveOrderGuard(redis_client=r, limits=V12RiskLimits(), kill_switch=KillSwitch(r))
    res = guard.check(_ctx())
    assert res.allowed is False
    assert res.rule == "R-05_DAILY_CAP"
    assert "3/3" in res.reason


def test_daily_trade_cap_passes_when_below():
    r = FakeRedis()
    today = datetime.now(EST).strftime("%Y-%m-%d")
    r.set(f"risk:daily_trades:{today}", "2")
    guard = LiveOrderGuard(redis_client=r, limits=V12RiskLimits(), kill_switch=KillSwitch(r))
    res = guard.check(_ctx())
    assert res.allowed is True


# ----- Symbol cooldown (R-06) -----


def test_symbol_cooldown_blocks_buy():
    r = FakeRedis()
    r.set("risk:cooldown:NVDA", "1")
    guard = LiveOrderGuard(redis_client=r, limits=V12RiskLimits(), kill_switch=KillSwitch(r))
    res = guard.check(_ctx())
    assert res.allowed is False
    assert res.rule == "R-06_COOLDOWN"


# ----- Confidence floor (R-09) -----


def test_confidence_floor_blocks_low_confidence():
    r = FakeRedis()
    guard = LiveOrderGuard(redis_client=r, limits=V12RiskLimits(), kill_switch=KillSwitch(r))
    res = guard.check(_ctx(confidence=0.50))
    assert res.allowed is False
    assert res.rule == "R-09_CONF_FLOOR"
    assert res.meta["confidence"] == 0.50


# ----- Single position cap (R-02) -----


def test_single_position_cap_blocks_oversize_request():
    r = FakeRedis()
    # Default max_single_position_pct is 0.03
    guard = LiveOrderGuard(redis_client=r, limits=V12RiskLimits(), kill_switch=KillSwitch(r))
    res = guard.check(_ctx(requested_size_pct=0.10))
    assert res.allowed is False
    assert res.rule == "R-03_SINGLE_CAP"


# ----- Portfolio exposure cap (R-04) -----


def test_exposure_cap_blocks_oversize_prospective():
    """current 0.38 + requested 0.025 = 0.405 > 0.40 cap. Passes R-03 (0.025<0.03)."""
    r = FakeRedis()
    guard = LiveOrderGuard(redis_client=r, limits=V12RiskLimits(), kill_switch=KillSwitch(r))
    res = guard.check(_ctx(current_total_exposure_pct=0.38, requested_size_pct=0.025))
    assert res.allowed is False
    assert res.rule == "R-04_EXPOSURE_CAP"


def test_exposure_cap_passes_within_limit():
    """current 0.10 + requested 0.025 = 0.125 < 0.40 cap."""
    r = FakeRedis()
    guard = LiveOrderGuard(redis_client=r, limits=V12RiskLimits(), kill_switch=KillSwitch(r))
    res = guard.check(_ctx(current_total_exposure_pct=0.10, requested_size_pct=0.025))
    assert res.allowed is True


# ----- Min-hold on SELL (R-10) -----


def test_min_hold_blocks_early_exit():
    r = FakeRedis()
    # Same calendar day as entry → held < min_hold_days=1
    now = datetime(2026, 7, 7, 15, 0, 0, tzinfo=EST)
    guard = LiveOrderGuard(
        redis_client=r,
        limits=V12RiskLimits(),
        kill_switch=KillSwitch(r),
        now_provider=lambda: now,
    )
    res = guard.check(
        _ctx(
            side="SELL",
            has_open_position=True,
            entry_at="2026-07-07T09:30:00-04:00",
            confidence=None,
            requested_size_pct=None,
            current_total_exposure_pct=None,
        )
    )
    assert res.allowed is False
    assert res.rule == "R-10_MIN_HOLD"


def test_min_hold_passes_after_threshold():
    r = FakeRedis()
    now = datetime(2026, 7, 12, 12, 0, 0, tzinfo=EST)
    guard = LiveOrderGuard(
        redis_client=r,
        limits=V12RiskLimits(),
        kill_switch=KillSwitch(r),
        now_provider=lambda: now,
    )
    res = guard.check(
        _ctx(
            side="SELL",
            has_open_position=True,
            entry_at="2026-07-07T09:30:00-04:00",
            confidence=None,
            requested_size_pct=None,
            current_total_exposure_pct=None,
        )
    )
    assert res.allowed is True


# ----- Order of evaluation -----


def test_killswitch_short_circuits_before_other_rules():
    """A halted system should short-circuit and not reach other checks."""
    r = FakeRedis()
    ks = KillSwitch(r)
    ks.halt("emergency")
    guard = LiveOrderGuard(redis_client=r, limits=V12RiskLimits(), kill_switch=ks)
    # No matter what ctx says, halt wins:
    res = guard.check(
        _ctx(
            confidence=0.10,  # would fail R-09
            requested_size_pct=0.99,  # would fail R-02
            mode="nonexistent",  # would fail MODE_BLOCK
        )
    )
    assert res.allowed is False
    assert res.rule == "KILL_SWITCH"


def test_blacklist_before_daily_cap():
    """Blacklisted symbol should be caught even if daily cap is exhausted."""
    r = FakeRedis()
    r.sadd(BLACKLIST_KEY, "NVDA")
    today = datetime.now(EST).strftime("%Y-%m-%d")
    r.set(f"risk:daily_trades:{today}", "10")  # over cap
    guard = LiveOrderGuard(redis_client=r, limits=V12RiskLimits(), kill_switch=KillSwitch(r))
    res = guard.check(_ctx(symbol="NVDA"))
    assert res.rule == "BLACKLIST"


# ----- Make helper -----


def test_make_live_order_guard_uses_v12_defaults():
    """Spot-check the helper builder applies V12RiskLimits defaults."""
    r = FakeRedis()
    guard = make_live_order_guard(r)
    assert guard.limits.risk_per_trade_pct == V12RiskLimits().risk_per_trade_pct
    assert guard.limits.min_entry_confidence == V12RiskLimits().min_entry_confidence
