# V12 Trading Pipeline

> **Scope**: Signal flow from scan → actionable list → entry → hold hooks.  
> **Not in scope**: Risk red lines (see [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md)), state machine (see [POSITION_LIFECYCLE.md](POSITION_LIFECYCLE.md)), cron (see [SCHEDULER.md](SCHEDULER.md)).

---

## System Invariants

See [SYSTEM_OVERVIEW.md §5.1](SYSTEM_OVERVIEW.md#51-system-invariants). Pipeline-specific:

- **INV-1**: One entry evaluation per `(mode, symbol, date)` — write `entry:evaluated:{mode}:{YYYYMMDD}` SADD on first eval.
- **INV-2**: Skip entry if symbol in `position:{mode}:index`.

---

## Pipeline Contract

Each stage has fixed **inputs**, **outputs**, and **downstream consumers**. Stages must not skip outputs or write keys owned by another module.

### Stage failure contract

| Stage | On failure | Redis / side effects | Log code |
|-------|------------|----------------------|----------|
| 1 SCAN | Use prior tier1 if age < 48h; else empty tier1 | Overwrite or skip | `SCAN_DEGRADED` |
| 2 CATALYST | tier2 := tier1 (unfiltered), flag degraded | Write tier2 without expulsion | `L2_DEGRADED` |
| 3 PRE-MARKET | `actionable:today` = `[]` | DEL + empty LIST | `PREMARKET_FAIL_CLOSED` |
| 4 ENTRY | No order; journal DENY/PASS | No new `position:*` | per decision |
| 5 HOLD (trail tick) | Skip tick; broker SL unchanged | No position mutation | `TRAIL_TICK_SKIP` |
| 6 REVIEW | All open → HOLD | No `pending_action` writes | `REVIEW_SKIPPED` |

Aligns with [SCHEDULER.md](SCHEDULER.md#failure-handling) and [SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md#6-failure-degradation-policy).

| Stage | Owner module | Schedule | Input | Output key(s) | Consumer |
|-------|--------------|----------|-------|---------------|----------|
| 1 SCAN | SwingScanner | 16:30 ET | Universe + D1 bars | `watchlist:tier1`, `watchlist:tier1:details` | CatalystScorer |
| 2 CATALYST | CatalystScorer | 08:00 ET | tier1 | `watchlist:tier2`, `watchlist:tier2:details` | EntryOrchestrator (caller reads tier2) |
| 3 PRE-EVAL | EntryOrchestrator caller | 09:35 ET | tier2 (read by caller) | `ctx.actionable_symbols: Set[str]` (in-process) | EntryOrchestrator stage 1..7 |
| 4 ENTRY | LLMAnalyst + Gatekeeper + BracketOrder | 09:35–15:30 ET | `ctx.actionable_symbols` | `position:{mode}:{symbol}` | PositionManager |
| 5 HOLD | PositionManager | Continuous (5m) | Open positions | Trail updates, broker orders | — |
| 6 REVIEW | DailyReview | 16:15 ET | Open positions | `journal:*`, queued actions | Next-session executor |

---

## Stage 1 — SCAN (SwingScanner)

### Purpose

Produce a ranked **candidate pool** from daily bars only. No news, no orders.

### Filters (hard, AND)

| Parameter | Value | Config path |
|-----------|-------|-------------|
| `price_min` / `price_max` | 10 / 500 | `swing_scanner.yaml` → `filters.price_range` |
| `min_avg_dollar_volume_20d` | 30_000_000 | `filters.min_avg_dollar_volume_20d` |
| `min_atr_percent_14d` | 0.030 | `filters.min_atr_percent_14d` |
| `min_relative_strength_20d` | 0.0 | `filters.min_relative_strength_20d` |
| `max_distance_from_52w_high` | 0.25 | `filters.max_distance_from_52w_high` |
| `require_above_ema20` | true | `filters.require_above_ema20` |

### Scoring (ranking only)

```
composite =
    relative_strength_20d * w_rs      # default 0.30
  + atr_percent_14d       * w_atr     # default 0.25
  + volume_breakout_score * w_vol     # default 0.25
  + pivot_proximity       * w_pivot   # default 0.20
```

Weights: `swing_scanner.yaml` → `scoring.weights.*`

### Output contract

| Key | Type | TTL | Max members |
|-----|------|-----|-------------|
| `watchlist:tier1` | ZSET symbol→score | 24h | 20 |
| `watchlist:tier1:details` | HASH symbol→JSON | 24h | same symbols |

**Detail JSON minimum fields**: `price`, `atr_percent`, `relative_strength`, `volume_breakout`, `pivot_proximity`, `composite_score`, `scanned_at`.

### Acceptance (stage)

- [ ] Top N respects `output_top_n` from config
- [ ] No symbol below hard filters appears in tier1
- [ ] Job completes within `scheduler.swing_scanner.timeout_seconds`

---

## Stage 2 — CATALYST (CatalystScorer)

### Purpose

Rank tier1 by **setup + catalyst**; expel weak names. Still no orders.

### Inputs

- `watchlist:tier1` (max 20)
- News window: **48 hours**
- `market_regime:current` (for multiplier)

### Scoring dimensions

| Dimension | Weight | Source |
|-----------|--------|--------|
| Technical (L1 composite) | 0.40 | tier1 score |
| Catalyst (LLM) | 0.35 | news + prompt |
| Regime alignment | 0.15 | `risk_regimes.yaml` → `l2_catalyst_multipliers` |
| Sector momentum | 0.10 | sector ETF vs SPY |

### Expulsion rule

Remove from tier2 if: `catalyst_score < 2.0` **AND** `technical_score < 0.5`.

### Output contract

| Key | Type | TTL | Max members |
|-----|------|-----|-------------|
| `watchlist:tier2` | ZSET | 24h | 10 |
| `watchlist:tier2:details` | HASH | 24h | 10 |

**Detail JSON minimum**: `llm_summary`, `catalyst_type`, `has_catalyst`, `catalyst_score`, `technical_score`, `regime_multiplier`.

### Acceptance (stage)

- [ ] tier2 ⊆ tier1
- [ ] Expelled symbols not present in tier2
- [ ] Degraded mode (no LLM) logs `L2_DEGRADED` and passes tier1 through

---

## Stage 3 — PRE-EVAL (EntryOrchestrator caller)

### Purpose

Build **today's entry candidate set** for the entry pipeline. Tier2 → pre-eval filter → `ctx.actionable_symbols: Set[str]` (in-process; no Redis key).

### Removal rules (now enforced inside EntryOrchestrator stage_1 guard + stage_4 SR + stage_3 YOLO)

| Condition | Action | Enforced in |
|-----------|--------|-------------|
| Gap > +8% or < -5% | Remove | Caller (gap check pre-pipeline) or SR-04 |
| Open positions ≥ `max_concurrent_positions` | No new slots | PositionManager + R-04 |
| Symbol in `risk:cooldown:{symbol}` | Remove | Gatekeeper R-06 |
| Regime = PANIC | `ctx.actionable_symbols` = `{}` | SR-07 regime gate |

### Output contract

| Field | Type | Content |
|-----|------|---------|
| `ctx.actionable_symbols` | `Set[str]` | 0–N symbols (caller-supplied; EntryOrchestrator does not read Redis for this) |

Parameters: `max_entries_per_day` enforced as `len(ctx.actionable_symbols) ≤ N`; `gap_block_high` / `gap_block_low` enforced by caller or SR-04.

### History

Stage 3 was previously owned by `PreMarketReview` (wrote `actionable:today` LIST to Redis at 08:30 ET). Module + cron wrapper removed in hygiene-A/I (`6d5a2bf8` / `e737f541`). Re-purposed in hygiene-J to caller-side pre-eval feeding in-process `ctx.actionable_symbols`. No production code change required (the live entry path was already caller-supplied; SR-04 inside stage_4 enforces the actionable check).

### Acceptance (stage)

- [ ] `ctx.actionable_symbols` is a finite `Set[str]` (never `None`, never a list)
- [ ] Caller populates from `watchlist:tier2` (or test fixture for backtests)
- [ ] Empty set → entry pipeline runs but processes 0 symbols (clean no-op)

---

## Stage 4 — ENTRY

### Purpose

Convert actionable symbol → **one bracket entry** if all gates pass.

### LLMAnalyst output contract

**Module rule**: LLMAnalyst produces **analysis only**. It does not compute shares, `size_pct`, or orders.

#### Type: `AnalystOutput` (JSON)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `symbol` | string | yes | Ticker |
| `side` | enum | yes | `BUY` \| `PASS` |
| `confidence` | float | yes | 0.0–1.0; used for PASS/BUY gate only |
| `thesis_tags` | string[] | yes | e.g. `["earnings_beat", "sector_strength"]` |
| `invalidators` | string[] | yes | What would falsify thesis; empty array allowed |
| `expected_holding_days` | int | yes | Analyst estimate, typically 2–15 |
| `entry_rationale` | string | yes | One paragraph, factual |
| `risk_flags` | string[] | no | e.g. `["wide_spread", "low_float"]` |
| `analyst_version` | string | yes | e.g. `v12-analyst-1` |
| `generated_at` | ISO8601 | yes | Timestamp |

#### Validation rules

- `side == BUY` requires `confidence >= min_entry_confidence` **before** Gatekeeper (or analyst must emit PASS).
- Invalid JSON or missing required field → treat as `PASS` + journal `ANALYST_INVALID`.
- `expected_holding_days` outside 1–20 → clamp to [2, 15] and log warning.

#### Persistence

- Full `AnalystOutput` copied into TradeJournal on entry attempt.
- Subset (`thesis_tags`, `expected_holding_days`) copied into `position:{mode}:{symbol}` at OPEN.

### Decision sequence (per symbol, once per day)

```
0. IF symbol in entry:evaluated:{mode}:{date} OR position:{mode}:index → skip (INV-1/2)
1. LLMAnalyst → AnalystOutput
2. IF side == PASS OR confidence < min_entry_confidence → journal PASS; stop
3. RiskGatekeeper.validate_order_intent() → ALLOW | DENY + size_pct (risk formula only)
4. IF ALLOW → BracketOrderService.submit(...)
5. PositionManager.register(...) → state OPEN
6. TradeJournal.log_entry(...)  // includes AnalystOutput + Gatekeeper audit fields
7. SADD entry:evaluated:{mode}:{date} symbol
```

### Entry thresholds (pipeline-owned)

| Parameter | Default | Authority |
|-----------|---------|-----------|
| `min_entry_confidence` | 0.70 | `swing_scanner.yaml` or `trading_modes.yaml` |
| `entry_window_open` | 09:35 ET | `scheduler.yaml` — enforced by Gatekeeper R-09 |
| `entry_window_close` | 15:30 ET | `scheduler.yaml` |

All **risk caps** (daily trades, exposure, cooldown): [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md).

### Bracket order contract

On BUY allow:

| Leg | Type | Rule |
|-----|------|------|
| Parent | MKT or LMT | qty from `size_pct` + NAV |
| Child SL | STP | `entry - ATR14 × sl_atr_multiple` |
| Trail config | — | Registered in PositionManager (activation 1× ATR) |

### Acceptance (stage)

- [ ] No entry outside window
- [ ] PASS symbols never create `position:*`
- [ ] Every entry has journal record + SL child order id

---

## Stage 5 — HOLD (hooks only)

Intraday logic owned by [POSITION_LIFECYCLE.md](POSITION_LIFECYCLE.md).

Pipeline obligation: **do not** emit duplicate entry signals for symbols already in `OPEN` or `TRAILING`.

---

## Stage 6 — REVIEW (DailyReview)

### Purpose

Post-close **recommendation** per open position (not immediate broker fire unless rule says so).

### Actions

| Action | Meaning |
|--------|---------|
| HOLD | No change |
| TRIM | Queue 50% sell next session |
| EXIT | Queue full exit next session |
| TIGHTEN | Reduce trail distance |
| ALERT | Human review only |

Decision matrix: [POSITION_LIFECYCLE.md](POSITION_LIFECYCLE.md#dailyreview-decision-matrix).

### Output

- Append to `journal:{date}:{symbol}`
- Optional `position:{mode}:{symbol}.pending_action` for next open

### Acceptance (stage)

- [ ] Every open position gets exactly one review record per trading day
- [ ] Failure → implicit HOLD for all

---

## Parameter Authority (Pipeline)

| Parameter | Canonical config | Loaded by | Enforced by |
|-----------|------------------|-----------|-------------|
| `output_top_n` (L1) | `swing_scanner.yaml` | SwingScanner | unit test tier1 size |
| `tier2_top_n` | `swing_scanner.yaml` | CatalystScorer | tier2 size |
| `min_entry_confidence` | `swing_scanner.yaml` | Entry orchestrator | before Gatekeeper |
| `max_entries_per_day` | `swing_scanner.yaml` | EntryOrchestrator caller (sets `len(ctx.actionable_symbols) ≤ N`) | caller's pre-eval |
| `gap_block_high` / `gap_block_low` | `swing_scanner.yaml` | EntryOrchestrator caller (or SR-04) | gap removal |
| `l2_catalyst_multipliers` | `risk_regimes.yaml` | CatalystScorer | scoring only |

Risk parameters: **only** [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md#parameter-authority-registry).

---

## Anti-Churn (Pipeline + Risk)

| Rule | Enforcer |
|------|----------|
| One entry evaluation per symbol per day | Entry orchestrator |
| No re-entry from actionable if position exists | PositionManager |
| `max_daily_trades` | RiskGatekeeper R-05 |
| `symbol_cooldown_hours` | RiskGatekeeper R-06 |
| `min_hold_days` on SELL | PositionManager + R-10 |

---

## Related Tests (bind when implemented)

| Test ID | Asserts |
|---------|---------|
| `test_tier1_filters` | Hard filters |
| `test_tier2_expulsion` | Expulsion rule |
| `test_pre_eval_empty_set` | Empty `ctx.actionable_symbols` → 0 entries (was `test_premarket_empty_on_failure`; renamed in hygiene-J) |
| `test_entry_requires_confidence` | min_entry_confidence |
| `test_no_duplicate_entry_same_day` | Anti-churn |

Full checklist: [MIGRATION_GUIDE.md](MIGRATION_GUIDE.md#acceptance-checklist).

---

## Decision Explainability Contract (Pipeline)

Minimum fields for every pipeline decision logged to TradeJournal:

| Decision | Required fields |
|----------|-----------------|
| `PASS` (analyst) | `symbol`, `stage=entry`, `decision=PASS`, `reason_code`, `confidence`, `entry_rationale` (short), `thesis_tags` |
| `DENY` (gatekeeper) | See [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md#decision-explainability-contract) |
| `BUY` (filled) | AnalystOutput + `allowed_size_pct`, `parent_order_id`, `sl_order_id`, `regime_at_entry` |
| `TRIM` / `EXIT` (review) | `stage=review`, `decision`, `reason_code`, `days_held`, `pnl_pct`, `pending_action` |

### Standard journal envelope

Every record MUST include:

| Field | Type |
|-------|------|
| `schema_version` | `1` |
| `symbol` | string |
| `mode` | string |
| `stage` | `scan` \| `catalyst` \| `premarket` \| `entry` \| `hold` \| `review` \| `exit` _(journal envelope; `premarket` retained for backward compat with historical records — pre-eval logic now lives in EntryOrchestrator, see Stage 3 § History)_ |
| `decision` | `PASS` \| `DENY` \| `ALLOW` \| `BUY` \| `HOLD` \| `TRIM` \| `EXIT` \| `TIGHTEN` \| `ALERT` |
| `reason_code` | string |
| `reason_detail` | string |
| `ts` | ISO8601 |

Exit-specific extensions: `entry_date`, `exit_date`, `pnl_pct`, `hold_days`, `mfe_pct`, `mae_pct`, `regime_at_entry`, `regime_at_exit` — see [POSITION_LIFECYCLE.md](POSITION_LIFECYCLE.md#tradejournal-schema).
