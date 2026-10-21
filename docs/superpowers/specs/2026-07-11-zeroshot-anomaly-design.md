# ZeroShot Anomaly — Design Spec

**Date:** 2026-07-11
**Topic:** Topic 3 of the 3-topic parallel-model-PnL brainstorm (1 3 2 order)
**Prior topics:** Topic 1 (Champion Model Selection) — committed in spec/plan, implementation complete except for blocked T11 deployment

## 1. Goals

Detect ensemble-vs-zeroshot forecast divergence as a **live, production-affecting signal** that reduces the live trading impact of ensemble trades that disagree with the zero-shot Chronos-Bolt baseline.

**Specific goals:**
1. Compute a per-symbol per-day divergence score from the daily shadow outputs of the current fine-tuned champion and `zeroshot_bolt_small`.
2. Persist the divergence result as a single daily artifact readable by the live EntryOrchestrator.
3. Inline that artifact into the live entry pipeline so entry decisions for high-divergence symbols are gated: shrink position size (medium severity) or skip the trade entirely (high severity).
4. Stay fail-soft: any failure (missing artifact, missing zeroshot prediction, compute error) MUST result in a pass-through with a log line, not a halt or crash.

**Non-goals (out of scope for v1):**
- Multi-day rolling z-score
- Cross-symbol correlation/contagion
- Sign-based composite signals (magnitude-only for v1)
- Interactive dashboards or alerting integrations (Slack/email)
- Auto-fallback to "skip ensemble entirely" — that's an existing ADR-008 mechanism, separate concern
- Anything past the 7-gate / champion + challenger flow (this complements, does not replace)

## 2. Decision summary (from brainstorm Q&A)

| Question | Decision |
|---|---|
| What does "ZeroShot anomaly" mean? | Ensemble-vs-zeroshot forecast divergence as an anomaly signal |
| Does it affect production? | **Yes** — live signal: reduce or skip ensemble trade on anomaly |
| Divergence metric? | Magnitude-based: `|ensemble_forecast − zeroshot_forecast|` (per symbol per day) |
| Architecture? | Approach B: pre-cached daily divergence map, consulted at entry time |
| Threshold / severity tiers? | none (<0.5%), medium (0.5–1.5%), high (≥1.5%) — fixed for v1, env-var configurable |

## 3. Architecture overview

```
┌───────────────────────────────────────────────────────────────┐
│ Daily shadow cron (16:05 NYC, run_shadow_cron.sh)             │
│   └─ shadow_trade.py writes per-model per-symbol JSONs        │
│       _shadow/E07_long_ctx/YYYY-MM-DD.json (champion)         │
│       _shadow/zeroshot_bolt_small/YYYY-MM-DD.json (baseline)  │
└──────────────────────┬────────────────────────────────────────┘
                       │
                       ▼
┌───────────────────────────────────────────────────────────────┐
│ NEW: compute_zs_anomaly.py (post-cron, scheduled 16:20)       │
│   - Reads both JSONs                                          │
│   - Computes per-symbol |ens − zs| forecast gap               │
│   - Classifies severity (none/medium/high)                    │
│   - Writes _shadow/anomalies/YYYY-MM-DD.json                  │
└──────────────────────┬────────────────────────────────────────┘
                       │ (one file read at entry time)
                       ▼
┌───────────────────────────────────────────────────────────────┐
│ EntryOrchestrator (live, runs at entry decision time)         │
│   └─ NEW stage: check_zs_anomaly                              │
│       - symbol severity == 'high'   → SKIP trade             │
│       - symbol severity == 'medium' → size *= 0.5             │
│       - symbol severity == 'none'   → pass-through           │
└───────────────────────────────────────────────────────────────┘
```

**Key property**: decision-time latency is a single JSON read, not a model call. Matches the existing "compute at cron, consult at decision" pattern used by `halt_decisions.jsonl`.

## 4. Components

### 4.1 New: `src/stockapp_v12/pipeline/zs_anomaly.py`

Pure-function module, no I/O. Public API:

```python
@dataclass(frozen=True)
class ZsGap:
    symbol: str
    ens_forecast_pct: float
    zs_forecast_pct: float
    gap_pct: float           # abs(ens - zs)
    date: str                # YYYY-MM-DD


@dataclass(frozen=True)
class ZsAnomaly:
    symbol: str
    gap_pct: float
    severity: str            # "none" | "medium" | "high"
    date: str


def compute_gap(ens: float, zs: float) -> float:
    """|ens − zs|. NaN handling: NaN inputs → NaN output (caller decides)."""


def classify_severity(gap: float, threshold_high: float = 1.5,
                       threshold_medium: float = 0.5) -> str:
    """Map a gap to 'none' | 'medium' | 'high'."""


def build_anomaly(gap: ZsGap, threshold_high: float = 1.5,
                   threshold_medium: float = 0.5) -> ZsAnomaly | None:
    """Build a ZsAnomaly from a ZsGap. Returns None if gap is NaN
    (caller should treat None as 'no anomaly signal available').
    """

def size_multiplier(severity: str) -> float:
    """Map severity to position-size multiplier.
       'none'   → 1.0
       'medium' → 0.5
       'high'   → 0.0   (caller treats 0.0 as 'skip')
    """
```

Pure functions; no I/O dependencies. Easy to unit-test exhaustively.

### 4.2 New: `scripts/compute_zs_anomaly.py`

Daily post-cron job. CLI invocation:

```bash
PYTHONPATH=src:scripts:. python scripts/compute_zs_anomaly.py \
    --date 2026-07-11 \
    --champion E07_long_ctx \
    --shadow-dir models/chronos/_shadow \
    --out models/chronos/_shadow/anomalies
```

Behavior:
1. Reads `_shadow/<champion>/<date>.json` (or whatever override `--champion` is given).
2. Reads `_shadow/zeroshot_bolt_small/<date>.json`.
3. Joins rows by `symbol` (rows that appear in both files only; mismatched symbols logged + skipped).
4. For each joined row, computes `gap_pct = |ens_forecast − zs_forecast|` using the `forecast_ret_pct` field already present in both JSONs.
5. Classifies severity using env-var-overridable thresholds (`ZS_ANOMALY_HIGH_PCT`, `ZS_ANOMALY_MEDIUM_PCT`).
6. Writes `_shadow/anomalies/<date>.json`:

```json
{
  "date": "2026-07-11",
  "champion": "E07_long_ctx",
  "computed_at": "2026-07-11T16:20:14-04:00",
  "thresholds": {"high_pct": 1.5, "medium_pct": 0.5},
  "n_symbols": 10,
  "n_skipped": 1,
  "anomalies": [
    {"symbol": "NVDA", "ens_pct": 2.1, "zs_pct": -0.4, "gap_pct": 2.5, "severity": "high"},
    {"symbol": "TSLA", "ens_pct": 1.6, "zs_pct": 0.9, "gap_pct": 0.7, "severity": "medium"}
  ]
}
```

`n_skipped` includes symbols missing from either input or with NaN forecasts.

### 4.3 Modified: `src/stockapp_v12/pipeline/stages.py` (or new stage file)

Add a new EntryOrchestrator stage (call site: between existing risk checks and sizing, exactly where the HaltGate integration lives so it sees the same context).

**Stage name:** `check_zs_anomaly`

**Behavior:**
1. Read `_shadow/anomalies/<today>.json`. If missing or invalid JSON → log warning, pass-through.
2. Look up the trade's symbol in `anomalies[]`. If absent → pass-through.
3. Read severity; apply size multiplier: 0.0 → skip, 0.5 → halve, 1.0 → unchanged.
4. Add a `meta.zs_anomaly` field to the position record (for downstream observability + backtest analysis).

### 4.4 Modified: `scripts/run_shadow_cron.sh`

Add a new post-cron invocation right after shadow_trade.py completes:

```bash
# Champion (post-cron): compute zs anomaly artifact for the live pipeline.
PYTHONPATH=src:scripts:. "$PYTHON" scripts/compute_zs_anomaly.py \
    --date "$TODAY" \
    --champion "$CHAMPION_CSV" \
    --shadow-dir "$STOCKAPP_ROOT/models/chronos/_shadow"
```

