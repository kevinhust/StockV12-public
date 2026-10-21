# Architecture Decoupling Upgrade — Phase 0–1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish measurable import baselines (Phase 0) and ship the first zero-behavior-change decoupling slice: complete ports surface, composition root, inject `MarketDataClient` into `SwingScanner`, inject `Notifier` into entry stages, and cage default Chronos archive resolution behind an explicit adapter factory (Phase 1).

**Architecture:** Hexagonal Modular Monolith (approved design). Keep package name `stockapp_v12`; introduce `adapters/` + `app/` without relocating the entire tree yet. All production call sites that today hit `archive/` via `ensure_v11_archive_on_path` gain an indirection so later phases can delete archive from PYTHONPATH.

**Tech Stack:** Python 3.12, pytest, existing `typing.Protocol` ports in `stockapp_v12.contracts`, optional `import-linter` (or a 50-line AST checker if dependency add is undesired), Redis only where already used (no new store ports in this plan).

**Spec:** `docs/superpowers/specs/2026-07-26-architecture-decoupling-upgrade-design.md` (Approved 2026-07-26)

**Branch convention:** `feature/arch-decouple-p0p1`. One logical commit per task below.

## Global Constraints

- No change to trading semantics, INV-1..7, R-01..R-10, ADR-008 determinism, ensemble A+B decision path, HaltGate paper-only.
- No big-bang move of `services/` or Redis key renames (Phase 2–3).
- Prefer constructor injection over module-global mutation.
- Every new port method is frozen for v12.x (positional stability).
- Re-export shims required for any moved public symbol.
- Tests must pass with `PYTHONPATH=src:scripts` (existing `pyproject.toml`).

---

## File Structure

### New files (Phase 0–1)

| Path | Responsibility |
|------|----------------|
| `docs/architecture/07-IMPORT-BASELINE.md` | Fan-in/out + archive call-site inventory (Phase 0 artifact) |
| `docs/ADR-011-Hexagonal-Modular-Monolith.md` | Accepted architecture decision pointer |
| `scripts/check_import_boundaries.py` | Warn/fail on forbidden imports (no new dep) |
| `src/stockapp_v12/ports/__init__.py` | Re-export of contracts as `ports` alias (optional package facade) |
| `src/stockapp_v12/adapters/__init__.py` | Adapter package marker |
| `src/stockapp_v12/adapters/legacy_v11/__init__.py` | Cage for archive imports |
| `src/stockapp_v12/adapters/legacy_v11/chronos.py` | `LegacyChronosAdapter` wrapping archive ChronosService |
| `src/stockapp_v12/adapters/legacy_v11/longbridge_market_data.py` | `LegacyLongbridgeMarketData` implementing MarketDataClient |
| `src/stockapp_v12/adapters/marketdata/composite.py` | Composite client: LB prefer + Polygon fallback (current scanner logic) |
| `src/stockapp_v12/adapters/marketdata/fake.py` | `FakeMarketData` for tests |
| `src/stockapp_v12/adapters/clock.py` | `SystemClock` + `FakeClock` |
| `src/stockapp_v12/app/__init__.py` | Composition root package |
| `src/stockapp_v12/app/wiring.py` | `build_runtime()` / `Settings` |
| `src/stockapp_v12/contracts/clock.py` | `Clock` Protocol |
| `src/stockapp_v12/contracts/broker.py` | Re-export / mirror of `BrokerAdapter` from core.execution |
| `tests/test_import_boundaries.py` | Unit tests for boundary checker modes |
| `tests/test_legacy_chronos_adapter.py` | Adapter factory + no bare archive import in ForecastService when injected |
| `tests/test_swing_scanner_market_data_injection.py` | Scanner uses injected MarketDataClient |
| `tests/test_wiring.py` | Composition root builds without archive if fakes provided |
| `tests/test_stages_notifier_injection.py` | Stages use orch.notifier, not global import |

### Modified files

| Path | Change |
|------|--------|
| `src/stockapp_v12/contracts/__init__.py` | Export `Clock`, `BrokerAdapter` |
| `src/stockapp_v12/__init__.py` | Re-export new contracts |
| `src/stockapp_v12/services/swing_scanner.py` | Accept optional `market_data: MarketDataClient` |
| `src/stockapp_v12/services/forecast_service.py` | Resolve default backend via `adapters.legacy_v11.chronos` |
| `src/stockapp_v12/services/entry_orchestrator.py` | Optional `notifier` inject; default to compat shim |
| `src/stockapp_v12/pipeline/stages.py` | Use `orch.notifier` instead of deferred import |
| `docs/architecture/README.md` | Link import baseline |
| `docs/MODULE_MAP.md` | Note `adapters/` + `app/` (Phase 1) |
| `docs/superpowers/specs/2026-07-26-architecture-decoupling-upgrade-design.md` | Status → Accepted |
| `pyproject.toml` | Optional marker `boundary` for checker tests |

### Explicitly out of scope this plan

