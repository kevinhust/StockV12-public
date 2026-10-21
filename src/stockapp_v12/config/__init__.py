"""V12 runtime config (B6) — typed Pydantic schema + env-var loader.

Public surface:
  - ``load_config()`` — assemble ``V12Config`` from ``os.environ``
  - ``V12Config`` + sub-models (``RedisConfig``, ``RiskConfig``,
    ``ForecastConfig``, ``PolygonConfig``, ``NotifierConfig``)

B6 lands the schema + loader; **no call sites are migrated** — the
follow-up work reads ``V12Config`` instead of ``os.environ.get(...)``
and is tracked separately (path-walk harmless per Council § 3.4).

See ``schema.py`` for the Pydantic model definitions and
``loader.py`` for the env-var → field mapping.
"""

from __future__ import annotations

from stockapp_v12.config.loader import load_config
from stockapp_v12.config.schema import (
    ForecastConfig,
    NotifierConfig,
    PolygonConfig,
    RedisConfig,
    RiskConfig,
    V12Config,
)

__all__ = [
    "load_config",
    "V12Config",
    "RedisConfig",
    "RiskConfig",
    "ForecastConfig",
    "PolygonConfig",
    "NotifierConfig",
]