"""Regression tests for P2-spec (council 2026-07-09): catalyst_type aliasing
contract must be documented and consistent.

Pre-fix: the aliasing `catalyst_type=(analyst.thesis_tags[0] if analyst
else None)` was a silent contract — no documentation, no test, easy to
break by adding a real `catalyst_type` field to AnalystOutput.

Post-fix: the aliasing is documented in stages.py and verified by a
source-inspection test that asserts the contract is unchanged.
"""

from __future__ import annotations

import inspect



def test_stages_documents_catalyst_aliasing():
    """P2: stages.py must contain a comment documenting the catalyst_type
    aliasing contract from analyst.thesis_tags[0]."""
    from stockapp_v12.pipeline import stages as stages_mod
    src = inspect.getsource(stages_mod)
    assert "catalyst_type aliasing contract" in src, (
        "P2-spec: stages.py must document the catalyst_type aliasing contract"
    )
    assert "thesis_tags[0]" in src, (
        "P2-spec: aliasing must be explicit about which field is aliased"
    )


def test_stages_aliasing_logic_unchanged():
    """P2: the aliasing logic itself must remain `thesis_tags[0] if thesis_tags else None`."""
    from stockapp_v12.pipeline import stages as stages_mod
    src = inspect.getsource(stages_mod)
    # Look for the aliasing pattern: `(analyst.thesis_tags[0] if analyst and analyst.thesis_tags else None)`
    expected_pattern = "analyst.thesis_tags[0] if analyst and analyst.thesis_tags else None"
    assert expected_pattern in src, (
        f"P2-spec: catalyst_type aliasing pattern changed — expected "
        f"`{expected_pattern}`"
    )


def test_position_record_has_catalyst_type_at_entry():
    """P2: PositionRecord must have a catalyst_type_at_entry field to receive
    the aliased value (per POSITION_LIFECYCLE.md)."""
    from stockapp_v12.services.position_manager import PositionRecord
    import dataclasses
    fields = {f.name for f in dataclasses.fields(PositionRecord)}
    assert "catalyst_type_at_entry" in fields
