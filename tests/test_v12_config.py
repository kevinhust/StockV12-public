"""B6 — V12Config typed runtime config (Pydantic schema + env loader).

Verifies:
  - All 5 sub-configs instantiate with sensible defaults (no env).
  - ``load_config()`` reads every documented env var correctly.
  - Validation rejects bad inputs (out-of-range port/db/spread).
  - Boolean parsing handles '1'/'true'/'yes'/'on' (truthy) and
    '0'/'false'/'no'/'off'/None (falsy).
  - Comma-separated ensemble paths parse to List[str].
  - Schema is importable via both ``stockapp_v12.config`` and
    ``stockapp_v12.config.schema``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from stockapp_v12.config import (
    ForecastConfig,
    NotifierConfig,
    PolygonConfig,
    RedisConfig,
    RiskConfig,
    V12Config,
    load_config,
)


# ─── Defaults (no env) ─────────────────────────────────────────────────────


def test_load_config_returns_v12_config_with_all_sub_configs():
    """``load_config()`` with empty env returns a fully-defaulted V12Config."""
    cfg = load_config(env={})
    assert isinstance(cfg, V12Config)
    assert isinstance(cfg.redis, RedisConfig)
    assert isinstance(cfg.risk, RiskConfig)
    assert isinstance(cfg.forecast, ForecastConfig)
    assert isinstance(cfg.polygon, PolygonConfig)
    assert isinstance(cfg.notifier, NotifierConfig)
    assert cfg.env == "dev"


def test_defaults_match_legacy_os_environ_get_values():
    """Defaults must match the legacy os.environ.get(...) defaults to avoid
    silently flipping behavior on B6 adoption."""
    cfg = load_config(env={})
    assert cfg.redis.host == "localhost"
    assert cfg.redis.port == 6379
    assert cfg.redis.db == 0
    assert cfg.redis.password is None
    assert cfg.risk.enabled is True  # STOCKAPP_V12_RISK defaults to "1"
    assert cfg.risk.entry_enabled is True
    assert cfg.forecast.model_path == "models/chronos"
    assert cfg.forecast.ensemble_paths == []
    assert cfg.forecast.fallback_spread_pct == 0.02
    assert cfg.polygon.api_key is None
    assert cfg.notifier.backend == "log"
    assert cfg.notifier.redis_channel == "v12:notify"
    assert cfg.notifier.webhook_username == "stockapp-v12"


# ─── Env-var mapping ───────────────────────────────────────────────────────


def test_redis_env_vars():
    """REDIS_HOST / REDIS_PORT / REDIS_DB / REDIS_PASSWORD map correctly."""
    cfg = load_config(env={
        "REDIS_HOST": "10.0.0.1",
        "REDIS_PORT": "6380",
        "REDIS_DB": "3",
        "REDIS_PASSWORD": "secret123",
    })
    assert cfg.redis.host == "10.0.0.1"
    assert cfg.redis.port == 6380
    assert cfg.redis.db == 3
    assert cfg.redis.password == "secret123"


def test_risk_env_vars_truthy_parsing():
    """STOCKAPP_V12_RISK parses '1'/'true'/'yes'/'on' as enabled."""
    for val in ("1", "true", "TRUE", "yes", "YES", "on"):
        cfg = load_config(env={"STOCKAPP_V12_RISK": val})
        assert cfg.risk.enabled is True, f"Expected True for {val!r}"
    for val in ("0", "false", "no", "off", ""):
        cfg = load_config(env={"STOCKAPP_V12_RISK": val})
        assert cfg.risk.enabled is False, f"Expected False for {val!r}"


def test_forecast_env_vars():
    """CHRONOS_MODEL_PATH + ensemble + fallback spread parse correctly."""
    cfg = load_config(env={
        "CHRONOS_MODEL_PATH": "models/chronos-v2",
        "CHRONOS_ENSEMBLE_MODEL_PATHS": "models/a,models/b,models/c",
        "CHRONOS_FALLBACK_SPREAD_PCT": "0.05",
    })
    assert cfg.forecast.model_path == "models/chronos-v2"
    assert cfg.forecast.ensemble_paths == ["models/a", "models/b", "models/c"]
    assert cfg.forecast.fallback_spread_pct == 0.05


def test_ensemble_paths_drops_empty_segments():
    """Empty / whitespace-only ensemble segments are dropped (not preserved)."""
    cfg = load_config(env={"CHRONOS_ENSEMBLE_MODEL_PATHS": "a,,b, ,c,"})
    assert cfg.forecast.ensemble_paths == ["a", "b", "c"]


def test_polygon_env_var():
    """POLYGON_API_KEY maps to polygon.api_key (string, never logged)."""
    cfg = load_config(env={"POLYGON_API_KEY": "test-key-xyz"})
    assert cfg.polygon.api_key == "test-key-xyz"


def test_notifier_env_vars():
    """All 5 V12_NOTIFIER_* env vars map correctly."""
    cfg = load_config(env={
        "V12_NOTIFIER_BACKEND": "redis",
        "V12_NOTIFIER_REDIS_URL": "redis://10.0.0.1:6379/0",
        "V12_NOTIFIER_REDIS_CHANNEL": "v12:custom",
        "V12_NOTIFIER_WEBHOOK_URL": "https://hooks.test/x",
        "V12_NOTIFIER_WEBHOOK_USERNAME": "custom-bot",
    })
    assert cfg.notifier.backend == "redis"
    assert cfg.notifier.redis_url == "redis://10.0.0.1:6379/0"
    assert cfg.notifier.redis_channel == "v12:custom"
    assert cfg.notifier.webhook_url == "https://hooks.test/x"
    assert cfg.notifier.webhook_username == "custom-bot"


def test_env_tag():
    """V12_ENV → env field (dev | staging | prod)."""
    assert load_config(env={"V12_ENV": "staging"}).env == "staging"
    assert load_config(env={"V12_ENV": "prod"}).env == "prod"
    assert load_config(env={}).env == "dev"


# ─── Validation (Pydantic) ────────────────────────────────────────────────


def test_redis_port_out_of_range_raises():
    """Port > 65535 → ValidationError (Pydantic enforces ge/le)."""
    with pytest.raises(ValidationError):
        RedisConfig(port=70000)


def test_redis_port_zero_raises():
    """Port 0 → ValidationError."""
    with pytest.raises(ValidationError):
        RedisConfig(port=0)


def test_redis_db_negative_raises():
    """db < 0 → ValidationError."""
    with pytest.raises(ValidationError):
        RedisConfig(db=-1)


def test_fallback_spread_out_of_range_raises():
    """fallback_spread_pct outside [0, 1] → ValidationError."""
    with pytest.raises(ValidationError):
        ForecastConfig(fallback_spread_pct=1.5)
    with pytest.raises(ValidationError):
        ForecastConfig(fallback_spread_pct=-0.1)


def test_risk_daily_cap_zero_raises():
    """daily_cap must be ≥ 1 (R-05)."""
    with pytest.raises(ValidationError):
        RiskConfig(daily_cap=0)


# ─── Substitutability ──────────────────────────────────────────────────────


def test_v12_config_is_serializable_to_dict():
    """V12Config must be dumpable to dict (e.g. for logging)."""
    cfg = load_config(env={"REDIS_HOST": "10.0.0.1"})
    d = cfg.model_dump()
    assert d["redis"]["host"] == "10.0.0.1"
    assert d["env"] == "dev"
    # Polygon api_key present even if None (serialized for audit).
    assert "api_key" in d["polygon"]


def test_load_config_does_not_mutate_callers_env():
    """``load_config(env={...})`` does not write back to ``os.environ``."""
    import os

    sentinel = "B6_NO_MUTATE_SENTINEL"
    os.environ.pop(sentinel, None)
    load_config(env={"REDIS_HOST": "test-host"})
    # The function must not have written REDIS_HOST back to the process env.
    assert os.environ.get("REDIS_HOST") != "test-host", (
        "load_config(env=...) leaked into os.environ — read-only contract violated"
    )
    assert sentinel not in os.environ


def test_schema_importable_from_both_paths():
    """Pydantic models importable from both ``stockapp_v12.config`` and
    ``stockapp_v12.config.schema`` (identity check)."""
    from stockapp_v12.config.schema import V12Config as _SchemaRoot

    assert V12Config is _SchemaRoot, "Re-export identity broken"