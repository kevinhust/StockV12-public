# V12 Scheduler

> **Scope**: When jobs run, dependencies, timeouts, failure behavior, half-day rules.  
> **Owner**: Scheduler / `lifecycle_scheduler` (V12 rewrite)  
> **Config authority**: `config/scheduler.yaml`

**Not in scope**: Trading logic ([TRADING_PIPELINE.md](TRADING_PIPELINE.md)), risk rules ([RISK_MANAGEMENT.md](RISK_MANAGEMENT.md)).

---

## Design Rules

1. **Trading days only** — NYSE calendar (`XNYS`); no jobs on holidays.
2. **Fail closed** — On job failure, follow table below; never “trade anyway.”
3. **Idempotent jobs** — Re-run same day overwrites same Redis keys with same TTL policy.
4. **No hidden polling** — V12 has no pattern-detection loop; only listed jobs + trail check.

---

## Daily Timeline (Regular Session)

All times **US/Eastern**.

| Time | Job ID | Module | Depends on |
|------|--------|--------|------------|
| 16:30 | `swing_scanner` | SwingScanner | Market closed |
| 08:00 | `tier2_passthrough` | **Not CatalystScorer** — copies tier1→tier2 (`L2_DEGRADED`) | `watchlist:tier1` |
| 09:35 | `entry_window` | EntryOrchestrator chain | tier2 / bars |
| 10:00–15:30 /5m | `entry_window` | same | — |
| 16:15 | `daily_review` | DailyReview | Market closed; positions loaded |

See [PRODUCTION_CAPABILITIES.md](PRODUCTION_CAPABILITIES.md) for honest status.


**Note:** The 08:30 `premarket_review` job (writer of `actionable:today` LIST) was
removed in hygiene-I (2026-07-09). The Redis key is no longer populated; live
entry path uses in-process `ctx.actionable_symbols` (set by callers of
`EntryOrchestrator`), not the Redis LIST. See
[architecture/00-ASSET_INVENTORY.md § Redis contract](architecture/00-ASSET_INVENTORY.md#redis-contract)
for the revised key ownership.

**Periodic** (market hours): ~~`trailing_stop_check`~~ **DISABLED** (2026-07-26).  
Protective SL remains on the broker/paper bracket. Do not re-enable cron until a price feed exists.


---

## Half-Day Sessions

When NYSE early close (13:00 ET):

| Job | Regular | Half-day |
|-----|---------|----------|
| Entry window close | 15:30 | **12:30** |
| DailyReview | 16:15 | **13:15** |
| SwingScanner | 16:30 | **13:30** |

Detection: `exchange_calendars` session end < 16:00.

---

## Job Specifications

### `swing_scanner`

| Field | Value |
|-------|-------|
| Timeout | 300s |
| Retries | 1 |
| Writes | `watchlist:tier1`, `watchlist:tier1:details` |

### `tier2_passthrough` (script: `run_catalyst_scorer.sh`)

| Field | Value |
|-------|-------|
| Capability | **PRODUCTION_PASSTHROUGH** — not catalyst scoring |
| Timeout | 120s |
| Reads | `watchlist:tier1` |
| Writes | `watchlist:tier2` (score=1.0 all members) |
| Marker | logs `L2_DEGRADED reason=catalyst_filter_unimplemented` |


### `daily_review`

| Field | Value |
|-------|-------|
| Timeout | 120s |
| Retries | 0 |
| Writes | `journal:*`, optional `pending_action` |

### `trailing_stop_check`

| Field | Value |
|-------|-------|
| Status | **DISABLED** (2026-07-26) — not installed by cron |
| Enabled | `scheduler.periodic.trailing_stop_check.enabled` → **false** in ops |
| Interval | 5 min (when re-enabled) |
| Market hours only | true |
| See | [PRODUCTION_CAPABILITIES.md](PRODUCTION_CAPABILITIES.md) |

---

## Failure Handling

| Job | On failure | Redis writes | Must NOT write | Log code |
|-----|------------|--------------|----------------|----------|
| `swing_scanner` | Prior tier1 if age < 48h; else delete/empty tier1 | tier1 (stale or empty) | actionable | `SCAN_DEGRADED` |
| `catalyst_scorer` | tier2 := copy of tier1 scores | tier2 + details (`L2_DEGRADED=true`) | — | `L2_DEGRADED` |
| `daily_review` | Implicit HOLD all | **No** `pending_action` | position state | `REVIEW_SKIPPED` |
| `trailing_stop_check` | Skip this tick | — | — | `TRAIL_TICK_SKIP` |
| Redis down | Abort all jobs | — | any order | `REDIS_DOWN` |
| Broker down | No new brackets | — | parent/SL orders | `BROKER_DOWN` |

**Broker note**: Existing SL/bracket orders remain at IB; trail tick failure does not cancel them.

Stage-level contract: [TRADING_PIPELINE.md](TRADING_PIPELINE.md#stage-failure-contract).  
Policy: [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md#6-failure-degradation-policy).

---

## Config Skeleton (`config/scheduler.yaml`)

```yaml
scheduler:
  timezone: US/Eastern
  trading_days_only: true

  jobs:
    swing_scanner:       { time: "16:30", timeout_seconds: 300, retry_count: 1, enabled: true }
    # Name is legacy; runtime is tier1→tier2 passthrough (L2_DEGRADED)
    catalyst_scorer:     { time: "08:00", timeout_seconds: 600, retry_count: 1, enabled: true }
    daily_review:        { time: "16:15", timeout_seconds: 120, retry_count: 0, enabled: true }

  periodic:
    # DISABLED until price feed + trail tick are wired (not in install_pipeline_cron)
    trailing_stop_check: { interval_minutes: 5, market_hours_only: true, enabled: false }

  entry_window:
    open: "09:35"
    close: "15:30"
    half_day_close: "12:30"
```

**Parameter authority**: times consumed by Scheduler (fire) + Gatekeeper R-09 (enforce).

---

## V11 Jobs Removed

Do not schedule in V12:

| V11 job | Replacement |
|---------|-------------|
| Pattern detection 5–10 min poll | — (removed) |
| L3 forced patrol ×5 | — (removed) |
| EOD 15:45 / 15:58 liquidation | `daily_review` + PANIC rule |
| Regime eval every 10 min | Daily or on-demand regime update |
| Portfolio sync 5s | Optional 60s or on-fill only |

---

## Acceptance (Scheduler Module)

| ID | Criterion | Test |
|----|-----------|------|
| SCH-1 | No job fires on holiday | `test_no_jobs_on_holiday` |
| SCH-2 | (reserved — see SCH-3) | — |
| SCH-3 | Half-day closes entry at 12:30 | `test_half_day_window` |
| SCH-4 | Trail job disabled when `enabled: false` | `test_trail_toggle` |
