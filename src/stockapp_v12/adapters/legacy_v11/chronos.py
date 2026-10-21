"""Legacy Chronos adapter — sole gateway from V12 to archive ChronosService.

Only this module may call ``ensure_v11_archive_on_path`` for Chronos.
``ForecastService`` resolves its default backend via ``build_legacy_chronos_backend``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import pandas as pd

from stockapp_v12.core._v11_compat import ensure_v11_archive_on_path

logger = logging.getLogger(__name__)


class LegacyChronosAdapter:
    """Wraps archive ChronosService; implements ChronosBackend detect/detect_batch."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def detect(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        return list(self._inner.detect(df) or [])

    def detect_batch(
        self, frames: Dict[str, pd.DataFrame]
    ) -> Dict[str, list[dict[str, Any]]]:
        return dict(self._inner.detect_batch(frames) or {})


def build_legacy_chronos_backend(
    *,
    model_path: Optional[str] = None,
    prediction_length: int = 10,
) -> Optional[LegacyChronosAdapter]:
    """Return ChronosBackend or None on soft failure (import/init). Never raises."""
    try:
        ensure_v11_archive_on_path()
        from src.services.chronos_service import ChronosService
    except ImportError as e:
        logger.warning("legacy Chronos import failed: %s", e)
        return None

    path = model_path or os.getenv("CHRONOS_MODEL_PATH", "models/chronos")
    try:
        inner = ChronosService(model_path=path, prediction_length=prediction_length)
    except Exception as e:
        # Message substring "ChronosService init failed" pinned by
        # tests/test_fail_closed_safety.py (hygiene-D M1).
        logger.warning(
            "ChronosService init failed "
            "(model_path=%s, horizon=%d): %s: %s — forecasts will "
            "fall back to ensemble/raw proxies",
            path,
            prediction_length,
            type(e).__name__,
            e,
        )
        return None
    return LegacyChronosAdapter(inner)


__all__ = ["LegacyChronosAdapter", "build_legacy_chronos_backend"]
