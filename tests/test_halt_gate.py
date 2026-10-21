"""Tests for HaltGate — Round 4 council paper-trade instrument.

HaltGate decides per-date whether to skip all trades based on trailing
rolling accuracy. Council Round 4 verdict (2026-07-10): adopt as
paper-trade gate with kill-switch + 60-day OOS validation.

Design contract:
  - `should_halt(date)` MUST NOT include `date`'s trades (lookahead-safe)
  - `update(date, trades)` adds a date's settled trades to history
  - `decide(date, trades)` is the recommended entry point — checks
    BEFORE consuming today's trades
"""
from __future__ import annotations

import pytest

from stockapp_v12.eval.halt_gate import HaltGate, HaltDecision


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

def _t(symbol: str, forecast: float, actual: float) -> dict:
    """Build a single settled trade row."""
    return {"symbol": symbol, "forecast_ret_pct": forecast, "actual_ret_pct": actual}


def _correct_trades() -> list[dict]:
    """All forecasts match actual sign — accuracy = 100%."""
    return [
        _t("AAPL", 1.0, 2.0),
        _t("NVDA", -1.5, -0.5),
        _t("TSLA", 0.8, 1.2),
    ]


def _wrong_trades() -> list[dict]:
    """All forecasts wrong — accuracy = 0%."""
    return [
        _t("AAPL", 1.0, -2.0),
        _t("NVDA", -1.5, 0.5),
        _t("TSLA", 0.8, -1.2),
    ]


def _mixed_trades(correct: int = 1, wrong: int = 1) -> list[dict]:
    """Mixed accuracy."""
    out = []
    for i in range(correct):
        out.append(_t(f"S{i}", 1.0, 1.0))
    for i in range(wrong):
        out.append(_t(f"W{i}", 1.0, -1.0))
    return out


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

def test_halt_gate_empty_state_does_not_halt():
    gate = HaltGate()
    should, ctx = gate.should_halt("2026-01-01")
    assert should is False
    assert ctx["reason"] == "insufficient_history"
    assert ctx["n_prior_trades"] == 0
    assert ctx["rolling_acc"] is None


def test_halt_gate_insufficient_history_does_not_halt():
    """Need >= min_history_days before evaluating."""
    gate = HaltGate(window=3, min_history_days=3, min_trades_in_window=2)
    # Only 2 dates loaded; should not halt
    gate.update("2026-01-01", _wrong_trades())
    gate.update("2026-01-02", _wrong_trades())
    should, ctx = gate.should_halt("2026-01-03")
    assert should is False
    assert ctx["reason"] == "insufficient_history"


def test_halt_gate_insufficient_trades_does_not_halt():
    """Even with enough dates, need >= min_trades_in_window."""
    gate = HaltGate(window=3, min_history_days=3, min_trades_in_window=10)
    # 3 dates but each only has 1 trade = 3 total < 10
    gate.update("2026-01-01", [_t("AAPL", 1.0, -1.0)])
    gate.update("2026-01-02", [_t("AAPL", 1.0, -1.0)])
    gate.update("2026-01-03", [_t("AAPL", 1.0, -1.0)])
    should, ctx = gate.should_halt("2026-01-04")
    assert should is False
    assert ctx["reason"] == "insufficient_trades"
    assert ctx["n_prior_trades"] == 3


# ---------------------------------------------------------------------------
# Reactive mode (default)
# ---------------------------------------------------------------------------

def test_halt_gate_triggers_on_low_accuracy_reactive():
    """3 wrong days in a row → halt on day 4 (reactive)."""
    gate = HaltGate(
        window=3, threshold=0.40,
        min_history_days=3, min_trades_in_window=3,
        persistent=False,
    )
    for d in ["2026-01-01", "2026-01-02", "2026-01-03"]:
        gate.update(d, _wrong_trades())  # accuracy 0%
    should, ctx = gate.should_halt("2026-01-04")
    assert should is True
    assert ctx["reason"] == "acc<0.4"
    assert ctx["rolling_acc"] == 0.0
    assert ctx["n_prior_trades"] == 9


def test_halt_gate_does_not_trigger_on_good_accuracy_reactive():
    """All correct days → no halt."""
    gate = HaltGate(
        window=3, threshold=0.40,
        min_history_days=3, min_trades_in_window=3,
        persistent=False,
    )
    for d in ["2026-01-01", "2026-01-02", "2026-01-03"]:
        gate.update(d, _correct_trades())  # accuracy 100%
    should, ctx = gate.should_halt("2026-01-04")
    assert should is False
    assert ctx["reason"] is None
    assert ctx["rolling_acc"] == 1.0