- `PositionStore` / `JournalStore` / full Redis abstraction (Phase 3)
- Splitting `services/` into domain packages (Phase 2)
- Deleting archive from NUC PYTHONPATH (Phase 4)
- Rewriting all scripts to CLI modules (Phase 5)

---

### Task 1: Mark design accepted + ADR-011 stub

**Files:**
- Modify: `docs/superpowers/specs/2026-07-26-architecture-decoupling-upgrade-design.md` (Status field)
- Create: `docs/ADR-011-Hexagonal-Modular-Monolith.md`
- Modify: `docs/architecture/07-DECOUPLING-UPGRADE-BLUEPRINT.md` (Status → Accepted)

**Interfaces:**
- Produces: ADR-011 as the decision record; no runtime API

- [ ] **Step 1: Update design status line**

In the design SSOT header table, set:

```markdown
| **Status** | Accepted 2026-07-26 |
```

- [ ] **Step 2: Write ADR-011**

```markdown
# ADR-011: Hexagonal Modular Monolith + Strangler for V11 Archive

- **Status:** Accepted
- **Date:** 2026-07-26
- **Context:** V12 needs upgrade-friendly decoupling without microservice cost.
- **Decision:** Adopt hexagonal modular monolith; confine archive to adapters/legacy_v11; complete ports before package splits.
- **Consequences:** import boundaries enforced; MarketData/Chronos/Broker/Notifier/Clock via ports; microservices deferred.
- **Detail SSOT:** docs/superpowers/specs/2026-07-26-architecture-decoupling-upgrade-design.md
- **Implementation plan (P0–P1):** docs/superpowers/plans/2026-07-26-architecture-decoupling-phase0-1.md
```

- [ ] **Step 3: Commit**

```bash
git add docs/ADR-011-Hexagonal-Modular-Monolith.md \
  docs/superpowers/specs/2026-07-26-architecture-decoupling-upgrade-design.md \
  docs/architecture/07-DECOUPLING-UPGRADE-BLUEPRINT.md
git commit -m "docs(adr-011): accept hexagonal modular monolith decoupling"
```

---

### Task 2: Phase 0 — archive call-site inventory + import baseline

**Files:**
- Create: `docs/architecture/07-IMPORT-BASELINE.md`
- Modify: `docs/architecture/README.md` (link the baseline)

**Interfaces:**
- Produces: human-readable inventory used by later tasks to prove call-site reduction

- [ ] **Step 1: Generate inventory with shell (do not hand-wave)**

Run from repo root:

```bash
# Archive / path seams
rg -n 'ensure_v11_archive_on_path|from src\.|sys\.path\.insert' \
  src/stockapp_v12 scripts --glob '*.{py,sh}' \
  | rg -v '__pycache__|archive/' \
  > /tmp/v12_archive_seams.txt

# Cross-package imports inside stockapp_v12
rg -n '^from stockapp_v12\.|^import stockapp_v12\.' \
  src/stockapp_v12 --glob '*.py' \
  | rg -v '__pycache__' \
  > /tmp/v12_internal_imports.txt

wc -l /tmp/v12_archive_seams.txt /tmp/v12_internal_imports.txt
```

- [ ] **Step 2: Write `docs/architecture/07-IMPORT-BASELINE.md`**

Structure (fill tables from Step 1 output — do not leave empty rows):

```markdown
# Import Baseline — 2026-07-26 (Phase 0)

## Archive / sys.path seams

| File | Pattern | Notes |
|------|---------|-------|
| ... | ensure_v11 / from src. / sys.path | ... |

## High fan-in modules (top 15 by inbound imports)

| Module | Approx inbound | Role |
|--------|----------------|------|
| ... | | |

## Known reverse edges (forbidden in target)

| From | To | Issue |
|------|-----|-------|
| pipeline.stages | services.* | reverse layer |
| pipeline.entry_window | services.entry_orchestrator | stale path |

## Phase 1 target deltas

- [ ] New archive seams only under adapters/legacy_v11
- [ ] ForecastService default path goes through adapters/legacy_v11
- [ ] SwingScanner accepts MarketDataClient (no new archive calls in tests)
```

- [ ] **Step 3: Link from architecture README**

Add a row pointing to `07-IMPORT-BASELINE.md`.

- [ ] **Step 4: Commit**

```bash
git add docs/architecture/07-IMPORT-BASELINE.md docs/architecture/README.md
git commit -m "docs(arch): phase 0 import and archive seam baseline"
```

---

### Task 3: Import boundary checker (warn mode)

**Files:**
- Create: `scripts/check_import_boundaries.py`
- Create: `tests/test_import_boundaries.py`

**Interfaces:**
- Produces: `check_import_boundaries.main(argv) -> int` exit code 0 warn-only by default; `--strict` fails on violations
- Forbidden edges (v1 ruleset):
  - `stockapp_v12.domain` — N/A until created; skip
  - `stockapp_v12.adapters` must not be imported by `pipeline`, `core` (except re-exports), `schemas`, `contracts`
  - `stockapp_v12.app` must not be imported by `services`, `pipeline`, `core`, `eval`
  - After Phase 1: `services.forecast_service` and `services.swing_scanner` must not call `ensure_v11` directly (only adapters may) — enforce in **strict** mode only after Task 6–7 land; for Task 3 implement rule engine + sample rules

