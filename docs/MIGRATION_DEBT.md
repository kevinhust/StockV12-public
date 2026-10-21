# V11 → V12 Migration Debt Register

> **Created:** 2026-07-09 (post code-review)
> **Purpose:** Track all known migration debts that were deferred during
> the V11→V12 migration. Each item includes severity, status, and the
> PR that addressed it (or "OPEN" if pending).

## Resolved Items

| ID | Severity | Description | Resolved By |
|----|----------|-------------|-------------|
| MD-01 | CRITICAL | TradeJournal.append used non-atomic GET/SET read-modify-write | PR-A (RPUSH) |
| MD-02 | CRITICAL | AuditLedger proxy \_\_getattr\_\_ proxied all methods (unsafe whitelist) | PR-A (KNOWN_JOURNAL_METHODS) |
| MD-03 | HIGH | Duplicate close_for_review in PositionManager (shadowed first def) | PR-B (B1) |
| MD-04 | MEDIUM | swing_scanner.py called ensure_v11_archive_on_path at module top-level | PR-B (B2) |
| MD-05 | LOW | LongbridgeScreener stub status not documented at class level | PR-B (B3) |
| MD-06 | MEDIUM | _record_veto swallowed exceptions with bare except:pass | PR-B (B4) |
| MD-07 | MEDIUM | Double sr.evaluate call (stage_4 + orchestrator re-eval) | PR-B (B5) |
| MD-08 | LOW | Magic numbers 0.75/0.05 inline in stages.py | PR-B (B6) |
| MD-09 | HIGH | WebhookNotifier blocked caller thread on HTTP POST | PR-C (C1, ThreadPoolExecutor) |
| MD-10 | HIGH | No SSRF defense on webhook URL (private IP reachable) | PR-C (C2, _is_safe_webhook_url) |
| MD-11 | MEDIUM | LogNotifier logged raw kwargs (potential secret leakage) | PR-C (C3, _redact_kwargs) |
| MD-12 | MEDIUM | test_writer_contract.py referenced non-existent scripts/ path | PR-D (D1, archive fallback) |
| MD-13 | MEDIUM | test_daily_review_chain used apply_immediately=True for pending test | PR-D (D3) |
| MD-14 | LOW | MIGRATION_GUIDE.md paths reference V11 locations without clarification | PR-D (D4) |
| MD-15 | LOW | 00-ASSET_INVENTORY.md not marked superseded | PR-D (D5) |

## Open Items

| ID | Severity | Description | Notes |
|----|----------|-------------|-------|
| MD-16 | LOW | Scripts docstrings reference V11 paths | D6 — deferred (scripts are archived, low impact) |
| MD-17 | LOW | eval_date guard missing in paper-soak sign agreement | D7 — related to pre-existing test failures |
| MD-18 | MEDIUM | conftest.py V11 sys.path injection is unscoped | D8 — affects test isolation |

## Pre-Existing Test Failures (not caused by migration)

These failures exist in the V12 codebase independent of migration debt.
They are tracked here for visibility but require separate fixes:

- `test_fail_closed_safety.py::test_forecast_service_chronos_init_failure_logs_warning`
- `test_live_guard.py::test_min_hold_blocks_early_exit`
- `test_paper_soak_sign_agreement.py` (4 tests — sign agreement logic)
- `test_shadow_ensemble.py::test_constants_match_ensemble_backend`
- `test_shadow_trade.py` (4 tests — shadow gate logic)
