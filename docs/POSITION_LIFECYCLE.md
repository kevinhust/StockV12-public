# V12 Position Lifecycle

> **Scope**: State machine, transitions, storage contract, hold/trim/exit behavior.  
> **Owner module**: `PositionManager`  
> **Not in scope**: Entry scoring ([TRADING_PIPELINE.md](TRADING_PIPELINE.md)), red lines ([RISK_MANAGEMENT.md](RISK_MANAGEMENT.md)).

---

## States

| State | Meaning | Who sets |
|-------|---------|----------|
| `CANDIDATE` | In tier2, not yet actionable | CatalystScorer / EntryOrchestrator pre-eval |
| `PENDING` | In `ctx.actionable_symbols`, awaiting entry fill | EntryOrchestrator |
| `OPEN` | Filled entry; SL active; trail not yet armed | PositionManager on fill |
| `TRAILING` | Trail armed and ratcheting | PositionManager |
| `CLOSED` | Flat; cooldown may apply | PositionManager on exit |

**Note**: `CANDIDATE` / `PENDING` may live only in watchlist keys; persisted position records start at `OPEN`.

---

## State Diagram

```
CANDIDATE ──(pre-eval)──► PENDING ──(entry fill)──► OPEN
                              │                        │
                         (rejected)              (+1 ATR profit)
                              │                        ▼
                              ▼                   TRAILING
                           (end)                     │
                                                    (trail/SL hit)
                                                        ▼
                                                     CLOSED
                              OPEN ──(SL hit)──────────────────► CLOSED
```

### Allowed transitions

| From | To | Trigger | Module |
|------|-----|---------|--------|
| PENDING | OPEN | Parent BUY filled | PositionManager |
| OPEN | TRAILING | `unrealized >= activation_atr × ATR` | PositionManager |
| OPEN | CLOSED | SL filled | PositionManager (broker callback) |
| TRAILING | CLOSED | Price ≤ trail_price or SL | PositionManager |
| OPEN/TRAILING | CLOSED | DailyReview EXIT + next-session fill | DailyReview + executor |
| OPEN/TRAILING | OPEN/TRAILING | DailyReview TRIM (partial) | DailyReview |

**Forbidden**: `CLOSED` → `OPEN` without new entry flow (must pass Gatekeeper again).

---

## Position Record (Redis Contract)

### Key: `position:{mode}:{symbol}`

JSON schema (version `lifecycle_schema: 1`):

```json
{
  "symbol": "NVDA",
  "mode": "paper_ibkr",
  "state": "TRAILING",
  "lifecycle_schema": 1,
  "entry_price": 135.20,
  "entry_at": "2026-06-02T10:15:00-04:00",
  "qty": 42,
  "atr_at_entry": 8.50,
  "sl_price": 122.45,
  "sl_order_id": "…",
  "parent_order_id": "…",
  "trail_price": 140.00,
  "highest_close_since_entry": 148.50,
  "days_held": 3,
  "pending_action": null,
  "catalyst_type_at_entry": "earnings",
  "regime_at_entry": "BULL_LONG"
}
```

### Index: `position:{mode}:index`

- Type: SET of symbols with `state ∈ {OPEN, TRAILING}`

### Field ownership (single writer per field)

| Field | Write owner | When written | Others |
|-------|-------------|--------------|--------|
| `symbol`, `mode`, `lifecycle_schema` | PositionManager | register | read-only |
| `state` | PositionManager | transitions | read-only |
| `entry_price`, `entry_at`, `qty` | PositionManager | on fill | read-only |
| `atr_at_entry` | PositionManager | on fill (from scan detail) | read-only |
| `parent_order_id` | BracketOrderService | submit | PositionManager copies |
| `sl_order_id`, `sl_price` | BracketOrderService | submit | PositionManager copies |
| `trail_price`, `highest_close_since_entry` | PositionManager | trail tick | read-only |
| `days_held` | DailyReview | 16:15 ET | read-only elsewhere |
| `pending_action` | DailyReview | review only | PositionManager reads next open |
| `catalyst_type_at_entry`, `regime_at_entry` | PositionManager | register (from analyst/Redis) | read-only |
| `thesis_tags`, `expected_holding_days` | PositionManager | register (from AnalystOutput) | read-only |
| `closed_at`, `exit_reason` | PositionManager | on CLOSED | read-only |
| `mfe_pct`, `mae_pct` | TradeJournal | on exit | PositionManager may read |

**TradeJournal**: append-only; never updates `position:*` JSON.

### Rules

| Rule | Enforcement |
|------|-------------|
| One record per (mode, symbol) | PositionManager.register |
| `state` must be enum above | PositionManager |
| `days_held` updated once per session at DailyReview | DailyReview |
| `pending_action ∈ {null, TRIM, EXIT, TIGHTEN}` | DailyReview only |

---

## Redis Key Registry

> **Rule**: Only the **Owner** module may CREATE/UPDATE/DELETE. Others: READ unless noted.