- [ ] **Step 1: Write failing test for parser**

```python
# tests/test_import_boundaries.py
from pathlib import Path
from scripts.check_import_boundaries import find_violations, Rule

SAMPLE = '''
from stockapp_v12.adapters.legacy_v11.chronos import LegacyChronosAdapter
'''


def test_finds_forbidden_adapters_import_in_pipeline(tmp_path: Path):
    f = tmp_path / "stages.py"
    f.write_text(SAMPLE)
    rules = [
        Rule(
            name="pipeline_no_adapters",
            importer_glob="**/pipeline/**/*.py",
            forbidden_module_prefixes=("stockapp_v12.adapters",),
        )
    ]
    # Pretend file lives under pipeline/
    pipeline_f = tmp_path / "pipeline" / "stages.py"
    pipeline_f.parent.mkdir()
    pipeline_f.write_text(SAMPLE)
    viols = find_violations(root=tmp_path, rules=rules)
    assert any("stockapp_v12.adapters" in v.message for v in viols)
```

Note: if `scripts` is not importable as package, implement tests by importing via path:

```python
import importlib.util
from pathlib import Path

def _load():
    p = Path("scripts/check_import_boundaries.py")
    spec = importlib.util.spec_from_file_location("check_import_boundaries", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
```

- [ ] **Step 2: Run test — expect FAIL (module missing)**

```bash
pytest tests/test_import_boundaries.py -v
```

Expected: import/path error or `find_violations` missing.

- [ ] **Step 3: Implement minimal checker**

```python
# scripts/check_import_boundaries.py
"""AST-based import boundary checker for stockapp_v12.

Default mode: --warn (exit 0, print violations).
Strict mode: --strict (exit 1 if any violation).
"""
from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple


@dataclass(frozen=True)
class Rule:
    name: str
    importer_glob: str  # relative to root, e.g. "src/stockapp_v12/pipeline/**/*.py"
    forbidden_module_prefixes: Tuple[str, ...]


@dataclass(frozen=True)
class Violation:
    rule: str
    path: str
    module: str
    message: str


DEFAULT_RULES: Tuple[Rule, ...] = (
    Rule(
        name="pipeline_no_adapters",
        importer_glob="src/stockapp_v12/pipeline/**/*.py",
        forbidden_module_prefixes=("stockapp_v12.adapters", "stockapp_v12.app"),
    ),
    Rule(
        name="contracts_no_adapters",
        importer_glob="src/stockapp_v12/contracts/**/*.py",
        forbidden_module_prefixes=("stockapp_v12.adapters", "stockapp_v12.app", "stockapp_v12.services"),
    ),
    Rule(
        name="core_no_adapters_app",
        importer_glob="src/stockapp_v12/core/**/*.py",
        forbidden_module_prefixes=("stockapp_v12.adapters", "stockapp_v12.app"),
    ),
)


def _iter_imports(path: Path) -> Iterable[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                yield node.module


def find_violations(root: Path, rules: Sequence[Rule] = DEFAULT_RULES) -> List[Violation]:
    out: List[Violation] = []
    for rule in rules:
        for path in root.glob(rule.importer_glob):
            if "__pycache__" in path.parts:
                continue
            for mod in _iter_imports(path):
                for prefix in rule.forbidden_module_prefixes:
                    if mod == prefix or mod.startswith(prefix + "."):
                        out.append(
                            Violation(
                                rule=rule.name,
                                path=str(path.relative_to(root)),
                                module=mod,
                                message=f"{path}: imports {mod} (rule {rule.name})",
                            )
                        )
    return out


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--warn", action="store_true", default=True)
    args = parser.parse_args(argv)
    viols = find_violations(args.root.resolve())
    for v in viols:
        print(v.message)
    if args.strict and viols:
        print(f"FAIL: {len(viols)} boundary violation(s)", file=sys.stderr)
        return 1
    print(f"OK ({'strict' if args.strict else 'warn'}): {len(viols)} known issue(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests + warn baseline on real tree**

```bash
pytest tests/test_import_boundaries.py -v
python scripts/check_import_boundaries.py --root .
```

Expected: tests PASS; warn mode exit 0 (may print 0 violations initially — good).

- [ ] **Step 5: Commit**

```bash
git add scripts/check_import_boundaries.py tests/test_import_boundaries.py
git commit -m "chore(arch): add import boundary checker (warn mode)"
```

---

### Task 4: Clock port + SystemClock / FakeClock

**Files:**
- Create: `src/stockapp_v12/contracts/clock.py`
- Create: `src/stockapp_v12/adapters/clock.py`
- Create: `src/stockapp_v12/adapters/__init__.py`
- Modify: `src/stockapp_v12/contracts/__init__.py`
- Modify: `src/stockapp_v12/__init__.py`
- Test: `tests/test_clock_port.py` (create)

**Interfaces:**
- Produces:

```python
class Clock(Protocol):
    def now_et(self) -> datetime: ...
    def today_et(self) -> date: ...

