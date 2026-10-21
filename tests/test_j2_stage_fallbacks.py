"""Regression tests for P2-spec (council 2026-07-09): FALLBACK_ANALYST_CONFIDENCE
and FALLBACK_ATR_PCT must be configurable via core/limits.StageFallbacks.

Pre-fix: the values were hardcoded in pipeline/stages.py as module-level
constants. Operators had to edit source code to retune them, and there
was no way to test the fallback values in isolation.

Post-fix: StageFallbacks dataclass in core/limits.py holds the values.
stages.py imports them and exposes backward-compatible module aliases.
"""

from __future__ import annotations



def test_stage_fallbacks_dataclass_exists():
    """P2: StageFallbacks dataclass must exist in core.limits."""
    from stockapp_v12.core.limits import StageFallbacks
    fb = StageFallbacks()
    assert fb.analyst_confidence == 0.75
    assert fb.atr_pct == 0.05


def test_stage_fallbacks_can_be_overridden():
    """P2: StageFallbacks values must be operator-tunable via constructor."""
    from stockapp_v12.core.limits import StageFallbacks
    fb = StageFallbacks(analyst_confidence=0.60, atr_pct=0.03)
    assert fb.analyst_confidence == 0.60
    assert fb.atr_pct == 0.03


def test_stages_module_aliases_match_dataclass_defaults():
    """P2: stages.FALLBACK_* constants must equal StageFallbacks defaults."""
    from stockapp_v12.core.limits import StageFallbacks
    from stockapp_v12.pipeline.stages import (
        FALLBACK_ANALYST_CONFIDENCE,
        FALLBACK_ATR_PCT,
    )
    assert FALLBACK_ANALYST_CONFIDENCE == StageFallbacks.analyst_confidence
    assert FALLBACK_ATR_PCT == StageFallbacks.atr_pct


def test_stages_imports_from_core_limits():
    """P2: stages.py must import the fallback values from core/limits, not
    hardcode them as literals.
    """
    import inspect
    from stockapp_v12.pipeline import stages as stages_mod
    src = inspect.getsource(stages_mod)
    # Must reference core.limits in the same logical block as the constants
    assert "from stockapp_v12.core.limits import" in src, (
        "P2: stages.py does not import from core/limits"
    )
    assert "StageFallbacks" in src, (
        "P2: stages.py does not reference StageFallbacks dataclass"
    )