(Deploy of this cron change is part of T11; **not** in scope of the spec/plan implementation, only the script edit is.)

## 5. Data flow — concrete example

**Setup:** today is 2026-07-11. Champion = `E07_long_ctx`. Symbols under test: AAPL, NVDA, TSLA, JPM, AMZN.

| Symbol | ens_forecast (E07) | zs_forecast (zeroshot) | gap_pct | severity |
|--------|---------------------|-------------------------|---------|----------|
| AAPL   | 1.2                 | 1.5                     | 0.3     | none     |
| NVDA   | 2.1                 | -0.4                    | 2.5     | **high** |
| TSLA   | 1.6                 | 0.9                     | 0.7     | medium   |
| JPM    | 0.8                 | 0.9                     | 0.1     | none     |
| AMZN   | NaN (missing)       | 0.5                     | NaN     | skipped  |

Result: EntryOrchestrator at decision time → NVDA trade is skipped entirely, TSLA sized at 50%, AAPL/JPM unchanged, AMZN no anomaly signal applied.

## 6. Severity / sizing policy

| Severity | Gap range (% absolute) | Position size multiplier | Behavior |
|----------|------------------------|--------------------------|----------|
| `none`   | < 0.5                  | 1.0                      | no change |
| `medium` | 0.5 ≤ gap < 1.5        | 0.5                      | halve size |
| `high`   | gap ≥ 1.5              | 0.0                      | skip trade |

Env-var overrides (per process):
- `ZS_ANOMALY_HIGH_PCT` (default 1.5)
- `ZS_ANOMALY_MEDIUM_PCT` (default 0.5)

## 7. Failure modes (must be fail-soft)

| Scenario | Behavior |
|----------|----------|
| Anomaly artifact missing (first day, cron missed) | Log warning, pass-through no anomaly gate |
| Anomaly artifact invalid JSON | Log warning, pass-through |
| Zeroshot shadow JSON missing for date | Skip anomaly compute entirely; log warning; pass-through next day |
| Symbol missing from champion JSON | Skip that symbol, log warning, compute gaps for other symbols |
| Zeroshot prediction is NaN for a symbol | Skip that symbol in anomaly compute (`gap_pct` is None → row excluded from `anomalies[]`) |
| Ensemble prediction is NaN | Same as above |
| Champion not specified in env / CLI arg | Default to `E07_long_ctx` (current best); log default |
| Anomaly gate crashes during entry | Log exception, pass-through (entry follows normal sizing) |
| Stale artifact from yesterday | Use it anyway; log warning; rely on live EntryOrchestrator's other freshness checks |

**Fail-soft principle**: paper-trade regime is acceptable to under-gate briefly during data-quality issues rather than over-gate on false positives.

## 8. Testing strategy

### 8.1 Unit tests (`tests/test_zs_anomaly.py`)

Cover `compute_gap`, `classify_severity`, `build_anomaly`, `size_multiplier` with:
- Boundary values (0.0, 0.49, 0.5, 1.49, 1.5, 2.5)
- Negative inputs (forecast can be negative for shorts)
- NaN handling (both inputs NaN → out NaN; one input NaN → still NaN; no crash on `abs(nan)`)
- Mixed-sign cases (ens=+2.0, zs=-1.0 → gap=3.0 → high)
- Threshold overrides via env vars
- Empty / no-symbols edge case

### 8.2 Integration tests (`tests/test_zs_anomaly_stage.py`)