def test_halt_gate_reactive_does_not_carry_state():
    """Reactive halt doesn't persist — next day's check is independent."""
    gate = HaltGate(
        window=3, threshold=0.40,
        min_history_days=3, min_trades_in_window=3,
        persistent=False,
    )
    # Day 1-3: all wrong
    for d in ["2026-01-01", "2026-01-02", "2026-01-03"]:
        gate.update(d, _wrong_trades())
    # Day 4: should halt (rolling acc 0% < 40%)
    should_4, _ = gate.should_halt("2026-01-04")
    assert should_4 is True
    # Update day 4 with good trades (does NOT count toward rolling for day 5;
    # window is dates strictly before day 5)
    gate.update("2026-01-04", _correct_trades())
    # Day 5: rolling window now [D1, D2, D3, D4], window=3 → [D2, D3, D4]
    # = wrong, wrong, correct → accuracy 1/3 ≈ 33% < 40% → still halt
    should_5, ctx_5 = gate.should_halt("2026-01-05")
    assert should_5 is True
    # Day 6: rolling window [D3, D4, D5], but D5 wasn't loaded → [D3, D4] + nothing
    # Actually with min_history_days=3 and only 4 dates loaded, day 6 has only
    # 3 prior dates. Let me load day 5 with good trades first.
    gate.update("2026-01-05", _correct_trades())
    # Day 6: window [D4, D5] for window=2 OR [D3, D4, D5] for window=3
    # window=3 → [D3=wrong, D4=correct, D5=correct] → 2/3 = 66.7% > 40% → no halt
    should_6, ctx_6 = gate.should_halt("2026-01-06")
    assert should_6 is False
    assert ctx_6["rolling_acc"] == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# Persistent mode
# ---------------------------------------------------------------------------

def test_halt_gate_persistent_stays_halted_on_low_acc():
    """Once triggered in persistent mode, stays halted while acc remains low."""
    gate = HaltGate(
        window=2, threshold=0.40, persistent=True, recovery_threshold=0.50,
        min_history_days=2, min_trades_in_window=2,
    )
    # Build up history: D1 wrong, D2 wrong → acc 0%
    gate.update("2026-01-01", _wrong_trades())
    gate.update("2026-01-02", _wrong_trades())
    # D3: rolling [D1, D2] = 0% < 40% → HALT (and enter persistent halt)
    should_3, _ = gate.should_halt("2026-01-03")
    assert should_3 is True
    # Update D3 with wrong trades (we still observe what happened)
    gate.update("2026-01-03", _wrong_trades())
    # D4: still in persistent halt. Rolling [D2, D3] = 0% < recovery 50% → stay halted
    should_4, ctx_4 = gate.should_halt("2026-01-04")
    assert should_4 is True
    assert ctx_4["reason"] == "persistent_cooldown"


def test_halt_gate_persistent_exits_on_recovery():
    """Persistent halt exits when rolling_acc >= recovery_threshold."""
    gate = HaltGate(
        window=2, threshold=0.40, persistent=True, recovery_threshold=0.50,
        min_history_days=2, min_trades_in_window=2,
    )
    # D1, D2 wrong → halt trigger on D3
    gate.update("2026-01-01", _wrong_trades())
    gate.update("2026-01-02", _wrong_trades())
    should_3, _ = gate.should_halt("2026-01-03")
    assert should_3 is True
    gate.update("2026-01-03", _wrong_trades())  # still wrong
    # D4: still halted
    should_4, _ = gate.should_halt("2026-01-04")
    assert should_4 is True
    gate.update("2026-01-04", _correct_trades())  # good recovery!
    # D5: rolling [D3=wrong, D4=correct] → 50% >= recovery 50% → exit halt
    should_5, ctx_5 = gate.should_halt("2026-01-05")
    assert should_5 is False
    assert ctx_5["reason"] is None


# ---------------------------------------------------------------------------
# Lookahead protection
# ---------------------------------------------------------------------------

def test_halt_gate_lookahead_protection_should_halt_excludes_today():
    """should_halt(today) MUST NOT include today's trades even if today is in history.

    Two scenarios:
      A. should_halt(today) BEFORE update(today) — naturally lookahead-safe
         because today is not in history yet.
      B. should_halt(today) AFTER update(today) — caller misuse, but the
         implementation MUST still exclude today's trades from the rolling
         window (defense in depth).
    """
    gate = HaltGate(
        window=3, threshold=0.40,
        min_history_days=3, min_trades_in_window=3,
    )
    # Pre-load 3 dates with correct trades
    gate.update("2026-01-01", _correct_trades())
    gate.update("2026-01-02", _correct_trades())
    gate.update("2026-01-03", _correct_trades())

    # Scenario A: should_halt(D4) before update(D4)
    should_a, ctx_a = gate.should_halt("2026-01-04")
    assert ctx_a["n_prior_trades"] == 9
    assert ctx_a["rolling_acc"] == 1.0  # 3 correct dates

    # Now update D4 with ALL WRONG trades
    gate.update("2026-01-04", _wrong_trades())

    # Scenario B: should_halt(D4) AFTER update(D4) — must still exclude D4
    should_b, ctx_b = gate.should_halt("2026-01-04")
    assert ctx_b["n_prior_trades"] == 9  # NOT 12 (would include D4)
    assert ctx_b["rolling_acc"] == 1.0  # still 100% from D1-D3
    assert ctx_b["reason"] is None


