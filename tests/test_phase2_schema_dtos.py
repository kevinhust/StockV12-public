"""Phase 2: DTOs live in schemas; services re-export; pipeline free of services."""

from __future__ import annotations

import ast
from pathlib import Path

from stockapp_v12.schemas.signal import (
    SignalRuleConfig,
    SignalRuleContext,
    SignalRuleResult,
)
from stockapp_v12.schemas.yolo import YoloFilterDecision
from stockapp_v12.services.signal_rule_engine import (
    SignalRuleConfig as ConfigFromServices,
    SignalRuleContext as ContextFromServices,
    SignalRuleResult as ResultFromServices,
)
from stockapp_v12.services.yolo_filter import YoloFilterDecision as YoloFromServices


def test_signal_dto_identity_services_reexport():
    assert ConfigFromServices is SignalRuleConfig
    assert ContextFromServices is SignalRuleContext
    assert ResultFromServices is SignalRuleResult


def test_yolo_dto_identity_services_reexport():
    assert YoloFromServices is YoloFilterDecision


def test_pipeline_ast_has_no_services_imports():
    root = Path(__file__).resolve().parents[1] / "src" / "stockapp_v12" / "pipeline"
    for path in root.rglob("*.py"):
        if path.name == "__init__.py" and path.stat().st_size == 0:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("stockapp_v12.services"), (
                    f"{path} imports {node.module}"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("stockapp_v12.services"), (
                        f"{path} imports {alias.name}"
                    )


def test_entry_context_from_pipeline_types():
    from stockapp_v12.pipeline.types import EntryContext, EntryResult
    from stockapp_v12.services.entry_orchestrator import (
        EntryContext as CtxFromOrch,
        EntryResult as ResFromOrch,
    )

    assert CtxFromOrch is EntryContext
    assert ResFromOrch is EntryResult
