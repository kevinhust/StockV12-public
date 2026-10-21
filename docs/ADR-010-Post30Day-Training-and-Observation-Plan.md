# ADR-010 — Post–30-Day Training Plan & Observation Portfolio

> **Purpose:** Decide what happens *after* the ADR-008 Day-30 OOS evidence bar
> is cleared: training cadence, what is *not* trained, observation set vs
> decision path, and promotion gates for the next 30–90 trading days.
> **Author:** Grok (Council-aligned — Architect / Skeptic / Pragmatist / Critic)
> **Date:** 2026-07-26
> **Status:** ✅ **Accepted** (2026-07-26) — user approved; implementation checklist unlocked.
> **Gate mapping (2026-07-26):** Trading-first layers in [GATE_DECISION_ARCHITECTURE.md](GATE_DECISION_ARCHITECTURE.md) (**Accepted**). ADR-010 is **not superseded**; D3 promotion pack maps to **L2 Path-change**; daily trading uses **L0+L1** only; H2H/Stage-1/7-gate are **D Diagnostic**.
> **Related docs:**
> [V3_POSTMORTEM_AND_TRAINING_PLAN.md](V3_POSTMORTEM_AND_TRAINING_PLAN.md) ·
> [V3_LESSONS_AND_EXP_PLAN.md](V3_LESSONS_AND_EXP_PLAN.md) (ADR-008 home) ·
> [ADR-009-WalkForward-Backfill-Addendum.md](ADR-009-WalkForward-Backfill-Addendum.md) ·
> [superpowers/specs/2026-07-10-champion-model-selection-design.md](superpowers/specs/2026-07-10-champion-model-selection-design.md) ·
> ROI review session 2026-07-26 (shadow SUMMARY through 2026-07-24)

---

## Context

### Where we are

| Fact | Evidence |
|---|---|
| Day-30 bar is already cleared for main models | ~40–46 distinct shadow dates for phase2_v2 / E05_seed42 / E06–E10 family |
| Ensemble OOS still thin | A+B ~18 dates (below 30 for full ensemble gate comfort) |
| v3-style long retrain is banned | v3 1M-step: IS MASE↑, OOS MASE↓ (postmortem 2026-06-29) |
| Hyperparam zoo exists | E05 seeds, E06–E10, zeroshot — Round 3 multi-seed + Round 4 halt |
| NUC outage gap repaired | 2026-07-20..07-24 walk-forward backfill; daily features via Polygon to 07-24 |
| Portfolio trimmed 2026-07-26 | zeroshot off daily cron; halt skips seed17/23/71 + zeroshot |

### Cumulative vs recent (sim PnL, thr=0.5%, as of 2026-07-24)

| Model | n_dates | Cum PnL% | Recent 10d PnL% | Gate | Role after this ADR |
|---|---:|---:|---:|---|---|
| E07_long_ctx | 40 | +263 | **−33** | PASS | Observation only — **not** promotion template |
| zeroshot_bolt_small | 40 | +236 | +47 | **FAIL** MASE | Weekly ref only — off daily |
| E09_higher_lr | 40 | +169 | +21 | PASS | Observation / optional ensemble B candidate |
| E08_fewer_steps | 40 | +164 | +2 | PASS | Observation / optional ensemble B candidate |
| E05_seed7 | 40 | +149 | +2 | PASS | Observation |
| E10_more_steps | 40 | +115 | +31 | PASS | Observation |
| E06_short_ctx | 40 | +42 | +37 | PASS | Observation |
| phase2_v2 | 46 | +28 | +34 | PASS | **Baseline / champion default** |
| E05_seed42 | 46 | +26 | +34 | PASS | **Ensemble B / challenger default** |
| Ensemble A+B | 18 | +38 | — | — | **Paper decision path** (hit≈0.61, MASE≈1.73, sparse trades) |

### Industry practice (compressed)

- Quant: **monthly** (or drift-triggered) walk-forward retrain; rolling train/test; not continuous online FT for this stack.
- Production ML: **champion–challenger** + monitoring; separate exploration from confirmation.
- Chronos / AutoGluon: zero-shot usable; fine-tune only with enough series + history; prefer gated FT and zero-shot+FT ensembles over unbounded steps.
- Anti-pattern: treat short-window sim PnL or in-sample MASE as the training objective (v3 + E07 recent collapse).

### Decision drivers

1. Day-30 is a **floor**, not a license to open a retrain festival.
2. E07 proves **cumulative glory ≠ live stability**.
3. Highest near-term ROI is **evaluation discipline + data continuity**, not a new hyperparam family.
4. GPU is available but must stay **single-slot, same-recipe** for any retrain.

---

## Decision

### D1 — Decision path vs observation path (split)