class SystemClock:
    def now_et(self) -> datetime: ...
    def today_et(self) -> date: ...

class FakeClock:
    def __init__(self, fixed: datetime): ...
    def now_et(self) -> datetime: ...
    def today_et(self) -> date: ...
```

- [ ] **Step 1: Failing test**

```python
# tests/test_clock_port.py
from datetime import datetime, date
from zoneinfo import ZoneInfo
from stockapp_v12.contracts.clock import Clock
from stockapp_v12.adapters.clock import FakeClock, SystemClock

EST = ZoneInfo("America/New_York")


def test_fake_clock_today():
    fixed = datetime(2026, 7, 15, 16, 5, tzinfo=EST)
    clock: Clock = FakeClock(fixed)
    assert clock.today_et() == date(2026, 7, 15)
    assert clock.now_et() == fixed


def test_system_clock_returns_et_aware():
    c = SystemClock()
    n = c.now_et()
    assert n.tzinfo is not None
    assert c.today_et() == n.date()
```

- [ ] **Step 2: Run — FAIL**

```bash
pytest tests/test_clock_port.py -v
```

- [ ] **Step 3: Implement Protocol + adapters**

```python
# src/stockapp_v12/contracts/clock.py
from __future__ import annotations
from datetime import date, datetime
from typing import Protocol

class Clock(Protocol):
    def now_et(self) -> datetime: ...
    def today_et(self) -> date: ...

__all__ = ["Clock"]
```

```python
# src/stockapp_v12/adapters/clock.py
from __future__ import annotations
from datetime import date, datetime
from zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")

class SystemClock:
    def now_et(self) -> datetime:
        return datetime.now(EST)

    def today_et(self) -> date:
        return self.now_et().date()

class FakeClock:
    def __init__(self, fixed: datetime):
        if fixed.tzinfo is None:
            fixed = fixed.replace(tzinfo=EST)
        self._fixed = fixed

    def now_et(self) -> datetime:
        return self._fixed

    def today_et(self) -> date:
        return self._fixed.astimezone(EST).date()
```

Re-export `Clock` from `contracts/__init__.py` and package root `__init__.py` the same way as `Notifier`.

- [ ] **Step 4: Run — PASS**

```bash
pytest tests/test_clock_port.py -v
```

- [ ] **Step 5: Commit**

```bash
git add src/stockapp_v12/contracts/clock.py src/stockapp_v12/adapters/ \
  src/stockapp_v12/contracts/__init__.py src/stockapp_v12/__init__.py \
  tests/test_clock_port.py
git commit -m "feat(ports): add Clock protocol with SystemClock and FakeClock"
```

---

### Task 5: BrokerAdapter re-export under contracts

**Files:**
- Create: `src/stockapp_v12/contracts/broker.py`
- Modify: `src/stockapp_v12/contracts/__init__.py`
- Modify: `src/stockapp_v12/__init__.py`
- Modify: `src/stockapp_v12/core/execution/bracket_order.py` (optional comment only — keep class defined for BC)
- Test: extend `tests/test_chronos_backend_protocol.py` pattern → `tests/test_broker_port.py`

**Interfaces:**
- Produces: `stockapp_v12.contracts.BrokerAdapter` is the **same Protocol object** as `stockapp_v12.core.execution.bracket_order.BrokerAdapter` **or** a duplicated Protocol with identical methods (prefer re-export to avoid dual Protocol identity issues)

Recommended approach — re-export:

```python
# src/stockapp_v12/contracts/broker.py
"""BrokerAdapter port — canonical import path for hexagonal consumers."""
from stockapp_v12.core.execution.bracket_order import BrokerAdapter

__all__ = ["BrokerAdapter"]
```

Note: this creates `contracts → core` dependency. Acceptable short-term; Phase 2 may move Protocol body into contracts and reverse the re-export. Document that in MODULE_MAP.

- [ ] **Step 1: Test re-export identity**

```python
# tests/test_broker_port.py
from stockapp_v12.contracts.broker import BrokerAdapter as FromContracts
from stockapp_v12.core.execution.bracket_order import BrokerAdapter as FromCore

def test_broker_adapter_reexport_identity():
    assert FromContracts is FromCore
```

- [ ] **Step 2–4: Implement, pass, commit**

```bash
git commit -m "feat(ports): expose BrokerAdapter via contracts package"
```

---

### Task 6: Legacy Chronos adapter + ForecastService default path

**Files:**
- Create: `src/stockapp_v12/adapters/legacy_v11/__init__.py`
- Create: `src/stockapp_v12/adapters/legacy_v11/chronos.py`
- Modify: `src/stockapp_v12/services/forecast_service.py` (`_resolve_backend` only)
- Test: `tests/test_legacy_chronos_adapter.py`
- Existing: `tests/test_forecast_service_degraded_log.py` must stay green

**Interfaces:**
- Produces:

```python
def build_legacy_chronos_backend(*, model_path: str, prediction_length: int) -> ChronosBackend | None:
    """Return ChronosBackend or None on soft failure (import/init). Never raises to caller."""