def test_halt_gate_decide_atomic_should_halt_then_update():
    """decide() returns the halt decision based on PRIOR state, then updates."""
    gate = HaltGate(
        window=3, threshold=0.40,
        min_history_days=3, min_trades_in_window=3,
    )
    gate.update("2026-01-01", _correct_trades())
    gate.update("2026-01-02", _correct_trades())
    gate.update("2026-01-03", _correct_trades())
    # decide() for D4 — should NOT see D4's own (currently empty) state
    should, ctx = gate.decide("2026-01-04", _wrong_trades())
    assert ctx["rolling_acc"] == 1.0  # from prior 3 correct
    # After decide(), D4 is in history
    # Next call: rolling window for D5 = [D2, D3, D4] = correct, correct, wrong → 2/3 = 66.7%
    should_5, ctx_5 = gate.decide("2026-01-05", _correct_trades())
    assert ctx_5["rolling_acc"] == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# Window and trade filtering
# ---------------------------------------------------------------------------

def test_halt_gate_window_size_respected():
    """Only the last `window` dates count toward rolling accuracy."""
    gate = HaltGate(
        window=2, threshold=0.40,
        min_history_days=2, min_trades_in_window=3,
    )
    # 5 dates: D1-D3 wrong, D4-D5 correct
    for d in ["2026-01-01", "2026-01-02", "2026-01-03"]:
        gate.update(d, _wrong_trades())
    for d in ["2026-01-04", "2026-01-05"]:
        gate.update(d, _correct_trades())
    # window=2 → only [D4, D5] matter → 100% acc → no halt
    should, ctx = gate.should_halt("2026-01-06")
    assert should is False
    assert ctx["rolling_acc"] == 1.0
    assert ctx["n_prior_trades"] == 6  # 2 dates × 3 trades


def test_halt_gate_filters_unsettled_trades():
    """Trades with None actual_ret_pct are excluded from accuracy computation."""
    gate = HaltGate(
        window=2, threshold=0.40,
        min_history_days=2, min_trades_in_window=3,
    )
    # D1: 1 settled correct, 1 unsettled (None)
    gate.update("2026-01-01", [
        _t("AAPL", 1.0, 2.0),  # correct, settled
        {"symbol": "NVDA", "forecast_ret_pct": 1.0, "actual_ret_pct": None},
    ])
    # D2: 2 settled correct
    gate.update("2026-01-02", _correct_trades()[:2])
    # Rolling: 3 settled trades, all correct → 100% → no halt
    should, ctx = gate.should_halt("2026-01-03")
    assert should is False
    assert ctx["rolling_acc"] == 1.0
    assert ctx["n_prior_trades"] == 3


def test_halt_gate_handles_empty_trades_list():
    """Dates with empty trades list don't break state but don't add evidence."""
    gate = HaltGate(
        window=2, threshold=0.40,
        min_history_days=2, min_trades_in_window=3,
    )
    gate.update("2026-01-01", _wrong_trades())  # 3 wrong
    gate.update("2026-01-02", [])  # empty!
    # Rolling window includes D1 (3 trades) only — not enough for min_trades=3?
    # Actually 3 == 3, so passes the guard. 0% accuracy → halt.
    should, ctx = gate.should_halt("2026-01-03")
    assert should is True
    assert ctx["n_prior_trades"] == 3


# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------

def test_halt_decision_dataclass_fields():
    """HaltDecision must carry rolling_acc, n_prior_trades, reason, halt_bool."""
    gate = HaltGate(
        window=2, threshold=0.40,
        min_history_days=2, min_trades_in_window=3,
    )
    gate.update("2026-01-01", _wrong_trades())
    gate.update("2026-01-02", _wrong_trades())
    should, ctx = gate.should_halt("2026-01-03")
    assert isinstance(ctx, dict)
    assert "rolling_acc" in ctx
    assert "n_prior_trades" in ctx
    assert "reason" in ctx
    assert "halt" in ctx


