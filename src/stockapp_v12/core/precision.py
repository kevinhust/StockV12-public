"""
Decimal precision helpers for V12 risk math (Phase A5).

Why this exists
---------------
Float arithmetic introduces two failure modes that bite financial code:

1. **Representational drift.** `0.01` is not representable in IEEE-754.
   Every multiplication by `risk_per_trade_pct` adds a tiny constant
   error (~2e-20), which compounds across multi-step calculations.

2. **Boundary fuzziness.** When a computed `size_pct` lands near the
   R-02 single-position cap (e.g. 0.02999999...), float rounding can flip
   it above the cap, causing non-deterministic accept/reject behavior
   across runs / platforms.

We use `decimal.Decimal` for all internal risk math and quantize to a
known precision at the API boundary. Public dataclasses still expose
`float` (downstream code in `entry_orchestrator.py` and `lifecycle_scheduler.py`
already casts via `float(...)`), so this is a non-breaking change.

Rounding policy
---------------
`ROUND_HALF_EVEN` (banker's rounding) is the IEEE-754 default and the
default for `decimal.Decimal`; we use it explicitly to be unambiguous.
This avoids the systemic upward bias of `ROUND_HALF_UP` in repeated
multiplications.

Precision constants
-------------------
* `PCT_Q` (6 dp) — for percentage fields (size_pct, exposures, R-limits)
* `DOLLAR_Q` (2 dp) — for cash/dollar amounts (qty × price, NAV cash)
* `SHARE_Q` (4 dp) — for fractional share counts before int truncation

Why 6 dp for percent: a 6-decimal-place size_pct of 0.030000 is below the
0.03 cap. Going finer (8 dp) wastes arithmetic and doesn't add information
since downstream code truncates to `float` anyway.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN
from typing import Union

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PCT_Q = Decimal("0.000001")        # 6 decimal places for percentages
DOLLAR_Q = Decimal("0.01")          # 2 decimal places for dollars
SHARE_Q = Decimal("0.0001")         # 4 decimal places for fractional shares
EPSILON_Q = Decimal("0.0000001")    # 7 dp — comparison guard

ROUNDING = ROUND_HALF_EVEN

Number = Union[int, float, str, Decimal]


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------

def D(value: Number) -> Decimal:
    """Coerce input to Decimal safely.

    `str` is preferred for literal numerics (`D("0.01")`) so we never
    silently inherit a float's binary representation. `int` and existing
    `Decimal` pass through unchanged. `float` is allowed but converted
    through `str(value)` to keep all 17 significant digits of IEEE-754.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, str):
        return Decimal(value)
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Round-trip through str to preserve all 17 sig digits
        return Decimal(str(value))
    raise TypeError(f"cannot coerce {type(value).__name__} to Decimal")


# ---------------------------------------------------------------------------
# Quantization
# ---------------------------------------------------------------------------

def round_pct(d: Number) -> Decimal:
    """Quantize a percentage to 6 dp."""
    return D(d).quantize(PCT_Q, rounding=ROUNDING)


def round_dollar(d: Number) -> Decimal:
    """Quantize a dollar amount to 2 dp."""
    return D(d).quantize(DOLLAR_Q, rounding=ROUNDING)


def round_shares(d: Number) -> Decimal:
    """Quantize a fractional share count to 4 dp (before int-truncation)."""
    return D(d).quantize(SHARE_Q, rounding=ROUNDING)


# ---------------------------------------------------------------------------
# Boundary helpers
# ---------------------------------------------------------------------------

def exceeds_cap(value: Number, cap: Number) -> bool:
    """Strict-greater-than cap check with epsilon guard.

    Use this instead of `Decimal(value) > Decimal(cap)` for the R-02 single-
    position cap. The epsilon prevents a value like `0.030000000001` from
    triggering the cap when the "true" value is exactly 0.03.
    """
    return D(value) - D(cap) > EPSILON_Q


def safe_div(numerator: Number, denominator: Number) -> Decimal:
    """Division that raises ValueError (not ZeroDivisionError) on zero denom.

    `calculate_risk_based_size_pct` already validates positivity upstream,
    but a defensive guard here makes the migration's invariants explicit.
    """
    d = D(denominator)
    if d == 0:
        raise ValueError("division by zero in safe_div")
    return D(numerator) / d


# ---------------------------------------------------------------------------
# Public float-cast helpers (API boundary)
# ---------------------------------------------------------------------------

def to_float(d: Number) -> float:
    """Cast Decimal back to float at the API boundary.

    Use this whenever a public dataclass field needs to preserve its
    `float` type signature for downstream consumers. The Decimal value
    has already been quantized, so this is a safe downcast.
    """
    return float(D(d))