class LegacyChronosAdapter:
    """Wraps archive ChronosService; implements detect/detect_batch."""
    def detect(self, df: pd.DataFrame) -> list[dict]: ...
    def detect_batch(self, frames: dict[str, pd.DataFrame]) -> dict[str, list[dict]]: ...
```

- Consumes: `ensure_v11_archive_on_path` **only inside** `adapters/legacy_v11/chronos.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_legacy_chronos_adapter.py
import inspect
from stockapp_v12.services import forecast_service as fs_mod
from stockapp_v12.adapters.legacy_v11 import chronos as legacy_chronos


def test_forecast_service_source_has_no_direct_ensure_v11():
    src = inspect.getsource(fs_mod.ForecastService._resolve_backend)
    assert "ensure_v11_archive_on_path" not in src
    assert "from src.services.chronos_service" not in src


def test_legacy_module_is_only_archive_gateway():
    src = inspect.getsource(legacy_chronos)
    assert "ensure_v11_archive_on_path" in src
```

- [ ] **Step 2: Run — FAIL** (current forecast_service still has ensure_v11)

```bash
pytest tests/test_legacy_chronos_adapter.py -v
```

- [ ] **Step 3: Implement adapter**

```python
# src/stockapp_v12/adapters/legacy_v11/chronos.py
from __future__ import annotations
import logging
import os
from typing import Any, Dict, Optional
import pandas as pd
from stockapp_v12.core._v11_compat import ensure_v11_archive_on_path

logger = logging.getLogger(__name__)


class LegacyChronosAdapter:
    def __init__(self, inner: Any):
        self._inner = inner

    def detect(self, df: pd.DataFrame) -> list[dict[str, Any]]:
        return list(self._inner.detect(df) or [])

    def detect_batch(self, frames: Dict[str, pd.DataFrame]) -> Dict[str, list[dict[str, Any]]]:
        return dict(self._inner.detect_batch(frames) or {})


def build_legacy_chronos_backend(
    *,
    model_path: Optional[str] = None,
    prediction_length: int = 10,
) -> Optional[LegacyChronosAdapter]:
    try:
        ensure_v11_archive_on_path()
        from src.services.chronos_service import ChronosService
    except ImportError as e:
        logger.warning("legacy Chronos import failed: %s", e)
        return None
    path = model_path or os.getenv("CHRONOS_MODEL_PATH", "models/chronos")
    try:
        inner = ChronosService(model_path=path, prediction_length=prediction_length)
    except Exception as e:
        logger.warning(
            "legacy Chronos init failed (model_path=%s): %s: %s",
            path, type(e).__name__, e,
        )
        return None
    return LegacyChronosAdapter(inner)
```

- [ ] **Step 4: Change ForecastService._resolve_backend**

Replace archive import block with:

```python
from stockapp_v12.adapters.legacy_v11.chronos import build_legacy_chronos_backend

self._resolved_backend = build_legacy_chronos_backend(
    prediction_length=self.horizon_days,
)
return self._resolved_backend
```

Preserve existing warning semantics / `None` fallback (read current file; match log messages if tests assert them — check `tests/test_forecast_service_degraded_log.py` and `tests/test_fail_closed_safety.py`).

- [ ] **Step 5: Run focused + related tests**

```bash
pytest tests/test_legacy_chronos_adapter.py tests/test_forecast_service_degraded_log.py \
  tests/test_forecast_output_schema.py tests/test_chronos_backend_protocol.py -v
```

Expected: PASS (skip known pre-existing failures only if already listed in MIGRATION_DEBT).

- [ ] **Step 6: Commit**

```bash
git commit -m "refactor(forecast): cage archive Chronos behind LegacyChronosAdapter"
```

---

### Task 7: MarketDataClient injection into SwingScanner

**Files:**
- Create: `src/stockapp_v12/adapters/marketdata/__init__.py`
- Create: `src/stockapp_v12/adapters/marketdata/fake.py`
- Create: `src/stockapp_v12/adapters/marketdata/composite.py`
- Create: `src/stockapp_v12/adapters/legacy_v11/longbridge_market_data.py` (optional if composite keeps LB init internal)
- Modify: `src/stockapp_v12/services/swing_scanner.py`
- Test: `tests/test_swing_scanner_market_data_injection.py`
- Existing: `tests/test_swing_scanner.py` must stay green

**Interfaces:**
- Produces:

```python
class FakeMarketData:
    def __init__(self, bars: dict[str, pd.DataFrame] | None = None, universe: list[str] | None = None): ...
    def fetch_daily_bars(self, symbol: str, days: int = 260) -> pd.DataFrame | None: ...
    def fetch_latest_close(self, symbol: str) -> float | None: ...
    def universe(self, tier: str = "default") -> list[str]: ...

