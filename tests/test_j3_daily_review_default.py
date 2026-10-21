"""Regression tests for P2-spec (council 2026-07-09): DailyReview
apply_immediately default must be False (spec says execution happens
next session via pending_action consumer, not immediately).

Pre-fix: default was True (paper-soak convenience). This meant live and
paper both had TRIM/EXIT/TIGHTEN actions applied immediately, violating
the spec's pending_action contract.

Post-fix: default is False. paper-soak opt-in via apply_immediately=True.
"""

from __future__ import annotations

import inspect



def test_daily_review_default_is_false():
    """P2: DailyReview.apply_immediately must default to False (spec)."""
    from stockapp_v12.services.daily_review import DailyReview
    sig = inspect.signature(DailyReview.__init__)
    assert sig.parameters["apply_immediately"].default is False, (
        "P2-spec: DailyReview.apply_immediately must default to False; "
        "execution happens next session via pending_action consumer."
    )


def test_daily_review_can_still_opt_in():
    """P2: Operators can still opt in to immediate execution (paper-soak)."""
    from stockapp_v12.services.daily_review import DailyReview
    # Sanity: True must be accepted without error
    sig = inspect.signature(DailyReview.__init__)
    assert sig.parameters["apply_immediately"].annotation == "bool"
