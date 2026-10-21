# Post-Mortem Pipeline — Self-Correction Layer

> **Source files:**
> - `src/services/post_mortem_service.py` — Main pipeline (Extract → Filter → Reflect → Inject → Shadow Backtest)
> - `src/services/market_data_service.py` — ATR% calculation + Redis caching (Polygon daily bars)
> - `src/services/post_mortem_models.py` — Pydantic data models
> - `src/services/reinforcement_service.py` — Positive reinforcement engine
> - `src/knowledge/store.py` — ChromaDB + Redis knowledge store
> - `src/core/risk_controller.py` — Override loading + three-layer guardrails
> - `scripts/audit_review.py` — CLI review tool
> - `scripts/telegram_approval_bot.py` — Telegram review interface
>
> Last updated: 2026-04-07 (V2.2)

---

## Architecture Overview

```
    ┌─────────────────────────────────────────────────────────┐
    │           16:30 EST — Daily Post-Mortem Job             │
    │           (lifecycle_scheduler.py)                       │
    └──────────────────────────┬──────────────────────────────┘
                               │
    ┌──────────────────────────▼──────────────────────────────┐
    │  STAGE 1: EXTRACTOR                                     │
    │                                                          │
    │  Query trade_events for:                                 │
    │    - SELL trades with realized_pnl_dollar < 0            │
    │    - pnl% < -1.5% (configurable)                        │
    │    - Within last 24h                                     │
    │  Dedup via Redis (lesson_{trace_id})                     │
    │  Cap: max 10 trades per run                              │
    └──────────────────────────┬──────────────────────────────┘
                               │  LosingTrade[]
    ┌──────────────────────────▼──────────────────────────────┐
    │  STAGE 2: 4-LAYER FILTER                                │
    │                                                          │
    │  L1 Qualify: market hours + hold > 6 min                 │
    │  L2 Bucket:  negative (<-1.8%) / positive / neutral     │
    │  L3 Refine:   signal quality, risky patterns, regime     │
    │  L4 Veto:     exposure limits, business rules            │
    │                                                          │
    │  Pass gates:  negative ≥5, positive ≥7, neutral = never  │
    └──────────────────────────┬──────────────────────────────┘
                               │  filtered trades
    ┌──────────────────────────▼──────────────────────────────┐
    │  STAGE 3: REFLECTOR (LLM)                               │
    │                                                          │
    │  Primary: ChatZhipuAI (glm-5)                            │
    │  Fallback: ChatOpenAI (gpt-4-turbo)                     │
    │                                                          │
    │  Structured output → TradeReviewModel (Pydantic)         │
    │  Generates: root cause, severity (1-10), suggestions     │
    │  Anti-PTSD rule: lessons must be falsifiable             │
    └──────────────────────────┬──────────────────────────────┘
                               │  LessonEnvelope
    ┌──────────────────────────▼──────────────────────────────┐
    │  STAGE 4: INJECTOR                                      │
    │                                                          │
    │  severity ≥ 7 → ChromaDB + Redis (long-term RAG)        │
    │  severity < 7 → Redis only (short-term cache)            │
    │                                                          │
    │  P0/P1 suggestions → rag:review:pending:{date}           │
    └──────────────────────────┬──────────────────────────────┘
                               │
    ┌──────────────────────────▼──────────────────────────────┐
    │  STAGE 4.5: SHADOW BACKTESTING                           │
    │                                                          │
    │  Replay last 30d of similar trades with suggested        │
    │  parameters applied. Attach impact metrics to the        │
    │  review payload so human reviewers see projected          │
    │  PnL improvement, losses avoided, and trades blocked.    │
    └──────────────────────────┬──────────────────────────────┘
                               │
    ┌──────────────────────────▼──────────────────────────────┐
    │  STAGE 5: POSITIVE REINFORCEMENT                         │
    │  (ReinforcementEngine)                                   │
    │                                                          │
    │  Audit trades where lessons were applied but entry        │
    │  avoided (SHADOW_LOGGED/REJECTED).                       │
    │  Check T+4h price: if avoidance was correct →            │
    │  increment lesson success_count in ChromaDB.             │
    └─────────────────────────────────────────────────────────┘
```