# ---------------------------------------------------------------------------
# Integration: 30-day v2 backfill (sanity check vs p0_halt.py)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Validation: NaN/inf handling (HIGH-priority review finding)
# ---------------------------------------------------------------------------

def test_halt_gate_update_rejects_nan_forecast():
    """update() must raise ValueError on NaN forecast_ret_pct at ingestion time."""
    gate = HaltGate()
    bad_trades = [{"symbol": "X", "forecast_ret_pct": float("nan"), "actual_ret_pct": 1.0}]
    with pytest.raises(ValueError, match="non-finite"):
        gate.update("2026-01-01", bad_trades)


def test_halt_gate_update_rejects_inf_actual():
    """update() must reject inf actual_ret_pct."""
    gate = HaltGate()
    bad_trades = [{"symbol": "X", "forecast_ret_pct": 1.0, "actual_ret_pct": float("inf")}]
    with pytest.raises(ValueError, match="non-finite"):
        gate.update("2026-01-01", bad_trades)


def test_halt_gate_update_rejects_missing_forecast():
    """update() must reject trades missing forecast_ret_pct."""
    gate = HaltGate()
    bad_trades = [{"symbol": "X", "actual_ret_pct": 1.0}]  # no forecast_ret_pct
    with pytest.raises(ValueError, match="missing 'forecast_ret_pct'"):
        gate.update("2026-01-01", bad_trades)


def test_halt_gate_decision_is_frozen():
    """HaltDecision must be immutable (frozen dataclass)."""
    from stockapp_v12.eval.halt_gate import HaltDecision
    d = HaltDecision(halt=True, rolling_acc=0.3, n_prior_trades=10, reason="acc<0.4")
    with pytest.raises(Exception):  # FrozenInstanceError
        d.halt = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Integration: 30-day v2 backfill (sanity check vs p0_halt.py)
# ---------------------------------------------------------------------------

def test_halt_gate_30day_v2_matches_p0_halt_sweep():
    """Run on 30-day v2 backfill, verify halted_pnl matches p0_halt.py output.

    This is a smoke test that ensures the HaltGate logic matches the
    exploratory analysis script. Expected results (re-measured 2026-07-10):
      3d_40_reactive: v2_pnl = +69.47%, skipped 10 dates

    Backfill JSONs must be available locally at /tmp/gpu_phase2_v2_backfill.
    Sync from GPU box if missing: scp -r gpu:/home/trainer/Stockapp/models/chronos/_shadow/phase2_v2/_backfill /tmp/gpu_phase2_v2_backfill
    """
    import json
    from pathlib import Path

    backfill_dir = Path("/tmp/gpu_phase2_v2_backfill")
    if not backfill_dir.exists():
        pytest.skip(
            "Local copy of v2 backfill not at /tmp/gpu_phase2_v2_backfill. "
            "Sync via: scp -r gpu:/home/trainer/Stockapp/models/chronos/_shadow/phase2_v2/_backfill /tmp/"
        )

    signal_threshold = 0.5
    window = 3
    halt_threshold = 0.40

    gate = HaltGate(
        window=window, threshold=halt_threshold,
        persistent=False, recovery_threshold=0.50,
        min_history_days=3, min_trades_in_window=10,
    )

    per_date = []
    for f in sorted(backfill_dir.glob("*.json")):
        with open(f) as fp:
            d = json.load(fp)
        per_date.append((d["eval_date"], d.get("rows", [])))

    halted_pnl = 0.0
    n_skipped_dates = 0
    n_halted_dates = 0

    for date_str, rows in per_date:
        actionable = [
            r for r in rows
            if r.get("actual_ret_pct") is not None
            and abs(r.get("forecast_ret_pct", 0)) > signal_threshold
        ]
        should_halt, _ = gate.should_halt(date_str)
        if should_halt:
            n_skipped_dates += 1
        else:
            for t in actionable:
                sign = 1 if t["forecast_ret_pct"] > 0 else -1
                halted_pnl += sign * t["actual_ret_pct"]
            n_halted_dates += 1
        gate.update(date_str, rows)

    # Expected from p0_halt.py run on 2026-07-10:
    #   3d_40_reactive: v2_pnl = +69.47%, skipped 10 dates
    assert halted_pnl == pytest.approx(69.47, abs=2.0), (
        f"HaltGate v2 halted_pnl drift: got {halted_pnl:+.2f}%, expected +69.47%"
    )
    assert n_skipped_dates == 10, (
        f"HaltGate skip-date count drift: got {n_skipped_dates}, expected 10"
    )
    assert n_halted_dates == 20, (
        f"HaltGate halt-date count drift: got {n_halted_dates}, expected 20"
    )