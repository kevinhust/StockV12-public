"""Regression tests for P2-boundaries (council 2026-07-09): EntryContext
and EntryResult must live in pipeline.types to break the
services.entry_orchestrator <-> pipeline.stages circular import.

Pre-fix: EntryContext/EntryResult defined in services.entry_orchestrator.
pipeline.stages imported them from there. entry_orchestrator then did
a deferred local import of pipeline.stages to break the cycle.

Post-fix: pipeline.types holds the canonical dataclasses. Both
services.entry_orchestrator and pipeline.stages import from there.
The deferred local import is replaced with a top-level one.
"""

from __future__ import annotations

import inspect



def test_entry_types_in_pipeline_types_module():
    """P2: EntryContext and EntryResult must be importable from pipeline.types."""
    from stockapp_v12.pipeline.types import EntryContext, EntryResult
    assert EntryContext is not None
    assert EntryResult is not None


def test_entry_types_same_class_via_all_paths():
    """P2: All import paths must resolve to the SAME class object."""
    from stockapp_v12.pipeline.types import (
        EntryContext as PCTX,
        EntryResult as PRES,
    )
    from stockapp_v12.services.entry_orchestrator import (
        EntryContext as SECTX,
        EntryResult as SERES,
    )
    from stockapp_v12.pipeline.stages import (
        EntryContext as STCTX,
        EntryResult as STRES,
    )
    assert PCTX is SECTX is STCTX
    assert PRES is SERES is STRES


def test_no_deferred_local_import_in_entry_orchestrator():
    """P2: services.entry_orchestrator must no longer defer the pipeline.stages import.

    The cycle is broken by moving EntryContext/EntryResult to pipeline.types,
    so the deferred local import should now be a regular top-level import.
    """
    from stockapp_v12.services import entry_orchestrator as eo
    src = inspect.getsource(eo)
    # Look inside the evaluate method for a deferred import pattern.
    evaluate_section = src.split("def evaluate(")[1] if "def evaluate(" in src else src
    # The old pattern was a comment about 'Local import to avoid a top-level cycle'
    assert "Local import to avoid a top-level cycle" not in evaluate_section, (
        "P2-boundaries: services.entry_orchestrator still has the deferred "
        "import hack — should be a top-level import now that the cycle is broken."
    )
