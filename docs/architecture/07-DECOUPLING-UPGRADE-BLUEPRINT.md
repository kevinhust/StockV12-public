# Architecture Decoupling Upgrade Blueprint (index)

> **Status:** Accepted 2026-07-26 · **Phase 0–4 code complete** (NUC soak still ops)  
> **SSOT (full design):** [`docs/superpowers/specs/2026-07-26-architecture-decoupling-upgrade-design.md`](../superpowers/specs/2026-07-26-architecture-decoupling-upgrade-design.md)  
> **Phase 3:** Redis store ports — `docs/REDIS_KEY_REGISTRY.md`  
> **Phase 4:** Legacy cage + `STOCKAPP_DISABLE_V11_ARCHIVE` — `docs/NUC_ARCHIVE_FREE_RUNBOOK.md`  
> **Import baseline:** [`docs/architecture/07-IMPORT-BASELINE.md`](./07-IMPORT-BASELINE.md) · **ADR:** [`docs/ADR-011-Hexagonal-Modular-Monolith.md`](../ADR-011-Hexagonal-Modular-Monolith.md)

This file is a short index for the architecture-doc series. The full blueprint lives in the superpowers spec above.

## Decision

**Hexagonal Modular Monolith + Strangler Fig** for V11 archive elimination.  
Microservices rejected for V12 scale/cadence.

## Phase map (summary)

| Phase | Name | Risk to trading | Status (2026-07-26) |
|------:|------|-----------------|---------------------|
| 0 | Measure + import baseline | None | **Done** |
| 1 | Complete ports + composition root | Low | **Done** (incl. Task 11 verification) |
| 2 | DTO lift + pipeline free of `services` | Medium | **Done** 2026-07-26 — `schemas/signal.py`, `schemas/yolo.py`, boundary strict green |
| 3 | Redis behind store ports | Medium | **Done** 2026-07-26 — Position/Journal/EntryEval/Watchlist ports + Redis/InMemory adapters |
| 4 | Kill legacy archive cage | Medium–High | **Code done** + **NUC day-0 soak started** 2026-07-26 — see `NUC_ARCHIVE_FREE_RUNBOOK.md` |
| 5 | Thin scripts / CLI entrypoints | Low | **Done** 2026-07-26 — `app/bootstrap.py`, `app/cli.py`, pyproject console_scripts |

### Phase 0–1 delivered slice

| Item | Location |
|------|----------|
| ADR-011 | `docs/ADR-011-Hexagonal-Modular-Monolith.md` |
| Import baseline + deltas | `docs/architecture/07-IMPORT-BASELINE.md` |
| Boundary checker | `scripts/check_import_boundaries.py` |
| Ports (`Clock`, `BrokerAdapter` re-export, existing MD/Notifier/Chronos) | `src/stockapp_v12/contracts/` |
| Adapters (clock, marketdata Fake/Composite, legacy Chronos) | `src/stockapp_v12/adapters/` |
| Composition root | `src/stockapp_v12/app/wiring.py` |
| Thin consumer (no network) | `scripts/demo_wiring_runtime.py` |
| MODULE_MAP M16/M17 | `docs/MODULE_MAP.md` |

Remaining for Phase 1 **exit**: Task 11 verification gate (targeted pytest + grep gates).  
Known residual: `SwingScanner` default Longbridge seam still uses `ensure_v11` (inject path is clean).

## Program “done”

Replace backends via wiring only · no prod `archive/` on PYTHONPATH · import direction enforced · unit tests without Redis · shadow/paper invariants preserved.

## Next

1. ~~Approve the SSOT design~~  
2. ~~Implementation plan for Phase 0–1 only~~  
3. ~~ADR-011~~  
4. **Task 11** — Phase 0–1 verification gate (suite + grep)  
5. Phase 2 plan — split fat `services/` (move+shim)  
