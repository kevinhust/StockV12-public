# V12 Decoupling Plan — Phase 3 (Design)

**Status:** Drafted 2026-07-02 (workflow: Discovery → Council → **Design** → Implementation → Test)
**Source-of-truth:** [`00-ASSET_INVENTORY.md`](00-ASSET_INVENTORY.md) § 1–4 · [`01-COUNCIL-LOG.md`](01-COUNCIL-LOG.md) § 3.4 verdict
**Audience:** Internal — drives the Phase 4 (Implementation) commits.
**Update cadence:** Append-only. New contract → new section. Verifiable items only; no aspirational interfaces.

> This doc is **binding** for pre-launch decoupling scope. It is the synthesis of
> the Council verdict (pre-launch: passive `ChronosBackend` Protocol + `Notifier`
> Protocol + determinism baseline; defer B2–B5 post-launch) into concrete files,
> signatures, snapshot shapes, and CI gate criteria. Anything not specified here
> is out of scope for this phase.

---

## 0. Reading Guide

The doc answers, in order:

1. **What exactly ships?** (file paths + signatures)
2. **What does NOT ship?** (locked out-of-scope list, carry-forward from Council § 3.4)
3. **How do we prove nothing broke?** (determinism baseline test, exact hash inputs, snapshot location)
4. **What are the done gates?** (verifiable per-commit + final acceptance)
5. **What risks remain?** (carry-forward from Council § 3.3 Critic notes)

A reader should be able to implement commits 0–3 mechanically from § 1–4 alone.
§ 5 is the regression watch list.

---

## 1. Goals & Non-Goals (locked from Council § 3.4)

### 1.1 Goals (pre-launch ship)

| ID | Goal | Source |
|---|---|---|
| **G1** | `v12` src package can be imported in a clean process without the `PYTHONPATH` hack being required for *any* path that does **not** actively need `ChronosService` | T1, B1 verdict |
| **G2** | The two real seams `ChronosBackend` and `Notifier` are **documented as Protocols** at a stable, package-level location | Council Q1 + Q7 |
| **G3** | A pre-flight **determinism baseline test** locks today's `shadow_trade` + `shadow_ensemble` output bytes for any test fixture; future commits must not break it | Critic + Skeptic synthesis |
| **G4** | Existing 27 tests stay green at every commit | Council § 1.1 constraint |
| **G5** | ADR-008 shadow gate (`PnL>0`, `MASE≤v2×1.05`, `win_rate≥0.40`, `n_trades≥30`, `n_dates≥30`) stays in effect on NUC after every commit | Council § 1.1 constraint |

### 1.2 Non-Goals (locked out-of-scope pre-launch)

| ID | Item | Verdict source |
|---|---|---|
| **NG1** | Move `predict_one_symbol_for_date` / `simulate_pnl` / `hit_rate` / `build_test_tsdf_full` into `v12/evaluation/` | Council § 3.4 — defer B2 |
| **NG2** | Refactor `EntryOrchestrator` into a typed stage chain | Council § 3.4 — defer B3 |
| **NG3** | Introduce `MarketDataClient` Protocol for SwingScanner | Council § 3.4 — defer B4 |
| **NG4** | Replace `Notifier` loguru stub with Redis pubsub / Slack impl | Council § 3.4 — defer B5 (impl); ship Protocol only |
| **NG5** | Lift configs from `repo_root/config/` into `v12/config/` | Council § 3.4 — defer B6 |
| **NG6** | `multiprocessing.Pool` fork-shape changes for shadow harness | Council § 3.3 — moot for this phase; tracked as § 5.3 |

Any commit touching NG1–NG6 is a **scope violation** unless a new Council session
re-opens the verdict.

### 1.3 What counts as success for this phase

- `pip install -e ./v12` from a fresh venv, then `python -c "from stockapp_v12.contracts import ChronosBackend, Notifier"`, succeeds.
- All 27 existing tests still pass.
- New `test_shadow_determinism.py` passes against the locked baseline.
- `python -m v12.scripts.shadow_trade --models baseline_v1 --predict-today` on a known fixture day produces byte-identical per-date JSON to the snapshot.
- No change to any `__name__` / `__file__` of existing modules (Critic § 3.3 — observability drift guard).

---

## 2. New Module Layout

One new package, **passive**, no behavior change at call sites:

```
v12/src/stockapp_v12/
├── bridge/                (existing)
├── contracts/             ← NEW (this phase)
│   ├── __init__.py
│   ├── chronos_backend.py
│   └── notifier.py
├── core/                  (existing)
├── pipeline/              (existing)
├── schemas/               (existing)
└── services/              (existing — gains 2 re-export shims)
```

The `contracts/` package contains **only Protocols** — no concrete classes, no
runtime logic, no I/O. It must import cleanly on a fresh Python install with
zero side-effects beyond `typing.Protocol` definitions.

---

## 3. Interface Contracts (binding signatures)

### 3.1 `v12/src/stockapp_v12/contracts/chronos_backend.py`

This Protocol is the **authoritative seam** for the chronos prediction backend.
The existing `services.forecast_service.py:21-24` definition is preserved in
place for backward compatibility (1-line re-export shim — see § 4.1).

**Exact file contents:**

```python
"""ChronosBackend — passive Protocol for V12 forecast backends.

This is the authoritative seam between V12 (consumer) and any prediction
backend (provider). Today the only impl is ``services.chronos_service.ChronosService``
in repo-root ``src/`` — reached via sys.path. Post-launch, an in-tree impl
under ``v12/backends/`` will satisfy this Protocol and the sys.path import
becomes optional (injected adapter pattern).

Invariant: this Protocol is duck-typed. There are NO isinstance checks
in V12 production code, NO abstract-method enforcement at runtime. The
Protocol exists to *document the seam*, not to gate it.
"""

from __future__ import annotations

from typing import Any, Dict, Protocol

import pandas as pd


class ChronosBackend(Protocol):
    """Backend producing per-symbol prediction signals from daily OHLCV bars.

    The detection payload schema (signals[i]['meta']) is documented in
    ``services/forecast_service.forecast_daily`` — keys consumed downstream:
    current_price, forecast_price, forecast_q10, forecast_q90, horizon.

    Implementations MUST:
      - Return ``[]`` (empty list) on soft failure, never raise, for ``detect``.
      - Return ``{}`` (empty dict) for missing symbols in ``detect_batch``;
        raise only on a hard backend load failure (caller already catches).
      - Be pickle-safe if the caller serializes the backend (NUC shadow harness
        re-imports per cron run, but future parallel evaluation may pickle).
    """

    def detect(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        """Run inference on a single symbol's daily-bar DataFrame."""
        ...

    def detect_batch(self, frames: Dict[str, pd.DataFrame]) -> Dict[str, list[dict[str, Any]]]:
        """Run inference on a batch of symbols in one backend call (preferred)."""
        ...


__all__ = ["ChronosBackend"]
```

**Stability guarantee:** `detect` and `detect_batch` signatures are frozen for
v12.x. New optional kwargs (e.g. `temperature: float = 1.0`) are allowed via
PEP 3107 keyword-only addition; **positional-arg changes are a major bump.**

### 3.2 `v12/src/stockapp_v12/contracts/notifier.py`

Mirrors the existing `_Notifier` public API in `services/notification_service.py`.
Today every method is a `logger.info`/`logger.warning` pass-through; the Protocol
locks the surface so post-launch B5 can ship a Redis pubsub impl without
touching callers.

**Exact file contents:**

```python
"""Notifier — passive Protocol for V12 operational alerts.

Mirrors ``services.notification_service._Notifier`` public API. Today the only
impl is the loguru stub (``services.notification_service.notifier``). Post-launch
B5 will introduce a Redis pubsub-backed impl; the Protocol is the stable seam.

Invariant: notifier is a *delivery* surface for low-priority operational
summaries. PANIC/EXIT decisions are NOT delivered through this channel —
they are actioned by PositionManager.close_for_review / trim_position and
durable via TradeJournal (Redis LIST journal:{date}:{sym}). Do not route
auditable state through notifier.
"""

from __future__ import annotations

from typing import List, Optional, Protocol


class Notifier(Protocol):
    """Delivery surface for operational alerts. No-op by default in tests."""

    def notify_stock_pool_updated(
        self, tier: str = "", symbols: Optional[List[str]] = None, source: str = "",
    ) -> None: ...

    def notify_order_intent(self, **kwargs) -> None: ...

    def notify_order_submitted(self, **kwargs) -> None: ...

    def notify_order_filled(self, **kwargs) -> None: ...

    def notify_order_rejected(self, **kwargs) -> None: ...

    def notify_daily_review(self, symbol: str = "", action: str = "", **kwargs) -> None: ...

    def notify_position_state_change(
        self, symbol: str = "", old: str = "", new: str = "", **kwargs,
    ) -> None: ...

    def notify(self, *args, **kwargs) -> None: ...


__all__ = ["Notifier"]
```