---

## Stage 1: Extractor

**File:** `post_mortem_service.py` — `extract_losers()`

### SQL Query

```sql
SELECT event_id, trace_id, symbol, side, quantity, price,
       pattern_type, mode, time AS event_time,
       risk_metadata, context_snapshot,
       realized_pnl_dollar AS realized_pnl,
       realized_pnl_pct
FROM trade_events
WHERE time >= {cutoff}
  AND trace_id IS NOT NULL
  AND side = 'SELL'
  AND realized_pnl_dollar IS NOT NULL
  AND realized_pnl_dollar < 0
ORDER BY realized_pnl_dollar ASC;
```

### Filtering

After SQL returns all losing SELL trades:

| Filter | Value | Notes |
|--------|-------|-------|
| PnL threshold | Dynamic: `-1.0 × ATR%` per symbol | V2.2: volatility-adjusted; static `-1.5%` available as fallback |
| Lookback | 24h (configurable) | `lookback_hours` param |
| Dedup | Redis `lesson_{trace_id}` | Skip already-processed trades |
| Cap | 10 trades | `MAX_LESSONS_PER_RUN` — avoid runaway LLM calls |

**Dynamic ATR Thresholds (V2.2):** Instead of a hardcoded `-1.5%` threshold, each trade is evaluated against `-1.0 × ATR%` for its symbol. A `-1.5%` loss on a low-vol utility stock (ATR% ≈ 1.0%) exceeds the `-1.0%` threshold and gets extracted. The same `-1.5%` loss on a high-vol crypto stock (ATR% ≈ 5.0%) is below the `-5.0%` threshold and is treated as noise.

**ATR Calculation:** `ATR% = (ATR_14 / entry_price) × 100` using Polygon daily bars. Cached in Redis at `market_data:atr_pct:{symbol}:{date}` with 48h TTL.

**Source file:** `src/services/market_data_service.py` — `MarketDataService.get_atr_pct()`

### Output

```python
LosingTrade(
    trace_id="uuid",
    symbol="TSLA",
    pnl_percent=-3.2,        # percentage points
    pattern_type="BREAKOUT",
    mode="PAPER",
    risk_metadata={...},      # confidence, regime, fusion_sources
    context_snapshot={...},   # original reasoning chain
)
```

---

## Stage 2: Four-Layer Filter

**File:** `post_mortem_service.py` — `filter_trade()`

Only trades that pass all four layers reach the LLM. This prevents wasting API calls on noise.

### Layer 1: Qualify

| Check | Rule | Rationale |
|-------|------|-----------|
| Timestamp | Must have `entry_time` | Can't analyze without timing |
| Hold duration | > 6 minutes (360s) | Sub-6min trades are noise / execution artifacts |

### Layer 2: Bucket (Performance-based, dynamic ATR)

| Bucket | Criteria | Meaning |
|--------|----------|---------|
| `negative` | pnl < -1.2 × ATR% | Clear failure worth analyzing |
| `positive` | pnl > 0.8 × ATR% AND confidence ≥ 0.75 | Good execution despite loss — edge case |
| `neutral` | everything else | Ambiguous — **always rejected** |

V2.2 replaces the hardcoded `-1.8%` / `1.2%` thresholds with ATR-scaled equivalents. The same ATR% cached during extraction is reused (Redis cache hit).

### Layer 3: Refine (Signal quality)

| Signal | Effect | Reason |
|--------|--------|--------|
| Weak signal (confidence < 0.65) | `positive` → `neutral` | Weak conviction shouldn't be "positive" |
| Risky pattern (CHOPPY, FAKEOUT, RANGE_BOUND) | → `negative` | Known problematic patterns |
| Regime mismatch (bull entry → bear exit) | → `negative` | Environment changed after entry |

### Layer 4: Veto (Business rules)