| Key pattern | Type | Owner | Written when | TTL | Recomputable? | On failure |
|-------------|------|-------|--------------|-----|---------------|------------|
| `watchlist:tier1` | ZSET | SwingScanner | 16:30 job success | 24h | Yes (rerun scan) | Use prior if <48h |
| `watchlist:tier1:details` | HASH | SwingScanner | 16:30 | 24h | Yes | Same |
| `watchlist:tier2` | ZSET | CatalystScorer | 08:00 | 24h | Yes | Degrade from tier1 |
| `watchlist:tier2:details` | HASH | CatalystScorer | 08:00 | 24h | Yes | Same |
| `actionable:today` | LIST | _(removed in hygiene-I)_ | — | — | — | Key no longer populated; live entry path uses in-process `ctx.actionable_symbols` |
| `entry:evaluated:{mode}:{date}` | SET | Entry orchestrator | First entry eval | 48h | No | — |
| `position:{mode}:{symbol}` | JSON string | PositionManager | OPEN→CLOSED | until CLOSED+7d | No | — |
| `position:{mode}:index` | SET | PositionManager | register / CLOSED | — | Derived from positions | — |
| `risk:daily_trades:{date}` | INT | RiskGatekeeper | After BUY fill | midnight ET | No | — |
| `risk:cooldown:{symbol}` | string | PositionManager | On CLOSED | 48h | No | — |
| `risk:daily_pnl:{date}` | FLOAT | Portfolio sync | Periodic | 48h | Yes from fills | — |
| `market_regime:current` | JSON | Regime service | Regime update | 24h | Yes | Stale → conservative |
| `journal:{date}:{symbol}` | JSON/list | TradeJournal | Each decision | 365d+ | No (audit) | Drop + alert |
| `portfolio:{mode}:snapshot` | JSON | Portfolio sync | On fill / periodic | 1h | Yes | Stale blocks BUY (R stale) |

**Recovery**: If `position:{mode}:{symbol}` disagrees with broker, broker is source of truth for qty; PositionManager reconciles state, journals `RECONCILE_*`.

---

## OPEN Phase

| Responsibility | Owner |
|----------------|-------|
| Attach SL child order id | BracketOrderService |
| Persist entry snapshot | PositionManager |
| Block duplicate entry same symbol | PositionManager + pipeline |
| Enforce min hold on discretionary SELL | PositionManager + Gatekeeper R-10 |

---

## TRAILING Phase

### Activation

```
IF state == OPEN AND unrealized_pnl >= activation_atr * atr_at_entry:
    state = TRAILING
    trail_price = highest_close_since_entry - trail_distance_atr * atr_at_entry
```

Parameters: [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md#stop-loss--trailing-risk-parameters).

### Update (every 5 min, market hours)

```
highest_close_since_entry = max(highest_close_since_entry, last_close)
new_trail = highest_close_since_entry - trail_distance * atr_at_entry
trail_price = max(trail_price, new_trail)   # ratchet up only

IF last_price <= trail_price:
    submit SELL (full qty)
    state = CLOSED
```

Scheduler: [SCHEDULER.md](SCHEDULER.md#periodic-trailing_stop_check).

---

## Exit Triggers (Priority)

| Priority | Trigger | Bypass min hold? |
|----------|---------|------------------|
| 1 | SL filled at broker | Yes |
| 2 | Trailing stop hit | Yes |
| 3 | Regime PANIC → DailyReview EXIT queue | Yes (next open) |
| 4 | DailyReview EXIT (loss + days rule) | No* |
| 5 | Manual SELL | No |

\*Unless SL-equivalent emergency path defined in ops runbook.

### On CLOSED

1. Set `state = CLOSED`, `closed_at`, `exit_reason`
2. Write TradeJournal exit record (MFE/MAE)
3. Set `risk:cooldown:{symbol}` TTL = `symbol_cooldown_hours`
4. Remove from `position:{mode}:index`

---

## DailyReview Decision Matrix

Run **16:15 ET**; owner: `DailyReview` job. Outputs `pending_action` only (execution next session unless SL path).

| Condition | Days held | Action |
|-----------|-----------|--------|
| Profit > 2×ATR, catalyst valid | any | HOLD |
| Profit > 2×ATR, catalyst gone | any | TRIM 50% |
| PnL between 0 and 2×ATR | < 5 | HOLD |
| PnL between 0 and 2×ATR | 5–15 | TIGHTEN trail |
| Loss < 1×ATR | < 3 | HOLD |
| Loss < 1×ATR | 3–5 | ALERT |
| Loss > 1×ATR | > 5 | EXIT |
| Regime PANIC | any | EXIT (queue) |
| Days > 15 | any | ALERT |

### TRIM semantics

- Sell 50% at next session open (or MKT if configured)
- Move SL to breakeven on remainder
- State stays `TRAILING` or `OPEN` with reduced qty

---

## TradeJournal Schema

Key: `journal:{date}:{symbol}` — **append-only** list of envelope records.

### Envelope (all stages)

See [TRADING_PIPELINE.md — Standard journal envelope](TRADING_PIPELINE.md#standard-journal-envelope).

### Exit record extensions (required on CLOSED)

| Field | Type |
|-------|------|
| `exit_reason` | enum: `sl`, `trail`, `review_exit`, `panic`, `manual` |
| `entry_date`, `exit_date` | ISO date |
| `pnl_pct` | float |
| `hold_days` | int |
| `mfe_pct`, `mae_pct` | float |
| `regime_at_entry`, `regime_at_exit` | string |

Writer: **TradeJournal only** (other modules call `TradeJournal.append()`).

---

## Acceptance (Lifecycle Module)

| ID | Criterion | Test |
|----|-----------|------|
| PL-1 | Invalid transition rejected | `test_invalid_transition` |
| PL-2 | Trail only ratchets up | `test_trail_ratchet` |
| PL-3 | SELL blocked day 0 without SL | `test_min_hold_block` |
| PL-4 | Cooldown key set on CLOSED | `test_cooldown_on_exit` |
| PL-5 | DailyReview writes `pending_action` | `test_review_output` |

Full list: [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md#acceptance-checklist).
