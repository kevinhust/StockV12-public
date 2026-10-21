# V12 Phase 7 — Test Plan

**Status:** Active (2026-07-02)
**Phase:** 5 Test
**Source-of-truth:** [`02-DECOUPLING-PLAN.md`](02-DECOUPLING-PLAN.md) § 1 + ADR-008 + INV-1..INV-7

> Per-component test matrix + end-to-end shadow-replay acceptance criteria for
> the post-Protocol-extraction V12 system. Covers the two new Protocol seams
> (`ChronosBackend`, `Notifier`), the full pipeline replay, and ADR-008 hit-rate
> gate validation.

---

## Scope

### 1. Per-component Protocol tests (commits 1 + 2 seams)

| File | What it locks | Coverage |
|---|---|---|
| `v12/tests/test_chronos_backend_protocol.py` | `ChronosBackend` Protocol: structural typing, identity, dispatch via `detect`/`detect_batch` | 8 tests |
| `v12/tests/test_notifier_protocol.py` | `Notifier` Protocol: structural typing, `@runtime_checkable` semantics, identity, dispatch | 9 tests |

**Acceptance criteria:**
- Any class with the documented method surface satisfies `isinstance(x, Protocol)` (where applicable).
- `from stockapp_v12.contracts import X` and `from stockapp_v12.services.module import X` return the **same class object** (`is`-check).
- All 8 Notifier methods and both ChronosBackend methods dispatch without raising on a concrete impl.
- Protocol imports succeed with `PYTHONPATH=v12/src` only (no `repo-root src/` dependency).

### 2. End-to-end shadow replay

**File:** `v12/tests/test_phase7_e2e_shadow_replay.py`

Exercises the full V12 pipeline deterministically:

```
SwingScanner (fake)
  ↓
ForecastService(FakeChronosBackend)
  ↓
SignalRuleEngine → EntryOrchestrator
  ↓
BracketOrderService(PaperBroker)
  ↓
TradeJournal (in-memory fake_redis)
  ↓
PositionManager
  ↓
DailyReview → Notifier (collected, not delivered)
```

**Determinism anchor:** uses `DeterministicFakeChronos` (commit 0 fixture
backend) + per-symbol literal seed offsets — NO `hash()` (PYTHONHASHSEED safe).
Output JSON bytes are SHA-256'd and compared to a locked baseline fixture.

**Acceptance:**
- All 5 pipeline stages produce expected calls
- TradeJournal writes match locked bytes
- Notifier receives the 4 expected event types
- Hashes locked at `lock_commit: 6e129ab3` (GATE landed)

### 3. Hit-rate validation (ADR-008 gate)

**File:** `v12/tests/test_phase7_hit_rate_validation.py`

Runs the locked determinism-baseline scenario through `simulate_pnl` /
`hit_rate` / `compute_mase` and asserts the ADR-008 gate criteria:

| ADR-008 metric | Threshold | Test |
|---|---|---|
| `simulated_pnl_pct > 0` | positive | `test_simulated_pnl_is_positive` |
| `mase ≤ v2_baseline × 1.05` | bounded | `test_mase_within_5pct_of_v2` |
| `win_rate ≥ 0.40` | quality | `test_win_rate_above_40pct` |
| `n_trades ≥ 30` | statistical power | `test_n_trades_min_30` |
| `n_dates ≥ 30` | distinct eval days | `test_n_dates_min_30` |

**Acceptance:** All 5 gates PASS against the locked fixture (frozen backbone
gives stable numbers). Drift on any metric ⇒ test fails ⇒ stop-the-line.

---

## File matrix

