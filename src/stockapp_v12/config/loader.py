"""V12 config loader (B6) — env-var-driven Pydantic assembly.

Reads the runtime environment and assembles a typed ``V12Config``.
The roadmap § B6 spec calls out three legacy surfaces that must
be merged:

  - ``config/*.yaml`` — legacy V11 (loaded separately; B6 does not
    touch these — they remain V11-only)
  - ``configs/experiments/*.yaml`` — V12 experiment harnesses
    (loaded by ``scripts/train_*.py``; B6 does not touch these)
  - ``os.environ.get(...)`` scattered across modules — **THIS is
    what B6 consolidates** (notifier backend, redis, polygon,
    chronos model path, ensemble paths, risk-enabled flag).

Source of truth (env-var → Pydantic field mapping):

  RedisConfig:
    REDIS_HOST                  → redis.host  (default "localhost")
    REDIS_PORT                  → redis.port  (default 6379)
    REDIS_DB                    → redis.db    (default 0)
    REDIS_PASSWORD              → redis.password

  RiskConfig:
    STOCKAPP_V12_RISK           → risk.enabled (default true)
    STOCKAPP_V12_ENTRY_ENABLED  → risk.entry_enabled (default true)

  ForecastConfig:
    CHRONOS_MODEL_PATH          → forecast.model_path
    CHRONOS_ENSEMBLE_MODEL_PATHS → forecast.ensemble_paths (comma-sep)
    CHRONOS_FALLBACK_SPREAD_PCT → forecast.fallback_spread_pct

  PolygonConfig:
    POLYGON_API_KEY             → polygon.api_key

  NotifierConfig:
    V12_NOTIFIER_BACKEND        → notifier.backend
    V12_NOTIFIER_REDIS_URL      → notifier.redis_url
    V12_NOTIFIER_REDIS_CHANNEL  → notifier.redis_channel
    V12_NOTIFIER_WEBHOOK_URL    → notifier.webhook_url
    V12_NOTIFIER_WEBHOOK_USERNAME → notifier.webhook_username

  V12Config.env:
    V12_ENV                     → env (default "dev")

No call sites are migrated in B6 — that's the follow-up work.
"""

from __future__ import annotations

import os
from typing import Optional

from stockapp_v12.config.schema import (
    ForecastConfig,
    NotifierConfig,
    PolygonConfig,
    RedisConfig,
    RiskConfig,
    V12Config,
)


def _truthy(value: Optional[str]) -> bool:
    """Parse '1'/'true'/'yes' as True; anything else (including None) is False."""
    if value is None:
        return False
    return value.strip().lower() in ("1", "true", "yes", "on")


def load_config(env: Optional[dict] = None) -> V12Config:
    """Build a ``V12Config`` from ``os.environ`` (or an injected env dict for tests).

    :param env: Optional mapping to read from instead of ``os.environ``.
        Pass an explicit dict for deterministic unit tests.
    :return: Fully populated ``V12Config`` with defaults applied.
    """
    src = env if env is not None else dict(os.environ)

    redis = RedisConfig(
        host=src.get("REDIS_HOST", "localhost"),
        port=int(src.get("REDIS_PORT", "6379")),
        db=int(src.get("REDIS_DB", "0")),
        password=src.get("REDIS_PASSWORD"),
    )

    # STOCKAPP_V12_RISK defaults to "1" (legacy behavior — V12 risk
    # is on by default unless explicitly disabled).
    risk_enabled_env = src.get("STOCKAPP_V12_RISK", "1")
    risk = RiskConfig(
        enabled=_truthy(risk_enabled_env) if risk_enabled_env is not None else True,
        entry_enabled=_truthy(src.get("STOCKAPP_V12_ENTRY_ENABLED", "1")),
    )

    ensemble_paths_raw = src.get("CHRONOS_ENSEMBLE_MODEL_PATHS", "")
    forecast = ForecastConfig(
        model_path=src.get("CHRONOS_MODEL_PATH", "models/chronos"),
        ensemble_paths=[p.strip() for p in ensemble_paths_raw.split(",") if p.strip()],
        fallback_spread_pct=float(src.get("CHRONOS_FALLBACK_SPREAD_PCT", "0.02")),
    )

    polygon = PolygonConfig(api_key=src.get("POLYGON_API_KEY"))

    notifier = NotifierConfig(
        backend=src.get("V12_NOTIFIER_BACKEND", "log"),
        redis_url=src.get("V12_NOTIFIER_REDIS_URL"),
        redis_channel=src.get("V12_NOTIFIER_REDIS_CHANNEL", "v12:notify"),
        webhook_url=src.get("V12_NOTIFIER_WEBHOOK_URL"),
        webhook_username=src.get("V12_NOTIFIER_WEBHOOK_USERNAME", "stockapp-v12"),
    )

    return V12Config(
        redis=redis,
        risk=risk,
        forecast=forecast,
        polygon=polygon,
        notifier=notifier,
        env=src.get("V12_ENV", "dev"),
    )


__all__ = ["load_config"]