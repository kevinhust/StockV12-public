"""Notifier Protocol — per-component structural tests (Phase 7, commit 4).

Locks the seam between V12 (consumer: EntryOrchestrator / DailyReview /
PositionManager) and any delivery backend (provider: today the in-tree
`_Notifier`, post-launch B5 a Redis pubsub impl). Mirrors § 6.3 acceptance.

Tests verify:
- @runtime_checkable Protocol works (isinstance True for compliant impl).
- Identity across all 4 import paths.
- All 8 methods dispatch without raising.
- A non-compliant impl is rejected by isinstance.
"""
from __future__ import annotations

from typing import List, Optional


from stockapp_v12.contracts import Notifier
from stockapp_v12.contracts.notifier import Notifier as DirectNotifier
from stockapp_v12.services.notification_service import (
    Notifier as ShimNotifier,
    notifier as default_notifier,
)
from stockapp_v12 import Notifier as RootNotifier


# ---- 1. Identity -----------------------------------------------------------


def test_contracts_notifier_identity():
    """All four import paths must return the same Protocol class."""
    assert Notifier is DirectNotifier
    assert Notifier is ShimNotifier
    assert Notifier is RootNotifier


# ---- 2. isinstance + runtime_checkable ------------------------------------


def test_default_notifier_satisfies_protocol():
    """The in-tree _Notifier concrete impl must isinstance Notifier."""
    assert isinstance(default_notifier, Notifier)


def test_minimal_compliant_impl_satisfies_protocol():
    """Any class with the 8 notify methods must isinstance Notifier."""

    class _CompliantNotifier:
        def notify_stock_pool_updated(
            self, tier: str = "", symbols: Optional[List[str]] = None, source: str = "",
        ) -> None:
            pass

        def notify_order_intent(self, **kwargs) -> None:
            pass

        def notify_order_submitted(self, **kwargs) -> None:
            pass

        def notify_order_filled(self, **kwargs) -> None:
            pass

        def notify_order_rejected(self, **kwargs) -> None:
            pass

        def notify_daily_review(self, symbol: str = "", action: str = "", **kwargs) -> None:
            pass

        def notify_position_state_change(
            self, symbol: str = "", old: str = "", new: str = "", **kwargs,
        ) -> None:
            pass

        def notify(self, *args, **kwargs) -> None:
            pass

    assert isinstance(_CompliantNotifier(), Notifier)


def test_non_compliant_impl_fails_isinstance():
    """A class missing methods must NOT isinstance Notifier."""

    class _NonCompliant:
        def notify(self, *args, **kwargs) -> None:
            pass
        # Missing: notify_stock_pool_updated, notify_order_intent, etc.

    assert not isinstance(_NonCompliant(), Notifier)


# ---- 3. Method surface (8 methods) ---------------------------------------


EXPECTED_METHODS = {
    "notify_stock_pool_updated",
    "notify_order_intent",
    "notify_order_submitted",
    "notify_order_filled",
    "notify_order_rejected",
    "notify_daily_review",
    "notify_position_state_change",
    "notify",
}


def test_default_notifier_has_all_8_methods():
    """Default _Notifier must expose all 8 Protocol methods."""
    actual = {m for m in dir(default_notifier) if m.startswith("notify_") or m == "notify"}
    assert actual == EXPECTED_METHODS


def test_compliant_impl_has_all_8_methods():
    """Compliant impl must expose all 8 methods (duck-typed)."""

    class _C:
        def notify_stock_pool_updated(self, tier="", symbols=None, source=""): pass
        def notify_order_intent(self, **kwargs): pass
        def notify_order_submitted(self, **kwargs): pass
        def notify_order_filled(self, **kwargs): pass
        def notify_order_rejected(self, **kwargs): pass
        def notify_daily_review(self, symbol="", action="", **kwargs): pass
        def notify_position_state_change(self, symbol="", old="", new="", **kwargs): pass
        def notify(self, *args, **kwargs): pass

    actual = {m for m in dir(_C()) if m.startswith("notify_") or m == "notify"}
    assert actual == EXPECTED_METHODS


# ---- 4. Dispatch -----------------------------------------------------------


def test_default_notifier_methods_dispatch_without_raising(caplog):
    """All 8 methods must be callable on the default notifier without raising."""
    import logging
    caplog.set_level(logging.INFO)

    default_notifier.notify_stock_pool_updated(tier="A", symbols=["AAPL"], source="scanner")
    default_notifier.notify_order_intent(symbol="AAPL", side="long")
    default_notifier.notify_order_submitted(symbol="AAPL", order_id="o1")
    default_notifier.notify_order_filled(symbol="AAPL", qty=10)
    default_notifier.notify_order_rejected(symbol="AAPL", reason="margin")
    default_notifier.notify_daily_review(symbol="AAPL", action="HOLD")
    default_notifier.notify_position_state_change(symbol="AAPL", old="OPEN", new="TRAILING")
    default_notifier.notify("ad-hoc message")

    # Verify the logger captured the calls (V12-NOTIFY prefix in log output).
    notify_logs = [r.message for r in caplog.records if "[V12-NOTIFY]" in r.message]
    assert len(notify_logs) >= 7  # notify() is a no-op pass, so 7 logged + 1 silent


# ---- 5. Substitutability --------------------------------------------------


def test_two_compliant_impls_are_substitutable():
    """Two different compliant impls must both be Notifier-protocol."""
    a = default_notifier

    class _B:
        def notify_stock_pool_updated(self, tier="", symbols=None, source=""): pass
        def notify_order_intent(self, **kwargs): pass
        def notify_order_submitted(self, **kwargs): pass
        def notify_order_filled(self, **kwargs): pass
        def notify_order_rejected(self, **kwargs): pass
        def notify_daily_review(self, symbol="", action="", **kwargs): pass
        def notify_position_state_change(self, symbol="", old="", new="", **kwargs): pass
        def notify(self, *args, **kwargs): pass

    b = _B()
    assert isinstance(a, Notifier)
    assert isinstance(b, Notifier)