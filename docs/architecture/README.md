# V12 Architecture Review — Document Set

This directory holds the workflow artifacts for the pre-launch architecture review
of StockApp V12 (US-equities swing-trading system).

## Workflow

We follow the agreed iterative workflow:

**Discovery → Council → Design → Implementation → Test**

Each phase writes its own document under this folder. Phase documents are
append-only inside their own file; cross-cutting changes get a new ADR under
`v12/docs/`.

## Files

| File | Phase | Status | Purpose |
|---|---|---|---|
| [`00-ASSET_INVENTORY.md`](00-ASSET_INVENTORY.md) | 1 Discovery | **Complete (2026-07-02)** | Complete asset map, component table, initial coupling findings, priority queue. § 5/§ 6 superseded by `01-COUNCIL-LOG.md` § 3.4. |
| [`01-COUNCIL-LOG.md`](01-COUNCIL-LOG.md) | 2 Council | **Complete (2026-07-02)** | 4-voice council (Architect / Skeptic / Pragmatist / Critic). Verdict in § 4.4: ship passive `ChronosBackend` + `Notifier` Protocols pre-launch; defer B2–B5. Q1..Q8 resolutions in § 5. |
| [`02-DECOUPLING-PLAN.md`](02-DECOUPLING-PLAN.md) | 3 Design | **Complete (2026-07-02)** | Concrete `v12/contracts/ChronosBackend` + `Notifier` Protocol signatures, determinism baseline test spec (commit 0), per-commit done gates (commits 1–3), risk register. Driven by the council's § 4.4 commit plan. |
| [`03-IMPLEMENTATION-LOG.md`](03-IMPLEMENTATION-LOG.md) | 4 Implementation | **Phase 6 close-gate pending NUC cron 2026-07-02** — commits 0 + 1 + 2 + GATE done (`78cbdf22`, `0ec23b97`, `a1505f0f`, `6e129ab3`) + Phase 7 commits 4+5 (`366a1a74`, `64026077`); awaiting next trading day 16:05/16:10 ET ADR-008 | `64026077` |
| [`04-TEST-PLAN.md`](04-TEST-PLAN.md) | 5 Test | **Complete (2026-07-02)** | 30 Phase-7 tests across 4 files. Commit 4 per-component (16/16) + commit 5 E2E + ADR-008 hit-rate (14/14). Full V12 suite 319 PASS / 6 pre-existing baseline FAIL. | `64026077` |
| [`05-PHASE-CLOSE-OUT.md`](05-PHASE-CLOSE-OUT.md) | Close-out | **Active (2026-07-02)** | § 6.4 acceptance reinterpreted (CI + NUC-system-functional = close; longitudinal ADR-008 = post-launch audit). Day-1 NUC snapshot. Architecture review round 1 closed. | _(this commit)_ |
| [`06-POST-LAUNCH-ROADMAP.md`](06-POST-LAUNCH-ROADMAP.md) | Post-launch | **Active (2026-07-02)** | B2 (eval helpers → v12/evaluation/), B3 (EntryOrchestrator stages), B4 (MarketDataClient Protocol), B5 (Notifier impl upgrade), B6 (config lift), AUDIT-1 (longitudinal ADR-008 @ 2026-08-13). Sequencing: AUDIT-1 → B5 → B2 → opportunistic B3/B4/B6. | _(this commit)_ |
| [`07-DECOUPLING-UPGRADE-BLUEPRINT.md`](07-DECOUPLING-UPGRADE-BLUEPRINT.md) | Decoupling program | **Accepted (2026-07-26)** | Hexagonal modular monolith + Strangler Fig. Index only — full SSOT in [`../superpowers/specs/2026-07-26-architecture-decoupling-upgrade-design.md`](../superpowers/specs/2026-07-26-architecture-decoupling-upgrade-design.md). | — |
| [`08-CODE-REVIEW-2026-07-27.md`](08-CODE-REVIEW-2026-07-27.md) | Planned full-project review | **Wave-1 complete** | Security + architecture + trading path + ops; prioritized backlog P0–P3. | — |
| [`07-IMPORT-BASELINE.md`](07-IMPORT-BASELINE.md) | Phase 0 baseline | **Complete (2026-07-26)** | Archive/sys.path seam inventory (40 hits) + internal import fan-in top-15 + known reverse edges. Measurement baseline for Phase 1 call-site reduction. | — |

## Ground Rules (agreed 2026-07-02)

1. **Iterative commits.** Each phase lands its own commit; reviews happen at
   the seam between phases.
2. **Shadow-run safety.** No change touches the live shadow crons
   (`install_shadow_cron.sh`, `install_shadow_ensemble_cron.sh`) until the
   shadow gate (ADR-008) still passes on NUC.
3. **Invariants preserved.** INV-1..INV-7 and R-01..R-10 cannot drift
   silently — refactors must keep current tests green
   (`tests/test_inv1_*.py`, `tests/test_r05_*.py`, `tests/test_r06_*.py`,
   `tests/test_signal_rules_sr07_*.py`, etc.).
4. **No silent changes to evaluation numbers.** `simulate_pnl`,
   `hit_rate`, `build_test_tsdf_full`, `predict_one_symbol_for_date` must
   produce numerically identical output across refactors (existing
   shadow/hit-rate tests lock this in).
5. **Two paths deprecated at the seam.** Whenever code moves, both the
   old import path and the new one stay valid for one release window
   (re-export shims), removed only after the next phase's doc lands.

## Cross-References

- `v12/docs/SYSTEM_OVERVIEW.md` — master spec (7 INVARIANTS, single-responsibility matrix)
- `v12/docs/TRADING_PIPELINE.md` — Stage 1..7 contract
- `v12/docs/RISK_MANAGEMENT.md` — R-01..R-10 risk rules
- `v12/docs/POSITION_LIFECYCLE.md` — position state machine
- `v12/docs/SCHEDULER.md` — cron layout
- `v12/docs/Shadow-Ensemble-Runbook.md` — shadow gate, ADR-008
- `v12/docs/MIGRATION_GUIDE.md` — V11 → V12 lift history
