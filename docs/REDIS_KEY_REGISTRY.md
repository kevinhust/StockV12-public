# Redis Key Registry (V12)

> **Status:** Phase 3 (2026-07-26) — SSOT for state keys used by store ports.  
> **Invariant:** Key names and payload shapes must stay byte-compatible unless dual-write is specified.

| Key pattern | Type | Owner module | Store port |
|-------------|------|--------------|------------|
| `position:{mode}:{SYMBOL}` | STRING JSON | PositionManager | `PositionStore` |
| `position:{mode}:index` | SET | PositionManager / EntryGuard (INV-2) | `PositionStore` / `EntryEvalStore` |
| `risk:cooldown:{SYMBOL}` | STRING + TTL | PositionManager | `PositionStore.close_atomic` |
| `journal:{YYYY-MM-DD}:{SYMBOL}` | LIST JSON | TradeJournal | `JournalStore` |
| `entry:evaluated:{mode}:{YYYYMMDD}` | SET + TTL | EntryEvaluationGuard | `EntryEvalStore` |
| `watchlist:tier1` | ZSET | SwingScanner | `WatchlistStore` |
| `watchlist:tier1:details` | HASH | SwingScanner | `WatchlistStore` |
| `watchlist:tier2` | ZSET | Catalyst / entry window | `WatchlistStore.list_members` |
| `market_regime:current` | STRING | regime job | (not yet ported) |

Detail schemas: [POSITION_LIFECYCLE.md](POSITION_LIFECYCLE.md), [TRADING_PIPELINE.md](TRADING_PIPELINE.md).

## Adapters

| Port | Redis | In-memory |
|------|-------|-----------|
| PositionStore | `adapters.stores.RedisPositionStore` | `InMemoryPositionStore` |
| JournalStore | `RedisJournalStore` | `InMemoryJournalStore` |
| EntryEvalStore | `RedisEntryEvalStore` | `InMemoryEntryEvalStore` |
| WatchlistStore | `RedisWatchlistStore` | `InMemoryWatchlistStore` |

Composition: `build_runtime(use_memory_stores=True)` or `build_runtime(redis_client=r)`.