class CompositeMarketDataClient:
    """Behavioral equivalent of current SwingScanner._fetch_daily_bars routing."""
    def __init__(self, *, lb_service=None, polygon_client=None, prefer_longbridge: bool = True, polygon_fallback: bool = True): ...
```

- Modify `SwingScanner.__init__`:

```python
def __init__(
    self,
    config_path: Optional[str] = None,
    redis_client: Optional[redis.Redis] = None,
    *,
    market_data: Optional[MarketDataClient] = None,
):
    ...
    self.market_data = market_data  # may be None → build default composite
```

- Change `_fetch_daily_bars` to:

```python
def _fetch_daily_bars(self, symbol: str, days: int = 260) -> Optional[pd.DataFrame]:
    if self.market_data is not None:
        return self.market_data.fetch_daily_bars(symbol, days=days)
    return self._fetch_daily_bars_builtin(symbol, days)
```

Rename current body to `_fetch_daily_bars_builtin` **or** construct default `CompositeMarketDataClient` in `__init__` when `market_data is None` (preferred — single path).

**Preferred single path:**

```python
if market_data is not None:
    self.market_data = market_data
else:
    self.lb_service = _init_longbridge()
    self._init_polygon()
    self.market_data = CompositeMarketDataClient(
        lb_service=self.lb_service,
        polygon_client=self.polygon_client,
        prefer_longbridge=self.config.get("data", {}).get("prefer_longbridge", True),
        polygon_fallback=self.config.get("data", {}).get("polygon_fallback", True),
    )
```

`_fetch_daily_bars` always delegates to `self.market_data.fetch_daily_bars`.

- [ ] **Step 1: Failing injection test**

```python
# tests/test_swing_scanner_market_data_injection.py
import pandas as pd
from stockapp_v12.adapters.marketdata.fake import FakeMarketData
from stockapp_v12.services.swing_scanner import SwingScanner


def _bars(n=80):
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1_000_000,
        },
        index=idx,
    )


def test_scanner_uses_injected_market_data(monkeypatch):
    fake = FakeMarketData(bars={"AAPL": _bars()})
    # Prevent accidental network/archive init when only market_data provided
    s = SwingScanner(config_path=None, redis_client=None, market_data=fake)
    out = s._fetch_daily_bars("AAPL", days=60)
    assert out is not None
    assert len(out) >= 30
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
```

- [ ] **Step 2: Run — FAIL** (unexpected kwarg)

```bash
pytest tests/test_swing_scanner_market_data_injection.py -v
```

- [ ] **Step 3: Implement Fake + Composite + constructor change**

Move logic from current `_fetch_daily_bars` into `CompositeMarketDataClient.fetch_daily_bars` (copy carefully; preserve prefer_lb + polygon fallback + min 30 rows + column filter).

Implement `fetch_latest_close` as last close of `fetch_daily_bars` or None.

Implement `universe` returning `[]` in composite (scanner still loads universe from yaml as today — do not change universe source in Phase 1).

- [ ] **Step 4: Run scanner tests**

```bash
pytest tests/test_swing_scanner.py tests/test_swing_scanner_market_data_injection.py \
  tests/test_market_data_client_protocol.py -v
```

Expected: PASS. If existing tests monkeypatch `_fetch_daily_bars`, keep that method as a thin wrapper so monkeypatch still works:

```python
def _fetch_daily_bars(self, symbol: str, days: int = 260):
    return self.market_data.fetch_daily_bars(symbol, days=days)
```

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(scan): inject MarketDataClient into SwingScanner"
```

---

### Task 8: Notifier injection into EntryOrchestrator + stages

**Files:**
- Modify: `src/stockapp_v12/services/entry_orchestrator.py`
- Modify: `src/stockapp_v12/pipeline/stages.py` (lines ~340 and ~387 deferred imports)
- Test: `tests/test_stages_notifier_injection.py`
- Existing entry/veto tests must stay green

**Interfaces:**
- Produces: `EntryOrchestrator(..., notifier: Notifier | None = None)`  
  default: `from stockapp_v12.services._notifier_compat import notifier as default_notifier`
- Stages use `orch.notifier.notify_order_intent(...)` / `notify_order_submitted(...)`

- [ ] **Step 1: Failing test**

```python
# tests/test_stages_notifier_injection.py
import inspect
from stockapp_v12.pipeline import stages


def test_stages_source_has_no_notifier_compat_import():
    src = inspect.getsource(stages)
    assert "services._notifier_compat" not in src
    assert "notification_service" not in src
```

Also behavioral test with a recording fake:

```python
class RecordingNotifier:
    def __init__(self):
        self.calls = []
    def notify_order_intent(self, **kwargs):
        self.calls.append(("intent", kwargs))
    def notify_order_submitted(self, **kwargs):
        self.calls.append(("submitted", kwargs))
    # stub remaining Notifier methods as no-ops
    def __getattr__(self, name):
        return lambda **kwargs: None
```

Wire a minimal `orch` via `EntryOrchestrator.__new__` or existing fixtures from `tests/test_entry_orchestrator.py` if present — prefer reusing fixture patterns already in repo.