| Rule | Effect |
|------|--------|
| Positive bucket + pnl < 0.8% | `positive` → `neutral` (too small to be meaningful) |

### Final Decision + Priority Score

```
score = 1 + (2 × L3_hits) + (3 × L4_hits)

Pass gates:
  negative bucket → score >= 5
  positive bucket → score >= 7 AND no L4 vetoes
  neutral bucket  → NEVER passes
```

### FilterResult Output

```python
FilterResult(
    passed=True,
    bucket="negative",
    layer_reasons=["L2: bucketed as negative", "Regime mismatch: BULL → BEAR"],
    priority_score=7,
)
```

---

## Stage 3: Reflector (LLM Root Cause Analysis)

**File:** `post_mortem_service.py` — `reflect()`

### LLM Configuration

| Priority | Model | API Key |
|----------|-------|---------|
| Primary | ChatZhipuAI (glm-5) | `ZAI_API_KEY` |
| Fallback | ChatOpenAI (gpt-4-turbo) | `OPENAI_API_KEY` |

Temperature: 0.2 (low for analytical consistency)

### Structured Output Schema (TradeReviewModel)

```python
class TradeReviewModel(BaseModel):
    overview: str                          # One sentence summary
    execution_score: int  # 0-10           # Execution discipline
    signal_score: int  # 0-10              # Signal quality/setup
    pnl_attribution: List[PnlFactor]       # [{factor, pct, desc}]
    example_category: A|B|C|D|E            # A=Excellence ... E=Fatal Error
    failure_reason: str                    # Root cause category
    key_lesson: List[str]                  # Key takeaways
    system_suggestions: List[SystemSuggestion]  # [{priority: P0/P1/P2, suggestion}]
    severity: int  # 1-10                  # 1=minor fluke, 10=critical systemic flaw
    confidence: float  # 0.0-1.0           # LLM confidence in analysis
```

### Failure Reason Categories

| Reason | Description |
|--------|-------------|
| `false_breakout` | Price broke level but failed to follow through |
| `regime_mismatch` | Trade direction opposed current market regime |
| `liquidity_trap` | Entered in low-liquidity environment |
| `stale_signal` | Signal was valid but too late to act on |
| `overconfidence` | Position sizing too large for conviction |
| `news_shock` | Unexpected news event reversed the move |
| `correlation_trap` | Hidden correlation caused unexpected behavior |
| `other` | Doesn't fit above categories |

### Anti-PTSD Rule (in System Prompt)

> "Do NOT recommend blanket avoidance. Specify CONDITIONAL factors. The lesson must be falsifiable."

This prevents the system from learning "never trade TSLA" — instead it should learn "avoid BREAKOUT on TSLA when VIX > 25 and sector rotation is defensive."

### LessonEnvelope (Final Output)

```python
LessonEnvelope(
    doc_id="lesson_{trace_id}",
    symbol="TSLA",
    regime="BULL_LONG",
    pattern="BREAKOUT",
    failure_reason="false_breakout",
    lesson_text="In BULL_LONG, when BREAKOUT appears but VIX>25...",
    severity=8,
    confidence=0.85,
    pnl_pct=-3.2,
    time_of_day="morning",
    review=TradeReviewModel(...),
)
```

---

## Stage 4: Injector (Knowledge Storage)

**File:** `post_mortem_service.py` — `inject()` + `knowledge/store.py`

### Severity Gate

| Severity | ChromaDB | Redis | RAG Searchable |
|----------|----------|-------|----------------|
| ≥ 7 | YES (semantic search) | YES (timeline cache) | YES |
| < 7 | NO (empty doc) | YES only | NO |

**Why the gate?** ChromaDB is for long-term pattern recognition. Low-severity "fluke" trades pollute semantic search — they go to Redis short-term cache only (7-day TTL).

### Storage Flow

