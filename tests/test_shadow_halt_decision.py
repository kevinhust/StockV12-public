"""Tests for shadow_halt_decision.py — Round 4 paper-trade logger.

Tests cover:
  - load_per_date_trades (cron + backfill, dedup, sorting)
  - compute_halt_decision (pure function, variant configs)
  - append_decision (JSONL format, model tagging)
  - run_for_model (integration: file I/O + HaltGate)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_model_dir(tmp_path):
    """Create a mock shadow model dir with cron + backfill per-date JSONs."""
    model_dir = tmp_path / "phase2_v2"
    model_dir.mkdir()
    backfill_dir = model_dir / "_backfill"
    backfill_dir.mkdir()

    # Backfill dates (10-20 days ago, settled)
    for i in range(5, 15):
        d = f"2026-06-{i:02d}"
        payload = {
            "model": "phase2_v2",
            "eval_date": d,
            "rows": [
                {"symbol": "AAPL", "forecast_ret_pct": 1.0, "actual_ret_pct": 2.0},
                {"symbol": "NVDA", "forecast_ret_pct": -1.0, "actual_ret_pct": -0.5},
            ],
        }
        (backfill_dir / f"{d}.json").write_text(json.dumps(payload))

    # Cron date (today, may have unsettled trades)
    today_payload = {
        "model": "phase2_v2",
        "eval_date": "2026-06-15",
        "rows": [
            {"symbol": "AAPL", "forecast_ret_pct": 1.5, "actual_ret_pct": None},
            {"symbol": "TSLA", "forecast_ret_pct": 2.0, "actual_ret_pct": None},
        ],
    }
    (model_dir / "2026-06-15.json").write_text(json.dumps(today_payload))

    return model_dir


# ---------------------------------------------------------------------------
# load_per_date_trades
# ---------------------------------------------------------------------------

def test_load_per_date_trades_loads_backfill_and_cron(mock_model_dir):
    """Should load JSONs from both _backfill/ and cron root."""
    from scripts.shadow_halt_decision import load_per_date_trades
    per_date = load_per_date_trades(mock_model_dir)
    assert len(per_date) == 11  # 10 backfill + 1 cron
    dates = [d for d, _ in per_date]
    assert dates == sorted(dates)  # sorted ascending


def test_load_per_date_trades_cron_overrides_backfill(mock_model_dir):
    """If same date exists in both cron and backfill, cron wins."""
    from scripts.shadow_halt_decision import load_per_date_trades

    # Add a cron JSON for a date that also exists in backfill
    dup_date = "2026-06-10"
    cron_payload = {
        "model": "phase2_v2",
        "eval_date": dup_date,
        "rows": [{"symbol": "OVERRIDE", "forecast_ret_pct": 9.9, "actual_ret_pct": 9.9}],
    }
    (mock_model_dir / f"{dup_date}.json").write_text(json.dumps(cron_payload))

    per_date = load_per_date_trades(mock_model_dir)
    entry = [(d, rows) for d, rows in per_date if d == dup_date]
    assert len(entry) == 1
    assert entry[0][1][0]["symbol"] == "OVERRIDE"


def test_load_per_date_trades_empty_dir(tmp_path):
    """Empty dir returns empty list."""
    from scripts.shadow_halt_decision import load_per_date_trades
    result = load_per_date_trades(tmp_path / "nonexistent")
    assert result == []


# ---------------------------------------------------------------------------
# compute_halt_decision
# ---------------------------------------------------------------------------

def test_compute_halt_decision_insufficient_history():
    """Few prior dates → no halt, reason=insufficient_history."""
    from scripts.shadow_halt_decision import compute_halt_decision
    per_date = [
        ("2026-01-01", [{"symbol": "X", "forecast_ret_pct": 1.0, "actual_ret_pct": -1.0}]),
        ("2026-01-02", [{"symbol": "X", "forecast_ret_pct": 1.0, "actual_ret_pct": -1.0}]),
    ]
    variant = {"name": "test", "window": 3, "threshold": 0.40,
               "persistent": False, "recovery_threshold": 0.50,
               "min_history_days": 3, "min_trades_in_window": 10}
    result = compute_halt_decision(per_date, "2026-01-03", variant)
    assert result["halt"] is False
    assert result["reason"] == "insufficient_history"


def test_compute_halt_decision_triggers_on_bad_history():
    """All wrong trades in history → halt fires."""
    from scripts.shadow_halt_decision import compute_halt_decision
    per_date = []
    for i in range(5, 15):  # 10 dates, all wrong
        d = f"2026-06-{i:02d}"
        per_date.append((d, [
            {"symbol": "X", "forecast_ret_pct": 1.0, "actual_ret_pct": -1.0},
            {"symbol": "Y", "forecast_ret_pct": 1.0, "actual_ret_pct": -1.0},
        ]))
    variant = {"name": "test", "window": 3, "threshold": 0.40,
               "persistent": False, "recovery_threshold": 0.50,
               "min_history_days": 3, "min_trades_in_window": 3}
    result = compute_halt_decision(per_date, "2026-06-20", variant)
    assert result["halt"] is True
    assert result["reason"] == "acc<0.4"
    assert result["rolling_acc"] == 0.0


def test_compute_halt_decision_counts_would_skip():
    """Decision includes n_would_skip_trades from today's data."""
    from scripts.shadow_halt_decision import compute_halt_decision
    per_date = []
    for i in range(5, 15):
        d = f"2026-06-{i:02d}"
        per_date.append((d, [
            {"symbol": "X", "forecast_ret_pct": 1.0, "actual_ret_pct": 1.0},
        ] * 5))
    # Today's trades
    today = "2026-06-20"
    per_date.append((today, [
        {"symbol": "A", "forecast_ret_pct": 1.0, "actual_ret_pct": None},  # actionable
        {"symbol": "B", "forecast_ret_pct": 2.0, "actual_ret_pct": None},  # actionable
        {"symbol": "C", "forecast_ret_pct": 0.1, "actual_ret_pct": None},  # NOT actionable (< 0.5)
    ]))
    variant = {"name": "test", "window": 3, "threshold": 0.40,
               "persistent": False, "recovery_threshold": 0.50,
               "min_history_days": 3, "min_trades_in_window": 10}
    result = compute_halt_decision(per_date, today, variant)
    assert result["n_would_skip_trades"] == 3  # all today's rows
    assert result["n_would_skip_actionable"] == 2  # only |forecast| > 0.5


