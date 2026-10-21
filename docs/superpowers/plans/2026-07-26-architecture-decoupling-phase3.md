# Architecture Decoupling — Phase 3 (executed)

**Goal:** Put Redis behind store ports so domain/application modules can run with InMemory stores (zero Redis) while production keeps byte-compatible keys.

**Status:** Implemented 2026-07-26 on `feature/arch-decouple-p3` (merged into PR #1 branch).

## Delivered

| Port | Redis adapter | In-memory | Consumers |
|------|---------------|-----------|-----------|
| `PositionStore` | `RedisPositionStore` | `InMemoryPositionStore` | `PositionManager` |
| `JournalStore` | `RedisJournalStore` | `InMemoryJournalStore` | `TradeJournal` |
| `EntryEvalStore` | `RedisEntryEvalStore` | `InMemoryEntryEvalStore` | `EntryEvaluationGuard` |
| `WatchlistStore` | `RedisWatchlistStore` | `InMemoryWatchlistStore` | `SwingScanner.publish`, `read_tier2_symbols` |

- BC: `redis_client=` constructors still work (wrap into Redis*Store)
- Preferred: `store=` / `watchlist_store=` inject
- Key registry: `docs/REDIS_KEY_REGISTRY.md`
- Wiring: `build_runtime(use_memory_stores=True)` or `redis_client=`
- Tests: `tests/test_phase3_stores.py` + lifecycle/reconcile/scanner green

## Out of scope (Phase 4+)

- Cage SwingScanner default Longbridge `ensure_v11`
- Drop archive from NUC PYTHONPATH
- Thin CLI / remove scripts path hacks
