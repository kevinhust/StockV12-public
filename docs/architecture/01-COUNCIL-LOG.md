# V12 Architecture Council — Phase 2 (Council)

**Status:** Concluded 2026-07-02
**Source-of-truth:** [`00-ASSET_INVENTORY.md`](00-ASSET_INVENTORY.md) § 5–8
**Audience:** Internal — feeds Phase 3 (Design).

> This is the structured disagreement record for the pre-launch decoupling
> scope decision. Per the council skill, raw voices come first, bias
> guardrails are visible, the strongest dissent is preserved even where
> rejected, and the verdict is the synthesized path — not unanimity.

---

## 1. The Question

> Given Phase 1 inventory (12 components, T1..T8 coupling, B1..B5 priority,
> Q1..Q8 council questions), which decoupling items MUST ship pre-launch,
> which can defer post-launch, in what order, and under what constraints?

### 1.1 Constraints
- Live shadow crons at **16:05 + 16:10 ET on NUC** must not regress (ADR-008 gate: PnL > 0, MASE ≤ v2 × 1.05, win_rate ≥ 0.40, n_trades ≥ 30, n_dates ≥ 30).
- 27 existing tests must stay green (already passing).
- INV-1..INV-7 + R-01..R-10 cannot drift silently (see `v12/docs/SYSTEM_OVERVIEW.md`).
- Public Protocols (`ChronosBackend`, `BrokerAdapter`) must stay stable across refactor.
- Single-account US equities swing — not HFT; latency tolerance is ~minutes, not µs.
- User prefers iterative commit / verify / next-step over big-bang.

### 1.2 What counts as success
- v12 src can be packaged (wheel or Docker image) without `PYTHONPATH` hack for production entry path.
- ADR-008 shadow gate still passes on NUC after every commit in this phase.
- Existing tests still pass; new tests cover each *commit* not just each *phase*.

---

## 2. Voices (raw, in order spoken)

### 2.1 Architect (pre-council anchor — locked in first so synthesis isn't biased)

**Position:** Scope the pre-launch refactor to **B1 + B2 + a thin shim for T1+T3+T7**. B3/B4/B5 defer post-launch.

**Reasoning:**
1. B1 (Q1) is the only true packaging blocker — v12 cannot ship as a real Python package until `from services.chronos_service import ChronosService` sys.path hack goes away.
2. B2 (Q2) is the numeric-correctness blocker — shadow gate decisions use shadow-script numbers; those drift from production if `simulate_pnl`/`hit_rate`/`predict_one_symbol_for_date`/`build_test_tsdf_full` keep two homes.
3. B3/B4/B5 (Q3/Q4/Q5) are real but isolated; each can refactor on its own clock, no time pressure pre-launch.

**Main risk:** touching five files (`lifecycle_scheduler.py`, `forecast_service.py`, `shadow_trade.py`, `shadow_ensemble.py`, `eval_chronos_h2h_v3.py`) in one change-set could regress ADR-008.

---

### 2.2 Skeptic — *lateral role; pre-existing assumption-challenger*

**Position:** Ship the smallest thing that trades, observes, and notifies correctly — defer every decoupling item post-launch. **The premise that decoupling is the highest-leverage move pre-launch is wrong.**

**Reasoning:**
1. The priority list B1–B5 is clean-up, not launch-blocking. None of T1–T8 represent runtime failure modes — they're architectural smells. A sys.path hack that *works* is not a launch blocker; a Notifier stub that *doesn't fire* is.
2. God-objects and duplicate scripts are refactor debt, not launch debt. EntryOrchestrator fanning to 8 only hurts if you're changing it. You're not — you're shipping v1.
3. You don't yet know which coupling actually hurts. Pre-launch, you have no production traffic telling you which seams flex under load. Refactoring now optimizes for imagined future pain.

**Risk in their recommendation:** If v1 is fragile, you refactor under fire with paying users and live positions. Mitigation: a 1-week hardening sprint focused **only on T1 (sys.path → clean import)** and **C9 (real observability)** — nothing else.

**Surprise (their note for the others):** The honest answer is that decoupling *isn't the bottleneck* — **determinism and reproducibility of the backtest → live parity is**. Before refactoring seams, verify that `predict_one_symbol_for_date` returns identical bytes in all three homes. That's a 2-day test, and it tells you whether any of this matters.

