# Data Integrity Checks — StockApp-V12

> **Purpose:** Canonical reference for the executable data-integrity
> checks that guard against the three bugs that broke the Day-30
> walk-forward gate in 2026-07-01.
> **Author:** Grok (Council-aligned)
> **Date:** 2026-07-01
> **Status:** ✅ Deployed (round 3 council, post-mortem).
> **Related docs:** [ADR-009-WalkForward-Backfill-Addendum.md](ADR-009-WalkForward-Backfill-Addendum.md) ·
> [V3_LESSONS_AND_EXP_PLAN.md](V3_LESSONS_AND_EXP_PLAN.md)

---

## Why this doc exists

The original 30-day walk-forward backfill failed the Day-30 promotion
gate because of three bugs that compounded:

| Bug  | Class   | What went wrong                                                          | Outcome                                                |
|------|---------|--------------------------------------------------------------------------|--------------------------------------------------------|
| #1   | code    | eval_date slicing collapsed 30 dates to 1 identical row                  | gate passed `n_trades=30` on 30 copies                 |
| #2   | data    | Phase 1 2026 H1 had a 6-month silent gap                                 | walk-forward trained on gappy data, gate meaningless   |
| #3   | code    | `write_partitioned_parquet` overwrites partitions on every cron run      | daily cron was about to wipe 6 months of history     |

