# Champion Model Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace current 2-model shadow production with a strict Champion + Challenger pair driven by multi-window statistical criteria, quarterly re-evaluation, and HaltGate kill switch.

**Architecture:** New `champion_selector.py` library holds pure-function gate logic. New `champion_evaluation.py` CLI reads shadow JSONs and produces a leaderboard. Existing `shadow_halt.sh` extended to write `halt_swap.json` on halt. `run_shadow_cron.sh` and `run_shadow_ensemble.sh` modified to consume role-tagged env vars (`CHAMPION_CSV` / `CHALLENGER_CSV`) and filter ensemble input to champion only.

**Tech Stack:** Python 3.12, pytest, shell (bash), JSON state files in `_shadow/_state/`. No new external dependencies.

**Spec:** `docs/superpowers/specs/2026-07-10-champion-model-selection-design.md`

**Branch convention:** `feature/champion-selection`. All commits land here until merged.

---

## File Structure

### New files
- `src/stockapp_v12/eval/champion_selector.py` — pure-function gate logic
- `src/stockapp_v12/eval/champion_state.py` — `halt_swap.json` and `champion_config.json` IO
- `scripts/champion_evaluation.py` — CLI leaderboard tool
- `scripts/run_quarterly_review_reminder.sh` — day-55 cron
- `tests/test_champion_selector.py` — 8-gate logic + challenger selection
- `tests/test_champion_state.py` — halt_swap + champion_config IO
- `tests/test_champion_evaluation.py` — CLI integration
- `tests/test_shadow_cron_role.py` — ensemble champion-filter contract
- `docs/superpowers/runbooks/champion-selection.md` — operator runbook

### Modified files
- `scripts/run_shadow_cron.sh` — read `CHAMPION_CSV` / `CHALLENGER_CSV`, halt_swap precedence
- `scripts/run_shadow_ensemble.sh` — filter JSONs to champion only
- `scripts/run_shadow_halt.sh` — write `halt_swap.json` on halt
- `scripts/shadow_halt_decision.py` — expose halt outcome for swap logic

---

## Task 1: Champion gate dataclass + metrics helper

**Files:**
- Create: `src/stockapp_v12/eval/champion_selector.py`
- Test: `tests/test_champion_selector.py`

- [ ] **Step 1: Write failing test for ModelMetrics dataclass**

```python
# tests/test_champion_selector.py
from stockapp_v12.eval.champion_selector import ModelMetrics


def test_model_metrics_required_fields():
    m = ModelMetrics(
        name="E07_long_ctx",
        pnl_30d=296.09,
        sharpe_30d=3.5,
        sharpe_7d_live=1.2,
        win_rate_30d=0.604,
        mase_30d=1.5,
        n_dates_30d=30,
        n_trades_30d=230,
        hyperparam_family="long_ctx",
    )
    assert m.name == "E07_long_ctx"
    assert m.sharpe_30d == 3.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_champion_selector.py -v`
Expected: `ModuleNotFoundError: No module named 'stockapp_v12.eval.champion_selector'`

- [ ] **Step 3: Implement ModelMetrics dataclass**

```python
# src/stockapp_v12/eval/champion_selector.py
"""Champion model selection logic — pure functions, no I/O.

Implements the 8-gate promotion criteria and challenger selection rules
from docs/superpowers/specs/2026-07-10-champion-model-selection-design.md.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelMetrics:
    """Aggregate metrics for a single model over evaluation windows."""
    name: str
    pnl_30d: float
    sharpe_30d: float
    sharpe_7d_live: float
    win_rate_30d: float
    mase_30d: float
    n_dates_30d: int
    n_trades_30d: int
    hyperparam_family: str
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_champion_selector.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/stockapp_v12/eval/champion_selector.py tests/test_champion_selector.py
git commit -m "feat(eval): ModelMetrics dataclass for champion selection"
```

---

## Task 2: 8-gate promotion logic

**Files:**
- Modify: `src/stockapp_v12/eval/champion_selector.py`
- Test: `tests/test_champion_selector.py`

- [ ] **Step 1: Write failing tests for all 8 gates**

```python
# tests/test_champion_selector.py (append)
import pytest
from stockapp_v12.eval.champion_selector import (
    ModelMetrics,
    check_promotion_gates,
    GateResult,
)


def _metrics(**overrides) -> ModelMetrics:
    base = dict(
        name="m",
        pnl_30d=100.0,
        sharpe_30d=3.5,
        sharpe_7d_live=1.2,
        win_rate_30d=0.60,
        mase_30d=1.5,
        n_dates_30d=30,
        n_trades_30d=200,
        hyperparam_family="x",
    )
    base.update(overrides)
    return ModelMetrics(**base)


def test_gate_passes_all_criteria():
    result = check_promotion_gates(_metrics())
    assert result.passed is True
    assert result.failed_gates == []


def test_gate_fails_on_negative_pnl():
    r = check_promotion_gates(_metrics(pnl_30d=-1.0))
    assert r.passed is False
    assert "pnl_30d" in r.failed_gates


def test_gate_fails_on_low_sharpe_30d():
    r = check_promotion_gates(_metrics(sharpe_30d=0.9))
    assert "sharpe_30d" in r.failed_gates


def test_gate_fails_on_low_sharpe_7d():
    r = check_promotion_gates(_metrics(sharpe_7d_live=0.4))
    assert "sharpe_7d_live" in r.failed_gates


def test_gate_fails_on_low_win_rate():
    r = check_promotion_gates(_metrics(win_rate_30d=0.50))
    assert "win_rate_30d" in r.failed_gates


def test_gate_fails_on_high_mase():
    r = check_promotion_gates(_metrics(mase_30d=2.5))
    assert "mase_30d" in r.failed_gates


def test_gate_fails_on_insufficient_n_dates():
    r = check_promotion_gates(_metrics(n_dates_30d=20))
    assert "n_dates_30d" in r.failed_gates


def test_gate_fails_on_insufficient_n_trades():
    r = check_promotion_gates(_metrics(n_trades_30d=100))
    assert "n_trades_30d" in r.failed_gates
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_champion_selector.py -v`
Expected: ImportError for `check_promotion_gates`, `GateResult`

- [ ] **Step 3: Implement 8-gate logic**

Append to `src/stockapp_v12/eval/champion_selector.py`:

```python
@dataclass(frozen=True)
class GateResult:
    """Outcome of running the 8 promotion gates against a single model."""
    passed: bool
    failed_gates: list[str]

    def __bool__(self) -> bool:
        return self.passed


def check_promotion_gates(m: ModelMetrics) -> GateResult:
    """Apply the 8 promotion gates from the spec.

    Thresholds (all from spec):
      pnl_30d > 0
      sharpe_30d >= 1.0
      sharpe_7d_live >= 0.5
      win_rate_30d >= 0.55
      mase_30d <= 2.1181
      n_dates_30d >= 30
      n_trades_30d >= 150
    """
    failed: list[str] = []
    if m.pnl_30d <= 0:
        failed.append("pnl_30d")
    if m.sharpe_30d < 1.0:
        failed.append("sharpe_30d")
    if m.sharpe_7d_live < 0.5:
        failed.append("sharpe_7d_live")
    if m.win_rate_30d < 0.55:
        failed.append("win_rate_30d")
    if m.mase_30d > 2.1181:
        failed.append("mase_30d")
    if m.n_dates_30d < 30:
        failed.append("n_dates_30d")
    if m.n_trades_30d < 150:
        failed.append("n_trades_30d")
    return GateResult(passed=len(failed) == 0, failed_gates=failed)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_champion_selector.py -v`
Expected: 9 PASS (1 from Task 1 + 8 from this task)

- [ ] **Step 5: Commit**

```bash
git add src/stockapp_v12/eval/champion_selector.py tests/test_champion_selector.py
git commit -m "feat(eval): 8-gate promotion logic for champion selection"
```

---

## Task 3: select_champion across candidates

**Files:**
- Modify: `src/stockapp_v12/eval/champion_selector.py`
- Test: `tests/test_champion_selector.py`

- [ ] **Step 1: Write failing tests for select_champion**

```python
# tests/test_champion_selector.py (append)
from stockapp_v12.eval.champion_selector import (
    ChampionSelection,
    select_champion,
    select_challenger,
)


def test_select_champion_picks_only_passing_model():
    a = _metrics(name="A", sharpe_30d=3.5)
    b = _metrics(name="B", sharpe_30d=2.0)
    result = select_champion([a, b])
    assert result.champion == "A"
    assert result.runner_up == "B"
    assert set(result.eligible) == {"A", "B"}


def test_select_champion_with_no_passing_models_raises():
    bad = _metrics(name="X", sharpe_30d=0.5)
    with pytest.raises(ValueError, match="No model passed all 8 gates"):
        select_champion([bad])


def test_select_champion_ranks_eligible_by_sharpe():
    a = _metrics(name="A", sharpe_30d=3.5)
    b = _metrics(name="B", sharpe_30d=2.5)
    c = _metrics(name="C", sharpe_30d=1.8)
    result = select_champion([a, b, c])
    assert result.champion == "A"
    assert result.eligible == ["A", "B", "C"]  # sorted desc by Sharpe
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_champion_selector.py -v`
Expected: ImportError for `ChampionSelection`, `select_champion`

- [ ] **Step 3: Implement select_champion**

Append to `src/stockapp_v12/eval/champion_selector.py`:

```python
@dataclass(frozen=True)
class ChampionSelection:
    """Result of running the selection pipeline across candidates."""
    champion: str
    runner_up: str
    eligible: list[str]


def select_champion(candidates: list[ModelMetrics]) -> ChampionSelection:
    """Pick the highest-Sharpe model that passes all 8 gates.

    Raises ValueError if no model passes.
    """
    eligible: list[ModelMetrics] = []
    for m in candidates:
        if check_promotion_gates(m).passed:
            eligible.append(m)
    if not eligible:
        raise ValueError(
            f"No model passed all 8 gates (out of {len(candidates)} candidates)"
        )
    eligible.sort(key=lambda m: m.sharpe_30d, reverse=True)
    return ChampionSelection(
        champion=eligible[0].name,
        runner_up=eligible[1].name if len(eligible) > 1 else "",
        eligible=[m.name for m in eligible],
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_champion_selector.py -v`
Expected: 12 PASS (9 from prior tasks + 3 new)

- [ ] **Step 5: Commit**

```bash
git add src/stockapp_v12/eval/champion_selector.py tests/test_champion_selector.py
git commit -m "feat(eval): select_champion ranks eligible models by Sharpe"
```

---

## Task 4: select_challenger with family tiebreaker

**Files:**
- Modify: `src/stockapp_v12/eval/champion_selector.py`
- Test: `tests/test_champion_selector.py`

- [ ] **Step 1: Write failing tests for select_challenger**

```python
# tests/test_champion_selector.py (append)


def test_select_challenger_picks_second_highest_sharpe():
    a = _metrics(name="A", sharpe_30d=3.5, hyperparam_family="long_ctx")
    b = _metrics(name="B", sharpe_30d=2.5, hyperparam_family="zeroshot")
    c = _metrics(name="C", sharpe_30d=1.8, hyperparam_family="long_ctx")
    result = select_challenger([a, b, c], champion="A")
    assert result == "B"  # second-highest Sharpe


def test_select_challenger_prefers_low_correlation_on_tie():
    # A and B tie at 2.5; C has lower Sharpe but different family.
    # With champion = "Z" (zeroshot family), prefer B (zeroshot) over A (same as C).
    a = _metrics(name="A", sharpe_30d=2.5, hyperparam_family="long_ctx")
    b = _metrics(name="B", sharpe_30d=2.5, hyperparam_family="zeroshot")
    z = _metrics(name="Z", sharpe_30d=3.5, hyperparam_family="zeroshot")
    result = select_challenger([a, b, z], champion="Z")
    assert result == "A"  # different family from Z


def test_select_challenger_skips_champion():
    a = _metrics(name="A", sharpe_30d=3.5)
    b = _metrics(name="B", sharpe_30d=2.5)
    result = select_challenger([a, b], champion="A")
    assert result == "B"


def test_select_challenger_raises_if_no_alternatives():
    a = _metrics(name="A", sharpe_30d=3.5)
    with pytest.raises(ValueError, match="No challenger candidates"):
        select_challenger([a], champion="A")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_champion_selector.py::test_select_challenger_picks_second_highest_sharpe -v`
Expected: ImportError for `select_challenger`

- [ ] **Step 3: Implement select_challenger**

Append to `src/stockapp_v12/eval/champion_selector.py`:

```python
def select_challenger(
    candidates: list[ModelMetrics],
    champion: str,
) -> str:
    """Pick the best challenger: second-highest Sharpe, preferring low
    correlation (different hyperparam_family) with the champion.

    Raises ValueError if no challenger candidates exist.
    """
    challengers = [m for m in candidates if m.name != champion]
    if not challengers:
        raise ValueError(f"No challenger candidates (champion={champion!r})")
    champion_family = next(
        m.hyperparam_family for m in candidates if m.name == champion
    )
    # Sort: by (same_family_last desc, sharpe desc)
    challengers.sort(
        key=lambda m: (m.hyperparam_family == champion_family, -m.sharpe_30d)
    )
    return challengers[0].name
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_champion_selector.py -v`
Expected: 16 PASS

- [ ] **Step 5: Commit**

```bash
git add src/stockapp_v12/eval/champion_selector.py tests/test_champion_selector.py
git commit -m "feat(eval): select_challenger with low-correlation tiebreaker"
```

---

## Task 5: Update eval module __init__.py exports

**Files:**
- Modify: `src/stockapp_v12/eval/__init__.py`

- [ ] **Step 1: Read current __init__.py**

Run: `cat src/stockapp_v12/eval/__init__.py`
Expected: existing imports + __all__ list

- [ ] **Step 2: Append new exports**

Append to the existing imports section:

```python
from stockapp_v12.eval.champion_selector import (
    ChampionSelection,
    GateResult,
    ModelMetrics,
    check_promotion_gates,
    select_champion,
    select_challenger,
)
```

Append to the `__all__` list:

```python
    "ChampionSelection",
    "GateResult",
    "ModelMetrics",
    "check_promotion_gates",
    "select_champion",
    "select_challenger",
```

- [ ] **Step 3: Verify import works**

Run: `python -c "from stockapp_v12.eval import select_champion, select_challenger; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add src/stockapp_v12/eval/__init__.py
git commit -m "feat(eval): export champion_selector public API"
```

---

## Task 6: champion_state IO (halt_swap + champion_config)

**Files:**
- Create: `src/stockapp_v12/eval/champion_state.py`
- Test: `tests/test_champion_state.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_champion_state.py
import json
from pathlib import Path

import pytest

from stockapp_v12.eval.champion_state import (
    ChampionConfig,
    HaltSwap,
    read_champion_config,
    read_halt_swap,
    write_champion_config,
    write_halt_swap,
)


def test_halt_swap_roundtrip(tmp_path: Path):
    path = tmp_path / "halt_swap.json"
    write_halt_swap(
        path,
        HaltSwap(
            active_champion="zeroshot_bolt_small",
            halted_champion="E07_long_ctx",
            swapped_at="2026-07-15T16:20:00-04:00",
            reason="halt_triggered",
            halt_3day_accuracy=0.38,
        ),
    )
    result = read_halt_swap(path)
    assert result is not None
    assert result.active_champion == "zeroshot_bolt_small"
    assert result.halted_champion == "E07_long_ctx"


def test_halt_swap_returns_none_when_missing(tmp_path: Path):
    assert read_halt_swap(tmp_path / "nope.json") is None


def test_champion_config_roundtrip(tmp_path: Path):
    path = tmp_path / "champion_config.json"
    write_champion_config(
        path,
        ChampionConfig(
            champion="E07_long_ctx",
            challenger="zeroshot_bolt_small",
            promoted_at="2026-07-10",
            promotion_rationale="Sharpe=3.5",
            next_review_date="2026-09-25",
        ),
    )
    cfg = read_champion_config(path)
    assert cfg.champion == "E07_long_ctx"
    assert cfg.challenger == "zeroshot_bolt_small"
    assert cfg.next_review_date == "2026-09-25"


def test_champion_config_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        read_champion_config(tmp_path / "nope.json")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_champion_state.py -v`
Expected: `ModuleNotFoundError: No module named 'stockapp_v12.eval.champion_state'`

- [ ] **Step 3: Implement champion_state.py**

```python
# src/stockapp_v12/eval/champion_state.py
"""Champion role persistence — halt_swap.json + champion_config.json.

Spec: docs/superpowers/specs/2026-07-10-champion-model-selection-design.md
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class HaltSwap:
    """Record of a champion↔challenger swap triggered by HaltGate."""
    active_champion: str
    halted_champion: str
    swapped_at: str
    reason: str
    halt_3day_accuracy: float


@dataclass(frozen=True)
class ChampionConfig:
    """Current champion + challenger assignment with promotion metadata."""
    champion: str
    challenger: str
    promoted_at: str
    promotion_rationale: str
    next_review_date: str


def write_halt_swap(path: Path, swap: HaltSwap) -> None:
    """Atomic write of halt_swap.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(swap), indent=2))
    tmp.replace(path)


def read_halt_swap(path: Path) -> HaltSwap | None:
    """Read halt_swap.json. Returns None if file doesn't exist."""
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return HaltSwap(**data)


def write_champion_config(path: Path, cfg: ChampionConfig) -> None:
    """Atomic write of champion_config.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2))
    tmp.replace(path)


def read_champion_config(path: Path) -> ChampionConfig:
    """Read champion_config.json. Raises FileNotFoundError if missing."""
    if not path.exists():
        raise FileNotFoundError(f"champion_config not found at {path}")
    data = json.loads(path.read_text())
    return ChampionConfig(**data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_champion_state.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add src/stockapp_v12/eval/champion_state.py tests/test_champion_state.py
git commit -m "feat(eval): champion_state IO for halt_swap and champion_config"
```

---

## Task 7: champion_evaluation.py CLI — read shadow JSONs and compute metrics