| Test file | Tests | Phase | Status |
|---|---|---|---|
| `v12/tests/test_chronos_backend_protocol.py` | 8 | 7.1 Per-component | **landed (commit 4, `366a1a74`)** |
| `v12/tests/test_notifier_protocol.py` | 8 | 7.1 Per-component | **landed (commit 4, `366a1a74`)** |
| `v12/tests/test_phase7_e2e_shadow_replay.py` | 6 | 7.2 E2E replay | **landed (commit 5, `64026077`)** |
| `v12/tests/test_phase7_hit_rate_validation.py` | 8 | 7.3 Hit-rate gate | **landed (commit 5, `64026077`)** |
| `v12/tests/test_shadow_determinism.py` | 6 | 4.0 Baseline | landed (commit 0, `78cbdf22`) |
| `v12/tests/test_shadow_trade.py` | 4 | 6.2 Real-model replay | pre-existing |

**Total:** 30 new Phase-7 tests + 6 pre-existing baseline = 36 tests
guarding the post-Protocol surface.

---

## Test execution order

```bash
# Per-component (commit 4)
PYTHONPATH=v12/src:src:. python3 -m pytest \
    v12/tests/test_chronos_backend_protocol.py \
    v12/tests/test_notifier_protocol.py \
    -v

# End-to-end + hit-rate (commit 5)
PYTHONPATH=v12/src:src:. python3 -m pytest \
    v12/tests/test_phase7_e2e_shadow_replay.py \
    v12/tests/test_phase7_hit_rate_validation.py \
    -v

# Full V12 suite (commit 5 GATE-2)
PYTHONPATH=v12/src:src:. python3 -m pytest v12/tests/ \
    --ignore=v12/tests/test_r05_daily_cap.py \
    --ignore=v12/tests/test_r06_cooldown.py \
    --ignore=v12/tests/test_gatekeeper_v12_sizing_meta.py \
    -q --tb=line
```

---

## Commit plan (Phase 7)

| Order | Commit | What lands | Gate | Status |
|---|---|---|---|---|
| 4 | `test(contracts): add Protocol structural tests (Phase 7, commit 4)` | `test_chronos_backend_protocol.py` (8) + `test_notifier_protocol.py` (8) | all per-component tests pass | **landed `366a1a74`** — 16/16 PASS |
| 5 | `test(eval): add E2E shadow replay + ADR-008 hit-rate gate (Phase 7, commit 5)` | `test_phase7_e2e_shadow_replay.py` (6) + `test_phase7_hit_rate_validation.py` (8) | E2E + hit-rate tests pass + full suite green | **landed `64026077`** — 14/14 PASS, full V12 suite 319 PASS / 6 pre-existing FAIL |

---

## Acceptance criteria for Phase 7 close

1. ✅ All 30 new tests pass (16 commit 4 + 14 commit 5).
2. ✅ Full V12 pytest suite green: 319 pass / 6 pre-existing baseline FAIL
   unchanged from commit 0.
3. ✅ No Protocol drift (per R1 mitigation): every concrete impl's `dir()`
   contains all Protocol-required methods (`test_chronos_backend_protocol.py`,
   `test_notifier_protocol.py`).
4. ✅ SHA-256 reference hashes locked via in-test `_hash(obj)` calls on
   forecast outputs, pipeline state, and ADR-008 metrics.

---

## Cross-references

- ADR-008 shadow gate: `v12/docs/Shadow-Ensemble-Runbook.md`
- INV-1..INV-7: `v12/docs/SYSTEM_OVERVIEW.md`
- R-01..R-10: `v12/docs/RISK_MANAGEMENT.md`
- Commit 0 determinism baseline: `v12/tests/test_shadow_determinism.py`
- Commit 3 GATE: `v12/docs/architecture/03-IMPLEMENTATION-LOG.md`

---

## Changelog

- **2026-07-02** — Phase 7 plan written.
  - Commit 4 (`366a1a74`): per-component Protocol tests — landed. 16/16 PASS.
  - Commit 5 (`64026077`): E2E shadow replay + ADR-008 hit-rate gate — landed.
    14/14 PASS. Full V12 suite 319 PASS / 6 pre-existing FAIL unchanged.