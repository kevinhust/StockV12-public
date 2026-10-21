"""Notifier — passive Protocol for V12 operational alerts.

Mirrors the existing ``_Notifier`` public API in
``services/notification_service``. Today every method is a
``logger.info``/``logger.warning`` pass-through; the Protocol locks the
surface so post-launch B5 (per Council § 3.4 verdict) can ship a Redis
pubsub impl without touching callers.

Invariant: notifier is a *delivery* surface for low-priority operational
summaries. PANIC/EXIT decisions are NOT delivered through this channel —
they are actioned by ``PositionManager.close_for_review`` /
``trim_position`` and durable via ``TradeJournal`` (Redis LIST
``journal:{date}:{sym}``). Do not route auditable state through notifier.

Stability guarantee (Design § 3.2): each method signature is frozen for
v12.x. New methods may be added (default impl ``pass``); **renaming or
removing a method is a major bump and requires Council.**
"""

from __future__ import annotations

from typing import List, Optional, Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    """Delivery surface for operational alerts. No-op by default in tests.

    Implementations should be:
      - **Idempotent**: same call → same log line (or dedup'd if backed by pubsub).
      - **Non-blocking**: callers invoke from sync code paths; long network
        I/O must be offloaded (queue or fire-and-forget).
      - **Failure-isolated**: notifier errors must NEVER propagate up to
        trading logic — wrap in try/except internally and log on failure.
    """

    def notify_stock_pool_updated(
        self,
        tier: str = "",
        symbols: Optional[List[str]] = None,
        source: str = "",
    ) -> None:
        """Called after SwingScanner refreshes a watchlist tier."""
        ...

    def notify_order_intent(self, **kwargs) -> None:
        """Called when EntryOrchestrator decides to enter (pre-submit)."""
        ...

    def notify_order_submitted(self, **kwargs) -> None:
        """Called when BracketOrderService.submit_bracket_order returns."""
        ...

    def notify_order_filled(self, **kwargs) -> None:
        """Called when broker reports a fill (TBD post-launch B5 wiring)."""
        ...

    def notify_order_rejected(self, **kwargs) -> None:
        """Called when broker rejects an order (TBD post-launch B5 wiring)."""
        ...

    def notify_daily_review(
        self,
        symbol: str = "",
        action: str = "",
        **kwargs,
    ) -> None:
        """Called when DailyReview produces a HOLD/TRIM/EXIT/TIGHTEN/ALERT decision."""
        ...

    def notify_position_state_change(
        self,
        symbol: str = "",
        old: str = "",
        new: str = "",
        **kwargs,
    ) -> None:
        """Called when PositionManager transitions OPEN → TRAILING → CLOSED."""
        ...

    def notify(self, *args, **kwargs) -> None:
        """Generic fallback for ad-hoc operational messages. Use sparingly —
        prefer the typed methods above so future structured sinks can route
        by category.
        """


__all__ = ["Notifier"]