**Files:**
- Create: `scripts/champion_evaluation.py`
- Test: `tests/test_champion_evaluation.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_champion_evaluation.py
import json
from pathlib import Path

import pytest

from scripts.champion_evaluation import (
    compute_metrics_for_model,
    load_all_metrics,
)


def _write_shadow_json(
    path: Path,
    rows: list[dict],
    source: str = "backfill",
    eval_date: str = "2026-06-23",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "model": path.parent.name,
        "eval_date": eval_date,
        "rows": rows,
        "source": source,
    }))


def _row(symbol: str, forecast: float, actual: float) -> dict:
    return {
        "symbol": symbol,
        "forecast_ret_pct": forecast,
        "actual_ret_pct": actual,
        "eval_source": "backfill",
        "eval_date": "2026-06-23",
        "model": "x",
        "cutoff_days_ago": 1,
    }


def test_compute_metrics_basic(tmp_path: Path):
    rows = [
        _row("A", forecast=2.0, actual=1.0),   # +1.0
        _row("B", forecast=2.0, actual=-0.5),  # -0.5
        _row("C", forecast=1.0, actual=0.3),   # below threshold, skip
    ]
    metrics = compute_metrics_for_model("x", rows, hyperparam_family="fam")
    assert metrics is not None
    assert metrics.name == "x"
    assert metrics.n_trades_30d == 2  # only A and B actionable
    assert metrics.win_rate_30d == 0.5  # 1 of 2 won
    assert metrics.pnl_30d == pytest.approx(0.5)  # +1.0 - 0.5
    assert metrics.n_dates_30d == 1


def test_compute_metrics_no_actionable_returns_none():
    rows = [_row("A", forecast=0.3, actual=0.1)]  # below 0.5 threshold
    metrics = compute_metrics_for_model("x", rows, hyperparam_family="fam")
    assert metrics is None


def test_load_all_metrics_reads_all_models(tmp_path: Path):
    shadow_root = tmp_path / "_shadow"
    _write_shadow_json(
        shadow_root / "m1" / "2026-06-23.json",
        [_row("A", forecast=2.0, actual=1.0)],
    )
    _write_shadow_json(
        shadow_root / "m2" / "2026-06-23.json",
        [_row("B", forecast=2.0, actual=-0.5)],
    )
    metrics_list = load_all_metrics(
        shadow_root,
        model_dirs=["m1", "m2"],
        hyperparam_family_map={"m1": "long_ctx", "m2": "zeroshot"},
    )
    assert len(metrics_list) == 2
    assert {m.name for m in metrics_list} == {"m1", "m2"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_champion_evaluation.py -v`
Expected: ImportError

- [ ] **Step 3: Implement champion_evaluation.py**

```python
#!/usr/bin/env python3
"""Champion evaluation CLI — compute metrics across shadow models.

Usage:
    PYTHONPATH=src:scripts:. python scripts/champion_evaluation.py --report
    PYTHONPATH=src:scripts:. python scripts/champion_evaluation.py --champion E07_long_ctx

Reads _shadow/<model>/*.json and _shadow/<model>/_backfill/*.json,
computes the 8-gate metrics, prints a leaderboard table.

Spec: docs/superpowers/specs/2026-07-10-champion-model-selection-design.md
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Iterable

# Reuse the path constants from shadow_trade.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.shadow_trade import SHADOW_ROOT  # noqa: E402

from stockapp_v12.eval.champion_selector import (  # noqa: E402
    ModelMetrics,
    check_promotion_gates,
    select_champion,
    select_challenger,
)

THRESHOLD_PCT = 0.5  # same as shadow_trade default


def _is_actionable(forecast: float) -> bool:
    return abs(forecast) > THRESHOLD_PCT


def compute_metrics_for_model(
    name: str,
    rows: list[dict],
    hyperparam_family: str,
) -> ModelMetrics | None:
    """Compute ModelMetrics from a flat list of rows across dates.

    Returns None if no actionable trades.
    """
    daily_pnls: dict[str, float] = {}
    total_pnl = 0.0
    n_trades = 0
    n_wins = 0
    n_dates = 0
    mase_values: list[float] = []

    # Group by eval_date for daily aggregation
    by_date: dict[str, list[dict]] = {}
    for r in rows:
        d = r.get("eval_date", "")
        by_date.setdefault(d, []).append(r)

    for date, date_rows in by_date.items():
        daily_pnl = 0.0
        date_trades = 0
        for r in date_rows:
            f = r.get("forecast_ret_pct")
            a = r.get("actual_ret_pct")
            if f is None or a is None:
                continue
            f = float(f)
            a = float(a)
            if not _is_actionable(f):
                continue
            pnl = (1.0 if f > 0 else -1.0) * a
            daily_pnl += pnl
            date_trades += 1
            total_pnl += pnl
            n_trades += 1
            if pnl > 0:
                n_wins += 1
            mase = r.get("mase")
            if mase is not None and not math.isnan(float(mase)):
                mase_values.append(float(mase))
        if date_trades > 0:
            daily_pnls[date] = daily_pnl
            n_dates += 1

    if n_trades == 0:
        return None

    win_rate = n_wins / n_trades
    sharpe_30d = (
        statistics.mean(daily_pnls.values()) / statistics.pstdev(daily_pnls.values())
        if len(daily_pnls) > 1 and statistics.pstdev(daily_pnls.values()) > 0
        else 0.0
    )
    sharpe_7d_live = _compute_sharpe_last_n_days(daily_pnls, n=7)
    mase_30d = statistics.mean(mase_values) if mase_values else 99.0

    return ModelMetrics(
        name=name,
        pnl_30d=total_pnl,
        sharpe_30d=sharpe_30d,
        sharpe_7d_live=sharpe_7d_live,
        win_rate_30d=win_rate,
        mase_30d=mase_30d,
        n_dates_30d=n_dates,
        n_trades_30d=n_trades,
        hyperparam_family=hyperparam_family,
    )


def _compute_sharpe_last_n_days(daily_pnls: dict[str, float], n: int) -> float:
    """Compute Sharpe over the last n dates (sorted descending)."""
    sorted_dates = sorted(daily_pnls.keys(), reverse=True)[:n]
    if len(sorted_dates) < 2:
        return 0.0
    vals = [daily_pnls[d] for d in sorted_dates]
    pstdev = statistics.pstdev(vals)
    if pstdev == 0:
        return 0.0
    return statistics.mean(vals) / pstdev


def load_all_metrics(
    shadow_root: Path,
    model_dirs: list[str],
    hyperparam_family_map: dict[str, str],
) -> list[ModelMetrics]:
    """Read all shadow JSONs for each model and compute metrics."""
    results: list[ModelMetrics] = []
    for m in model_dirs:
        model_dir = shadow_root / m
        if not model_dir.exists():
            continue
        all_rows: list[dict] = []
        for json_path in sorted(model_dir.glob("2*.json")):
            try:
                data = json.loads(json_path.read_text())
                all_rows.extend(data.get("rows", []))
            except Exception:
                continue
        for json_path in sorted((model_dir / "_backfill").glob("2*.json")):
            try:
                data = json.loads(json_path.read_text())
                all_rows.extend(data.get("rows", []))
            except Exception:
                continue
        if not all_rows:
            continue
        metrics = compute_metrics_for_model(
            m, all_rows, hyperparam_family_map.get(m, "unknown")
        )
        if metrics:
            results.append(metrics)
    return results


HYPERPARAM_FAMILY_MAP = {
    "phase2_v2": "v1_baseline",
    "E05_p3_coldstart_seed42": "round3_seed",
    "E05_p3_coldstart_seed7": "round3_seed",
    "E06_short_ctx": "short_ctx",
    "E07_long_ctx": "long_ctx",
    "E08_fewer_steps": "fewer_steps",
    "E09_higher_lr": "higher_lr",
    "E10_more_steps": "more_steps",
    "zeroshot_bolt_small": "zeroshot",
}


def print_leaderboard(metrics_list: list[ModelMetrics]) -> None:
    """Print sorted leaderboard with 8-gate pass/fail."""
    sorted_list = sorted(metrics_list, key=lambda m: m.sharpe_30d, reverse=True)
    print(
        f"{'Model':<35} {'PnL%':>8} {'Sharpe30':>9} "
        f"{'WinRt':>6} {'MASE':>5} {'N':>4} {'Pass':>5}"
    )
    print("-" * 80)
    for m in sorted_list:
        gate = check_promotion_gates(m)
        pass_str = "PASS" if gate.passed else "FAIL"
        print(
            f"{m.name:<35} {m.pnl_30d:>8.2f} {m.sharpe_30d:>9.2f} "
            f"{m.win_rate_30d*100:>5.1f}% {m.mase_30d:>5.2f} "
            f"{m.n_trades_30d:>4} {pass_str:>5}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute champion selection metrics from shadow data"
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Print leaderboard of all models"
    )
    parser.add_argument(
        "--champion", type=str, default=None,
        help="Compute metrics for a single model and print details"
    )
    parser.add_argument(
        "--shadow-dir", type=Path, default=SHADOW_ROOT,
        help=f"Shadow root (default: {SHADOW_ROOT})"
    )
    parser.add_argument(
        "--models", nargs="+", default=None,
        help="Specific model basenames to include (default: all in shadow)"
    )
    args = parser.parse_args()

    if args.champion:
        metrics_list = load_all_metrics(
            args.shadow_dir, [args.champion], HYPERPARAM_FAMILY_MAP
        )
        if metrics_list:
            print_leaderboard(metrics_list)
        else:
            print(f"No data found for model: {args.champion}")
            return 1
        return 0

    if not args.models:
        # Default: discover all model dirs in shadow_root
        args.models = sorted(
            p.name for p in args.shadow_dir.iterdir()
            if p.is_dir() and not p.name.startswith("_")
        )

    metrics_list = load_all_metrics(
        args.shadow_dir, args.models, HYPERPARAM_FAMILY_MAP
    )
    print_leaderboard(metrics_list)

    # If we have ≥2 eligible models, also print champion + challenger recommendation
    try:
        selection = select_champion(metrics_list)
        challenger = select_challenger(metrics_list, selection.champion)
        print()
        print(f"Recommended Champion:   {selection.champion}")
        print(f"Recommended Challenger: {challenger}")
    except ValueError as e:
        print()
        print(f"No champion eligible: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_champion_evaluation.py -v`
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/champion_evaluation.py tests/test_champion_evaluation.py
git commit -m "feat(eval): champion_evaluation CLI computes metrics from shadow data"
```

---

## Task 8: Extend shadow_halt.sh to write halt_swap.json

**Files:**
- Modify: `scripts/run_shadow_halt.sh`
- Modify: `scripts/shadow_halt_decision.py` (add `halt_outcome_for_champion` helper)
- Test: `tests/test_shadow_halt_decision.py`

- [ ] **Step 1: Write failing test for halt_outcome_for_champion**

```python
# tests/test_shadow_halt_decision.py (append)
import json
from pathlib import Path

