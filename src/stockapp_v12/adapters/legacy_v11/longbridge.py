"""Legacy Longbridge data service — sole gateway for archive LongbridgeDataService.

Only this module may call ``ensure_v11_archive_on_path`` for Longbridge market data.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from stockapp_v12.core._v11_compat import ensure_v11_archive_on_path

logger = logging.getLogger(__name__)


def get_legacy_longbridge_service() -> Optional[Any]:
    """Load V11 LongbridgeDataService or return None on soft failure.

    Never raises to callers. Returns None when:
      - STOCKAPP_DISABLE_V11_ARCHIVE is set
      - archive not on disk / import fails
      - service factory fails
    """
    try:
        if not ensure_v11_archive_on_path():
            logger.debug("V11 archive unavailable or disabled — no Longbridge service")
            return None
        from src.services.longbridge_data_service import get_longbridge_service

        return get_longbridge_service()
    except Exception as e:
        logger.debug("V11 longbridge service not available: %s", e)
        return None


__all__ = ["get_legacy_longbridge_service"]