---

### 2.3 Pragmatist — *shipping-speed-focused*

**Position:** Ship **B1 only** as a Protocol extraction (no behavior change) for pre-launch; **defer B2, B3, B4, B5** to post-launch. Shadow crons don't care about cleanliness — they care that `forecast_service.py` keeps producing identical bytes.

**Reasoning:**
1. **B1 is the only item that touches the live surface.** T1=9/10 because shadow scripts `sys.path.insert` into `src.services.chronos_service`. Extract `v12.contracts.ChronosBackend` as a Protocol, keep the existing class where it is, add a *one-line* re-export at the old import path. Zero call sites change, 27 tests stay green, crons don't notice.
2. **B2 is the real value but is a behavioral move.** Shifting `simulate_pnl` / `hit_rate` / `build_test_tsdf_full` / `predict_one_symbol_for_date` into `v12/evaluation/` risks output drift (log lines, dict ordering, caching side-effects). Shadow gate cares about MASE/PnL bit-identical, not architecture. Defer until after market close, then hash outputs before/after.
3. **B3/B4/B5 are pure internal refactor.** C5 god-object, C2 scanner, C9 stub have **zero** influence on ADR-008 metrics. Cleaner code ≠ safer crons. The council's "must-decouple" instinct is architectural, not gate-driven.

**Risk in their recommendation:** If B1's Protocol signature drifts from the implicit duck-type, the 16:05/16:10 ET crons fail mid-flight. Mitigation: keep B1 strictly passive (Protocol only, no runtime `isinstance` checks, no inheritance requirement) so the impl continues duck-typing.

**Surprise (their note for the others):** The cheapest "decoupling win" is **a Protocol with no enforcement** — it makes the seam visible for B2's post-launch move without paying a single test-cycle of risk. Other voices will push to *use* the Protocol (rename, relocate, retest). That's the work that breaks crons. **Adding the contract without consuming it is the entire pre-launch deliverable.**

---

### 2.4 Critic — *downside-risk + edge-case focused*

**Position:** B1 and B2 look mechanical but aren't — protocolization and relocation each carry determinism and coupling traps that green tests won't reveal.

**Reasoning:**
1. **B1's Protocol is structural, not nominal.** V11's `ChronosService` can lack a method any callsite assumes (warm-up, retry-after-pickle-fail, dry-run, adapter-not-injected), and Protocol only checks shape at the call site — not import. Tests cover the happy path; non-fatal code paths surface only in production.
2. **B2 breaks "identical numbers" three ways:** (a) **module identity** — `isinstance`, `__module__`-keyed logging, pickled-by-qualified-name closures all break silently; (b) **implicit globals** — `pandas.option_context`, `numpy` RNG, and very likely a per-symbol/date memoization cache inside `predict_one_symbol_for_date`; (c) **lazy side-effects on re-import** — a Redis `SETNX` warm-up, env-init, or logging handler ordered differently in `v12/evaluation/`. Bitwise-equal under those conditions is a fantasy.
3. **NUC crons at 16:05/16:10 fork multiprocessing workers.** Closures, module-level memoized state, or `functools.lru_cache` keyed on a path-dependent identity will fork-inherit or fork-break differently than the parent call did.

**Risk:** Without pinning *tolerance, comparison scope, and pre/post seed-state reset* before the move, tests will green and NUC will drift — exactly the silent promotion regression this refactor was meant to prevent.

**Surprise (their note for the others):** **Observability surface drift.** `shadow_trade.py` and `shadow_ensemble.py` almost certainly Sentry-tag, structured-log, and feature-store key off `__name__`/`__file__`. Relocating quietly forks dashboards, alerting rules, and the audit trail the SR-07/ADR-008 gate depends on. The other voices won't think about it because it isn't on the diff.

---

## 3. Synthesis (with bias guardrails)