- [ ] **Step 2: Run — FAIL**

- [ ] **Step 3: Implement**

In `EntryOrchestrator.__init__` add:

```python
notifier: Optional[Notifier] = None,
...
from stockapp_v12.services._notifier_compat import notifier as _default_notifier
self.notifier = notifier if notifier is not None else _default_notifier
```

In `stages.py` replace both deferred imports with `orch.notifier`.

- [ ] **Step 4: Run**

```bash
pytest tests/test_stages_notifier_injection.py tests/test_entry_orchestrator.py \
  tests/test_veto_alarm.py -v --tb=short
```

- [ ] **Step 5: Commit**

```bash
git commit -m "refactor(entry): inject Notifier into orchestrator stages"
```

---

### Task 9: Composition root `app/wiring.py`

**Files:**
- Create: `src/stockapp_v12/app/__init__.py`
- Create: `src/stockapp_v12/app/wiring.py`
- Create: `tests/test_wiring.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Settings:
    redis_url: str | None = None
    chronos_model_path: str | None = None
    use_fake_market_data: bool = False

@dataclass
class Runtime:
    clock: Clock
    market_data: MarketDataClient
    notifier: Notifier
    # optional heavy pieces left None until needed
    forecast_backend: ChronosBackend | None = None

def build_runtime(settings: Settings | None = None) -> Runtime:
    ...
```

Phase 1 wiring builds **lightweight** runtime (clock + market_data + notifier + optional chronos). Full EntryOrchestrator assembly can remain in scripts for now — document `TODO(phase5): assemble EntryOrchestrator here`.

- [ ] **Step 1: Test**

```python
# tests/test_wiring.py
from stockapp_v12.app.wiring import Settings, build_runtime
from stockapp_v12.adapters.marketdata.fake import FakeMarketData


def test_build_runtime_with_fakes():
    rt = build_runtime(Settings(use_fake_market_data=True))
    assert rt.clock.today_et() is not None
    bars = rt.market_data.fetch_daily_bars("AAPL")
    # Fake default may return None — still a valid soft miss
    assert bars is None or hasattr(bars, "columns")


def test_build_runtime_accepts_external_market_data():
    fake = FakeMarketData()
    rt = build_runtime(Settings())
    # allow override via keyword in build_runtime
```

Implement `build_runtime(settings=None, *, market_data=None, notifier=None, clock=None)`.

- [ ] **Step 2–4: Implement, pass, commit**

```python
# src/stockapp_v12/app/wiring.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
from stockapp_v12.adapters.clock import SystemClock
from stockapp_v12.adapters.marketdata.fake import FakeMarketData
from stockapp_v12.contracts.clock import Clock
from stockapp_v12.contracts.market_data_client import MarketDataClient
from stockapp_v12.contracts.notifier import Notifier
from stockapp_v12.notifiers import make_notifier_from_env
from stockapp_v12.contracts.chronos_backend import ChronosBackend

@dataclass(frozen=True)
class Settings:
    use_fake_market_data: bool = False
    chronos_model_path: Optional[str] = None

@dataclass
class Runtime:
    clock: Clock
    market_data: MarketDataClient
    notifier: Notifier
    forecast_backend: Optional[ChronosBackend] = None

def build_runtime(
    settings: Optional[Settings] = None,
    *,
    clock: Optional[Clock] = None,
    market_data: Optional[MarketDataClient] = None,
    notifier: Optional[Notifier] = None,
    forecast_backend: Optional[ChronosBackend] = None,
    load_chronos: bool = False,
) -> Runtime:
    settings = settings or Settings()
    clock = clock or SystemClock()
    if market_data is None:
        if settings.use_fake_market_data:
            market_data = FakeMarketData()
        else:
            from stockapp_v12.adapters.marketdata.composite import CompositeMarketDataClient
            # Composite default: no live clients unless caller supplies — for unit safety
            market_data = CompositeMarketDataClient(lb_service=None, polygon_client=None)
    notifier = notifier or make_notifier_from_env()
    if forecast_backend is None and load_chronos:
        from stockapp_v12.adapters.legacy_v11.chronos import build_legacy_chronos_backend
        forecast_backend = build_legacy_chronos_backend(model_path=settings.chronos_model_path)
    return Runtime(
        clock=clock,
        market_data=market_data,
        notifier=notifier,
        forecast_backend=forecast_backend,
    )
```

```bash
git commit -m "feat(app): add composition root wiring for ports"
```

---

### Task 10: Wire one real consumer script (thin) + docs sync

**Files:**
- Modify: pick **one** low-risk script that already constructs `SwingScanner` — prefer `scripts/validate_longbridge_screener.py` **or** document-only if that script is integration-heavy
- Safer alternative: add `scripts/demo_wiring_runtime.py` that only prints runtime types (no network) — use this if touching validate script is risky
- Modify: `docs/MODULE_MAP.md` (add M16 adapters, M17 app)
- Modify: `docs/architecture/07-IMPORT-BASELINE.md` (Phase 1 delta checkboxes)
- Modify: `docs/architecture/07-DECOUPLING-UPGRADE-BLUEPRINT.md` (Phase 0–1 status)