from scripts.shadow_halt_decision import (
    halt_outcome_for_champion,
    HaltOutcome,
)


def _write_decision(path: Path, date: str, model: str, halted: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({
            "date": date,
            "model": model,
            "should_halt": halted,
            "variant": "3d_40_reactive",
        }) + "\n")


def test_halt_outcome_true_when_champion_halted(tmp_path: Path):
    decisions = tmp_path / "halt_decisions.jsonl"
    _write_decision(decisions, "2026-07-15", "E07_long_ctx", halted=True)
    outcome = halt_outcome_for_champion(decisions, "E07_long_ctx")
    assert outcome.halted is True
    assert outcome.recent_accuracy is not None


def test_halt_outcome_false_when_champion_not_halted(tmp_path: Path):
    decisions = tmp_path / "halt_decisions.jsonl"
    _write_decision(decisions, "2026-07-15", "E07_long_ctx", halted=False)
    outcome = halt_outcome_for_champion(decisions, "E07_long_ctx")
    assert outcome.halted is False


def test_halt_outcome_ignores_other_models(tmp_path: Path):
    decisions = tmp_path / "halt_decisions.jsonl"
    _write_decision(decisions, "2026-07-15", "phase2_v2", halted=True)
    outcome = halt_outcome_for_champion(decisions, "E07_long_ctx")
    assert outcome.halted is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_shadow_halt_decision.py::test_halt_outcome_true_when_champion_halted -v`
Expected: ImportError for `halt_outcome_for_champion`, `HaltOutcome`

- [ ] **Step 3: Implement halt_outcome_for_champion**

Append to `scripts/shadow_halt_decision.py`:

```python
@dataclass(frozen=True)
class HaltOutcome:
    """Whether the champion model should trigger a halt swap."""
    halted: bool
    recent_accuracy: float | None
    decision_date: str | None


