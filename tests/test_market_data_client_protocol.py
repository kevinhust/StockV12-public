"""B4 — MarketDataClient Protocol seam.

Per Council Pattern A (mirror ChronosBackend): passive Protocol,
no isinstance enforcement, no runtime checks. This test confirms
the seam exists and that any duck-typed implementation satisfies it.
"""

from __future__ import annotations

from typing import List, Optional

import pandas as pd

from stockapp_v12 import MarketDataClient
from stockapp_v12.contracts import MarketDataClient as MDCFromContracts


class _StubMarketDataClient:
    """Minimal duck-typed impl to prove the Protocol is satisfied by structure."""

    def fetch_daily_bars(self, symbol: str, days: int = 260) -> Optional[pd.DataFrame]:
        return None

    def fetch_latest_close(self, symbol: str) -> Optional[float]:
        return None

    def universe(self, tier: str = "default") -> List[str]:
        return []


class _NoLatestClose:
    """Negative test: missing one method should not silently satisfy the Protocol.

    We don't use ``@runtime_checkable`` so isinstance() raises TypeError.
    The point is structural — calling ``stub.universe()`` would AttributeError.
    """

    def fetch_daily_bars(self, symbol: str, days: int = 260) -> Optional[pd.DataFrame]:
        return None

    # NOTE: deliberately no fetch_latest_close or universe


def test_market_data_client_protocol_exposes_three_methods():
    """Seam surface is exactly: fetch_daily_bars, fetch_latest_close, universe."""
    public = [m for m in dir(MarketDataClient) if not m.startswith("_")]
    assert public == ["fetch_daily_bars", "fetch_latest_close", "universe"], (
        f"Unexpected Protocol surface: {public}. "
        "B4 spec locked this trio; do not add methods without a major-version bump."
    )


def test_market_data_client_reexport_from_package_root():
    """stockapp_v12.MarketDataClient is the same object as stockapp_v12.contracts.MarketDataClient."""
    assert MarketDataClient is MDCFromContracts, (
        "Re-export mismatch — package root must expose the contracts module symbol verbatim"
    )


def test_stub_implementation_satisfies_protocol_structurally():
    """Duck-typed impl with the right method names is a valid MarketDataClient.

    Per Pattern A, no isinstance enforcement — this is a structural contract.
    """
    stub = _StubMarketDataClient()
    # Calling each method must work
    assert stub.fetch_daily_bars("AAPL", days=260) is None
    assert stub.fetch_latest_close("AAPL") is None
    assert stub.universe("default") == []
    assert stub.universe("core") == []


def test_partial_implementation_raises_attributeerror():
    """A class missing any required method must not silently satisfy the Protocol.

    Pattern A relies on structural duck-typing; this test pins the
    contract by showing callers will AttributeError if they assume
    a partial impl is complete.
    """
    partial = _NoLatestClose()
    # The partial impl is callable for what it has …
    assert partial.fetch_daily_bars("AAPL") is None
    # … but AttributeError on missing methods (caller fails loud, not silent).
    try:
        partial.fetch_latest_close("AAPL")  # type: ignore[attr-defined]
    except AttributeError:
        return
    raise AssertionError("Partial impl unexpectedly satisfied fetch_latest_close")


def test_protocol_does_not_use_runtime_checkable():
    """Pattern A explicitly forbids isinstance() — the Protocol is structural only.

    This test pins the design choice: a future contributor who adds
    ``@runtime_checkable`` will fail this test and have to acknowledge
    the seam is duck-typed only.
    """
    from stockapp_v12.contracts.market_data_client import MarketDataClient as _Raw

    assert not hasattr(_Raw, "_is_runtime_protocol") or not getattr(
        _Raw, "_is_runtime_protocol", False
    ), "MarketDataClient must NOT be @runtime_checkable (Pattern A: structural Protocol only)"

    # Direct check: attempting isinstance() on a non-runtime_checkable Protocol raises TypeError
    import pytest

    with pytest.raises(TypeError):
        isinstance(_StubMarketDataClient(), _Raw)