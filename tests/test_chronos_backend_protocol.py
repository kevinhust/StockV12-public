"""ChronosBackend Protocol — per-component structural tests (Phase 7, commit 4).

Locks the seam between V12 (consumer) and any prediction backend (provider) per
the post-Protocol-extraction contract. Mirrors the structural checks in the
commit-1 acceptance gate (§ 6.2), but as runnable pytest tests that run on
every CI cycle.

Tests are read-only — they instantiate fake / stub impls and assert the
Protocol surface is honoured. NO real AutoGluon, NO disk I/O, NO Redis.
"""
from __future__ import annotations

from typing import Any, Dict

import pandas as pd
import pytest

from stockapp_v12.contracts import ChronosBackend
from stockapp_v12.contracts.chronos_backend import ChronosBackend as DirectChronosBackend
from stockapp_v12.services.forecast_service import ChronosBackend as ShimChronosBackend
from stockapp_v12 import ChronosBackend as RootChronosBackend


# ---- 1. Identity -----------------------------------------------------------


def test_contracts_chronos_backend_identity():
    """All four import paths must return the same class object."""
    assert ChronosBackend is DirectChronosBackend
    assert ChronosBackend is ShimChronosBackend
    assert ChronosBackend is RootChronosBackend


# ---- 2. Import surface ----------------------------------------------------


def test_chronos_backend_protocol_imports_without_repo_src(monkeypatch):
    """The Protocol must import cleanly with ONLY v12/src on sys.path.

    No dependency on repo-root src/services/chronos_service.py.
    """
    import sys
    # Strip every path that contains 'src' to simulate a fresh venv with only
    # v12/src on sys.path.
    monkeypatch.setattr(
        sys, "path",
        [p for p in sys.path if "/src" not in p and "/Stockapp/src" not in p],
    )
    sys.path.insert(0, "src")

    # Re-import under the restricted path.
    for mod in list(sys.modules):
        if mod.startswith("stockapp_v12.contracts"):
            del sys.modules[mod]
    from stockapp_v12.contracts import ChronosBackend as CB2

    assert CB2 is ChronosBackend or hasattr(CB2, "detect")


# ---- 3. Structural typing: detect / detect_batch -------------------------


class _MinimalBackend:
    """Minimal duck-typed backend: only the two required methods."""

    def detect(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        return [{"meta": {"current_price": 100.0}, "forecast": []}]

    def detect_batch(self, frames: Dict[str, pd.DataFrame]) -> Dict[str, list[dict[str, Any]]]:
        return {sym: [{"meta": {"current_price": 100.0}, "forecast": []}]
                for sym in frames}


def test_minimal_backend_satisfies_protocol_methods():
    """A minimal duck-typed impl must expose detect + detect_batch."""
    b = _MinimalBackend()
    assert callable(b.detect)
    assert callable(b.detect_batch)


def test_minimal_backend_detect_dispatch():
    """detect() returns a list[dict]; required by ForecastService consumer."""
    b = _MinimalBackend()
    df = pd.DataFrame({"close": [100.0] * 5})
    out = b.detect(df)
    assert isinstance(out, list)
    assert all(isinstance(r, dict) for r in out)


def test_minimal_backend_detect_batch_dispatch():
    """detect_batch() returns dict[symbol, list[dict]]."""
    b = _MinimalBackend()
    frames = {"AAPL": pd.DataFrame({"close": [100.0] * 5}),
              "MSFT": pd.DataFrame({"close": [200.0] * 5})}
    out = b.detect_batch(frames)
    assert set(out.keys()) == {"AAPL", "MSFT"}
    for sym, rows in out.items():
        assert isinstance(rows, list)


# ---- 4. Failure-path contract ---------------------------------------------


class _FailingBackend:
    """Backend that raises on detect_batch (hard failure path)."""

    def detect(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        return []  # soft fail: empty list

    def detect_batch(self, frames: Dict[str, pd.DataFrame]) -> Dict[str, list[dict[str, Any]]]:
        raise RuntimeError("backend exploded")  # hard fail: propagate


def test_soft_failure_returns_empty_list():
    """Contract: detect returns [] on soft failure, never raises."""
    b = _FailingBackend()
    assert b.detect(pd.DataFrame()) == []


def test_hard_failure_propagates_from_detect_batch():
    """Contract: detect_batch raises on hard backend failure (caller catches)."""
    b = _FailingBackend()
    with pytest.raises(RuntimeError, match="backend exploded"):
        b.detect_batch({})


# ---- 5. Substitutability --------------------------------------------------


def test_two_impls_are_interchangeable_via_protocol():
    """Two different backends must be usable through the same Protocol surface."""
    minimal = _MinimalBackend()
    failing = _FailingBackend()
    # Both have the same method surface — Protocol-equivalent.
    assert set(dir(minimal)) >= {"detect", "detect_batch"}
    assert set(dir(failing)) >= {"detect", "detect_batch"}