# ---------------------------------------------------------------------------
# append_decision
# ---------------------------------------------------------------------------

def test_append_decision_creates_jsonl(tmp_path):
    """Append writes valid JSONL with model tag + timestamp."""
    from scripts.shadow_halt_decision import append_decision
    model_dir = tmp_path / "test_model"
    model_dir.mkdir()

    decision = {
        "date": "2026-07-10",
        "variant": "3d_40_reactive",
        "halt": False,
        "rolling_acc": 0.52,
        "n_prior_trades": 47,
        "reason": None,
        "n_would_skip_trades": 0,
        "n_would_skip_actionable": 0,
    }
    path = append_decision("test_model", decision, model_dir)
    assert path == model_dir / "halt_decisions.jsonl"
    assert path.exists()

    lines = path.read_text().strip().split("\n")
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["model"] == "test_model"
    assert record["date"] == "2026-07-10"
    assert record["halt"] is False
    assert "generated_at" in record


def test_append_decision_appends_not_overwrites(tmp_path):
    """Multiple appends create multiple lines, not overwrite."""
    from scripts.shadow_halt_decision import append_decision
    model_dir = tmp_path / "test_model"
    model_dir.mkdir()

    for i in range(3):
        decision = {
            "date": f"2026-07-1{i}",
            "variant": "3d_40_reactive",
            "halt": False,
            "rolling_acc": 0.5,
            "n_prior_trades": 10,
            "reason": None,
            "n_would_skip_trades": 0,
            "n_would_skip_actionable": 0,
        }
        append_decision("test_model", decision, model_dir)

    lines = (model_dir / "halt_decisions.jsonl").read_text().strip().split("\n")
    assert len(lines) == 3


