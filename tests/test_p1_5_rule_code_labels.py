"""Regression tests for P1-5 (council 2026-07-09): rule code labels in
LiveOrderGuard must match the spec's R-rule numbering.

Pre-fix: code emitted R-02_SINGLE_CAP and R-08_EXPOSURE_CAP, but per spec:
  R-02 = drawdown limit (not single-position cap)
  R-03 = single position cap
  R-04 = portfolio exposure cap
  R-08 = correlation limit (not exposure cap)

Operators reading journal entries with the wrong labels would look up the
wrong rule and misunderstand why a trade was denied.

Post-fix: labels are R-03_SINGLE_CAP and R-04_EXPOSURE_CAP.
"""

from __future__ import annotations

import inspect


from stockapp_v12.bridge.live_guard import LiveOrderGuard


def test_no_wrong_r02_single_cap_label():
    """P1-5: R-02_SINGLE_CAP must not appear in live_guard source."""
    src = inspect.getsource(LiveOrderGuard)
    assert "R-02_SINGLE_CAP" not in src, (
        "P1-5: R-02 is drawdown per spec, not single-position cap. "
        "Use R-03_SINGLE_CAP."
    )


def test_no_wrong_r08_exposure_cap_label():
    """P1-5: R-08_EXPOSURE_CAP must not appear in live_guard source."""
    src = inspect.getsource(LiveOrderGuard)
    assert "R-08_EXPOSURE_CAP" not in src, (
        "P1-5: R-08 is correlation per spec, not exposure cap. "
        "Use R-04_EXPOSURE_CAP."
    )


def test_correct_r03_single_cap_label():
    """P1-5: R-03_SINGLE_CAP label exists in source."""
    src = inspect.getsource(LiveOrderGuard)
    assert "R-03_SINGLE_CAP" in src


def test_correct_r04_exposure_cap_label():
    """P1-5: R-04_EXPOSURE_CAP label exists in source."""
    src = inspect.getsource(LiveOrderGuard)
    assert "R-04_EXPOSURE_CAP" in src
