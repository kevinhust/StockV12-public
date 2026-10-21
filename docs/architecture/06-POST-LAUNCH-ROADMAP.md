# Post-Launch Roadmap (2026-07-02 → TBD)

**Status:** B2-B6 ✅ DONE (2026-07-08, commits `d391d1c3`–`c48fb66e`). Hygiene audit A–G ✅ DONE (2026-07-09, commits `6d5a2bf8`–`3b42a298`). Hygiene H–J ✅ DONE (2026-07-09, commits `dcbd176d`–`29dbc840`). Awaiting AUDIT-1 (~2026-08-13).
**Source:** Council verdict § 4.4 — B2/B3/B4/B5/B6 all defer post-launch
**Trigger:** Architecture review round 1 closed (see `05-PHASE-CLOSE-OUT.md`)

---

## Backlog (Council § 3.4 + § 4.4)

| ID | Item | Council risk | Priority | Owner | Status | Commit |
|---|---|---|---|---|---|---|
| **B2** | Move `simulate_pnl` / `hit_rate` / `build_test_tsdf_full` / `predict_one_symbol_for_date` → `v12/evaluation/` | Module identity drift; implicit globals; lazy re-import side-effects (per Critic) | **P1** — first post-launch refactor | user | ✅ DONE | `d391d1c3` |
| **B3** | `EntryOrchestrator` stage chain (decompose C5 god-object) | Internal refactor; zero ADR-008 impact (per Council) | P2 | user | ✅ DONE | `ec5aa2f1` |
| **B4** | `MarketDataClient` Protocol (SwingScanner seam, C2) | Internal refactor; zero ADR-008 impact | P2 | user | ✅ DONE | `1ecb9dd4` |
| **B5** | Notifier impl upgrade (current `_Notifier` → Redis pubsub or webhook) | Operational alerting quality | P2 | user | ✅ DONE | `ee1cb32f` |
| **B6** | Config lift (consolidate `config/` + `configs/` + env vars) | Path-walk harmless; not blocking | P3 | user | ✅ DONE | `c48fb66e` |
| **Hygiene A–G** | Code-hygiene audit (dead code, dedup, fail-open → fail-closed, WARNING logs, coverage lift) | Read-only audit + surgical fix | P2 | user | ✅ DONE | `6d5a2bf8`–`3b42a298` |
| **Hygiene H–J** | Doc sync + Hygiene-A cron regression scrub + Stage 3 re-architect (PRE-MARKET → PRE-EVAL caller-side) | Doc drift + zombie cron at 08:30 ET + spec/impl mismatch | P2 | user | ✅ DONE | `dcbd176d`–`29dbc840` |
| **AUDIT-1** | Longitudinal ADR-008 audit (30-day accumulated mase / PnL / win_rate per model) | Day-1 ADR-008 cannot pass; first meaningful reading = ~30 trading days | **P1** | user | 🕒 Pending | n/a (target 2026-08-13) |

### Hygiene A–G landing summary (2026-07-09)
- **A** `6d5a2bf8` — deleted dead `services/premarket_review.py` (116 lines, zero callers)
- **B** `cadd6d99` — removed 8 dead imports (B3 leftover) + `v12_risk_enabled` alias
- **C** `351ddc40` — dedup'd quantile extraction into `eval/quantiles.py` (eval/predict + ensemble backend share)
- **D** `0a66de4a` — **4 HIGH fail-open safety boundaries → fail-closed** (gatekeeper R-05 cap, limits YAML, forecast Chronos init, veto_alarm is_alarmed) + 9 regression tests
- **EF** `8e201d80` — 5 MEDIUM silent excepts → WARNING + 3 DEBUG → WARNING in swing_scanner
- **G** `3b42a298` — coverage lift: `paper_broker.py` 39%→100%, `stages.py` 67%→75%, total **83%→84%**