# ---------------------------------------------------------------------------
# Integration: 30-day v2 backfill (matches HaltGate integration test)
# ---------------------------------------------------------------------------

def test_shadow_halt_decision_replay_30day_v2_backfill():
    """Replay halt decisions on 30-day v2 backfill.

    Expected: 30 dates × 3 variants = 90 decisions for v2.
    Primary variant (3d_40_reactive): 10 halts out of 30 dates.
    """
    backfill_dir = Path("/tmp/gpu_phase2_v2_backfill")
    if not backfill_dir.exists():
        pytest.skip(
            "v2 backfill not at /tmp/gpu_phase2_v2_backfill. "
            "Sync via: scp -r gpu:.../phase2_v2/_backfill /tmp/gpu_phase2_v2_backfill"
        )

    from scripts.shadow_halt_decision import (
        load_per_date_trades,
        compute_halt_decision,
        COUNCIL_VARIANTS,
    )

    per_date = load_per_date_trades(backfill_dir.parent)  # model_dir, not backfill_dir
    # Actually we need the parent dir since load_per_date_trades reads from model_dir/_backfill/
    # For the local copy at /tmp/gpu_phase2_v2_backfill, we need to set up a model_dir
    # Let me just load directly
    per_date_direct = []
    for f in sorted(backfill_dir.glob("*.json")):
        with open(f) as fp:
            d = json.load(fp)
        per_date_direct.append((d["eval_date"], d.get("rows", [])))

    # Replay 3d_40_reactive
    variant_3d40 = next(v for v in COUNCIL_VARIANTS if v["name"] == "3d_40_reactive")
    halt_dates = []
    for d, _ in per_date_direct:
        result = compute_halt_decision(per_date_direct, d, variant_3d40)
        if result["halt"]:
            halt_dates.append(d)

    # Expected from p0_halt.py: 10 halts on 30 dates
    assert len(halt_dates) == 10, f"Expected 10 halts, got {len(halt_dates)}: {halt_dates}"
    assert len(per_date_direct) == 30


# ---------------------------------------------------------------------------
# halt_outcome_for_champion (Task 8 — champion swap on halt)
# ---------------------------------------------------------------------------

from scripts.shadow_halt_decision import (  # noqa: E402  (appended)
    halt_outcome_for_champion,
    HaltOutcome,
)


def _write_decision(
    path: Path,
    date: str,
    model: str,
    halted: bool,
    rolling_acc: float | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "date": date,
        "model": model,
        "should_halt": halted,
        "variant": "3d_40_reactive",
    }
    if rolling_acc is not None:
        entry["rolling_acc"] = rolling_acc
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def test_halt_outcome_true_when_champion_halted(tmp_path: Path):
    decisions = tmp_path / "halt_decisions.jsonl"
    _write_decision(decisions, "2026-07-15", "E07_long_ctx", halted=True)
    outcome = halt_outcome_for_champion(decisions, "E07_long_ctx")
    assert outcome.halted is True


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


def test_halt_outcome_picks_latest_entry_per_champion(tmp_path: Path):
    """When multiple decisions exist for champion, return the most recent."""
    decisions = tmp_path / "halt_decisions.jsonl"
    _write_decision(decisions, "2026-07-14", "E07_long_ctx", halted=False)
    _write_decision(decisions, "2026-07-15", "E07_long_ctx", halted=True, rolling_acc=0.38)
    outcome = halt_outcome_for_champion(decisions, "E07_long_ctx")
    assert outcome.halted is True
    assert outcome.decision_date == "2026-07-15"
    assert outcome.recent_accuracy == 0.38


def test_halt_outcome_missing_file_returns_not_halted(tmp_path: Path):
    """Missing file = no halt fired."""
    outcome = halt_outcome_for_champion(tmp_path / "nope.jsonl", "E07_long_ctx")
    assert outcome.halted is False
    assert outcome.decision_date is None