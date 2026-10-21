"""Task 6: Legacy Chronos adapter cages archive imports for ForecastService."""

from __future__ import annotations

import inspect

from stockapp_v12.adapters.legacy_v11 import chronos as legacy_chronos
from stockapp_v12.services import forecast_service as fs_mod


def test_forecast_service_source_has_no_direct_ensure_v11():
    """ForecastService._resolve_backend must not call archive bridge directly."""
    src = inspect.getsource(fs_mod.ForecastService._resolve_backend)
    assert "ensure_v11_archive_on_path" not in src
    assert "from src.services.chronos_service" not in src


def test_legacy_module_is_only_archive_gateway():
    """Only adapters/legacy_v11/chronos may call ensure_v11 for Chronos."""
    src = inspect.getsource(legacy_chronos)
    assert "ensure_v11_archive_on_path" in src


def test_forecast_service_resolve_uses_build_legacy_chronos_backend():
    """Default path must go through build_legacy_chronos_backend factory."""
    src = inspect.getsource(fs_mod.ForecastService._resolve_backend)
    assert "build_legacy_chronos_backend" in src