### Hygiene H–J landing summary (2026-07-09)
- **H** `dcbd176d` — sync `06-POST-LAUNCH-ROADMAP.md` with B2-B6 + hygiene A-G landing; Status header + Backlog table + hygiene summary updated
- **I** `e737f541` — **Hygiene-A cron regression scrub**: deleted `run_premarket_review.sh` (would `ModuleNotFoundError` at 08:30 ET); updated `install_pipeline_cron.sh` (5→4 daily jobs); scrubbed `SCHEDULER.md` + `00-ASSET_INVENTORY.md` Redis ownership + `v12_deployment_health.py` health-check + `backtest_v12_paper.py` TODO. Live trading path unaffected (nothing reads `actionable:today` for decisions; `signal_rule_engine` uses in-process `ctx.actionable_symbols`).
- **J** `29dbc840` — **Stage 3 re-architect**: PRE-MARKET → PRE-EVAL (caller-side). Stage 3 is now owned by EntryOrchestrator's caller (e.g. backtest harness reads `watchlist:tier2`); output is in-process `ctx.actionable_symbols: Set[str]`. No production code change. Updated 4 spec files + 2 cross-references; journal envelope `stage=premarket` retained for backward compat with historical records.

### AUDIT-1 procedure (unchanged from § AUDIT-1)
First meaningful ADR-008 reading at 30-day accumulation boundary.
Shadow `_shadow/{model}/YYYY-MM-DD.json` will accumulate daily via
NUC cron; the audit reports per-model mase / pnl / win_rate / hit_rate
and applies the ADR-008 gate to each. See `## AUDIT-1` below for
the full procedure.

---

## B2 — move shadow eval helpers to `v12/evaluation/`

**Goal:** Single home for `simulate_pnl`, `hit_rate`, `signal_hit_rate`,
`build_test_tsdf_full`, `predict_one_symbol_for_date`, `compute_mase`,
`naive_errors_from_actual`. Today they live in `v12/scripts/eval_chronos_h2h_v3.py`
and are imported by `shadow_trade.py` / `shadow_ensemble.py` via sys.path.

**Council risk (per Critic):**
1. **Module identity drift** — `isinstance`, `__module__`-keyed logging,
   pickled-by-qualified-name closures break silently. Mitigation: snapshot all
   `.__module__` and `.__qualname__` for these functions pre- and post-move;
   diff must be empty for any caller that pickles them.
2. **Implicit globals** — `pandas.option_context`, `numpy` RNG, per-symbol
   memoization cache inside `predict_one_symbol_for_date`. Mitigation: audit
   `predict_one_symbol_for_date` body line-by-line; document every captured
   global; the determinism baseline (`test_shadow_determinism.py`) catches drift.
3. **Lazy re-import side-effects** — Redis SETNX warm-up, env-init, logging
   handler ordering. Mitigation: re-run determinism baseline after move; hashes
   must be unchanged.

**Pre-work (before touching code):**
1. SHA-256 snapshot every output of `eval_chronos_h2h_v3.py`, `shadow_trade.py`,
   `shadow_ensemble.py` against current backbone (use `--predict-today` against
   2026-07-01 fixture).
2. Record all `.__module__` / `.__qualname__` of moved functions and any
   closures that reference them.
3. Identify all pickled-by-qualified-name consumers (none expected — verify
   with grep).

**Acceptance:**
- All pre-work SHA-256s unchanged after the move.
- Determinism baseline (`test_shadow_determinism.py`) 6/6 PASS unchanged.
- NUC shadow crons next trading day produce byte-identical SUMMARY.json +
   per-date JSON to pre-move baseline.
- Zero changes to `__name__` / `__file__` of *unchanged* modules (per R2).

---

## B3 — `EntryOrchestrator` stage chain decomposition

**Goal:** Break `entry_orchestrator.py` (currently 384+ LoC, C5 god-object per
Phase 1 inventory) into discrete stages per `v12/docs/TRADING_PIPELINE.md`
Stage 1..7 contract.

**Stages:**
1. Begin entry evaluation (INV-1, INV-2 guard)
2. Forecast retrieval
3. Signal rule evaluation (SR-01..SR-07)
4. YOLO no-trade filter (SR-06)
5. Gatekeeper sizing (R-05 daily cap, R-06 cooldown)
6. Bracket order submission
7. Position registration + journal write