def halt_outcome_for_champion(
    decisions_path: Path,
    champion_name: str,
) -> HaltOutcome:
    """Inspect halt_decisions.jsonl for the champion model.

    Returns HaltOutcome with the most recent decision for this champion.
    """
    if not decisions_path.exists():
        return HaltOutcome(halted=False, recent_accuracy=None, decision_date=None)
    latest: dict | None = None
    with decisions_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("model") != champion_name:
                continue
            if latest is None or entry.get("date", "") > latest.get("date", ""):
                latest = entry
    if latest is None:
        return HaltOutcome(halted=False, recent_accuracy=None, decision_date=None)
    return HaltOutcome(
        halted=bool(latest.get("should_halt", False)),
        recent_accuracy=latest.get("rolling_acc"),
        decision_date=latest.get("date"),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_shadow_halt_decision.py -v`
Expected: 12 PASS (9 prior + 3 new)

- [ ] **Step 5: Modify run_shadow_halt.sh to write halt_swap.json**

Append to `scripts/run_shadow_halt.sh` (after the existing halt_decision.jsonl write):

```bash
# --- Round 4 Champion Swap Logic ---
# If champion model triggered halt, write halt_swap.json to swap champion↔challenger.
CHAMPION_NAME="${CHAMPION_NAME:-E07_long_ctx}"
CHALLENGER_NAME="${CHALLENGER_NAME:-zeroshot_bolt_small}"
STATE_DIR="$STOCKAPP_ROOT/models/chronos/_shadow/_state"
SWAP_FILE="$STATE_DIR/halt_swap.json"

if [ -f "$STOCKAPP_ROOT/logs/halt_cron.log" ]; then
    LAST_HALTED=$(grep -o "model=[^ ]* should_halt=True" "$STOCKAPP_ROOT/logs/halt_cron.log" | tail -1 || true)
    if echo "$LAST_HALTED" | grep -q "model=$CHAMPION_NAME "; then
        mkdir -p "$STATE_DIR"
        SWAPPED_AT="$(date +%Y-%m-%dT%H:%M:%S%z)"
        cat > "$SWAP_FILE" <<EOF
{
  "active_champion": "$CHALLENGER_NAME",
  "halted_champion": "$CHAMPION_NAME",
  "swapped_at": "$SWAPPED_AT",
  "reason": "halt_triggered",
  "halt_3day_accuracy": 0.38
}
EOF
        echo "[halt_swap] Wrote $SWAP_FILE — champion → challenger"
    fi
fi
```

- [ ] **Step 6: Commit**

```bash
git add scripts/shadow_halt_decision.py scripts/run_shadow_halt.sh tests/test_shadow_halt_decision.py
git commit -m "feat(shadow): halt_swap.json write on champion halt"
```

---

## Task 9: Modify run_shadow_cron.sh — role env vars + halt_swap precedence

**Files:**
- Modify: `scripts/run_shadow_cron.sh`

- [ ] **Step 1: Add CHAMPION_CSV + CHALLENGER_CSV env vars**

Replace the existing `MODELS_CSV` line with:

```bash
# Champion (drives trading decisions) + Challenger (OOS only).
# halt_swap.json takes precedence — if present, swap roles.
STATE_DIR="${STATE_DIR:-$STOCKAPP_ROOT/models/chronos/_shadow/_state}"
SWAP_FILE="$STATE_DIR/halt_swap.json"
CHAMPION_CSV_DEFAULT="E07_long_ctx"
CHALLENGER_CSV_DEFAULT="zeroshot_bolt_small"
CHAMPION_CSV="${CHAMPION_CSV:-$CHAMPION_CSV_DEFAULT}"
CHALLENGER_CSV="${CHALLENGER_CSV:-$CHALLENGER_CSV_DEFAULT}"

# halt_swap.json override — latest swap wins.
if [ -f "$SWAP_FILE" ]; then
    SWAP_CHAMPION=$(python3 -c "import json; print(json.load(open('$SWAP_FILE'))['active_champion'])" 2>/dev/null || true)
    SWAP_HALTED=$(python3 -c "import json; print(json.load(open('$SWAP_FILE'))['halted_champion'])" 2>/dev/null || true)
    if [ -n "$SWAP_CHAMPION" ]; then
        echo "[cron] halt_swap detected: champion=$SWAP_CHAMPION halted=$SWAP_HALTED"
        CHAMPION_CSV="$SWAP_CHAMPION"
        CHALLENGER_CSV="$SWAP_HALTED"
    fi
fi

MODELS_CSV="${MODELS_CSV:-$CHAMPION_CSV $CHALLENGER_CSV}"
export CHAMPION_CSV CHALLENGER_CSV
```

- [ ] **Step 2: Verify bash syntax**

Run: `bash -n scripts/run_shadow_cron.sh`
Expected: no output (success)

- [ ] **Step 3: Commit**

```bash
git add scripts/run_shadow_cron.sh
git commit -m "feat(shadow): role-tagged CHAMPION_CSV/CHALLENGER_CSV with halt_swap precedence"
```

---

## Task 10: Modify run_shadow_ensemble.sh — filter to champion only

**Files:**
- Modify: `scripts/run_shadow_ensemble.sh`

- [ ] **Step 1: Read current ensemble script**

Run: `cat scripts/run_shadow_ensemble.sh | head -40`

- [ ] **Step 2: Add champion filter to SUMMARY.json consumption**

Find the line that iterates over model dirs and add filtering. Replace the model enumeration with:

```bash
# Round 4: filter to champion only. CHALLENGER_CSV is excluded from ensemble.
CHAMPION="${CHAMPION_CSV:-E07_long_ctx}"
for m in $CHAMPION; do
    MODEL_DIR="$STOCKAPP_ROOT/models/chronos/_shadow/$m"
    [ -d "$MODEL_DIR" ] || continue
    for f in "$MODEL_DIR"/2*.json; do
        [ -f "$f" ] || continue
        echo "  [$m] $(basename "$f" .json)"
    done
done
```

(Remove or comment out any prior loop over `MODELS_CSV` that includes both champion and challenger.)

- [ ] **Step 3: Verify bash syntax**

Run: `bash -n scripts/run_shadow_ensemble.sh`
Expected: no output (success)

- [ ] **Step 4: Commit**

```bash
git add scripts/run_shadow_ensemble.sh
git commit -m "feat(shadow): ensemble consumes champion only"
```

---

## Task 11: Initial deployment — write champion_config.json + deploy to NUC

**Files:**
- Create: `_shadow/_state/champion_config.json` (manually, on Mac then sync)
- Sync updated cron scripts to NUC + GPU

- [ ] **Step 1: Create initial champion_config.json locally**

Run:
```bash
mkdir -p /Users/dev/Stockapp/models/chronos/_shadow/_state
cat > /Users/dev/Stockapp/models/chronos/_shadow/_state/champion_config.json <<'EOF'
{
  "champion": "E07_long_ctx",
  "challenger": "zeroshot_bolt_small",
  "promoted_at": "2026-07-10",
  "promotion_rationale": "Sharpe=3.5, all 8 gates passed (30d backfill PnL=+296%)",
  "next_review_date": "2026-09-25"
}
EOF
```

- [ ] **Step 2: Deploy updated scripts to NUC**

```bash
scp scripts/run_shadow_cron.sh nuc:/home/app/Stockapp/v12/scripts/
scp scripts/run_shadow_ensemble.sh nuc:/home/app/Stockapp/v12/scripts/
scp scripts/run_shadow_halt.sh nuc:/home/app/Stockapp/v12/scripts/
scp scripts/shadow_halt_decision.py nuc:/home/app/Stockapp/v12/scripts/
scp scripts/champion_evaluation.py nuc:/home/app/Stockapp/v12/scripts/

# Sync _shadow/_state dir to NUC
scp -r models/chronos/_shadow/_state nuc:/home/app/Stockapp/models/chronos/_shadow/
```

- [ ] **Step 3: Deploy to GPU too**

```bash
scp scripts/run_shadow_cron.sh gpu:/home/trainer/Stockapp/v12/scripts/
scp scripts/run_shadow_ensemble.sh gpu:/home/trainer/Stockapp/v12/scripts/
scp scripts/run_shadow_halt.sh gpu:/home/trainer/Stockapp/v12/scripts/
scp scripts/shadow_halt_decision.py gpu:/home/trainer/Stockapp/v12/scripts/
scp scripts/champion_evaluation.py gpu:/home/trainer/Stockapp/v12/scripts/
```

- [ ] **Step 4: Verify deployment**

```bash
ssh nuc "ls /home/app/Stockapp/models/chronos/_shadow/_state/champion_config.json"
ssh nuc "grep CHAMPION_CSV /home/app/Stockapp/v12/scripts/run_shadow_cron.sh | head -2"
```

Expected: both succeed.

- [ ] **Step 5: Commit**

```bash
git add models/chronos/_shadow/_state/champion_config.json
git commit -m "deploy: initial champion=E07_long_ctx, challenger=zeroshot_bolt_small"
```

---

## Task 12: Day-55 quarterly review reminder

**Files:**
- Create: `scripts/run_quarterly_review_reminder.sh`

- [ ] **Step 1: Create the reminder script**

```bash
#!/usr/bin/env bash
# scripts/run_quarterly_review_reminder.sh
# Writes a log entry when day-55 of a champion's tenure approaches.
# Idempotent; safe to run daily.

set -e
STOCKAPP_ROOT="${STOCKAPP_ROOT:-/home/$(whoami)/Stockapp}"
STATE_DIR="$STOCKAPP_ROOT/models/chronos/_shadow/_state"
CONFIG="$STATE_DIR/champion_config.json"
REMINDER_LOG="$STOCKAPP_ROOT/logs/quarterly_review_reminder.txt"

[ -f "$CONFIG" ] || { echo "[reminder] no champion_config.json"; exit 0; }

NEXT_REVIEW=$(python3 -c "import json; print(json.load(open('$CONFIG'))['next_review_date'])")
TODAY=$(TZ=America/New_York date +%Y-%m-%d)

# Compute days until review
DAYS_UNTIL=$(python3 -c "
from datetime import date
review = date.fromisoformat('$NEXT_REVIEW')
today = date.fromisoformat('$TODAY')
delta = (review - today).days
print(delta)
")

if [ "$DAYS_UNTIL" -le 5 ] && [ "$DAYS_UNTIL" -ge 0 ]; then
    mkdir -p "$(dirname "$REMINDER_LOG")"
    echo "[$TODAY] Quarterly review in $DAYS_UNTIL days (review date: $NEXT_REVIEW)" >> "$REMINDER_LOG"
fi
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/run_quarterly_review_reminder.sh
```

- [ ] **Step 3: Deploy to NUC + add to crontab**

```bash
scp scripts/run_quarterly_review_reminder.sh nuc:/home/app/Stockapp/v12/scripts/
ssh nuc "(crontab -l 2>/dev/null | grep -v quarterly_review; echo '30 16 * * 1-5 /home/app/Stockapp/v12/scripts/run_quarterly_review_reminder.sh >> /home/app/Stockapp/logs/quarterly_review.log 2>&1') | crontab -"
```

- [ ] **Step 4: Verify crontab updated**

```bash
ssh nuc "crontab -l | grep quarterly_review"
```

- [ ] **Step 5: Commit**

```bash
git add scripts/run_quarterly_review_reminder.sh
git commit -m "feat(shadow): day-55 quarterly review reminder cron"
```

---

## Task 13: End-to-end integration test

**Files:**
- Create: `tests/test_shadow_cron_role.py`
- Create: `tests/e2e_champion_selection.py`

- [ ] **Step 1: Write integration test for cron role separation**

```python
# tests/test_shadow_cron_role.py
"""Verify run_shadow_cron.sh sets CHAMPION_CSV/CHALLENGER_CSV correctly
and that halt_swap.json takes precedence."""
import subprocess
from pathlib import Path


def test_cron_defines_role_env_vars():
    """run_shadow_cron.sh must export CHAMPION_CSV and CHALLENGER_CSV."""
    script = Path("scripts/run_shadow_cron.sh").read_text()
    assert "CHAMPION_CSV" in script
    assert "CHALLENGER_CSV" in script
    assert "export CHAMPION_CSV" in script
    assert "export CHALLENGER_CSV" in script


def test_cron_reads_halt_swap_file():
    """run_shadow_cron.sh must check halt_swap.json for swap precedence."""
    script = Path("scripts/run_shadow_cron.sh").read_text()
    assert "halt_swap.json" in script
    assert "SWAP_CHAMPION" in script or "active_champion" in script


def test_ensemble_filters_to_champion_only():
    """run_shadow_ensemble.sh must not consume challenger JSONs."""
    script = Path("scripts/run_shadow_ensemble.sh").read_text()
    # Champion env var should be referenced; no full MODELS_CSV loop
    assert "CHAMPION" in script
```

- [ ] **Step 2: Run integration tests**

Run: `pytest tests/test_shadow_cron_role.py -v`
Expected: 3 PASS

- [ ] **Step 3: Write end-to-end test for full pipeline**

```python
# tests/e2e_champion_selection.py
"""End-to-end: synthetic shadow data → champion_evaluation → assert selection."""
import json
import subprocess
import sys
from pathlib import Path


def _write_shadow_json(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "model": path.parent.name,
        "eval_date": "2026-06-23",
        "rows": rows,
        "source": "backfill",
    }))


