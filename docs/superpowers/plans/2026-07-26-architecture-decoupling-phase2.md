# Architecture Decoupling — Phase 2 (executed)

**Goal:** Remove runtime `pipeline → services` coupling by lifting SR/YOLO DTOs into `schemas`, re-exporting from services for BC, and enforcing import boundaries.

**Status:** Implemented 2026-07-26 on `feature/arch-decouple-p2`.

## Delivered

| Item | Detail |
|------|--------|
| `schemas/signal.py` | SignalRuleConfig/Context/Result + SR-* codes |
| `schemas/yolo.py` | YoloFilterDecision (+ to_dict) |
| services re-export | `signal_rule_engine` / `yolo_filter` re-export same objects |
| pipeline | imports only schemas + local types; entry_window uses `pipeline.types.EntryContext` |
| boundary checker | `pipeline_no_services_adapters_app`, `schemas_no_services_adapters_app` |
| tests | `tests/test_phase2_schema_dtos.py` + SR/YOLO/entry suite green |

## Out of scope (later phases)

- Physical split of remaining services modules into `application/` packages
- PositionStore / JournalStore
- Cage SwingScanner default `ensure_v11` Longbridge path
- Archive deletion from NUC PYTHONPATH