### 3.1 Where the voices agree
- **B3 / B4 / B5 all defer post-launch.** Unanimous. Do not refactor EntryOrchestrator, SwingScanner, or Notifier pre-launch.
- **A pre-launch move exists** that unblocks packaging without disturbing gate numbers. The Skeptic's call for v1 hardening overlaps with the Pragmatist's B1-passive plan.
- **Determinism (the Skeptic's "surprise") matters.** Before *any* move, we need a baseline that locks ADR-008 numbers.

### 3.2 The Skeptic's premise check — addressed explicitly

The Skeptic challenged the question itself ("decoupling isn't the bottleneck; runtime correctness and observability are"). I owe a counter, not a dismissal:

- **T7 / C9 are real but not higher-leverage than T1.** PANIC/EXIT decisions are *actioned* by `PositionManager.close_for_review()` and `PositionManager.trim_position()`, not by the Notifier. The Notifier is a delivery adapter for low-priority operational summaries. The durable audit trail is `TradeJournal` (Redis LIST `journal:{date}:{sym}`) — already in place and tested. Hardening C9 pre-launch is fine *as a side project*, but it's not the **packaging blocker** that has to land before v1.0.
- **T1 is the bottleneck for "ship v12 as a real package."** Without resolving the sys.path hack, v12 cannot be installed via `pip install` or shipped in a clean Docker layer — and a Python package that can't be installed isn't v1.0; it's a development harness. So the user's commissioning intent (system ready for paper/live trading) depends on resolving T1.
- The Skeptic's "1-week hardening sprint" is a valid alternative *if* the user wanted to ship v12 only to themselves. The user's ` 等全部完成一起推` → `做完一部分，验收，下一步` switch implies they want this to become a real artifact.

**Conclusion: Skeptic's premise partially accepted (C9 hardening is welcome if low-cost); partially rejected (it's not the highest-leverage blocker). B1 stays in scope.**

### 3.3 Where I'm being moved by the council

- **Pragmatist + Critic both pull my recommendation LEFT** on B2. I agree: B2 changes module identity, can re-order side-effects, and the cron gate is bit-sensitive. **B2 defers.**
- **Critic's tolerance / seed-state rule** is unconditional: any move, even a passive Protocol, must be preceded by a baseline-hash test of today's shadow outputs. I accept this fully.
- **Critic's multiprocessing-fork concern** is narrower than the proposal but real: the 16:05/16:10 crons call shadow scripts via Python interpreter, no `multiprocessing.Pool` fork. So the concern is moot for the *immediate* refactor but worth a note in § 4 as a future-B2 risk.
- **Critic's observability-surface-drift** doesn't apply to B1 (passive Protocol — no module path changes; `__name__`/`__file__` of existing code is untouched).

### 3.4 Updated scope (post-council)

| Item | Pre-launch? | Why |
|---|:---:|---|
| **B1** (passive Protocol: `v12/contracts/ChronosBackend.py`) | ✅ Yes | Unblocks packaging; zero call-site change; sys.path shim preserved so crons don't notice. |
| **B1+** (passive Protocol: `v12/contracts/Notifier.py`) | ✅ Yes | Same as B1 for the observability surface; sets up post-launch Notifier impl without disturbing the stub signature. |
| **B2** (move `simulate_pnl` etc. to `v12/evaluation/`) | ❌ No — defer post-launch | Carries module-identity, side-effect-ordering, memoization-cache risks per Critic; not strictly required for packaging. |
| **B3** (EntryOrchestrator stage chain) | ❌ No — defer | C5 internal refactor; zero impact on shadow gate. |
| **B4** (MarketDataClient Protocol) | ❌ No — defer | C2 internal refactor; zero impact on shadow gate. |
| **B5** (Notifier impl upgrade) | ❌ No — defer | C9 stub; the stub signature is already used by `EntryOrchestrator` / `DailyReview` so sealing it with a Protocol is the pre-launch move; impl upgrade is post. |
| **B6** (config lift) | ❌ No — defer | Path-walk harmless, not blocking. |
| **Pre-flight determinism test** | ✅ Yes — **before** B1 | Lock today's `shadow_trade` + `shadow_ensemble` per-symbol row JSON hashes in a baseline. Green means B1 hasn't broken anything when re-run. |

### 3.5 Strongest dissent (preserved even where rejected)

> **Skeptic:** decoupling isn't the bottleneck pre-launch — observability + runtime correctness are. The whole priority list is upside-down.

This is a real framing challenge. The user accepted the Phase 1 framing, so the framing is locked for *this* council. But the Skeptic's underlying point — **don't pre-launch-refactor stuff that doesn't impact the gate** — becomes a ground rule for § 4 (the lessons-learned capture), not a verdict-shift.

