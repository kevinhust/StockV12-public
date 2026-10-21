"""Legacy HardRiskGatekeeper — sole gateway for archive risk_controller.

Production V12 entry path uses bridge.gatekeeper sizing extensions + injected
gatekeeper_fn. This module exists for tests and any remaining V11 front-door
callers that still need the archived HardRiskGatekeeper class.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Type

from stockapp_v12.core._v11_compat import ensure_v11_archive_on_path

logger = logging.getLogger(__name__)


def load_hard_risk_gatekeeper_class() -> Optional[Type[Any]]:
    """Import archive HardRiskGatekeeper or return None if archive disabled/missing."""
    try:
        if not ensure_v11_archive_on_path():
            return None
        from src.core.risk_controller import HardRiskGatekeeper

        return HardRiskGatekeeper
    except Exception as e:
        logger.warning("HardRiskGatekeeper import failed: %s: %s", type(e).__name__, e)
        return None


__all__ = ["load_hard_risk_gatekeeper_class"]