**Council risk:** Internal refactor; ADR-008 has zero sensitivity per Council
(unless stage ordering changes, which it shouldn't).

**Acceptance:**
- All 7 stages have explicit entry/exit + return type contracts.
- Stage ordering matches current behavior bit-for-bit (verified by
  `test_entry_orchestrator.py` + paper_soak_sign_agreement).
- Zero call-site changes (orchestrator is a singleton in callers).
- ADR-008 next NUC run still passes.

---

## B4 — `MarketDataClient` Protocol

**Goal:** Extract `v12/contracts/MarketDataClient` Protocol mirroring the
current SwingScanner's data-source seam. Today SwingScanner directly imports
`src.services.data_loader` via sys.path; the same Pattern A (passive Protocol)
as `ChronosBackend` applies.

**API surface (TBD, design needed):**
```python
class MarketDataClient(Protocol):
    def fetch_daily_bars(self, symbol: str, as_of: date) -> pd.DataFrame: ...
    def fetch_latest_close(self, symbol: str) -> Optional[float]: ...
    def universe(self, tier: str) -> List[str]: ...
```

**Trigger:** When adding a 2nd data source (Polygon, Alpaca, IBKR native).
Until then, the Protocol extraction has no consumer; per Council pattern,
add the contract without consuming it (the cheapest decoupling win).

---

## B5 — Notifier impl upgrade (Redis pubsub / webhook)

**Goal:** Replace in-tree `_Notifier` (logging-only stub) with Redis pubsub
backed delivery surface. Same `Notifier` Protocol contract from Phase 6
commit 2 (locked). Consumers (`EntryOrchestrator`, `DailyReview`,
`PositionManager`) untouched.

**Pre-work:** Council § 4.4 already locked the Protocol surface in Phase 6.
This is the impl-side upgrade only — no consumer changes.

**Implementation:**
- `v12/notifiers/redis_pubsub_notifier.py` — Redis pubsub backend satisfying
  `Notifier` Protocol.
- `v12/notifiers/webhook_notifier.py` — Slack/Discord webhook backend
  (alternative).
- Env-var selection (`V12_NOTIFIER_BACKEND=log|redis|webhook`).
- Backwards-compatible default: `log` keeps current behavior.

**Acceptance:**
- 9 per-component tests from Phase 7 commit 4 still pass.
- New tests for Redis pubsub delivery (drained by test subscriber).
- Webhook impl covered by `requests_mock` / equivalent.
- NUC crons still produce identical SUMMARY.json.

---

## B6 — config lift

**Goal:** Consolidate `config/` (legacy V11) + `configs/` (V12) + env vars
scattered across scripts. Single `v12/config/` package with typed Pydantic
schema for runtime config.

**Council risk:** Path-walk harmless; no ADR-008 sensitivity.

**Trigger:** Whenever convenient — not blocking any other B-item.

---

## AUDIT-1 — Longitudinal ADR-008 audit (~2026-08-13)

**Goal:** First meaningful ADR-008 reading at 30-day accumulation boundary.

**Procedure:**
1. SSH to NUC; pull all `_shadow/{model}/YYYY-MM-DD.json` for 2026-07-02 to
   2026-08-12 (~30 trading days).
2. Aggregate per-model mase_mean, simulated_pnl_pct, win_rate, hit_rate,
   n_trades, n_dates.
3. Apply ADR-008 gate to each model.
4. If `phase2_v2` consistently negative alpha → recommend switch to
   `E05_p3_coldstart_seed42`-only or ensemble.
5. If both pass → no action; continue accumulation.
6. If both fail → call Council (back to Phase 2).

**Acceptance:** Audit report committed to `v12/docs/audit/2026-08-13-adr008.md`
with table per model + recommendation.

---

## Sequencing recommendation

Per user confirmation (close-out note), the immediate next-step after this
doc lands is:

1. **Monitor weekly** — pull NUC SUMMARY.json every Friday; track per-model
   trend.
2. **AUDIT-1 at 2026-08-13** — first meaningful gate reading.
3. **B5 next** — Notifier upgrade is highest-leverage post-launch operational
   win (Slack alerts on PANIC/EXIT, not just log lines).
4. **B2 after AUDIT-1** — once we know which model is the winner, refactor
   the eval helpers around the chosen backbone.
5. **B3 / B4 / B6** — opportunistically; not on critical path.

---

## Cross-references

- Council verdict: `v12/docs/architecture/01-COUNCIL-LOG.md` § 4.4
- Phase close-out: `v12/docs/architecture/05-PHASE-CLOSE-OUT.md`
- ADR-008 shadow gate: `v12/docs/Shadow-Ensemble-Runbook.md`
- Phase 1 coupling findings: `v12/docs/architecture/00-ASSET_INVENTORY.md`
- Determinism baseline: `v12/tests/test_shadow_determinism.py`

---

## Changelog

- **2026-07-02** — Post-launch roadmap written. B2/B3/B4/B5/B6 + AUDIT-1
  scoped per Council § 4.4. Sequencing recommendation: AUDIT-1 first
  (~2026-08-13), then B5 → B2 → opportunistic B3/B4/B6.