```
lesson_{trace_id}
    │
    ├─→ ChromaDB collection "negative_lessons"
    │   document: "[TSLA] [BULL_LONG] [BREAKOUT] Failure: false_breakout. ..."
    │   metadata: {trace_id, symbol, regime, severity, confidence, created_at, ...}
    │
    └─→ Redis
        SET  lesson_cache:lesson_{trace_id}  → LessonEnvelope JSON  (TTL 7d)
        ZADD lesson_timeline                 → {lesson_id: timestamp}
```

### ChromaDB Document Format

```
[{symbol}] [{regime}] [{pattern}] Failure: {failure_reason}. {lesson_text}
```

This format ensures semantic search can match by symbol, regime, and pattern.

---

## Stage 4.5: Shadow Backtesting (Impact Simulation)

**File:** `post_mortem_service.py` — `_simulate_suggestions()`

### Purpose

When the LLM generates P0/P1 suggestions (e.g., "Reduce kelly_fraction to 0.4"), the reviewer sees only text — no data. Stage 4.5 automatically simulates the impact of those suggestions by replaying the last 30 days of similar trades with the suggested parameters applied, and attaches the results to the review payload.

### When It Runs

Between `inject()` and `_queue_for_review()`. Only runs for lessons with `severity >= 7` that have P0/P1 suggestions.

### Parameter Parsing

Suggestions are parsed into parameter overrides via regex:

| Pattern Match | Parameter | Guardrail Range |
|---------------|-----------|-----------------|
| `kelly_fraction to X` | `kelly` | 0.1 – 1.0 |
| `risk_scaling X` | `risk_scaling` | 0.3 – 2.0 |
| `min_confidence X` | `min_confidence` | 0.1 – 0.8 |

Unparseable suggestions are skipped. If none parse, `simulation` is `None` and the review is queued without it.

### Simulation Logic

```python
# Position sizing formula (mirrors risk_controller.py exactly)
base = 0.01
base *= regime_multiplier          # BULL_LONG=1.3, NEUTRAL=1.0, PANIC=0.3
base *= min(confidence / 0.7, 2.0)
base *= kelly_override             # from suggestion
base = min(base, 0.05)            # hard cap
base *= risk_scaling_override     # from suggestion
```

For each historical trade matching the same `pattern_type`:

1. **If `min_confidence` is set and trade's confidence < threshold** → trade would be **blocked** (PnL = 0)
2. **Otherwise** → simulate PnL proportionally: `simulated_pnl = original_pnl * (new_size / original_size)`

### Output Format

```json
{
  "matched_trades": 12,
  "original_total_pnl": -4520.0,
  "simulated_total_pnl": -2180.0,
  "pnl_improvement": 2340.0,
  "trades_blocked": 3,
  "losses_avoided": 3,
  "wins_blocked": 0,
  "lookback_days": 30
}
```

This is attached to the review payload as the `shadow_backtest` field. The CLI and Telegram review tools display it automatically (extra JSON field is printed as-is).

### Edge Cases

| Case | Handling |
|------|----------|
| No historical trades matching pattern | `{"matched_trades": 0}` |
| Suggestion doesn't parse | Skip that suggestion, simulate others |
| All suggestions unparseable | `None` — queue without simulation |
| Simulation shows worse outcomes | Still queue — negative result is useful data |

---

## Stage 5: Positive Reinforcement

**File:** `reinforcement_service.py` — `ReinforcementEngine`

### Purpose

Not all learning comes from failures. The reinforcement engine validates that the system correctly **avoided** bad trades by applying previous lessons.

### How It Works

```
1. Query trade_events where:
     was_lesson_applied = true
     AND status IN ('SHADOW_LOGGED', 'REJECTED')

2. For each avoided trade:
     a. Fetch Polygon 5-min bars for T+4h window
     b. Compare T+4h price vs entry price:
        - Avoided BUY + price dropped → SUCCESS (avoidance was correct)
        - Avoided SELL + price rose   → SUCCESS (avoidance was correct)
     c. If SUCCESS: increment success_count on all applied lesson_ids in ChromaDB

3. Output: count of reinforced lessons
```

