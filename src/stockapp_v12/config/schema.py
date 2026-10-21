"""V12 typed runtime config (B6) — Pydantic schema.

Single source of truth for runtime config that today is scattered across:
  - ``config/*.yaml`` (legacy V11, not loaded by V12 code)
  - ``configs/experiments/*.yaml`` (V12 experiment harnesses only)
  - ``os.environ.get(...)`` scattered across 5+ modules

B6 introduces a single Pydantic ``V12Config`` model so future call sites
can ``from stockapp_v12.config import load_config`` and stop reading
``os.environ`` directly. **No call sites are migrated in B6** —
that's the follow-up work after the schema lands.

Council risk (per roadmap § B6): path-walk harmless, zero ADR-008
sensitivity. Sub-configs are independently optional so partial
deployments degrade gracefully.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class RedisConfig(BaseModel):
    """Redis connection config (used by position_manager, journal, veto_counter)."""

    host: str = Field(default="localhost", description="Redis hostname")
    port: int = Field(default=6379, ge=1, le=65535, description="Redis port")
    db: int = Field(default=0, ge=0, description="Redis logical DB index")
    password: Optional[str] = Field(default=None, description="Redis AUTH password (optional)")


class RiskConfig(BaseModel):
    """V12 risk-gate config (controls whether the V12 trading path is armed)."""

    enabled: bool = Field(default=True, description="V12 entry enabled (master kill-switch)")
    entry_enabled: bool = Field(default=True, description="Per-day entry gate (env-overridable)")
    daily_cap: int = Field(default=4, ge=1, le=50, description="R-05 max buys per day")
    cooldown_seconds: int = Field(default=3600, ge=0, description="R-06 symbol cooldown seconds")


class ForecastConfig(BaseModel):
    """Chronos forecast backend config."""

    model_path: str = Field(
        default="models/chronos",
        description="Chronos model directory (relative to repo root)",
    )
    ensemble_paths: List[str] = Field(
        default_factory=list,
        description="Optional ensemble model paths (overrides single-model path when non-empty)",
    )
    fallback_spread_pct: float = Field(
        default=0.02,
        ge=0.0,
        le=1.0,
        description="Fallback forecast spread (q90-q10)/(q90+q10) when backend returns no quantiles",
    )


class PolygonConfig(BaseModel):
    """Polygon data provider config (optional, used by SwingScanner fallback)."""

    api_key: Optional[str] = Field(default=None, description="Polygon.io API key (env-only, never log)")


class NotifierConfig(BaseModel):
    """Notifier backend config (B5 surface, env-driven)."""

    backend: str = Field(default="log", description="Notifier backend: log | redis | webhook")
    redis_url: Optional[str] = Field(default=None, description="Redis URL for redis backend")
    redis_channel: str = Field(default="v12:notify", description="Redis pubsub channel")
    webhook_url: Optional[str] = Field(default=None, description="Slack/Discord webhook URL")
    webhook_username: str = Field(default="stockapp-v12", description="Webhook bot username")


class V12Config(BaseModel):
    """Top-level V12 runtime config (B6 root)."""

    redis: RedisConfig = Field(default_factory=RedisConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    forecast: ForecastConfig = Field(default_factory=ForecastConfig)
    polygon: PolygonConfig = Field(default_factory=PolygonConfig)
    notifier: NotifierConfig = Field(default_factory=NotifierConfig)
    env: str = Field(default="dev", description="Environment tag: dev | staging | prod")


__all__ = [
    "RedisConfig",
    "RiskConfig",
    "ForecastConfig",
    "PolygonConfig",
    "NotifierConfig",
    "V12Config",
]