Two of those bugs (#1 and #3) were the gate/code layer, and ECC has
the right tools to catch them — `silent-failure-hunter`,
`verification-before-completion`, `tdd-workflow`. They were not
auto-triggered because they're skill-level, not project-rule-level,
and our `.claude/rules.md` did not invoke them.

This doc describes the **executable checks** we deployed so the bugs
recur at our peril, not our operators'.

**Council verdict (2026-07-01, four-voice):** ship executable-only,
drop free-floating prose rules. The Critic's strongest dissent was
that none of these address schema/contract drift (renamed fields,
USD→cents, type-coerce). We acknowledge but defer to a future
round.

---

## The four deployed checks

### Check 1 — `scripts/verify_data_integrity.py` (canary)

**What it does:** scans `data/daily_clean/phase1/year=*/symbol=*`
partitions and reports:

- **Partition row-count floor** per symbol (min `MIN_ROWS_PER_SYMBOL`,
  default 1000)
- **Date contiguity** (max gap ≤ `MAX_GAP_DAYS`, default 90)
- **Year coverage** (≥ `MIN_YEARS_COVERED`, default 4)
- **Distinct timestamps** (no duplicates)
- **Feature freshness** (`data/features/daily/*.parquet` mtime ≤
  `FEATURE_FRESHNESS_HOURS`, default 36) — *optional, can be skipped*

**Catches:** Bug #2 (silent data gap) **retroactively** — runs
against the on-disk state, so even a 6-month-old gap is found by
the next invocation.

**Exit codes:**
- `0` — all checks PASS
- `1` — at least one symbol/feature failed
- `2` — missing data dir (operator error)

**Usage:**

```bash
# Standard run, full report:
PYTHONPATH=v12/src:src:. python3 scripts/verify_data_integrity.py

# CI terse mode (single line, exits with PASS/FAIL):
python3 scripts/verify_data_integrity.py --ci

# Skip feature freshness (when features tree is intentionally missing):
python3 scripts/verify_data_integrity.py --skip-feature-freshness

# Debug — only check the first symbol (faster):
python3 scripts/verify_data_integrity.py --first-symbol-only

# Pre-walk-forward-gate pre-flight:
python3 scripts/verify_data_integrity.py --ci || {
  echo "FATAL: data integrity failed; do not run walk-forward"
  exit 1
}
```

**Wired-in:**
- Manual pre-gate invocation (operator runs before `backfill_30_days.py`)
- Available for cron use (would slot into pre-shadow-cron pre-flight)

---

### Check 2 — `check_shadow_gate` distinct-eval_dates assertion

**Where:** `v12/scripts/shadow_trade.py::check_shadow_gate`, line ~432.

**What it does:** adds a 5th gate condition:

```python
# 5. Distinct eval_dates — Bug #1 class guard.
if n_dates < min_n:
    reasons.append(
        f"n_dates {n_dates} < {min_n} "
        f"(distinct eval_dates — Bug #1 guard; see ADR-009 addendum)"
    )
```

`n_dates` is the cardinality of the `eval_date` set inside the
aggregate. If eval-date slicing is wrong (Bug #1), all 30 "different"
dates collapse to 1 real date, and `n_dates=1` fails the assertion.

**Catches:** Bug #1 class — mechanical pass on identical rows is now
impossible. Runs **every time** `check_shadow_gate` is called.

**No manual action required** — it's a code assertion in the gate.

**Why this wasn't there before:** the original gate checked
`n_trades ≥ 30` but never asked whether those 30 trades came from
30 distinct dates. The mechanical check was satisfied; the semantic
check was not.

---

### Check 3 — `v12/tests/test_writer_contract.py` (regression suite)

**What it does:** TDD-style regression for incremental writers.
Five tests cover the contract an incremental writer MUST satisfy:

| Test                                            | Invariant                                              |
|-------------------------------------------------|--------------------------------------------------------|
| `test_two_writes_merge_not_overwrite`           | 30 + 10 days → 40 rows in partition (not 10)          |
| `test_overlapping_dates_dedupe_keep_last`       | Same 10 days written twice → 10 rows (deduped)        |
| `test_empty_second_write_does_not_destroy_first`| Empty 2nd write does not destroy the 1st              |
| `test_multi_year_writes_create_year_partitions` | Multi-year fixture → one partition per year           |
| `test_write_twice_same_data_is_idempotent`      | Same data twice → same row count                      |

**Catches:** Bug #3 class — silent-overwrite regression.

If any future writer modifies `write_partitioned_parquet` (or adds a
new incremental writer) and regresses the contract, pytest fails.

**Run:** `pytest v12/tests/test_writer_contract.py -v`

**Why this was missing:** the original implementation's docstring
*claimed* it was incremental-safe but had no test pinning that claim.
Now the contract is enforced.

---

### Check 4 — `install_polygon_cron.sh` first-install DRY_RUN gate

**Where:** `scripts/install_polygon_cron.sh`, lines 38-115.

**What it does:** refuses to install the polygon cron for the first
time unless a sentinel file `~/.cache/stockapp/polygon_cron_dry_run_*`
exists, which is created only after the operator runs
`DRY_RUN=1 /home/trainer/Stockapp/scripts/run_polygon_ingest_cron.sh`
once and confirms the output looks sane.

**Override:** `INSTALL_FORCE=1 /home/trainer/Stockapp/scripts/install_polygon_cron.sh`
skips the gate (only if you've audited the wrapper).

**Catches:** Bug #3 class — destructive cron silent failure on first
deploy.

**Why this matters:** when the polygon cron was first deployed, the
fetcher was overwriting partitions. If that bug had not been caught
*that afternoon*, every nightly run starting that evening would have
wiped history progressively.

**Idempotent:** re-running the installer after install is a no-op
(grep for the existing line exits 0).

---

## What this doc does NOT cover

### Bug-class gaps (intentional, documented for future work)

1. **Schema/contract drift.** None of the four checks catches:
   - A field renamed (`adj_close` → `adjusted_close`)
   - A unit conversion (`USD` → `cents`, off-by-100x)
   - A join key silent type-coercion (str vs int)
   - A format change (RFC3339 → epoch) that's still "valid" syntactically

   The Critic on the council specifically called this out as the next
   unaddressed bug class. **Action:** add a `tools/contract_diff.py`
   or expand `verify_data_integrity.py` with a schema-row check.

2. **GPU pipeline silent corruption.** AutoGluon Chronos checkpoints
   can be persisted without obvious breakage but produce garbage
   inference. We do not have a periodic checkpoint-output-sanity
   check.

3. **Order/policy drift.** Trading rules (`RISK_MANAGEMENT.md`) can
   change in `v12/scripts/sr01.py` etc. without a parallel check on
   the running live config. **Action:** unit test that asserts each
   `config/*.yaml` value matches its constant in `shadow_trade.py`
   (currently a manual sync).

### Why we didn't ship a rules.md update for these

The council's strongest dissent was that `.claude/rules.md` updates
without executable anchors rot fast — operators don't re-read them,
LLM-driven agents grep them but don't enforce them. So we ship the
checks as code, not as prose, and put a thin banner in `.claude/rules.md`
pointing operators at THIS document.

If you want a `<process>` step added, write it as code first.

---

## How to extend

Each of the four checks has a deliberate surface for extension:

1. **Canary thresholds** — every threshold has both a CLI flag and
   a script-level constant. To harden, lower thresholds. To skip a
   noisy check on a known-bad symbol, pass `--symbols` overrides.
2. **Gate assertion** — `check_shadow_gate` is structured as
   numbered conditions; new conditions slot in at #6, #7, etc.
   Add the condition in `shadow_trade.py` AND a corresponding test
   in `v12/tests/test_shadow_trade.py`.
3. **Writer tests** — `test_writer_contract.py` is a pytest module;
   copy any test and modify for a new writer. The TDD rule is: write
   the test first, run it (red), then implement the writer (green).
4. **DRY_RUN gate** — when adding a new destructive cron, copy
   `install_polygon_cron.sh`, replace the wrapper path, set your own
   sentinel name pattern.

If you find a new bug class not covered here, add it as code, not
as a rule. The doc gets a brief update; the executable is what counts.

---

## Data repair workflow

When `verify_data_integrity.py` returns FAIL on a symbol's raw parquet,
the canonical repair loop is:

```bash
# Step 1: detect + repair raw data (auto-detects gaps; use --gap-ticker
#         for symbols that had historical name changes like META/FB)
python3 scripts/repair_data_gap.py --symbol SYM [--gap-ticker ALIAS]

# Step 2: REGENERATE FEATURE parquets for the repaired symbol(s).
#          repair_data_gap.py only writes raw data — feature parquets
#          must be re-derived or the model trains on stale features.
PYTHONPATH=v12/src:src:. python3 v12/scripts/feature_engineering.py \
    --freq daily --phase 1 --symbols SYM1,SYM2,...

# Step 3: re-run canary to confirm both raw and feature layers are clean.
python3 scripts/verify_data_integrity.py --ci
# If FAIL: investigate, fix, repeat. Don't claim "data is fine".
# If PASS: data is genuinely fine — proceed to gate / sweep.
```

**Why this is two phases, not one:** `repair_data_gap.py` only writes
to `data/daily_clean/phase1/year=YYYY/symbol=SYM/data.parquet`. The
`data/features/daily/{SYM}.parquet` consumption layer is independent
and is regenerated by `v12/scripts/feature_engineering.py`. Skipping
Step 2 makes the canary `feature_freshness` check pass on mtime alone,
while the underlying feature rows are still based on the gappy raw
input. **META, 2026-07-01:** first repair pass had META features at
1162 rows with 0 in the gap window; only after Step 2 did META
features reach 1251 rows.

**Ticker-alias handling:** Polygon stores some symbols under their
historical ticker (e.g., META was FB on Polygon during 2022). Use
`--gap-ticker` to fetch the alias range and merge under the canonical
symbol — the repair script re-stamps the `symbol` column to the
canonical name so the resulting partition is uniformly queryable.

**Idempotence:** all steps are idempotent. The Bug #3 fix in
`write_partitioned_parquet` ensures incremental merges don't re-write
the full year on every repair. Re-running `repair_data_gap.py` against
a clean symbol is a no-op.

---

## Operational cadence

| When                                | Run                                                      |
|-------------------------------------|----------------------------------------------------------|
| Before `backfill_30_days.py`        | `scripts/verify_data_integrity.py --ci`                   |
| Before `check_shadow_gate`          | (already wired in — no separate run)                     |
| Before adding any new destructive cron | `scripts/install_*_cron.sh` (sentinel gate enforces)   |
| After ANY writer code change        | `pytest v12/tests/test_writer_contract.py -v`            |
| Weekly (manual)                     | All four above, full output                              |

---

## References

- `scripts/verify_data_integrity.py` — Check 1
- `v12/scripts/shadow_trade.py::check_shadow_gate` — Check 2
- `v12/tests/test_writer_contract.py` — Check 3
- `scripts/install_polygon_cron.sh` (DRY_RUN gate block) — Check 4
- `.claude/rules.md` — thin banner pointing here
