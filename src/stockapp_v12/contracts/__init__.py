"""Stable Protocol seams for V12 external integrations.

This package contains **only Protocols** — no concrete classes, no runtime
logic, no I/O. It must import cleanly on a fresh Python install with zero
side-effects beyond ``typing.Protocol`` definitions.

Pre-launch scope (Council § 3.4 + Design § 1):
  - ``ChronosBackend`` (commit 1, 2026-07-02): forecast backend seam
    (formerly inline in ``services.forecast_service``).
  - ``Notifier`` (commit 2, 2026-07-02): operational-alert delivery surface
    (mirrors ``services.notification_service._Notifier`` public API).
  - ``Clock`` (arch-decouple phase 0/1): ET time source for injectable session
    windows / EOD / journal keys (SystemClock + FakeClock adapters).
  - ``BrokerAdapter`` (arch-decouple phase 0/1): re-export of core Protocol for
    hexagonal consumers (same object identity as
    ``core.execution.bracket_order.BrokerAdapter``).

Post-launch roadmap (deferred per Council verdict):
  - ``MarketDataClient``: SwingScanner data-source seam (B4)
  - in-tree concrete impls under ``v12/backends/`` and ``v12/notifiers/``
  - move ``BrokerAdapter`` body into contracts and reverse re-export (Phase 2)
"""

from stockapp_v12.contracts.broker import BrokerAdapter
from stockapp_v12.contracts.chronos_backend import ChronosBackend
from stockapp_v12.contracts.clock import Clock
from stockapp_v12.contracts.entry_eval_store import EntryEvalStore
from stockapp_v12.contracts.journal_store import JournalStore
from stockapp_v12.contracts.market_data_client import MarketDataClient
from stockapp_v12.contracts.notifier import Notifier
from stockapp_v12.contracts.position_store import PositionStore
from stockapp_v12.contracts.watchlist_store import WatchlistStore

__all__ = [
    "BrokerAdapter",
    "ChronosBackend",
    "Clock",
    "EntryEvalStore",
    "JournalStore",
    "MarketDataClient",
    "Notifier",
    "PositionStore",
    "WatchlistStore",
]