| Path | Models | Writes shadow? | Drives paper signal? |
|---|---|---|---|
| **Decision** | Ensemble A+B = `phase2_v2` + `E05_p3_coldstart_seed42` | Yes | **Yes** (default paper path) |
| **Champion / Challenger roles** | Champion=`phase2_v2`, Challenger=`E05_seed42` | Yes | Roles for HaltGate swap logic only |
| **Observation (daily)** | + E05_seed7, E06, **E07**, E08, E09, E10 | Yes | **No** |
| **Archived / skip daily** | zeroshot; E05 seed17/23/71 | No (halt skip) | No |

**Invariant:** Exactly one paper decision path at a time (ensemble A+B unless human overrides). Observation models never auto-promote.

### D2 — What we will **not** train (hard bans, 30–90d)

| Ban | Why |
|---|---|
| v3-style long fine-tune (≫ phase2_v2 step budget) | Proven OOS overfit |
| New hyperparam family sweep (E11+ style) | Diminishing returns; rank noise at n≈40 |
| Promote/train “like E07” because of cum PnL | Recent 10d collapse = selection-bias template |
| zeroshot back into daily promotion path | Gate FAIL (MASE) |
| Continuous / online fine-tune without ADR-008 | Industry + our stack: no gate → uninterpretable |
| Fabricating bars or silent gap fill | Data integrity (ADR-009 lessons) |

### D3 — What we **may** train (single retrain slot)

**Cadence:** at most **one** new fine-tune checkpoint per ~20–22 trading days (≈ monthly), **or** on a pre-defined drift trigger (below)—whichever comes first, not both stacked without human review.

**Recipe:** **same family as phase2_v2** (Chronos-Bolt fine-tune hyperparameters locked to the last promoted baseline recipe). Output name pattern: `phase2_vN` (e.g. `phase2_v3` only if recipe differs in *data cutoff*, not “1M steps”).

**Walk-forward:** train only on data with `timestamp ≤ retrain_cutoff` (trading day). Never peek post-cutoff actuals during fit.

**Promotion of `phase2_vN` into challenger or ensemble B requires all of:**

1. **ADR-008** walk-forward gate PASS on accumulated shadow (or H2H-equivalent panel).
2. **Recent window:** last 20 trading days sim metrics not worse than `phase2_v2` on the same symbols/threshold (hit_rate and total PnL%; ties OK).
3. **Sample size:** `n_trades` in that 20d window ≥ floor (default **15**; configurable; if unmet → “insufficient evidence”, no promote).
4. **HaltGate:** no worse 3d rolling halt rate than baseline over the same window (paper log only).
5. **Human approval** — no auto-promote (matches champion design non-goal).

### D4 — Non-training experiments (preferred portfolio)

Run in parallel with D3; most calendar time goes here.

| ID | Experiment | Effort | Exit / success |
|---|---|---|---|
| **E-EVAL** | Multi-window report: full / 10d / 20d; rank only if n_trades ≥ floor | Low | Automated weekly artifact under `models/chronos/_shadow/` or `docs/reviews/` |
| **E-REGIME** | Slice shadow by vol regime / trend vs range (simple rules, no new model) | Low | Doc: which models fail which regime (E07 autopsy first) |
| **E-SEAT** | Side-car ensemble logs: `phase2_v2+E09` and/or `phase2_v2+E08` vs A+B (no trade impact) | Med | ≥15–20 days parallel; promote seat only if better hit + not worse fallback/spread |
| **E-DATA** | Longbridge primary + Polygon fallback for daily features; gap alert if last bar &gt; 1 trading day stale | Med | Zero silent multi-day feature gaps on NUC |
| **E-FT** | Monthly same-recipe WFO retrain → `phase2_vN` (D3) | Med–High | Gate pack in D3; else archive checkpoint |

**Deferred (explicitly out of 30–90d scope):** Chronos-2 full swap, large universe retrain, YOLO coupling changes, live capital.

### D5 — Observation set (cron) — ratified 2026-07-26

Daily `MODELS_CSV` (8):

```
phase2_v2
E05_p3_coldstart_seed42
E05_p3_coldstart_seed7
E06_short_ctx
E07_long_ctx
E08_fewer_steps
E09_higher_lr
E10_more_steps
```

Halt allowlist = above + `ensemble`.  
Skip: `zeroshot_bolt_small`, `E05_p3_coldstart_seed17|23|71`.

Ensemble default unchanged:

```
phase2_v2 : E05_p3_coldstart_seed42
```

---

## Drift triggers (optional retrain *before* monthly slot)

Any one may open the D3 slot early (still max one active challenger train at a time):

1. Ensemble or champion **HaltGate** `3d_40_reactive` would-halt ≥ 3 sessions in 10 trading days (paper).
2. Rolling 20d ensemble **hit_rate** &lt; 0.45 **and** n_trades ≥ floor.
3. Feature **data gap** &gt; 2 trading days on ≥ 3 of 10 shadow symbols (must repair data first; retrain only after features healthy).