**Stability guarantee:** Each method signature is frozen for v12.x. New methods
may be added (default impl `pass`); **renaming or removing a method is a major
bump and requires Council.**

### 3.3 `v12/src/stockapp_v12/contracts/__init__.py`

```python
"""Stable Protocol seams for V12 external integrations.

See chronos_backend.py and notifier.py for the binding contracts.
"""

from stockapp_v12.contracts.chronos_backend import ChronosBackend
from stockapp_v12.contracts.notifier import Notifier

__all__ = ["ChronosBackend", "Notifier"]
```

---

## 4. Backward-Compatibility Shims (zero call-site change)

Per Council § 3.3 (Critic: "passive Protocol — no module path changes; `__name__`/`__file__`
of existing code is untouched") and Pragmatist ("B1 strictly passive, no `isinstance`
checks, no inheritance requirement"). The existing imports keep working with no
test changes.

### 4.1 `v12/src/stockapp_v12/services/forecast_service.py`

After commit 1, the existing inline `ChronosBackend` definition at lines 21–24
is replaced by:

```python
from stockapp_v12.contracts.chronos_backend import ChronosBackend  # re-export shim
```

This is a literal delete + 1-line import. The Protocol class object reference is
preserved (same name, same module re-export, same `detect`/`detect_batch`
attribute access). Pyright/mypy continue to type-check the existing call sites
(`backend.detect(df)`, `backend.detect_batch(frames)`) identically.

**Verification:** `from stockapp_v12.services.forecast_service import ChronosBackend`
returns the SAME class object as `from stockapp_v12.contracts import ChronosBackend`
(`is` identity, not just equality).

### 4.2 `v12/src/stockapp_v12/services/notification_service.py`

After commit 2, add at the top:

```python
from stockapp_v12.contracts.notifier import Notifier  # re-export shim (Protocol)
```

The existing `_Notifier` class (concrete impl, loguru stub) stays in place.
`_Notifier` is not re-exported via `__all__` (it's a private class per leading
underscore). External callers should use the module-level `notifier` instance or
import `Notifier` from `stockapp_v12.contracts` for type hints.

**Verification:** `isinstance(services.notification_service.notifier, Notifier)`
returns `True` (Protocol structural check).

### 4.3 `v12/src/stockapp_v12/__init__.py`

Add to imports block + `__all__`:

```python
from stockapp_v12.contracts import ChronosBackend, Notifier
```

This makes the Protocols available at the package root for `from stockapp_v12 import ChronosBackend, Notifier`.
Existing `__all__` entries unchanged.

---

## 5. Determinism Baseline Test (commit 0)

The Critic's "tolerance / seed-state rule" (§ 3.3) is unconditional. **Before any
Protocol is added**, we lock today's shadow outputs into a baseline fixture.
After every commit in this phase, the baseline test re-runs the same fixture
and compares hashes. Any divergence is a CI gate failure.

### 5.1 Hash inputs (exact list)

Three classes of JSON artifact. All hashes are **SHA-256 over UTF-8 encoded
JSON string with `sort_keys=True, default=str`** — this makes order-of-keys
irrelevant while preserving float repr semantics.

| Artifact class | Path shape | Source function | Hash covers |
|---|---|---|---|
| Per-model per-date JSON | `models/chronos/_shadow/<model>/<eval_date>.json` | `write_shadow_artifacts()` at `shadow_trade.py:584` | `{model, eval_date, rows}` |
| Per-model SUMMARY | `models/chronos/_shadow/<model>/SUMMARY.json` | same | stable fields only: `{model, accumulated{aggregate}, promotion_gate}` (excludes `today`, `generated_at`, `current_run_aggregate`) |
| Per-eval-date ensemble JSON | `models/chronos/_shadow/ensemble/<eval_date>.json` | `run_shadow_ensemble()` at `shadow_ensemble.py:412` | `{ensemble_name, ensemble_models, eval_date, fallback_spread_pct, n_symbols, n_used_fallback, rows}` |

**Excluded fields (deliberate):**
- `generated_at` — wall-clock; non-deterministic by design
- `today` — refreshes each cron run; not a hash input
- `current_run_aggregate` — per-run, not accumulated; for diagnostic only

### 5.2 Test fixture (deterministic input)

A single deterministic test fixture covers all three artifact classes. The
fixture is committed in-tree and committed data:

- **Symbols:** `["AAPL", "MSFT", "NVDA"]` (3 symbols, covers batch + per-symbol paths)
- **OHLCV frame:** 60 trading days of synthetic-but-deterministic data
  (seeded RNG, fixed seed `42`). Columns: `date, open, high, low, close, volume`.
  Values generated by a closed-form function `price(t) = 100 + 5*sin(t/7) + 0.1*t`
  — fully reproducible across machines.
- **Model:** `baseline_v1` (frozen Chronos backbone per ADR-008). The test does
  NOT load the real Chronos model (too slow, non-deterministic on GPU). Instead
  it injects a **fake backend** that returns deterministic signals based on
  the input frame's last close. See § 5.5 for the fake backend contract.
- **Walk window:** `walk_days=1` (single eval date = `2026-06-15` Monday,
  a known past date in the test corpus).

### 5.3 Snapshot file shape

**Path:** `v12/tests/fixtures/determinism_baseline.json` (new directory, new file).

```json
{
  "schema_version": 1,
  "locked_at": "2026-07-02T16:30:00Z",
  "fixture_symbols": ["AAPL", "MSFT", "NVDA"],
  "fixture_eval_date": "2026-06-15",
  "fixture_seed": 42,
  "fixture_ohlcv_rows_per_symbol": 60,
  "backend_kind": "DeterministicFakeChronos",
  "hashes": {
    "shadow_trade_baseline_v1_2026-06-15": "<sha256>",
    "shadow_trade_baseline_v1_SUMMARY": "<sha256>",
    "shadow_ensemble_2026-06-15": "<sha256>"
  },
  "lock_commit": "<git rev-parse HEAD at lock time>"
}
```

The `lock_commit` field anchors the baseline to a specific code revision. Any
commit that intends to refresh the baseline (e.g. legitimate shadow-script
output change) MUST bump `schema_version` and update `locked_at` and
`lock_commit` in the same commit.

### 5.4 Test code (`v12/tests/test_shadow_determinism.py`)

```python
"""Pre-flight determinism baseline test (ADR-008 + Council § 3.3).

Locks the SHA-256 of today's shadow_trade + shadow_ensemble output for a
known fixture. Any commit that changes the bytes of these outputs (without
intentionally refreshing the baseline) fails CI.

Refresh flow: ``python -m v12.scripts.refresh_determinism_baseline``
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "determinism_baseline.json"


def _hash_json(obj: dict) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _build_fixture_frames(symbols, eval_date, seed=42):
    """Build 60-row OHLCV frames per symbol with closed-form price curve."""
    import math
    rng = np.random.default_rng(seed)
    frames = {}
    dates = pd.bdate_range(end=eval_date, periods=60)
    for sym in symbols:
        base = {"AAPL": 100, "MSFT": 200, "NVDA": 400}[sym]
        close = [
            base + 5 * math.sin(i / 7) + 0.1 * i + (rng.normal(0, 0.5))
            for i in range(60)
        ]
        df = pd.DataFrame({
            "date": dates,
            "open": [c * 0.99 for c in close],
            "high": [c * 1.01 for c in close],
            "low": [c * 0.98 for c in close],
            "close": close,
            "volume": [1_000_000 + i * 1000 for i in range(60)],
        })
        frames[sym] = df
    return frames


class DeterministicFakeChronos:
    """Implements ChronosBackend Protocol with bit-identical output."""

    def detect(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        last_close = float(df.iloc[-1]["close"])
        return [{
            "meta": {
                "current_price": last_close,
                "forecast_price": last_close * 1.01,
                "forecast_q10": last_close * 0.97,
                "forecast_q90": last_close * 1.05,
                "horizon": 10,
            }
        }]

    def detect_batch(self, frames: Dict[str, pd.DataFrame]) -> Dict[str, list]:
        return {sym: self.detect(df) for sym, df in frames.items()}


def test_shadow_trade_per_date_hash_matches_baseline(tmp_path):
    baseline = json.loads(FIXTURE_PATH.read_text())
    expected_hash = baseline["hashes"]["shadow_trade_baseline_v1_2026-06-15"]

    # … run shadow_trade in-process with DeterministicFakeChronos …

    assert _hash_json(actual_per_date_json) == expected_hash, (
        f"shadow_trade per-date JSON drifted; "
        f"refresh baseline via: python -m v12.scripts.refresh_determinism_baseline"
    )


def test_shadow_trade_summary_hash_matches_baseline():
    # similar, hash the stable-fields subset of SUMMARY.json
    ...


def test_shadow_ensemble_per_date_hash_matches_baseline():
    # similar, hash the ensemble/<eval_date>.json
    ...
```

### 5.5 Refresh script (`v12/scripts/refresh_determinism_baseline.py`)

When a developer legitimately changes shadow output (e.g. adds a new field to
per-date rows), the baseline must be re-locked. The refresh script:

1. Runs the same three artifact generations as § 5.4 with `DeterministicFakeChronos`.
2. Computes the three hashes.
3. Overwrites `v12/tests/fixtures/determinism_baseline.json` with new hashes,
   bumping `schema_version`, `locked_at`, and `lock_commit`.
4. Exits non-zero if any of the three artifact paths is missing (sanity check).
5. Prints the diff of `schema_version` (was N, now N+1) so the developer
   remembers to mention it in the commit message.

The script is intentionally NOT a test (won't run in CI). It is a developer
tool that mutates the committed baseline.

### 5.6 CI gate behavior

The CI gate for this phase is `pytest v12/tests/test_shadow_determinism.py`:

- **Pass:** All three hashes match → all 27 existing tests still pass → merge allowed.
- **Fail (single-hash drift):** Output bytes drifted → REVERT or RUN `refresh_determinism_baseline` and re-lodge.
- **Fail (multiple-hash drift, e.g. all three changed):** Likely a real behavior change → STOP, open Council session, do NOT silently refresh.

The CI runs the determinism test on every PR. The shadow crons on NUC still
run separately and still pass ADR-008 gate (Critic § 3.3: passive Protocol
doesn't touch the live surface).

---

## 6. Done Gates (verifiable per commit)

### 6.1 Commit 0 — `test(eval): add shadow determinism baseline`

**Files added:**
- `v12/tests/fixtures/determinism_baseline.json`
- `v12/tests/test_shadow_determinism.py`
- `v12/scripts/refresh_determinism_baseline.py`

**Acceptance:**
- `pytest v12/tests/test_shadow_determinism.py` passes (locks today's hashes).
- `pytest v12/tests/` — all 27 existing tests still pass.
- `python -m v12.scripts.refresh_determinism_baseline` exits 0 and bumps
  `schema_version` from 1 to 2 (idempotency check: re-run, hash must match,
  schema bumps from 2 to 3, etc.).
- `git log -1 --format=%H` matches `determinism_baseline.json:lock_commit`.

### 6.2 Commit 1 — `feat(contracts): add v12.contracts.ChronosBackend Protocol`

**Files added:**
- `v12/src/stockapp_v12/contracts/__init__.py`
- `v12/src/stockapp_v12/contracts/chronos_backend.py`

**Files modified:**
- `v12/src/stockapp_v12/services/forecast_service.py` — delete inline
  `class ChronosBackend(Protocol):` (lines 21–24), add 1-line re-export shim.
- `v12/src/stockapp_v12/__init__.py` — add 2 entries to imports + `__all__`.

**Acceptance:**
- `from stockapp_v12.contracts import ChronosBackend` succeeds in a fresh venv
  (only depends on `pandas` + `typing`, no `services.chronos_service` import).
- `from stockapp_v12.services.forecast_service import ChronosBackend` is
  **identity-equal** to the contract import (`is` check).
- `pytest v12/tests/` — all 27 existing + 3 new determinism tests still pass.
- `python -c "import sys; sys.path = [p for p in sys.path if 'src' not in p]; from stockapp_v12.contracts import ChronosBackend; print('OK')"`
  succeeds — proves the Protocol is importable without the `PYTHONPATH` hack.
- `__name__` and `__file__` of every existing module unchanged
  (`git diff --stat` shows only file additions and the 2-file modification; no renames).

### 6.3 Commit 2 — `feat(contracts): add v12.contracts.Notifier Protocol`

**Files added:**
- `v12/src/stockapp_v12/contracts/notifier.py`

**Files modified:**
- `v12/src/stockapp_v12/services/notification_service.py` — add 1-line re-export shim.
- `v12/src/stockapp_v12/__init__.py` — add 1 entry to imports + `__all__`.

**Acceptance:**
- `from stockapp_v12.contracts import Notifier` succeeds in a fresh venv.
- `from stockapp_v12.services.notification_service import notifier` continues
  to work; `isinstance(notifier, Notifier)` is `True`.
- `pytest v12/tests/` — all 27 existing + 3 new determinism tests still pass.
- All `_Notifier` methods called by existing call sites
  (`entry_orchestrator.py`, `daily_review.py`, `position_manager.py`) continue
  to be invoked identically (zero call-site change).

### 6.4 Commit 3 — (GATE) CI gate validation

No code changes. Runs:

1. The full pytest suite on a fresh CI runner (proves portability).
2. `python -m v12.scripts.shadow_trade --models baseline_v1 --predict-today`
   against a known past eval date in a fixture Redis; diff per-date JSON against
   a pre-recorded reference day; assert byte-identical.
3. (Manual) Verify NUC shadow crons at 16:05 + 16:10 ET still pass ADR-008 gate
   the next trading day after commit lands.

**Acceptance:** CI green + next NUC cron ADR-008 green → this phase closes.
B2–B5 move to the post-launch roadmap.

---

## 7. Risk Register (carry-forward from Council § 3.3)

| ID | Risk | Source | Mitigation in this phase |
|---|---|---|---|
| **R1** | Protocol signature drift breaks implicit duck-typed call sites | Council § 3.4 Pragmatist | Commits 1–2 strictly passive; identical method signatures (`detect`/`detect_batch`/`notify_*`); 27 existing tests catch any drift. |
| **R2** | `__name__` / `__file__` change in observability surface forks dashboards / alerting / SR-07 / ADR-008 audit trail | Council § 3.4 Critic (observability drift) | Existing modules unchanged; only new `contracts/` package added. `git diff --stat` confirms no file renames. |
| **R3** | Determinism baseline drift hides a real behavior change | Council § 3.3 Critic | § 5.6 CI gate: single-hash drift = auto-revert or refresh; multi-hash drift = STOP + Council. |
| **R4** | Frozen backbone (ADR-008) produces different bytes in CI vs NUC | Council § 3.3 Critic (implicit globals) | § 5.4 fixture uses `DeterministicFakeChronos` — no real Chronos loaded; baseline is backend-independent. The 16:05/16:10 NUC crons are validated separately per § 6.4. |
| **R5** | `multiprocessing.Pool` fork-shape on shadow harness | Council § 3.3 Critic | Moot for this phase (no fork changes); tracked as § 5.3 future-B2 risk. |
| **R6** | `YoloFilterDecision` forward-ref string still relies on sys.modules hack (T8) | Council § 3.4 (deferred) | Out of scope (NG-extension). Tracked under post-launch roadmap. |

---

## 8. Implementation Order (binding)

Per Council § 4.4:

| Order | Commit hash (placeholder) | What lands |
|---|---|---|
| **0** | `<TBD>` | `test(eval): add shadow determinism baseline` |
| **1** | `<TBD>` | `feat(contracts): add v12.contracts.ChronosBackend Protocol` |
| **2** | `<TBD>` | `feat(contracts): add v12.contracts.Notifier Protocol` |
| **3** | (GATE) | CI gate validation, no code |

Each commit lands separately. Between commits: run full pytest + manual review
of diff. After commit 2 lands: 24-hour soak (next NUC cron at 16:05 + 16:10 ET
must pass ADR-008). After soak: commit 3 gate validation.

---

## 9. Update Protocol

When a new contract lands:

1. Append a new § 3.x section with the Protocol definition + signature.
2. Add the file to § 2 module layout tree.
3. Add the re-export shim spec to § 4.x.
4. Add a new § 6.x commit acceptance block.
5. If the contract is risky (e.g. tight coupling to a real impl), add a row to § 7.

When the determinism baseline is refreshed:

1. Bump `determinism_baseline.json:schema_version`.
2. Update `determinism_baseline.json:locked_at` + `lock_commit`.
3. Cite the change in the commit message body (what drifted and why).

---

## 10. Changelog

- **2026-07-02** — Phase 3 design drafted. Synthesizes Council § 3.4 verdict
  into concrete files (§ 2–4), determinism baseline test (§ 5), per-commit
  acceptance gates (§ 6), and risk register (§ 7). Authored from
  `01-COUNCIL-LOG.md` § 3.4 + raw `services/forecast_service.py` and
  `services/notification_service.py` signatures.