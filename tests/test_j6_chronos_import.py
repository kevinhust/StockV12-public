"""Regression tests for Chronos archive boundary (P2-boundaries → Phase 1 cage).

Historical P2 (council 2026-07-09): forecast_service resolved V11 ChronosService
via an explicit ``ensure_v11_archive_on_path`` call inside the service module
(not bare ``from services.chronos_service import``).

Phase 1 / ADR-011 (2026-07-26): archive Chronos is caged behind
``adapters/legacy_v11/chronos``. ForecastService must not call ensure_v11 or
import ``src.services.chronos_service``; default backend is
``build_legacy_chronos_backend``.

See also: tests/test_legacy_chronos_adapter.py
"""

from __future__ import annotations

import inspect
import re


def test_forecast_service_uses_v11_compat_bridge():
    """Phase 1: forecast_service must cage Chronos behind legacy_v11 adapter."""
    from stockapp_v12.adapters.legacy_v11 import chronos as legacy_chronos
    from stockapp_v12.services import forecast_service as fs_mod

    fs_src = inspect.getsource(fs_mod)
    resolve_src = inspect.getsource(fs_mod.ForecastService._resolve_backend)
    legacy_src = inspect.getsource(legacy_chronos)

    # Service must not call the archive bridge or import archive Chronos directly
    assert "ensure_v11_archive_on_path" not in resolve_src, (
        "Phase 1: ForecastService._resolve_backend must not call "
        "ensure_v11_archive_on_path (caged in adapters/legacy_v11)"
    )
    assert "from src.services.chronos_service" not in resolve_src, (
        "Phase 1: ForecastService must not import archive ChronosService "
        "directly; use build_legacy_chronos_backend"
    )
    assert "build_legacy_chronos_backend" in resolve_src, (
        "Phase 1: ForecastService default path must use "
        "build_legacy_chronos_backend"
    )

    # Adapter is the sole Chronos archive gateway
    assert "ensure_v11_archive_on_path" in legacy_src, (
        "Phase 1: adapters/legacy_v11/chronos must call ensure_v11_archive_on_path"
    )
    assert "from src.services.chronos_service import" in legacy_src, (
        "Phase 1: legacy adapter must import via `from src.services...` "
        "after the V11 bridge call"
    )

    # Must NOT have bare `from services.chronos_service` as an active import
    # in either forecast_service or the legacy adapter (strip comments).
    for label, src in (("forecast_service", fs_src), ("legacy_chronos", legacy_src)):
        code_lines = [
            line
            for line in src.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        code_only = "\n".join(code_lines)
        bare_pattern = r"^\s*from\s+services\.chronos_service\s+import"
        assert not re.search(bare_pattern, code_only, re.MULTILINE), (
            f"P2-boundaries: active `from services.chronos_service import` "
            f"in {label} must not exist (use archive bridge path)"
        )