### Lesson Weight in ChromaDB

```python
metadata = {
    "success_count": 3,          # incremented by reinforcement
    "last_verified_at": "...",    # timestamp of last reinforcement
}
```

Higher `success_count` = more reliable lesson. The RAG retrieval layer can use this to boost well-validated lessons.

---

## Review Queue (Human-in-the-Loop)

### Redis Key Schema

| Key | Type | TTL | Purpose |
|-----|------|-----|---------|
| `rag:review:pending:{date}` | HASH | 48h | Lessons awaiting human review |
| `rag:review:approved:{date}` | STRING (JSON) | 48h | Approved override payload |
| `rag:review:rejected:{date}` | SET | 48h | Rejected doc_ids |
| `rag:review:notes:{date}` | HASH | 48h | Optional human notes |

### What Gets Queued

A lesson is queued for review when:
1. `severity >= 7` (high severity)
2. LLM generated `system_suggestions` with priority P0 or P1

### Pending Item Format

```json
{
  "doc_id": "lesson_abc123",
  "symbol": "TSLA",
  "severity": 8,
  "failure_reason": "false_breakout",
  "lesson_text": "In BULL_LONG, avoid BREAKOUT when VIX>25...",
  "pnl_pct": -3.2,
  "suggestions": [
    {"priority": "P0", "suggestion": "Reduce kelly_fraction to 0.4 for BREAKOUT in BULL_LONG"},
    {"priority": "P1", "suggestion": "Add min_confidence=0.6 for high-VIX BREAKOUT"}
  ],
  "shadow_backtest": {
    "matched_trades": 12,
    "original_total_pnl": -4520.0,
    "simulated_total_pnl": -2180.0,
    "pnl_improvement": 2340.0,
    "trades_blocked": 3,
    "losses_avoided": 3,
    "wins_blocked": 0,
    "lookback_days": 30
  },
  "queued_at": "2026-04-07T20:30:00Z"
}
```

---

## Review Tools

### CLI: `scripts/audit_review.py`

```bash
# List pending lessons
python scripts/audit_review.py --list
python scripts/audit_review.py --list --date 2026-04-06

# View lesson details
python scripts/audit_review.py --show lesson_abc123

# Approve with risk parameter overrides
python scripts/audit_review.py --approve lesson_abc123 \
    --kelly 0.4 \
    --min-confidence 0.6 \
    --risk-scaling 0.8

# Approve with symbol blacklist
python scripts/audit_review.py --approve lesson_abc123 \
    --blacklist "TSLA,NVDA"

# Reject
python scripts/audit_review.py --reject lesson_abc123 --reason "minor fluke"

# Bulk approve all pending
python scripts/audit_review.py --approve-all --kelly 0.5 --risk-scaling 0.7
```

### Telegram Bot: `scripts/telegram_approval_bot.py`

Interactive review via Telegram messages. Approve/reject with inline buttons, same override parameters.

---

## Override Application (Morning Risk Load)

### Schedule

```
09:15 EST — morning_risk_load job (lifecycle_scheduler.py)
```

### Flow

```
1. HardRiskGatekeeper() constructor called
2. _load_approved_overrides() reads rag:review:approved:{today}
3. Three-layer guardrails validate the overrides
4. If pass → write to Redis keys + apply to in-memory state
5. Publish "risk:overrides:reload" to notify running instances
```

### Three-Layer Guardrails

| Layer | Check | Rule |
|-------|-------|------|
| 1. Range | Kelly fraction | 0.1 – 1.0 |
| 1. Range | Min confidence | 0.1 – 0.8 |
| 1. Range | Risk scaling | 0.3 – 2.0 |
| 2. Consistency | High Kelly + low scaling | kelly > 0.8 AND scaling < 0.5 → reject |
| 3. Recency | Approved within 36 hours | Stale approvals ignored |

### Override Target Keys