---

## 4. Verdict

### 4.1 Consensus
- Decouple only what blocks the v12 package from being installable. Defer everything else.
- The pre-launch move is **minimal, passive, observable**.
- A determinism baseline must exist *before* any change.

### 4.2 Strongest dissent
The Skeptic: decoupling isn't the bottleneck. Rejected with a counter (§ 3.2), preserved as a future-council agenda item.

### 4.3 Premise check
Yes — the Skeptic challenged the question ("is decoupling the right move?"). The premise was locked because the user explicitly commissioned the decoupling workflow (`资产梳理 → 组件识别 → 解耦`). The challenge is preserved as input to the post-launch review.

### 4.4 Recommendation (synthesized path)

In order, with the smallest possible blast radius per commit:

| Order | Commit | What lands | Risk | Verifies |
|---|---|---|---|---|
| **0** | `test(eval): add shadow determinism baseline` | A test that hashes today's `shadow_trade` + `shadow_ensemble` output JSONs (or any subset that's stable) into a snapshot. Passes today = baseline locked. | Almost zero — read-only. | New file + 27 existing tests stay green. |
| **1** | `feat(contracts): add v12.contracts.ChronosBackend Protocol` | New file `v12/src/stockapp_v12/contracts/chronos_backend.py` defining the Protocol. Existing `ForecastService._resolve_backend()` continues to import-from-sys.path; the file is *passive* — no caller consumes it yet. | Zero call-site change. | Baseline test still green. Pyright/mypy on the new file. |
| **2** | `feat(contracts): add v12.contracts.Notifier Protocol` | New file `v12/src/stockapp_v12/contracts/notifier.py` defining the Protocol — mirrors the current `_Notifier` public API. No caller change. | Zero call-site change. | Baseline test still green. |
| **3** | (GATE) | Run the existing shadow cron's per-symbol logic locally (`python -m v12.scripts.shadow_trade --today YYYY-MM-DD`) and diff against a reference day. Must be identical. | Low if 1 + 2 are truly passive. | CI gate `test_shadow_determinism`. |

**After this phase lands**, B2–B5 are on the post-launch roadmap. Each gets its own council session.

---

## 5. Resolutions of Open Questions Q1..Q8

| # | Question | Resolution |
|---|---|---|
| Q1 | v12.contracts.ChronosBackend Protocol vs sys.path reliance | **Yes — add Protocol (passive).** Sys.path hack stays as-is until post-launch B2. |
| Q2 | Shadow scripts move into v12 src vs stay at v12/scripts with re-exports | **Defer post-launch.** B2 is the forum. |
| Q3 | EntryOrchestrator stage chain vs strategy pattern | **Defer post-launch (B3).** |
| Q4 | SwingScanner data-client scope | **Defer post-launch (B4).** Polygon + Longbridge only when we get there. |
| Q5 | ReviewContext base for Premarket + DailyReview | **Defer post-launch.** No pre-launch churn. |
| Q6 | Configs lift (repo_root → v12/config) | **Defer post-launch (B6).** Path walk is harmless. |
| Q7 | Notifier scope | **Pre-launch: add Protocol (B1+); impl upgrade post-launch (B5).** |
| Q8 | Per-component test matrix on top of ADR-008 | **Yes, build it as we land commits 0..3 above.** New `tests/test_shadow_determinism.py` is the seed. |

---

## 6. Update to the Phase 1 Inventory

The Phase 1 inventory doc carries forward unchanged for § 1–4 (those reflect current code).
§ 5 (Coupling) and § 6 (Priority Queue) are **superseded by this council log's § 3.4 verdict**.
A one-line cross-reference header was added at the top of `00-ASSET_INVENTORY.md` to
point to this doc so future readers don't act on the original B1–B5 wording in isolation.

> **Note:** the § 5/§ 6 content of `00-ASSET_INVENTORY.md` is preserved for traceability.
> Treat this council log as binding for pre-launch scope.

---

## 7. Changelog

- **2026-07-02** — Phase 2 council convened (4 voices). Verdict: ship passive `v12.contracts.ChronosBackend` + `Notifier` Protocols pre-launch; defer B2–B5 to post-launch. Adds pre-flight determinism baseline as commit 0 of the implementation phase.
