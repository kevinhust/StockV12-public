"""Phase 4: archive only via adapters/legacy_v11; disable flag honored."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from stockapp_v12.core import _v11_compat
from stockapp_v12.services import forecast_service as fs_mod
from stockapp_v12.services import swing_scanner as scan_mod


def test_disable_env_blocks_archive_path(monkeypatch):
    monkeypatch.setenv("STOCKAPP_DISABLE_V11_ARCHIVE", "1")
    assert _v11_compat.v11_archive_disabled() is True
    assert _v11_compat.ensure_v11_archive_on_path() is False


def test_services_have_no_ensure_v11_or_src_import():
    for mod in (fs_mod, scan_mod):
        path = Path(inspect.getfile(mod))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("src."), (
                    f"{path} imports {node.module}"
                )
                assert "ensure_v11" not in (node.module or "")
            if isinstance(node, ast.Import):
                for a in node.names:
                    assert not a.name.startswith("src.")
        src = path.read_text(encoding="utf-8")
        assert "ensure_v11_archive_on_path" not in src, path
        assert "from src." not in src, path


def test_only_legacy_v11_and_compat_call_ensure_v11():
    root = Path(__file__).resolve().parents[1] / "src" / "stockapp_v12"
    allowed = {
        root / "core" / "_v11_compat.py",
        root / "adapters" / "legacy_v11" / "chronos.py",
        root / "adapters" / "legacy_v11" / "longbridge.py",
        root / "adapters" / "legacy_v11" / "risk.py",
    }
    offenders = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        # definition site always contains the name
        if path.name == "_v11_compat.py":
            continue
        if "ensure_v11_archive_on_path(" in text and path not in allowed:
            offenders.append(str(path.relative_to(root.parent.parent)))
    assert offenders == [], f"ensure_v11 leaks outside cage: {offenders}"


def test_longbridge_factory_soft_fails_when_disabled(monkeypatch):
    monkeypatch.setenv("STOCKAPP_DISABLE_V11_ARCHIVE", "1")
    from stockapp_v12.adapters.legacy_v11.longbridge import (
        get_legacy_longbridge_service,
    )

    assert get_legacy_longbridge_service() is None


def test_risk_factory_soft_fails_when_disabled(monkeypatch):
    monkeypatch.setenv("STOCKAPP_DISABLE_V11_ARCHIVE", "1")
    from stockapp_v12.adapters.legacy_v11.risk import load_hard_risk_gatekeeper_class

    assert load_hard_risk_gatekeeper_class() is None
