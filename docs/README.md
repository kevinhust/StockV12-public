# StockApp V12 Documentation

Normative spec for the swing-trading rewrite. Read in this order:

1. **[SYSTEM_OVERVIEW.md](SYSTEM_OVERVIEW.md)** — Goals, boundaries, module ownership, policies
2. **[TRADING_PIPELINE.md](TRADING_PIPELINE.md)** — Stage I/O, thresholds, pipeline parameters
3. **[RISK_MANAGEMENT.md](RISK_MANAGEMENT.md)** — Red lines, sizing, risk parameter registry
4. **[POSITION_LIFECYCLE.md](POSITION_LIFECYCLE.md)** — State machine, Redis contract, review matrix
5. **[SCHEDULER.md](SCHEDULER.md)** — Job schedule, failures, half-days
6. **[DEPLOYMENT.md](DEPLOYMENT.md)** — Production host topology (NUC vs Mac mini vs GPU box)
7. **[PRODUCTION_CAPABILITIES.md](PRODUCTION_CAPABILITIES.md)** — Honest matrix: what cron really does vs stubs
8. **[MIGRATION_GUIDE.md](MIGRATION_GUIDE.md)** — Phases, file map, tests, rollback

**Changelog**: [../CHANGELOG.md](../CHANGELOG.md) — code-level change log
**Module map**: [MODULE_MAP.md](MODULE_MAP.md) — directory-to-module index
**Recent council review** (2026-07-09 → 2026-07-10): [reviews/council-review-2026-07-09/](reviews/council-review-2026-07-09/) — Sprints F/G/H/I/J/K completion records

### Architecture Decision Records (ADRs)

| ADR | Status | Summary |
|---|---|---|
| [ADR-009 — Walk-Forward Backfill Addendum](ADR-009-WalkForward-Backfill-Addendum.md) | ✅ | Day-30 backfill bugs + data integrity |
| [ADR-010 — Post–30-Day Training & Observation Plan](ADR-010-Post30Day-Training-and-Observation-Plan.md) | ✅ Accepted 2026-07-26 | Decision vs observation paths; monthly same-recipe WFO slot; bans (v3-style / E07 template / zeroshot daily); E-EVAL / E-DATA / E-SEAT portfolio |
| [Gate Decision Architecture](GATE_DECISION_ARCHITECTURE.md) | ✅ Accepted 2026-07-26 | Trading-first gates: **L0 Capital · L1 Session(Halt) · L2 Path-change+Human · D Diagnostic**; does not overturn ADR-010 |

Some ADRs (e.g. ADR-008) are maintained in a private knowledge base and are only summarized here.

ADR-008 (walk-forward promotion gate) lives in Obsidian + is summarized in [V3_LESSONS_AND_EXP_PLAN.md](V3_LESSONS_AND_EXP_PLAN.md).

Reference (non-normative): [_reference/INDUSTRY_REFERENCES.md](_reference/INDUSTRY_REFERENCES.md)

V11 code snapshot (slimmed reference): [../legacy/v11/](../legacy/v11/README.md) · Architecture history: [ARCHITECTURE_EVOLUTION.md](ARCHITECTURE_EVOLUTION.md)

V12 implementation: `src/stockapp_v12/` (see [../README.md](../README.md))