Wire the stage into a fake EntryOrchestrator with mock shadow JSONs and:
- Verify high severity → stage returns `skip=True`
- Verify medium severity → stage returns `size_multiplier=0.5`
- Verify none severity → stage pass-through
- Verify missing artifact → pass-through + log captured
- Verify stale artifact (yesterday's date) → use with warning
- Verify NaN gap in artifact → no anomaly applied

### 8.3 E2E test (`tests/test_e2e_zs_anomaly.py`)

Subprocess-driven: stage shadow JSONs for two days (yesterday + today), run `compute_zs_anomaly.py`, verify output artifact schema. Then use the artifact to drive the EntryOrchestrator stage end-to-end. Same pipeline-style e2e pattern as Topic 1's `test_e2e_champion_selection.py`.

### 8.4 Coverage target

≥ 80% on `zs_anomaly.py` (project standard). Integration + E2E tests count toward functional coverage.

## 9. Operational concerns

### 9.1 Schedule

- **Daily cron (16:20 NYC, 15 min after `run_shadow_cron.sh` ends):** `compute_zs_anomaly.py` runs after both `shadow_trade.py` (per-model JSONs) and `shadow_halt_decision.py` (halt_decisions.jsonl) finish.
- **Deploy:** T11 deploy includes installing this new cron line on NUC. Spec-side responsibility: leave the `run_shadow_cron.sh` patch ready for T11.

### 9.2 Observability

- Each computed artifact is itself a log artifact — readable, diffable, version-controllable.
- EntryOrchestrator stage writes `meta.zs_anomaly = {severity, gap_pct}` to every position record it touches, so backtest analysis can correlate anomaly status with realized PnL.
- Failure modes emit WARN logs through the standard logger (no new notifier integration for v1).

### 9.3 Backwards compatibility

- The stage is fail-soft — existing entries continue if the artifact is missing (paper-trade consequence: temporarily no anomaly protection, no break).
- No change to champion/challenger flow (Topic 1's `champion_evaluation.py`, `run_shadow_cron.sh`'s role env vars still apply).
- No change to ADR-008 or HaltGate — this layer sits *between* the existing pipeline and the existing halt gate, orthogonal.

## 10. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Threshold values wrong for current market regime | Medium | False positives → missed trades | Env-var overridable; quarterly review tunes them |
| Anomaly stage becomes permanent feature freeze in down regimes | Medium | Under-sizing too often | Severity tier "high" requires ≥1.5% gap (rare); medium is configurable down |
| Champion path changes (T10 functional gap) | Already known | Anomaly computation can't read champion JSON | Decoupled — `--champion` arg + env override; same gate as T11 deferral |
| NaN handling creates silent skip | Low | Anomaly under-detection | `n_skipped` field in artifact; backtest analysis can spot under-counts |
| Stale artifact causes false negatives after regime shift | Medium | Trades placed without fresh anomaly reading | WARN log on stale; EntryOrchestrator's other freshness checks apply |

## 11. Spec self-review

- **Placeholder scan:** No TBD/TODO/incomplete sections. ✓
- **Internal consistency:** Section 4.1 API matches 4.2 CLI invocation. Section 5 example values match 4.3 stage behavior. Section 6 table matches 4.1 `classify_severity` defaults. ✓
- **Scope check:** Single deliverable: anomaly module + cron + orchestrator stage. Borderline large (new file + script + stage + tests) but coherent unit. Fits in one implementation plan. ✓
- **Ambiguity check:** "Severity threshold" → explicit numbers + env override. "Stale artifact" → warn-and-use defined. "Missing zeroshot" → skip-and-log defined. ✓

## 12. Out of scope (YAGNI for v1)

Already declared in §1, consolidated here for cross-reference:
- Multi-day rolling z-score / adaptive thresholds
- Sign-based composite signal
- Cross-symbol correlation (one symbol at a time)
- Alerting integrations (Slack/email/etc.)
- Live refresh vs cached read (Approach B is single-source-of-truth; live refresh is Approach C)
- Auto-update of thresholds based on realized PnL

## 13. Open questions / follow-ups

- **Quarterly review of thresholds**: Add a "review thresholds" item to the operator runbook quarterly checklist (companion to the existing champion review).
- **Backtest of severity tiers**: After 30 days of OOS data, simulate the size multiplier's impact on realized PnL to validate threshold choice.
- **Champion path resolution**: Same blocker as Topic 1 T10 — `--champion` name → actual model checkpoint path resolution is needed for `run_shadow_ensemble.sh`. The anomaly spec uses only the shadow JSONs which are by-name, so this is decoupled — but the actual cron invocation that triggers the ensemble still depends on the same resolution.

---

**Spec complete and self-reviewed. Ready for user review and approval.**