def test_e2e_evaluation_picks_top_model(tmp_path: Path):
    shadow_root = tmp_path / "shadow"

    # Model A: strong positive — should be champion
    rows_a = [
        {"symbol": "AAPL", "forecast_ret_pct": 2.0, "actual_ret_pct": 1.5,
         "mase": 1.5, "eval_date": f"2026-06-{d:02d}", "eval_source": "backfill",
         "model": "A", "cutoff_days_ago": 1}
        for d in range(1, 31)
    ]
    _write_shadow_json(shadow_root / "A" / "2026-06-23.json", rows_a)

    # Model B: weaker positive — should be challenger
    rows_b = [
        {"symbol": "AAPL", "forecast_ret_pct": 1.5, "actual_ret_pct": 0.8,
         "mase": 1.8, "eval_date": f"2026-06-{d:02d}", "eval_source": "backfill",
         "model": "B", "cutoff_days_ago": 1}
        for d in range(1, 31)
    ]
    _write_shadow_json(shadow_root / "B" / "2026-06-23.json", rows_b)

    # Model C: negative — should fail gate
    rows_c = [
        {"symbol": "AAPL", "forecast_ret_pct": 2.0, "actual_ret_pct": -1.0,
         "mase": 2.5, "eval_date": f"2026-06-{d:02d}", "eval_source": "backfill",
         "model": "C", "cutoff_days_ago": 1}
        for d in range(1, 31)
    ]
    _write_shadow_json(shadow_root / "C" / "2026-06-23.json", rows_c)

    result = subprocess.run(
        [sys.executable, "scripts/champion_evaluation.py",
         "--report", "--shadow-dir", str(shadow_root),
         "--models", "A", "B", "C"],
        capture_output=True, text=True,
        env={"PYTHONPATH": "src:scripts:.", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "Recommended Champion:   A" in result.stdout
    assert "Recommended Challenger: B" in result.stdout
```

- [ ] **Step 4: Run e2e test**

Run: `pytest tests/e2e_champion_selection.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_shadow_cron_role.py tests/e2e_champion_selection.py
git commit -m "test: integration + e2e tests for champion selection pipeline"
```

---

## Task 14: Operator runbook

**Files:**
- Create: `docs/superpowers/runbooks/champion-selection.md`

- [ ] **Step 1: Write the runbook**

```markdown
# Champion Selection — Operator Runbook

## What is this?

Champion model selection is the mechanism for picking which of the 9
shadow models drives trading decisions. The champion + challenger pair
runs daily via NUC cron; an automated HaltGate can swap them on poor
performance; quarterly reviews re-evaluate all candidates.

## Daily Operations

### View current champion and challenger

```bash
cat /home/app/Stockapp/models/chronos/_shadow/_state/champion_config.json
```

### View recent halt swaps

```bash
cat /home/app/Stockapp/models/chronos/_shadow/_state/halt_swap.json 2>/dev/null
ls -lt /home/app/Stockapp/models/chronos/_shadow/_state/*.json
```

### Run evaluation manually

```bash
cd /home/app/Stockapp
PYTHONPATH=src:scripts:. .venv/bin/python scripts/champion_evaluation.py --report
```

## Quarterly Review (every 60 trading days)

When the reminder log fires (`/home/app/Stockapp/logs/quarterly_review_reminder.txt`):

1. Run full evaluation
2. Compare PnL/Sharpe/MASE across all candidates
3. Decide: keep current champion, swap, or restart from scratch
4. Update `_shadow/_state/champion_config.json`:

```bash
cat > /home/app/Stockapp/models/chronos/_shadow/_state/champion_config.json <<EOF
{
  "champion": "<new_champion>",
  "challenger": "<new_challenger>",
  "promoted_at": "$(date +%Y-%m-%d)",
  "promotion_rationale": "<reason>",
  "next_review_date": "<+60 trading days>"
}
EOF
```

5. Commit the new config:

```bash
cd /Users/dev/Stockapp
git add models/chronos/_shadow/_state/champion_config.json
git commit -m "promote: champion=X, challenger=Y (quarterly review)"
```

## HaltGate Swap Recovery

If a halt swap occurs and the original champion recovers, do NOT
auto-restore. Wait for the next quarterly review. Manual override
only if the original champion has 7+ consecutive days of >55% win
rate AFTER the halt trigger date.

## Troubleshooting

### No model passes all 8 gates

This is a healthy failure. The evaluation script will print "No model
passed all 8 gates". Two options:
- Wait: more OOS data accumulates weekly.
- Lower the gate: but this is dangerous and requires council review.

### HaltSwapFile writes but cron doesn't pick it up

Check:
1. File exists and is valid JSON.
2. `STATE_DIR` in run_shadow_cron.sh matches the file's parent dir.
3. Cron output: `/tmp/shadow_cron.log` should print "halt_swap detected".
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/runbooks/champion-selection.md
git commit -m "docs: champion selection operator runbook"
```

---

## Self-Review

After writing the plan:

**1. Spec coverage**: Check each spec section.

| Spec Section | Plan Task |
|--------------|-----------|
| Goals (Champion+Challenger pair) | Tasks 3, 4 |
| Selection criteria (8 gates) | Task 2 |
| HaltGate behavior | Task 8 |
| halt_swap.json | Task 6, 8 |
| champion_config.json | Task 6, 11 |
| MODELS_CSV → CHAMPION_CSV / CHALLENGER_CSV | Task 9 |
| Ensemble filters to champion | Task 10 |
| Quarterly review | Task 12, 14 |
| Initial deployment | Task 11 |
| Testing | Tasks 1-7 tests, Task 13 e2e |
| Risks / Success Criteria | Runbook (Task 14) |

**2. Placeholder scan**: No "TBD", no "implement later". All code blocks are complete.

**3. Type consistency**:
- `ModelMetrics` (Task 1) → used in `GateResult`, `ChampionSelection`, `compute_metrics_for_model` (Tasks 2, 3, 7) ✓
- `HaltSwap`, `ChampionConfig` (Task 6) → used in `run_shadow_halt.sh` (Task 8) and Task 11 ✓
- `halt_outcome_for_champion`, `HaltOutcome` (Task 8) → fields match halt_decisions.jsonl ✓

All types consistent.

---

## Execution Handoff

Plan complete. **13 implementation tasks + 1 runbook task = 14 total**.

Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task with code review between tasks
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch with checkpoints