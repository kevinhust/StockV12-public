# Safety Foundation — LiveOrderGuard / KillSwitch / AuditLedger

> Phase A1 (council verdict 2026-07-07).  
> **Policy refresh 2026-07-28 (ABC / code review):** paper-path defaults are
> **fail-closed** and **paper-mode whitelist**. See “Current defaults” below.
>
> Purpose: a single, composable safety layer that enforces every "never let
> this happen" rule in one place, before any money moves.

## Why this exists

Before A1, risk enforcement was split across:

- `bridge/gatekeeper.py` — R-01..R-10 (per-trade sized rules)
- `core/sizing.py` — R-02 max single position pct (capped inside size calc)
- `cron` shell scripts — silence-on-failure

This created three classes of gap:

1. **No system-wide halt.** A silent drift in the model, a runaway loop, an
   IBKR outage — none could stop *every* cron job at once.
2. **No symbol blacklist.** If we wanted to forbid new entries on `GME` for
   a week, the only option was `git commit` + redeploy.
3. **No durable audit trail on disk.** `TradeJournal` wrote to Redis (volatile,
   evictable under memory pressure). A server crash before the daily flush
   could erase the only record of *why* a trade happened.

A1 closes all three. The components are independent and side-effect-free so
each can be unit-tested in isolation, and so they can be retired or replaced
without rewiring the orchestrator.

## Components

### KillSwitch — `stockapp_v12.bridge.killswitch`

- Backed by Redis key `system:halt` (presence = halted).
- `halt(reason, halted_by, ttl_seconds=None)` engages the halt.
- `resume(resumed_by)` clears it (idempotent).
- `status()` returns a `HaltState` dataclass; `is_halted()` for hot-path checks.
- Two failure modes (constructor flag `fail_closed_on_error`):
  - `True` — Redis outage => treat as **halted** (prefer no-trade).
  - `False` — Redis outage => treat as **not halted** (A1 historical live
    preference: don't freeze on a Redis blip).

#### Current defaults (2026-07-28 ABC)

| Surface | Default | Rationale |
|---------|---------|-----------|
| `KillSwitch(...)` alone | still `fail_closed_on_error=False` | backward-compat for ad-hoc scripts |
| `LiveOrderGuard` / `make_live_order_guard` | **`fail_closed_on_error=True`** | paper NUC + capital protection; Redis down ⇒ no new entries |
| Cron preflight (`scripts/_preflight.sh`) | Redis PING fail ⇒ skip job | fail-closed at shell layer |

**Live trading (future):** if operators ever need A1-style fail-open on the
hot path, pass `fail_closed_on_halt_check=False` into
`make_live_order_guard` **explicitly** — never leave it as an accident of
defaults. Live money remains out of scope until paper soak completes
(see [PRODUCTION_CAPABILITIES.md](PRODUCTION_CAPABILITIES.md)).

CLI helper: **repo-root** `scripts/_preflight.sh` (Phase A2; not `v12/scripts/`).
Source it at the top of any cron job. On failure it **returns** non-zero when
sourced (never `exit` the parent). Callers typically map skip → exit 0 and
write `logs/pipeline/skip.log`.

### LiveOrderGuard — `stockapp_v12.bridge.live_guard`

Single `check(LiveGuardContext) -> LiveGuardResult` entry point. Composes
every pre-trade rule into one ordered evaluation chain:

| #  | Rule              | Reason if denied           | Source            |
|----|-------------------|----------------------------|-------------------|
| 1  | KillSwitch        | `KILL_SWITCH`              | A1                |
| 2  | Mode whitelist    | `MODE_BLOCK`               | A1 + ABC defaults |
| 3  | Symbol blacklist  | `BLACKLIST` / `BLACKLIST_CHECK_FAILED` | A1; **fail-closed** on Redis error (ABC) |
| 4  | Daily trade cap   | `R-05_DAILY_CAP`           | bridge/gatekeeper |
| 5  | Symbol cooldown   | `R-06_COOLDOWN`            | bridge/gatekeeper |
| 6  | Confidence floor  | `R-09_CONF_FLOOR`          | A1 (mirror)       |
| 7  | Single pos cap    | `R-03_SINGLE_CAP`          | bridge/gatekeeper |
| 8  | Portfolio exposure| `R-04_EXPOSURE_CAP`        | bridge/gatekeeper |
| 9  | Min-hold (SELL)   | `R-10_MIN_HOLD`            | A1                |

First veto wins. The order matters: `KILL_SWITCH` precedes `R-05` so a halt
short-circuits any other expensive Redis call.

#### Mode whitelist (ABC)

- Default allowed modes: **`paper_ibkr`**, **`paper`** (`DEFAULT_ALLOWED_MODES`).
- Empty whitelist is **not** “allow all” — it falls back to paper defaults.
- Live modes (`live*`) must be listed **explicitly** if ever enabled.

#### When `live_guard is None`

| Mode | Behavior |
|------|----------|
| paper / backtest | No-op (unit tests / research still work) |
| `live*` | **DENY** `LIVE_GUARD_REQUIRED` — never skip safety on live |

Production paper cron **always injects** a guard via
`make_live_order_guard` / `app.wiring.build_paper_entry_orchestrator`.

Helper: `make_live_order_guard(redis_client, allowed_modes=..., fail_closed_on_halt_check=True)`
loads V12 risk limits via `load_v12_risk_limits()`.

### AuditLedger — `stockapp_v12.audit.audit_ledger`

Append-only NDJSON mirror of `TradeJournal`. One file per trading date at
`v12/data/audit/YYYY-MM-DD.jsonl`. Each line is one complete decision
record (schema_version=1).

Properties:

- **Atomic per record**: `write + flush` of one line per call. A crash
  never corrupts prior records.
- **Best-effort**: failures are logged but never block the trading path.
  TradeJournal is the source of truth; this is a durable mirror.
- **Queryability**: `tail -f v12/data/audit/$(date +%F).jsonl | jq .` gives
  the operator a live stream of every entry attempt.

Two ways to wire it:

1. Manual: `ledger.record(symbol=..., stage=..., decision=..., reason_code=...)`
   for new code paths.
2. Proxy: `wrapped = audit_wrapped_journal(journal, ledger)` — preserves the
   existing TradeJournal call sites and silently mirrors every write.

## Configuration & operations

```bash
# Halt (anywhere with redis-cli access)
redis-cli -p 6380 SET system:halt 1
redis-cli -p 6380 SET system:halt:reason "model drift detected"
redis-cli -p 6380 SET system:halt:by "app@v12-scheduler"

# Resume
redis-cli -p 6380 DEL system:halt system:halt:reason system:halt:at system:halt:by

# Blacklist a symbol for a session
redis-cli -p 6380 SADD system:blacklist GME

# Read today's audit trail
tail -f v12/data/audit/$(date -Iseconds | cut -c1-10).jsonl | jq .
```

## Tests

```bash
# Phase A1 unit tests
PYTHONPATH=src:v12/src python3 -m pytest v12/tests/test_killswitch.py -v
PYTHONPATH=src:v12/src python3 -m pytest v12/tests/test_live_guard.py -v
PYTHONPATH=src:v12/src python3 -m pytest v12/tests/test_audit_ledger.py -v
```

37 tests, all green at the time of writing.

## Future work (out of scope for A1)

- A2 (cron preflight) will add Redis connection + schema-version checks
  to the same shell helper used here.
- A4 will read this audit ledger to compute per-symbol veto rates and
  trigger an alarm when vetoes exceed `n` over `m` cycles.
