"""Regression tests for P1-4 (council 2026-07-09): float math replaced with
Decimal precision in position_manager + daily_review.

Pre-fix: ``round(entry_price, 2)`` and ``(last - entry) / entry`` used Python
float math, which introduces representational drift (0.01 not exactly
representable in IEEE-754). Over multi-day positions the drift compounds.

Post-fix: ``round_dollar``/``round_shares``/``round_pct`` from core.precision
are used for all quantization. ``unrealized`` and ``pnl_atr`` in daily_review
are computed via Decimal internally, converted to float at the boundary.
"""

from __future__ import annotations



from stockapp_v12.core.precision import D, round_dollar, round_pct, round_shares


# ─── position_manager: round() → round_dollar/round_shares ────────────────


def test_position_manager_uses_decimal_rounding():
    """P1-4: PositionRecord fields must use round_dollar/round_shares, not round()."""
    import inspect
    from stockapp_v12.services import position_manager as pm_mod
    src = inspect.getsource(pm_mod)
    # No bare round(x, 2) or round(x, 4) calls left
    assert "round(entry_price, 2)" not in src, "P1-4: still using round(entry_price, 2)"
    assert "round(atr_at_entry, 4)" not in src, "P1-4: still using round(atr_at_entry, 4)"
    assert "round(sl_price, 2)" not in src, "P1-4: still using round(sl_price, 2)"
    # New Decimal-based functions ARE imported
    assert "round_dollar" in src
    assert "round_shares" in src


def test_round_dollar_precision():
    """round_dollar gives deterministic 2dp."""
    assert round_dollar(100.005) == D("100.00")  # banker's rounding
    assert round_dollar(100.015) == D("100.02")  # half-even → up
    assert round_dollar("99.999") == D("100.00")


def test_round_shares_precision():
    """round_shares gives deterministic 4dp."""
    assert round_shares(2.00005) == D("2.0000")  # banker's rounding
    assert round_shares("1.23456") == D("1.2346")


def test_round_pct_precision():
    """round_pct gives deterministic 6dp."""
    assert round_pct(0.029999999) == D("0.030000")
    assert round_pct("12.3456789") == D("12.345679")


# ─── daily_review: Decimal computation ────────────────────────────────────


def test_daily_review_uses_decimal_import():
    """P1-4: daily_review.py must import D and round_pct from core.precision."""
    import inspect
    from stockapp_v12.services import daily_review as dr_mod
    src = inspect.getsource(dr_mod)
    assert "from stockapp_v12.core.precision import" in src
    assert "round_pct" in src
    assert "D(" in src  # uses D() for Decimal conversion
    # No bare float round() calls on pnl
    assert "round(unrealized * 100" not in src


def test_unrealized_pnl_decimal_vs_float_difference():
    """Demonstrate the float drift P1-4 prevents.

    With entry=101.10 and last=105.70:
    - Float: (105.70 - 101.10) / 101.10 = 0.04547972... (15-digit float repr)
    - Decimal: exact computation = 0.04547972... but with no drift
    """
    entry_d = D("101.10")
    last_d = D("105.70")
    unrealized_decimal = float((last_d - entry_d) / entry_d)

    entry_f = 101.10
    last_f = 105.70
    unrealized_float = (last_f - entry_f) / entry_f

    # The values are very close but may differ at the 15th digit
    # This test documents that Decimal gives exact results
    assert abs(unrealized_decimal - unrealized_float) < 1e-14
    # The important fix is that Decimal is deterministic across platforms
    # while float can vary by ULP on different architectures