| Redis Key | Type | Written By | Read By |
|-----------|------|-----------|---------|
| `kelly_fraction:current` | STRING | morning_risk_load | HardRiskGatekeeper |
| `risk:min_confidence:current` | STRING | morning_risk_load | HardRiskGatekeeper |
| `risk_scaling:current` | STRING | morning_risk_load | HardRiskGatekeeper |
| `risk:blacklist:state` | HASH | morning_risk_load | HardRiskGatekeeper |

### Position Sizing with Overrides

```python
base_size = 0.01                           # 1% base
base_size *= regime_multiplier             # PANIC=0.3, BULL=1.3
base_size *= confidence_scale              # min(confidence/0.7, 2.0)
base_size *= kelly_override                # from approved post-mortem (0.1-1.0)
base_size = min(base_size, 0.05)           # hard cap 5%
base_size *= risk_scaling_override         # from approved post-mortem (0.3-2.0)
```

---

## Daily Timeline

```
16:30 EST  Post-Mortem Job
             ├─ Extract losing trades from last 24h
             ├─ 4-layer filter
             ├─ LLM root cause analysis
             ├─ Inject lessons (ChromaDB + Redis)
             ├─ Shadow backtest P0/P1 suggestions (30d replay)
             ├─ Queue suggestions + impact metrics for review
             └─ Positive reinforcement audit

Evening    Human Review
             ├─ CLI: audit_review.py --list
             ├─ CLI: audit_review.py --approve --kelly 0.4
             └─ Or: Telegram bot approval

09:15 EST  Morning Risk Load (next day)
             ├─ Read rag:review:approved:{today}
             ├─ Validate via three-layer guardrails
             ├─ Write overrides to Redis
             └─ Publish reload signal
```

---

## Data Models

### LosingTrade (Input to Pipeline)

```python
class LosingTrade(BaseModel):
    trace_id: str                  # UUID linking to LangGraph state
    event_id: str
    symbol: str
    side: str                      # BUY or SELL
    entry_price: float
    exit_price: float
    quantity: float
    realized_pnl: float            # Negative dollar value
    pnl_percent: float             # e.g. -2.3 means -2.3%
    pattern_type: str              # BREAKOUT, MOMENTUM, etc.
    mode: str                      # PAPER or LIVE
    entry_time: Optional[datetime]
    exit_time: Optional[datetime]
    risk_metadata: Dict            # confidence, regime, fusion_sources
    context_snapshot: Dict         # original reasoning chain
```

### FilterResult (Filter Output)

```python
class FilterResult(BaseModel):
    passed: bool
    bucket: "negative" | "positive" | "neutral"
    layer_reasons: List[str]       # e.g. ["L3: Regime mismatch: BULL → BEAR"]
    priority_score: int            # 1-based, higher = more important
```

### TradeReviewModel (LLM Structured Output)

```python
class TradeReviewModel(BaseModel):
    overview: str
    execution_score: int           # 0-10
    signal_score: int              # 0-10
    pnl_attribution: List[PnlFactor]
    example_category: A|B|C|D|E
    failure_reason: str
    key_lesson: List[str]
    system_suggestions: List[SystemSuggestion]
    severity: int                  # 1-10
    confidence: float              # 0.0-1.0
```

### LessonEnvelope (Canonical Storage Format)

```python
class LessonEnvelope(BaseModel):
    doc_id: str                    # "lesson_{trace_id}"
    trace_id: str
    symbol: str
    regime: str
    pattern: str
    failure_reason: str
    lesson_text: str               # Full text, no truncation
    severity: int                  # 1-10
    confidence: float              # 0.0-1.0
    pnl_pct: float
    time_of_day: str               # premarket/morning/midday/afternoon/eod
    market_context: Dict
    review: Optional[TradeReviewModel]
    created_at: datetime
    source: str                    # "post_mortem_v2"
```

---

## Parameters Quick Reference