Non-triggers: single bad day; cum PnL rank change alone; E07 reclaiming cum #1.

---

## Consequences

### Positive

- Stops GPU spend on low-signal hyperparam children.
- Aligns with quant monthly WFO + champion–challenger practice.
- Preserves shadow history and comparability (one decision path).
- Forces multi-window + trade-count floors so E07-class myths don’t re-enter.

### Negative / costs

- May delay a true “better backbone” (Chronos-2) by a quarter.
- Same-recipe monthly FT might under-explore if phase2_v2 recipe is locally suboptimal.
- Ensemble still thin (~18d); decision path risk until n_dates ≥ 30.

### Mitigations

- E-SEAT explores E08/E09 without killing A+B.
- E-EVAL makes under-exploration visible (if E09 dominates 3 consecutive 20d windows → human review to open a *one-shot* recipe delta, still not a sweep).
- Ensemble n_dates target: keep daily A+B until **≥30** dates before any Tier-2 narrative.

---

## Implementation checklist (after status → Accepted)

- [x] Mark this ADR **Accepted** and link from `docs/README.md` + Obsidian master-plan / `04-decisions/`.
- [x] Confirm NUC cron/halt allowlist match D5 (done 2026-07-26; re-verify after any deploy).
- [x] Add multi-window + n_trades floor report (E-EVAL) — `scripts/champion_evaluation.py --multi-window` (**4.6.2**, 2026-07-26).
- [x] E-DATA: `scripts/check_feature_freshness.py` + soft preflight in `run_shadow_cron.sh` (**4.6.3**, 2026-07-26).
- [x] E-SEAT: `--seat` / `ensemble_seat_*` dir + `run_shadow_ensemble_seat.sh` + `config/ensemble_seats.yaml` (**4.6.4**, 2026-07-26).
- [x] Document phase2_v2 **frozen hyperparam recipe** YAML — `config/chronos_phase2_v2_recipe.yaml` (**4.6.5**); **verified 2026-07-26 on GPU** against `train_meta.json` (steps=1000, lr=1e-5, batch=32, ctx=512).
- [x] First D3 retrain run: `phase2_wf_20260724` on GPU (cutoff 2026-07-24, recipe-matched, ~82s) — **observation only**, not promoted; human approval still required before any decision-path swap.
- [x] Do **not** auto-edit champion to E07/zeroshot; champion defaults remain phase2_v2 / E05_seed42.

---

## Council record (2026-07-26)

| Voice | Position |
|---|---|
| **Architect** | Gated challenger ops + eval hardening; not a retrain festival. D1 base + D2 single monthly slot + D3/D4 light parallel. |
| **Skeptic** | Prefer almost no training; E07 is a warning; fix evaluation/decision layer before weight matrices. |
| **Pragmatist** | Keep A+B; monthly same-recipe WFO; E08/E09 seat trials; data path &gt; new GPU zoo. |
| **Critic** | Selection bias under “evidence”; sparse trades flip rankings; process/data kill experiments first. |

**Consensus:** Ban v3-style and E07-as-template; eval + data first; any train = monthly same-recipe + gates.  
**Strongest dissent:** Skeptic (zero retrain) vs Pragmatist (three thin workstreams).  
**Verdict adopted in this ADR:** balanced portfolio — **E-EVAL + E-DATA + E-SEAT + single D3 slot**; no D5 backbone swap in window.

---

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Open-ended 90d training portfolio (many FT variants) | Rank noise; v3/E07 failure modes |
| Promote E07 now | Recent collapse; violates multi-window rule |
| Restore zeroshot to daily | MASE gate FAIL |
| Immediate Chronos-2 migration | High cost, unproven on our shadow contract |
| Freeze all models, no monthly slot | Under-explores data drift; industry monthly WFO still valuable as *one* challenger |

---

## References

- QuantInsti / IBKR / ML-for-Trading: walk-forward optimization, champion–challenger, retrain cadence.
- AutoGluon Chronos / Chronos-Bolt docs: fine-tune conditions; zero-shot + fine-tune ensemble presets.
- Internal: v3 postmortem; ADR-008 gate; ADR-009 backfill bugs; Round 3 multi-seed; Round 4 HaltGate; 2026-07-26 ROI + portfolio trim.

---

## Status transitions

| Status | Meaning |
|---|---|
| **DRAFT** | Discuss / edit |
| **Accepted** | User approved (this status as of 2026-07-26); implementation checklist unlocked |
| **Active** | D3/E-* work underway |
| **Superseded** | Replaced by later ADR (link here) |

**Approval:** ✅ Accepted by user 2026-07-26
