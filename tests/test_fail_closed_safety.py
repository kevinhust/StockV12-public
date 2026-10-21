"""hygiene-D — HIGH-severity fail-open → fail-closed regression tests.

Pins the four safety boundaries that were silently fail-open before
hygiene-D (per silent-failure-hunter audit 2026-07-09):

  H1 — bridge/gatekeeper.py: get_daily_trade_count returns 0 on Redis
       error, silently disabling R-05 cap.
  H2 — core/limits.py: YAML parse failure silently falls to defaults.
  M1 — services/forecast_service.py: ChronosService init silently nulls.
  M5 — services/veto_alarm.py: is_alarmed returns False on Redis error.

These tests pin the new fail-closed behavior so a future contributor
cannot regress safety semantics.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock


# ─── H1: gatekeeper.get_daily_trade_count fail-closed ──────────────────────


def test_gatekeeper_daily_trade_count_fails_closed_on_redis_error(caplog):
    """Redis error → returns cap (fail-closed), NOT 0 (fail-open).

    This is the single most important safety regression test added
    in hygiene-D: a Redis blip previously let trades flow past R-05.
    """
    from stockapp_v12.bridge.gatekeeper import get_daily_trade_count
    from stockapp_v12.core.limits import load_v12_risk_limits

    cap = load_v12_risk_limits().max_daily_trades
    redis = MagicMock()
    redis.get.side_effect = ConnectionError("redis unreachable")

    caplog.set_level(logging.ERROR)
    count = get_daily_trade_count(redis)

    assert count >= cap, (
        f"FAIL-OPEN REGRESSION: get_daily_trade_count returned {count} on "
        f"Redis error; expected >= {cap} (cap) to keep R-05 enforcing"
    )
    # Operator must see the failure.
    assert any(
        "R-05 daily-trade count read failed" in r.message
        for r in caplog.records
    ), "Expected EXCEPTION-level log on Redis error"


def test_gatekeeper_daily_trade_count_happy_path():
    """Happy path: Redis returns '3' → count == 3."""
    from stockapp_v12.bridge.gatekeeper import get_daily_trade_count

    redis = MagicMock()
    redis.get.return_value = b"3"

    assert get_daily_trade_count(redis) == 3


def test_gatekeeper_daily_trade_count_handles_none_redis_value():
    """Redis returns None (key absent) → count == 0."""
    from stockapp_v12.bridge.gatekeeper import get_daily_trade_count

    redis = MagicMock()
    redis.get.return_value = None

    assert get_daily_trade_count(redis) == 0


# ─── H2: limits YAML fail-closed ───────────────────────────────────────────


def test_limits_yaml_parse_failure_logs_and_returns_defaults(tmp_path, caplog):
    """YAML parse error → WARNING logged + defaults (NOT silent swallow)."""
    from stockapp_v12.core.limits import load_v12_risk_limits

    bad_path = tmp_path / "broken.yaml"
    bad_path.write_text("invalid: yaml: : :\n  - unmatched")  # malformed

    caplog.set_level(logging.WARNING)
    limits = load_v12_risk_limits(config_path=bad_path)

    # Defaults are still returned (don't crash startup) …
    assert limits.max_daily_trades >= 1
    # … but the failure is logged at WARNING.
    assert any(
        "risk config YAML parse failed" in r.message
        for r in caplog.records
    ), "Expected WARNING log on YAML parse failure"


def test_limits_yaml_missing_file_logs_warning(tmp_path, caplog):
    """Missing config → WARNING (not silent)."""
    from stockapp_v12.core.limits import load_v12_risk_limits

    missing = tmp_path / "does-not-exist.yaml"
    caplog.set_level(logging.WARNING)
    limits = load_v12_risk_limits(config_path=missing)
    assert limits.max_daily_trades >= 1
    assert any(
        "risk config not found" in r.message
        for r in caplog.records
    )


# ─── M1: forecast_service Chronos init fail-closed ─────────────────────────


def test_forecast_service_chronos_init_failure_logs_warning(caplog, monkeypatch):
    """ChronosService constructor raises → logger.warning + backend=None."""
    import src.services.chronos_service as _cs

    class _ExplodingChronos:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("model not loaded")

    monkeypatch.setattr(_cs, "ChronosService", _ExplodingChronos)
    from stockapp_v12.services import forecast_service as fsvc

    fs = fsvc.ForecastService()
    fs._backend = None
    fs._resolved_backend = None  # ensure the function actually runs

    caplog.set_level(logging.WARNING)
    backend = fs._resolve_backend()

    assert backend is None, (
        f"FAIL-OPEN REGRESSION: forecast_service returned {backend!r} on "
        f"ChronosService init failure; expected None"
    )
    assert any(
        "ChronosService init failed" in r.message
        for r in caplog.records
    ), "Expected WARNING log on Chronos init failure"


# ─── M5: veto_alarm.is_alarmed fail-closed ─────────────────────────────────


def test_veto_alarm_is_alarmed_redis_error_logs_warning(caplog):
    """Redis read error → returns False + WARNING log."""
    from stockapp_v12.services.veto_alarm import VetoCounter, VetoRateAlarm

    counter = MagicMock(spec=VetoCounter)
    alarm = VetoRateAlarm(counter=counter)
    redis = MagicMock()
    redis.get.side_effect = ConnectionError("redis down")

    caplog.set_level(logging.WARNING)
    alarmed = alarm.is_alarmed(redis_client=redis)

    assert alarmed is False  # safe default
    assert any(
        "is_alarmed redis read failed" in r.message
        for r in caplog.records
    ), "Expected WARNING log on is_alarmed Redis error"


def test_veto_alarm_is_alarmed_happy_path():
    """Happy path: Redis returns truthy → alarmed == True."""
    from stockapp_v12.services.veto_alarm import VetoCounter, VetoRateAlarm

    counter = MagicMock(spec=VetoCounter)
    alarm = VetoRateAlarm(counter=counter)
    redis = MagicMock()
    redis.get.return_value = b"1"

    assert alarm.is_alarmed(redis_client=redis) is True


def test_veto_alarm_is_alarmed_happy_path_no_alarm():
    """Happy path: Redis returns falsy → alarmed == False."""
    from stockapp_v12.services.veto_alarm import VetoCounter, VetoRateAlarm

    counter = MagicMock(spec=VetoCounter)
    alarm = VetoRateAlarm(counter=counter)
    redis = MagicMock()
    redis.get.return_value = None

    assert alarm.is_alarmed(redis_client=redis) is False