```yaml
# ═══════════════════════════════════════════════════════════════
# STAGE 1: EXTRACTOR
# ═══════════════════════════════════════════════════════════════
pnl_threshold: dynamic            # -1.0 × ATR% per symbol (V2.2)
atr_extract_multiplier: 1.0       # Threshold = -1.0 × ATR%
atr_default_fallback: 2.0         # Default ATR% when data unavailable
lookback_hours: 24                # Default scan window
max_lessons_per_run: 10           # Cap LLM API calls
dedup_key: "lesson_{trace_id}"    # Redis dedup

# ═══════════════════════════════════════════════════════════════
# STAGE 2: 4-LAYER FILTER
# ═══════════════════════════════════════════════════════════════
l1_min_hold_seconds: 360          # 6 minutes
l2_negative_threshold: dynamic    # -1.2 × ATR% (V2.2)
l2_positive_threshold: dynamic    # 0.8 × ATR% + confidence ≥ 0.75 (V2.2)
atr_negative_bucket: 1.2          # ATR multiplier for negative bucket
atr_positive_bucket: 0.8          # ATR multiplier for positive bucket
l3_weak_signal: 0.65              # confidence < 0.65 degrades positive → neutral
l3_risky_patterns: [CHOPPY, FAKEOUT, RANGE_BOUND]
l4_positive_min_pnl: 0.8          # Positive needs pnl ≥ 0.8%
negative_min_score: 5             # Priority score gate for negative
positive_min_score: 7             # Priority score gate for positive

# ═══════════════════════════════════════════════════════════════
# STAGE 3: REFLECTOR (LLM)
# ═══════════════════════════════════════════════════════════════
llm_primary: glm-5                # ChatZhipuAI
llm_fallback: gpt-4-turbo         # ChatOpenAI
llm_temperature: 0.2
structured_output: TradeReviewModel  # Pydantic schema

# ═══════════════════════════════════════════════════════════════
# STAGE 4: INJECTOR
# ═══════════════════════════════════════════════════════════════
severity_rag_threshold: 7         # ≥ 7 → ChromaDB + Redis
chromadb_collection: "negative_lessons"
redis_lesson_ttl: 604800          # 7 days
redis_timeline_key: "lesson_timeline"

# ═══════════════════════════════════════════════════════════════
# ATR CACHING (market_data_service.py)
# ═══════════════════════════════════════════════════════════════
atr_period: 14                    # 14-period Daily ATR
atr_lookback_buffer_days: 20      # Calendar days to fetch for 14 trading days
atr_cache_key: "market_data:atr_pct:{symbol}:{date}"
atr_cache_ttl: 172800             # 48 hours

# ═══════════════════════════════════════════════════════════════
# REVIEW QUEUE
# ═══════════════════════════════════════════════════════════════
review_min_severity: 7            # Queue P0/P1 when severity ≥ 7
review_ttl: 172800                # 48h
pending_key: "rag:review:pending:{date}"
approved_key: "rag:review:approved:{date}"
rejected_key: "rag:review:rejected:{date}"

# ═══════════════════════════════════════════════════════════════
# OVERRIDE GUARDRAILS (risk_controller.py)
# ═══════════════════════════════════════════════════════════════
kelly_fraction_range: [0.1, 1.0]
min_confidence_range: [0.1, 0.8]
risk_scaling_range: [0.3, 2.0]
consistency_check: kelly > 0.8 AND scaling < 0.5 → reject
recency_max_hours: 36

# ═══════════════════════════════════════════════════════════════
# POSITIVE REINFORCEMENT
# ═══════════════════════════════════════════════════════════════
verification_horizon: T+4h         # Check price 4 hours after avoided entry
bar_timespan: 5min                 # Polygon 5-minute bars for price check
```

---

## Tests

| Test File | Coverage |
|-----------|----------|
| `tests/test_post_mortem.py` | Extractor, filter layers, reflector, injector, shadow backtest, dynamic ATR |
| `tests/test_morning_risk.py` | Override loading, guardrails, review queue |
| `tests/test_market_data.py` | ATR calculation, Redis caching, cache hits/misses |
| `tests/e2e/test_record_to_reflection.py` | End-to-end: trade event → lesson → RAG retrieval |
