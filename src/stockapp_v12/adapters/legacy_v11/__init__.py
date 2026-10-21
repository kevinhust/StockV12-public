"""Cage for V11 archive imports — only modules in this package may call ensure_v11.

Phase 4:
  - chronos.py     — ChronosService
  - longbridge.py  — LongbridgeDataService
  - risk.py        — HardRiskGatekeeper
"""

from stockapp_v12.adapters.legacy_v11.chronos import (
    LegacyChronosAdapter,
    build_legacy_chronos_backend,
)
from stockapp_v12.adapters.legacy_v11.longbridge import get_legacy_longbridge_service
from stockapp_v12.adapters.legacy_v11.risk import load_hard_risk_gatekeeper_class

__all__ = [
    "LegacyChronosAdapter",
    "build_legacy_chronos_backend",
    "get_legacy_longbridge_service",
    "load_hard_risk_gatekeeper_class",
]