**Recommended script (zero network by default):**

```python
# scripts/demo_wiring_runtime.py
"""Smoke entrypoint: proves composition root builds with fakes."""
from stockapp_v12.app.wiring import Settings, build_runtime

def main() -> int:
    rt = build_runtime(Settings(use_fake_market_data=True))
    print("clock", rt.clock.today_et())
    print("market_data", type(rt.market_data).__name__)
    print("notifier", type(rt.notifier).__name__)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 1: Run demo**

```bash
PYTHONPATH=src:scripts python scripts/demo_wiring_runtime.py
```

Expected: exit 0, prints FakeMarketData / LogNotifier (or env notifier).

- [ ] **Step 2: Update MODULE_MAP** with adapters + app rows and dependency note.

- [ ] **Step 3: Mark Phase 0–1 checkboxes in baseline doc** where completed.

- [ ] **Step 4: Commit**

```bash
git commit -m "docs(arch): phase 0-1 complete — wiring demo and module map"
```

---

### Task 11: Phase 0–1 verification gate (whole suite slice)

**Files:** none (verification only)

- [ ] **Step 1: Boundary warn**

```bash
python scripts/check_import_boundaries.py --root .
```

- [ ] **Step 2: Targeted suite**

```bash
pytest \
  tests/test_import_boundaries.py \
  tests/test_clock_port.py \
  tests/test_broker_port.py \
  tests/test_legacy_chronos_adapter.py \
  tests/test_swing_scanner_market_data_injection.py \
  tests/test_stages_notifier_injection.py \
  tests/test_wiring.py \
  tests/test_market_data_client_protocol.py \
  tests/test_chronos_backend_protocol.py \
  tests/test_swing_scanner.py \
  tests/test_forecast_output_schema.py \
  tests/test_entry_orchestrator.py \
  -v --tb=short
```

Expected: all green (or only pre-documented MIGRATION_DEBT failures — do not introduce new ones).

- [ ] **Step 3: Grep gate for Phase 1 goals**

```bash
# ForecastService must not call ensure_v11 directly
rg -n 'ensure_v11_archive_on_path' src/stockapp_v12/services/forecast_service.py
# expect no matches

# Archive ensure only under adapters/legacy_v11 or _v11_compat definition
rg -n 'ensure_v11_archive_on_path' src/stockapp_v12 --glob '*.py'
# allow: core/_v11_compat.py, adapters/legacy_v11/*, tests

# stages must not import notifier compat
rg -n '_notifier_compat|notification_service' src/stockapp_v12/pipeline/stages.py
# expect no matches
```

- [ ] **Step 4: Optional full suite**

```bash
pytest -q --tb=line
```

Record pass/fail count in `docs/architecture/07-IMPORT-BASELINE.md` under “Phase 1 verification”.

- [ ] **Step 5: Final commit if docs updated**

```bash
git commit -m "test(arch): record phase 0-1 verification results"
```

---

## Phase 0–1 Done Definition (checklist)

- [ ] ADR-011 accepted in repo
- [ ] Import baseline document exists with real inventory
- [ ] Boundary checker runs in warn mode
- [ ] `Clock` port + fakes exist
- [ ] `BrokerAdapter` importable from `contracts`
- [ ] Archive Chronos only via `adapters/legacy_v11/chronos.py`
- [ ] `SwingScanner(market_data=...)` works with `FakeMarketData`
- [ ] Stages use `orch.notifier` (no deferred service import)
- [ ] `build_runtime()` composition root exists
- [ ] Targeted pytest gate green
- [ ] No intentional trading semantic changes

---

## Spec coverage (self-review)

| Design § (Phase 0–1) | Task |
|----------------------|------|
| Phase 0 measure + inventory | Task 2 |
| Import lint warn mode | Task 3 |
| Ports: Clock | Task 4 |
| Ports: Broker exposure | Task 5 |
| Chronos behind adapter / no bare archive in ForecastService | Task 6 |
| MarketDataClient wired into Scanner | Task 7 |
| Notifier injection (stages) | Task 8 |
| Composition root | Task 9 |
| Thin consumer / docs | Task 10 |
| Done gates / verification | Task 11 |
| ADR-011 | Task 1 |
| PositionStore / services split / archive delete | **Out of scope** (Phase 2–4) |

## Placeholder scan

No TBD steps; concrete paths and code included.

## Type consistency

- `MarketDataClient.fetch_daily_bars(symbol, days=260) -> Optional[DataFrame]`
- `Clock.now_et() / today_et()`
- `build_legacy_chronos_backend(...) -> Optional[LegacyChronosAdapter]`
- `Runtime` holds `clock`, `market_data`, `notifier`, `forecast_backend`

---

## After this plan

Phase 2 plan (separate doc): split `services/` with move+shim; migrate DTO types out of services so `pipeline` has zero services imports at runtime.

Do **not** start Phase 2 until Task 11 verification is green on the feature branch.
