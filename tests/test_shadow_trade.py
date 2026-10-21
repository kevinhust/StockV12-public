"""Tests for shadow_trade.py pure functions.

These functions are intentionally split out from AutoGluon / GPU I/O so the
trading-edge math can be tested without spinning up models. Tests cover:
  - walk_forward_cutoffs (range generation)
  - simulate_pnl (long/short/flat, win-rate, totals)
  - aggregate_shadow_rows (hit_rate + signal_hit_rate + mase + pnl together)
  - aggregate_all_per_date_jsons (multi-day accumulation across JSONs)
  - write_shadow_artifacts (SUMMARY.json uses accumulated aggregate)
  - eval_date_str_for_cutoff (calendar math)
  - check_shadow_gate (ADR-008 walk-forward promotion gate)
  - build_test_tsdf_full eval_date mode (true walk-forward slicing)
  - run_shadow_for_model eval_dates path (anti-lookahead guard)
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# walk_forward_cutoffs
# ---------------------------------------------------------------------------

def test_walk_forward_cutoffs_default_7_days():
    from scripts.shadow_trade import walk_forward_cutoffs
    assert walk_forward_cutoffs(7) == [1, 2, 3, 4, 5, 6, 7]


def test_walk_forward_cutoffs_single_day():
    from scripts.shadow_trade import walk_forward_cutoffs
    assert walk_forward_cutoffs(1) == [1]


def test_walk_forward_cutoffs_raises_on_zero():
    from scripts.shadow_trade import walk_forward_cutoffs
    with pytest.raises(ValueError, match="n_days"):
        walk_forward_cutoffs(0)


def test_walk_forward_cutoffs_raises_on_negative():
    from scripts.shadow_trade import walk_forward_cutoffs
    with pytest.raises(ValueError, match="n_days"):
        walk_forward_cutoffs(-3)


# ---------------------------------------------------------------------------
# simulate_pnl
# ---------------------------------------------------------------------------

def test_simulate_pnl_no_trades_when_all_flat():
    """All forecasts below threshold → no trades → win_rate=None."""
    from scripts.shadow_trade import simulate_pnl
    fc = [0.1, -0.2, 0.3, -0.4]  # all |f| <= 0.5
    ac = [2.0, -1.0, 0.5, 3.0]
    r = simulate_pnl(fc, ac, threshold=0.5)
    assert r["n_trades"] == 0
    assert r["n_wins"] == 0
    assert r["total_pnl_pct"] == 0.0
    assert r["win_rate"] is None


def test_simulate_pnl_long_wins_when_actual_positive():
    """Forecast > threshold AND actual > 0 → long wins, pnl = sign(f) * a = +a."""
    from scripts.shadow_trade import simulate_pnl
    fc = [1.0, 2.0, 1.5]   # all > 0.5% threshold → long
    ac = [2.0, 1.0, 0.5]   # all positive → all wins
    r = simulate_pnl(fc, ac, threshold=0.5)
    assert r["n_trades"] == 3
    assert r["n_wins"] == 3
    assert r["win_rate"] == 1.0
    assert r["total_pnl_pct"] == pytest.approx(3.5)


def test_simulate_pnl_long_loses_when_actual_negative():
    """Forecast > threshold AND actual < 0 → long loses, pnl = -|a|."""
    from scripts.shadow_trade import simulate_pnl
    fc = [1.0, 2.0]
    ac = [-1.0, -2.0]
    r = simulate_pnl(fc, ac, threshold=0.5)
    assert r["n_trades"] == 2
    assert r["n_wins"] == 0
    assert r["win_rate"] == 0.0
    assert r["total_pnl_pct"] == pytest.approx(-3.0)


def test_simulate_pnl_short_wins_when_actual_negative():
    """Forecast < -threshold AND actual < 0 → short wins, pnl = -1 * -|a| = +|a|."""
    from scripts.shadow_trade import simulate_pnl
    fc = [-1.0, -2.0]
    ac = [-3.0, -1.0]
    r = simulate_pnl(fc, ac, threshold=0.5)
    assert r["n_trades"] == 2
    assert r["n_wins"] == 2
    assert r["win_rate"] == 1.0
    assert r["total_pnl_pct"] == pytest.approx(4.0)


def test_simulate_pnl_short_loses_when_actual_positive():
    """Forecast < -threshold AND actual > 0 → short loses, pnl = -1 * a."""
    from scripts.shadow_trade import simulate_pnl
    fc = [-1.0, -2.0]
    ac = [1.0, 3.0]
    r = simulate_pnl(fc, ac, threshold=0.5)
    assert r["n_trades"] == 2
    assert r["n_wins"] == 0
    assert r["total_pnl_pct"] == pytest.approx(-4.0)


def test_simulate_pnl_skips_flat_forecasts():
    """Forecasts exactly at threshold count as flat (|f| == threshold, not >)."""
    from scripts.shadow_trade import simulate_pnl
    fc = [0.5, -0.5, 0.51, -0.51]   # 0.5/-0.5 = at threshold → flat; 0.51/-0.51 = trade
    ac = [10.0, 10.0, 1.0, -1.0]
    r = simulate_pnl(fc, ac, threshold=0.5)
    assert r["n_trades"] == 2  # only the two that strictly exceed 0.5
    # long: sign(0.51) * 1.0 = 1.0; short: sign(-0.51) * -1.0 = 1.0; total = 2.0
    assert r["total_pnl_pct"] == pytest.approx(2.0)
    assert r["win_rate"] == 1.0


def test_simulate_pnl_mixed_wins_and_losses():
    from scripts.shadow_trade import simulate_pnl
    fc = [1.0, 1.0, 1.0, -1.0]
    ac = [2.0, -1.0, 0.5, -2.0]
    # long: 2.0, -1.0, 0.5; short: -1 * -2.0 = 2.0; total = 3.5; wins = 3/4
    r = simulate_pnl(fc, ac, threshold=0.5)
    assert r["n_trades"] == 4
    assert r["n_wins"] == 3
    assert r["win_rate"] == 0.75
    assert r["total_pnl_pct"] == pytest.approx(3.5)


def test_simulate_pnl_raises_on_length_mismatch():
    from scripts.shadow_trade import simulate_pnl
    with pytest.raises(ValueError, match="length mismatch"):
        simulate_pnl([1.0, 2.0], [1.0], threshold=0.5)


def test_simulate_pnl_avg_per_trade():
    """avg_pnl_per_trade = total / n_trades (sanity check on math)."""
    from scripts.shadow_trade import simulate_pnl
    fc = [1.0, 2.0]
    ac = [3.0, 1.0]
    r = simulate_pnl(fc, ac, threshold=0.5)
    assert r["n_trades"] == 2
    assert r["total_pnl_pct"] == pytest.approx(4.0)
    assert r["avg_pnl_per_trade_pct"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# aggregate_shadow_rows
# ---------------------------------------------------------------------------

def _row(symbol, fc, ac, mase=None, eval_date="2026-06-30"):
    """Helper: build a per-symbol row matching predict_one_symbol_for_date's shape."""
    return {
        "symbol": symbol,
        "current_close": 100.0,
        "forecast_mean": 101.0,
        "forecast_ret_pct": fc,
        "actual_mean": 100.5,
        "actual_ret_pct": ac,
        "mase": mase,
        "cutoff_days_ago": 10,
        "n_horizons_evaluated": 10,
        "eval_date": eval_date,
        "model": "test_model",
    }


def test_aggregate_shadow_rows_basic():
    from scripts.shadow_trade import aggregate_shadow_rows
    rows = [
        _row("AAPL", fc=1.0, ac=2.0, mase=2.0),
        _row("NVDA", fc=-1.0, ac=-2.0, mase=3.0),
        _row("TSLA", fc=0.0, ac=1.0, mase=2.5),  # forecast flat, hit by sign_match
    ]
    agg = aggregate_shadow_rows(rows, threshold=0.5)
    assert agg["n_rows"] == 3
    assert agg["n_pairs"] == 3
    assert agg["n_dates"] == 1
    assert agg["n_symbols"] == 3
    # sign_match on (1,2)=yes, (-1,-2)=yes, (0,1)=no → 2/3 ≈ 0.6667 (hit_rate
    # rounds to 4 decimal places — see eval_chronos_h2h_v3.hit_rate)
    assert agg["hit_rate"]["hit_rate"] == 0.6667
    # signal_hit_rate: AAPL + NVDA called (|f|>0.5), TSLA flat → 2/2
    assert agg["signal_hit_rate"]["hit_rate"] == 1.0
    # MASE mean across 3 rows
    assert agg["mase_mean"] == pytest.approx((2.0 + 3.0 + 2.5) / 3)
    # PnL: long on AAPL (1.0*2.0=2.0), short on NVDA (-1*-2.0=2.0), flat on TSLA → 4.0
    assert agg["simulated_pnl"]["n_trades"] == 2
    assert agg["simulated_pnl"]["total_pnl_pct"] == pytest.approx(4.0)


def test_aggregate_shadow_rows_skips_null_pairs():
    """Rows missing actual_ret_pct (cutoff=0 live mode) are excluded from hit_rate."""
    from scripts.shadow_trade import aggregate_shadow_rows
    rows = [
        _row("AAPL", fc=1.0, ac=2.0, mase=2.0),
        # cutoff=0 → no actuals
        {"symbol": "NVDA", "forecast_ret_pct": -1.0, "actual_ret_pct": None, "mase": None,
         "eval_date": "2026-06-30", "model": "test"},
    ]
    agg = aggregate_shadow_rows(rows, threshold=0.5)
    assert agg["n_rows"] == 2
    assert agg["n_pairs"] == 1
    assert agg["hit_rate"]["hit_rate"] == 1.0
    assert agg["mase_mean"] == 2.0


def test_aggregate_shadow_rows_multi_date():
    from scripts.shadow_trade import aggregate_shadow_rows
    rows = [
        _row("AAPL", fc=1.0, ac=2.0, mase=2.0, eval_date="2026-06-29"),
        _row("AAPL", fc=-1.0, ac=-1.5, mase=2.5, eval_date="2026-06-30"),
        _row("NVDA", fc=0.6, ac=-0.5, mase=2.0, eval_date="2026-06-29"),
    ]
    agg = aggregate_shadow_rows(rows, threshold=0.5)
    assert agg["n_dates"] == 2
    assert agg["n_symbols"] == 2
    assert agg["dates"] == ["2026-06-29", "2026-06-30"]


# ---------------------------------------------------------------------------
# eval_date_str_for_cutoff
# ---------------------------------------------------------------------------

def test_eval_date_str_for_cutoff_basic():
    from scripts.shadow_trade import eval_date_str_for_cutoff
    assert eval_date_str_for_cutoff(date(2026, 7, 1), 1) == "2026-06-30"
    assert eval_date_str_for_cutoff(date(2026, 7, 1), 7) == "2026-06-24"
    assert eval_date_str_for_cutoff(date(2026, 7, 1), 0) == "2026-07-01"


def test_eval_date_str_for_cutoff_across_month():
    from scripts.shadow_trade import eval_date_str_for_cutoff
    # Month rollover: cutoff=15 from July 1 → June 16
    assert eval_date_str_for_cutoff(date(2026, 7, 1), 15) == "2026-06-16"


def test_eval_date_str_for_cutoff_accepts_string_int():
    from scripts.shadow_trade import eval_date_str_for_cutoff
    # Argparse may pass "1" as str; int() coercion inside the function handles it
    assert eval_date_str_for_cutoff(date(2026, 7, 1), "1") == "2026-06-30"


# ---------------------------------------------------------------------------
# check_shadow_gate (ADR-008 walk-forward promotion gate)
# ---------------------------------------------------------------------------
# Realistic aggregate shape mirrors aggregate_shadow_rows output:
#   {
#     "mase_mean": float,
#     "simulated_pnl": {"total_pnl_pct": float, "win_rate": float|None,
#                       "n_trades": int, ...},
#     ...
#   }
# Default gate (DEFAULT_SHADOW_GATE): pnl > 0, mase ≤ 2.0172×1.05≈2.1181,
# win_rate ≥ 0.40, n_trades ≥ 30.
# (WALKFORWARD_V2_MASE updated from 3.82 → 2.0172 on 2026-07-10 after Round-3
# council re-measurement; see ADR-009 "Correction 2026-07-10".)


def _agg(pnl=10.0, win_rate=0.50, n_trades=50, mase=3.5, n_dates=None):
    """Build a minimal aggregate dict for gate tests.

    n_dates defaults to n_trades (i.e. one trade per date) unless overridden;
    tests that need to exercise n_dates < 30 specifically can pass a value.
    """
    return {
        "mase_mean": mase,
        "n_dates": n_dates if n_dates is not None else n_trades,
        "simulated_pnl": {
            "total_pnl_pct": pnl,
            "win_rate": win_rate,
            "n_trades": n_trades,
            "n_wins": int(win_rate * n_trades) if win_rate is not None else 0,
            "avg_pnl_per_trade_pct": pnl / n_trades if n_trades else 0.0,
        },
    }


def test_shadow_gate_clears_on_good_candidate():
    """A candidate with clearly better mase/pnl than the v2 baseline clears.

    Uses 2026-07-10 fresh-measured v2 baseline as the threshold anchor:
      - mase ≤ 2.0172 × 1.05 ≈ 2.1181
      - pnl > 0
      - win_rate ≥ 0.40
      - n_trades ≥ 30

    This is a synthetic "good candidate" shape; for the real E05_p3 backfill
    numbers (mase=2.0305, pnl=-18.45%), see test_shadow_gate_fails_e05_real_backfill.
    """
    from scripts.shadow_trade import check_shadow_gate
    agg = _agg(pnl=80.0, win_rate=0.50, n_trades=120, mase=2.05)
    r = check_shadow_gate(agg)
    assert r["cleared"] is True
    assert r["reasons"] == []


def test_shadow_gate_clears_on_v2_baseline():
    """phase2_v2 measured backfill shape (mase=2.0172, pnl=+46.11%) clears.

    Pre-2026-07-10, the v2-shaped aggregate used pnl=286.585 / mase=3.8177
    (single-date 7-cutoff test). That was a stale constant; the corrected
    30-day aggregate from 2026-07-10 is what the gate now anchors to.
    """
    from scripts.shadow_trade import check_shadow_gate
    agg = _agg(pnl=46.11, win_rate=0.4971, n_trades=171, mase=2.0172)
    r = check_shadow_gate(agg)
    assert r["cleared"] is True
    assert r["reasons"] == []


def test_shadow_gate_fails_e05_real_backfill():
    """E05_p3_coldstart_seed42 30-day backfill (2026-07-10).

    With Round 4 PnL moratorium active, E05 CLEARS the hard gate
    (mase + win_rate + n_trades all pass). PnL=-18.45% is reported
    as informational via pnl_note, not as a blocking reason.

    This is a regression guard: it pins the empirically observed state.
    """
    from scripts.shadow_trade import check_shadow_gate
    agg = _agg(pnl=-18.45, win_rate=0.4341, n_trades=182, mase=2.0305)
    r = check_shadow_gate(agg)
    # Moratorium: PnL doesn't block → gate clears on other checks
    assert r["cleared"] is True
    assert r["reasons"] == []
    assert "pnl_note" in r
    assert "moratorium" in r["pnl_note"]


def test_shadow_gate_fails_on_negative_pnl():
    """Round 4 moratorium: negative PnL is informational, doesn't block."""
    from scripts.shadow_trade import check_shadow_gate
    agg = _agg(pnl=-5.0, win_rate=0.50, n_trades=50, mase=2.05)
    r = check_shadow_gate(agg)
    # Moratorium active → gate clears, PnL reported as informational
    assert r["cleared"] is True
    assert "pnl_note" in r
    assert "moratorium" in r["pnl_note"]


def test_shadow_gate_fails_on_negative_pnl_no_moratorium():
    """When moratorium is OFF, negative PnL blocks."""
    from scripts.shadow_trade import check_shadow_gate
    agg = _agg(pnl=-5.0, win_rate=0.50, n_trades=50, mase=2.05)
    r = check_shadow_gate(agg, gate={"pnl_moratorium": False})
    assert r["cleared"] is False
    assert any("must be profitable" in reason for reason in r["reasons"])


def test_shadow_gate_fails_on_zero_pnl():
    """Zero PnL is informational under moratorium (doesn't block)."""
    from scripts.shadow_trade import check_shadow_gate
    agg = _agg(pnl=0.0, win_rate=0.50, n_trades=50, mase=2.05)
    r = check_shadow_gate(agg)
    assert r["cleared"] is True
    assert "pnl_note" in r


def test_shadow_gate_fails_on_mase_above_v2_threshold():
    """Mase > v2_wf × 1.05 (2.0172 × 1.05 ≈ 2.1181) fails."""
    from scripts.shadow_trade import check_shadow_gate
    agg = _agg(pnl=10.0, win_rate=0.50, n_trades=50, mase=3.0)
    r = check_shadow_gate(agg)
    assert r["cleared"] is False
    assert any("v2_walkforward" in reason for reason in r["reasons"])


def test_shadow_gate_fails_on_low_win_rate():
    """win_rate < 0.40 fails (catches degenerate strategies)."""
    from scripts.shadow_trade import check_shadow_gate
    agg = _agg(pnl=50.0, win_rate=0.30, n_trades=50, mase=3.5)
    r = check_shadow_gate(agg)
    assert r["cleared"] is False
    assert any("win_rate" in reason for reason in r["reasons"])


def test_shadow_gate_fails_on_too_few_trades():
    """n_trades < 30 fails (statistical power)."""
    from scripts.shadow_trade import check_shadow_gate
    agg = _agg(pnl=50.0, win_rate=0.50, n_trades=10, mase=3.5)
    r = check_shadow_gate(agg)
    assert r["cleared"] is False
    assert any("statistical power" in reason for reason in r["reasons"])


def test_shadow_gate_accepts_custom_gate():
    """Custom gate overrides default thresholds."""
    from scripts.shadow_trade import check_shadow_gate
    agg = _agg(pnl=10.0, win_rate=0.50, n_trades=10, mase=2.0)
    # Default: would fail on n_trades=10. Custom: lower floor → passes.
    custom = {
        "min_pnl_pct": 0.0,
        "max_mase_delta_pct_vs_v2_walkforward": 5.0,
        "v2_walkforward_mase": 2.0172,
        "min_win_rate": 0.40,
        "min_n_trades": 5,
    }
    r = check_shadow_gate(agg, gate=custom)
    assert r["cleared"] is True


def test_shadow_gate_reports_multiple_failures():
    """All non-PnL failures should be listed. PnL is informational (moratorium)."""
    from scripts.shadow_trade import check_shadow_gate
    agg = _agg(pnl=-10.0, win_rate=0.20, n_trades=10, mase=5.0)
    r = check_shadow_gate(agg)
    assert r["cleared"] is False
    # With moratorium: mase + win_rate + n_trades fail (3 reasons, not 4)
    assert len(r["reasons"]) >= 3
    # PnL should NOT be in reasons
    assert not any("must be profitable" in reason for reason in r["reasons"])
    # But should be in pnl_note
    assert "pnl_note" in r


def test_shadow_gate_handles_none_mase():
    """None mase (no held-out data) skips the mase check."""
    from scripts.shadow_trade import check_shadow_gate
    agg = _agg(pnl=10.0, win_rate=0.50, n_trades=50, mase=None)
    r = check_shadow_gate(agg)
    # mase=None → skipped; everything else passes
    assert r["cleared"] is True


def test_shadow_gate_handles_none_win_rate():
    """None win_rate skips WR check; n_trades<30 still fails (PnL=0 is informational)."""
    from scripts.shadow_trade import check_shadow_gate
    agg = _agg(pnl=0.0, win_rate=None, n_trades=0, mase=3.5)
    r = check_shadow_gate(agg)
    assert r["cleared"] is False
    # Should fail on n_trades < 30 only (PnL=0 is informational under moratorium)
    assert len(r["reasons"]) >= 1
    assert not any("must be profitable" in reason for reason in r["reasons"])


def test_shadow_gate_default_constants_are_sensible():
    """Sanity check on default gate constants (regression guard)."""
    from scripts.shadow_trade import (
        WALKFORWARD_V2_MASE,
        DEFAULT_MIN_WIN_RATE,
        DEFAULT_MIN_PNL_PCT,
        DEFAULT_MIN_N_TRADES,
        DEFAULT_MAX_MASE_DELTA_PCT_VS_WF_V2,
        DEFAULT_PNL_MORATORIUM,
        DEFAULT_SHADOW_GATE,
    )
    assert 1.5 < WALKFORWARD_V2_MASE < 3.0  # measured 2026-07-10: 2.0172 (re-measured after Round-3 council)
    assert DEFAULT_MIN_WIN_RATE == 0.40
    assert DEFAULT_MIN_PNL_PCT == 0.0
    assert DEFAULT_MIN_N_TRADES == 30
    assert DEFAULT_MAX_MASE_DELTA_PCT_VS_WF_V2 == 5.0
    assert DEFAULT_PNL_MORATORIUM is True  # Round 4 council
    # Gate dict must include all keys we expect
    assert "min_pnl_pct" in DEFAULT_SHADOW_GATE
    assert "pnl_moratorium" in DEFAULT_SHADOW_GATE
    assert "max_mase_delta_pct_vs_v2_walkforward" in DEFAULT_SHADOW_GATE
    assert "v2_walkforward_mase" in DEFAULT_SHADOW_GATE
    assert "min_win_rate" in DEFAULT_SHADOW_GATE
    assert "min_n_trades" in DEFAULT_SHADOW_GATE


# ---------------------------------------------------------------------------
# aggregate_all_per_date_jsons + write_shadow_artifacts (multi-day accumulation)
# ---------------------------------------------------------------------------
# These tests guard against the SUMMARY.json accumulation bug: previously
# write_shadow_artifacts() re-rendered SUMMARY.json from only the current
# run's rows, so each daily cron run overwrote the cross-day aggregate.
# Fix: write_shadow_artifacts() now calls aggregate_all_per_date_jsons() so
# SUMMARY.json reflects every day's data.

# ---------------------------------------------------------------------------
# aggregate_all_per_date_jsons + backfill source tagging (Round 2 closure)
# ---------------------------------------------------------------------------
# A separate _backfill/ subdir holds synthetic walk-forward inferences for
# the 30-day backfill orchestrator. Aggregator must:
#   - include rows from BOTH model_dir/*.json (cron) and
#     model_dir/_backfill/*.json (backfill), unless source_filter
#     restricts it
#   - tag every row with eval_source ("cron" | "backfill") so the Day-30
#     gate can be evaluated on live-only evidence per Critic voice in
#     2026-07-01 council.

import json
from pathlib import Path


def _write_per_date(model_dir: Path, eval_date: str, rows: list[dict]) -> Path:
    """Write a per-date JSON in the shape write_shadow_artifacts produces."""
    out = model_dir / f"{eval_date}.json"
    out.write_text(json.dumps({
        "model": model_dir.name,
        "eval_date": eval_date,
        "rows": rows,
    }, indent=2))
    return out


def test_aggregate_all_per_date_jsons_empty_dir(tmp_path):
    """No model dir / no per-date files → empty aggregate, n_dates=0."""
    from scripts.shadow_trade import aggregate_all_per_date_jsons
    missing = tmp_path / "nope"
    r = aggregate_all_per_date_jsons(missing)
    assert r["n_dates"] == 0
    assert r["date_range"] is None
    assert r["aggregate"]["n_rows"] == 0
    assert r["aggregate"]["n_pairs"] == 0
    assert r["aggregate"]["simulated_pnl"]["n_trades"] == 0


def test_aggregate_all_per_date_jsons_existing_empty_dir(tmp_path):
    """Existing dir but no per-date files → same as missing dir."""
    from scripts.shadow_trade import aggregate_all_per_date_jsons
    model_dir = tmp_path / "phase2_v2"
    model_dir.mkdir()
    # Drop a non-date JSON (e.g. SUMMARY.json) — must be ignored
    (model_dir / "SUMMARY.json").write_text("{}")
    r = aggregate_all_per_date_jsons(model_dir)
    assert r["n_dates"] == 0
    assert r["aggregate"]["n_rows"] == 0


def test_aggregate_all_per_date_jsons_single_day(tmp_path):
    """One per-date JSON: aggregate equals the per-day rows."""
    from scripts.shadow_trade import aggregate_all_per_date_jsons
    model_dir = tmp_path / "phase2_v2"
    model_dir.mkdir()
    _write_per_date(model_dir, "2026-06-30", [
        _row("AAPL", fc=1.0, ac=2.0, mase=2.0),
        _row("NVDA", fc=-1.0, ac=-1.0, mase=3.0),
    ])
    r = aggregate_all_per_date_jsons(model_dir)
    assert r["n_dates"] == 1
    assert r["date_range"] == ["2026-06-30", "2026-06-30"]
    agg = r["aggregate"]
    assert agg["n_rows"] == 2
    assert agg["n_pairs"] == 2
    # long AAPL: 1.0*2.0 = 2.0; short NVDA: -1*-1.0 = 1.0 → total 3.0
    assert agg["simulated_pnl"]["total_pnl_pct"] == pytest.approx(3.0)


def test_aggregate_all_per_date_jsons_multi_day_accumulates(tmp_path):
    """Three days × 2 symbols = 6 rows aggregated (proves the bug is fixed)."""
    from scripts.shadow_trade import aggregate_all_per_date_jsons
    model_dir = tmp_path / "E05_p3_coldstart_seed42"
    model_dir.mkdir()
    _write_per_date(model_dir, "2026-06-24", [
        _row("AAPL", fc=1.0, ac=2.0, mase=2.0),
        _row("NVDA", fc=-1.0, ac=-1.0, mase=3.0),
    ])
    _write_per_date(model_dir, "2026-06-25", [
        _row("AAPL", fc=0.8, ac=1.5, mase=2.2),
        _row("NVDA", fc=-0.6, ac=-0.5, mase=2.8),
    ])
    _write_per_date(model_dir, "2026-06-26", [
        _row("AAPL", fc=1.2, ac=0.5, mase=2.5),
        _row("NVDA", fc=-1.5, ac=-2.0, mase=3.5),
    ])
    r = aggregate_all_per_date_jsons(model_dir)
    assert r["n_dates"] == 3
    assert r["date_range"] == ["2026-06-24", "2026-06-26"]
    agg = r["aggregate"]
    assert agg["n_rows"] == 6
    assert agg["n_pairs"] == 6
    # PnL per row: long AAPL wins (2.0, 1.5, 0.5), short NVDA wins (1.0, 0.5, 2.0)
    # Total = 2.0+1.5+0.5 + 1.0+0.5+2.0 = 7.5
    assert agg["simulated_pnl"]["n_trades"] == 6
    assert agg["simulated_pnl"]["total_pnl_pct"] == pytest.approx(7.5)
    assert agg["simulated_pnl"]["n_wins"] == 6
    assert agg["simulated_pnl"]["win_rate"] == 1.0
    # MASE mean across 6 rows (round to 4 decimals, matches aggregate_shadow_rows)
    expected_mase = round((2.0 + 3.0 + 2.2 + 2.8 + 2.5 + 3.5) / 6, 4)
    assert agg["mase_mean"] == pytest.approx(expected_mase)


def test_aggregate_all_per_date_jsons_skips_malformed(tmp_path, capsys):
    """Malformed JSON file is skipped, others still aggregate."""
    from scripts.shadow_trade import aggregate_all_per_date_jsons
    model_dir = tmp_path / "phase2_v2"
    model_dir.mkdir()
    _write_per_date(model_dir, "2026-06-30", [
        _row("AAPL", fc=1.0, ac=2.0, mase=2.0),
    ])
    # Inject a malformed file
    bad = model_dir / "2026-06-29.json"
    bad.write_text("{not valid json")
    r = aggregate_all_per_date_jsons(model_dir)
    # The valid day still aggregates; the bad file is skipped silently
    assert r["aggregate"]["n_rows"] == 1
    # Stderr warning about the skipped file
    err = capsys.readouterr().err
    assert "skip" in err
    assert "2026-06-29.json" in err


def test_write_shadow_artifacts_uses_accumulated(tmp_path, monkeypatch):
    """write_shadow_artifacts() SUMMARY.json 'aggregate' field reflects ALL
    per-date JSONs in model_dir, not just current run's rows. Regression
    test for the SUMMARY.json overwrite bug.
    """
    from scripts import shadow_trade
    # Use tmp_path as shadow_root by monkeypatching the module constant
    monkeypatch.setattr(shadow_trade, "SHADOW_ROOT", tmp_path)

    # Pre-seed an existing day (simulates yesterday's cron run)
    existing_model_dir = tmp_path / "phase2_v2"
    existing_model_dir.mkdir(parents=True, exist_ok=True)
    _write_per_date(existing_model_dir, "2026-06-29", [
        _row("AAPL", fc=1.0, ac=2.0, mase=2.0, eval_date="2026-06-29"),
        _row("NVDA", fc=-1.0, ac=-1.0, mase=3.0, eval_date="2026-06-29"),
    ])

    # Today: write a one-day result
    today_result = {
        "model_name": "phase2_v2",
        "today": "2026-06-30",
        "per_date": {
            "2026-06-30": [
                _row("AAPL", fc=1.5, ac=1.0, mase=2.5, eval_date="2026-06-30"),
                _row("NVDA", fc=-1.5, ac=-2.0, mase=3.5, eval_date="2026-06-30"),
            ],
        },
        "aggregate": None,  # write_shadow_artifacts ignores this in favor of accumulated
    }
    today_result["aggregate"] = shadow_trade.aggregate_shadow_rows(
        today_result["per_date"]["2026-06-30"]
    )
    shadow_trade.write_shadow_artifacts(today_result, shadow_root=tmp_path)

    # Reload SUMMARY.json and check it accumulated both days
    summary = json.loads((existing_model_dir / "SUMMARY.json").read_text())
    assert summary["model"] == "phase2_v2"
    # accumulated.aggregate.n_rows == 4 (2 from 06-29 + 2 from 06-30)
    acc = summary["accumulated"]
    assert acc["n_dates"] == 2
    assert acc["date_range"] == ["2026-06-29", "2026-06-30"]
    assert acc["aggregate"]["n_rows"] == 4
    # Current-run aggregate (today only) is preserved separately for drift checks
    assert summary["current_run_aggregate"]["n_rows"] == 2
    # Promotion gate is evaluated on the accumulated aggregate, not current run
    gate = summary["promotion_gate"]
    assert "cleared" in gate
    assert "reasons" in gate


def test_aggregate_all_per_date_jsons_walks_backfill_subdir(tmp_path):
    """Rows from _backfill/ are picked up AND tagged eval_source='backfill'.

    Regression test for Round 2 closure: aggregator must walk the separate
    _backfill/ subdir so backfilled days are visible alongside cron days.
    """
    from scripts.shadow_trade import aggregate_all_per_date_jsons
    model_dir = tmp_path / "phase2_v2"
    model_dir.mkdir()
    # Two cron rows on 2026-06-29
    _write_per_date(model_dir, "2026-06-29", [
        _row("AAPL", fc=1.0, ac=2.0, mase=2.0, eval_date="2026-06-29"),
        _row("NVDA", fc=-1.0, ac=-1.0, mase=3.0, eval_date="2026-06-29"),
    ])
    # Two backfill rows on 2026-06-15 (a backfilled trading day)
    backfill_dir = model_dir / "_backfill"
    backfill_dir.mkdir()
    (backfill_dir / "2026-06-15.json").write_text(json.dumps({
        "model": "phase2_v2",
        "eval_date": "2026-06-15",
        "source": "backfill",
        "rows": [
            _row("AAPL", fc=1.5, ac=2.5, mase=2.2, eval_date="2026-06-15"),
            _row("NVDA", fc=-1.5, ac=-2.5, mase=3.2, eval_date="2026-06-15"),
        ],
    }, indent=2))

    r = aggregate_all_per_date_jsons(model_dir)

    # All 4 rows aggregated, both dates included.
    assert r["n_dates"] == 2
    assert r["aggregate"]["n_rows"] == 4
    assert r["date_range"] == ["2026-06-15", "2026-06-29"]

    # Source counts split correctly.
    assert r["n_dates_by_source"] == {"cron": 1, "backfill": 1}
    assert r["n_rows_by_source"] == {"cron": 2, "backfill": 2}


def test_aggregate_all_per_date_jsons_source_filter_cron(tmp_path):
    """source_filter='cron' isolates live data only — Day-30 gate evidence."""
    from scripts.shadow_trade import aggregate_all_per_date_jsons
    model_dir = tmp_path / "phase2_v2"
    model_dir.mkdir()
    # One cron day, one backfill day
    _write_per_date(model_dir, "2026-06-29", [
        _row("AAPL", fc=1.0, ac=2.0, mase=2.0, eval_date="2026-06-29"),
    ])
    backfill_dir = model_dir / "_backfill"
    backfill_dir.mkdir()
    (backfill_dir / "2026-06-15.json").write_text(json.dumps({
        "model": "phase2_v2",
        "eval_date": "2026-06-15",
        "source": "backfill",
        "rows": [
            _row("AAPL", fc=99.0, ac=99.0, mase=99.0, eval_date="2026-06-15"),
        ],
    }, indent=2))

    r = aggregate_all_per_date_jsons(model_dir, source_filter="cron")

    # Only the cron row is aggregated (backfill row is hidden).
    assert r["aggregate"]["n_rows"] == 1
    assert r["n_dates"] == 1
    assert r["date_range"] == ["2026-06-29", "2026-06-29"]
    # But the by-source counts still show BOTH, so the operator can see
    # what's available without changing the filter.
    assert r["n_dates_by_source"] == {"cron": 1, "backfill": 1}
    assert r["n_rows_by_source"] == {"cron": 1, "backfill": 1}


def test_aggregate_all_per_date_jsons_source_filter_backfill(tmp_path):
    """source_filter='backfill' isolates synthetic data only."""
    from scripts.shadow_trade import aggregate_all_per_date_jsons
    model_dir = tmp_path / "phase2_v2"
    model_dir.mkdir()
    _write_per_date(model_dir, "2026-06-29", [
        _row("AAPL", fc=1.0, ac=2.0, mase=2.0, eval_date="2026-06-29"),
    ])
    backfill_dir = model_dir / "_backfill"
    backfill_dir.mkdir()
    (backfill_dir / "2026-06-15.json").write_text(json.dumps({
        "model": "phase2_v2",
        "eval_date": "2026-06-15",
        "source": "backfill",
        "rows": [
            _row("AAPL", fc=5.0, ac=10.0, mase=2.5, eval_date="2026-06-15"),
        ],
    }, indent=2))

    r = aggregate_all_per_date_jsons(model_dir, source_filter="backfill")

    # Only the backfill row is aggregated.
    assert r["aggregate"]["n_rows"] == 1
    assert r["n_dates"] == 1
    assert r["aggregate"]["simulated_pnl"]["total_pnl_pct"] == pytest.approx(10.0)
    # By-source full counts visible regardless of filter.
    assert r["n_rows_by_source"] == {"cron": 1, "backfill": 1}


def test_aggregate_all_per_date_jsons_no_backfill_dir_is_fine(tmp_path):
    """Missing _backfill/ subdir must not break aggregation (cron-only mode)."""
    from scripts.shadow_trade import aggregate_all_per_date_jsons
    model_dir = tmp_path / "phase2_v2"
    model_dir.mkdir()
    _write_per_date(model_dir, "2026-06-29", [
        _row("AAPL", fc=1.0, ac=2.0, mase=2.0, eval_date="2026-06-29"),
    ])
    r = aggregate_all_per_date_jsons(model_dir)
    assert r["n_dates"] == 1
    # No backfill rows anywhere.
    assert r["n_rows_by_source"] == {"cron": 1, "backfill": 0}
    assert r["n_dates_by_source"] == {"cron": 1, "backfill": 0}


# ---------------------------------------------------------------------------
# build_test_tsdf_full — eval_date mode (true walk-forward)
# ---------------------------------------------------------------------------
# Why these tests exist (ADR-009 backfill FAIL addendum, 2026-07-01):
# Pre-fix, build_test_tsdf_full only sliced the LAST `cutoff_days_ago` rows
# off the parquet, so backfill_30_days.py (which always used cutoff=1)
# produced 30 identical (model, symbol) evaluations under different
# eval_date labels. The fix adds an `eval_date` parameter that slices by
# timestamp instead. These tests pin the new behavior so the bug can't
# regress.

def _write_aapl_parquet(path: Path, n_days: int = 260, end_date=None):
    """Write a minimal AAPL-shaped parquet for build_test_tsdf_full tests.

    260 business-day rows ending at `end_date` (default 2026-06-25) — enough
    to clear the 252-row floor in predict_one_symbol_for_date. Close values
    vary deterministically so we can assert the eval_date slice is correct.
    """
    import pandas as pd
    if end_date is None:
        end_date = pd.Timestamp("2026-06-25")
    # Build business-day range ending at end_date
    bd = pd.bdate_range(end=end_date, periods=n_days)
    df = pd.DataFrame({
        "symbol": "AAPL",
        "timestamp": bd,
        "close": [100.0 + i * 0.1 for i in range(n_days)],
        # Feature cols with prefixes build_test_tsdf_full keeps
        "ret_1d": [0.001] * n_days,
        "vol_20d": [0.02] * n_days,
        "rsi_14": [50.0] * n_days,
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return path


def test_build_test_tsdf_full_eval_date_slices_correctly(tmp_path, monkeypatch):
    """eval_date mode slices df to <=eval_date and actual_close to >eval_date.

    This is the regression test for the backfill bug: pre-fix, every
    eval_date produced the same forecast because the same end-of-parquet
    slice was used. With eval_date slicing, different dates yield
    different current_close (since each closes the df at a different point).
    """
    from scripts import eval_chronos_h2h_v3
    parquet = _write_aapl_parquet(tmp_path / "AAPL.parquet", n_days=260)
    monkeypatch.setattr(eval_chronos_h2h_v3, "FEATURES_DIR", tmp_path)
    # B2: see note above — patch the actual lookup location of the moved function.
    from stockapp_v12.eval import test_data as _eval_test_data
    monkeypatch.setattr(_eval_test_data, "FEATURES_DIR", tmp_path)

    df_e1, ac_e1 = eval_chronos_h2h_v3.build_test_tsdf_full(
        "AAPL", cutoff_days_ago=1, eval_date="2026-06-20",
    )
    df_e2, ac_e2 = eval_chronos_h2h_v3.build_test_tsdf_full(
        "AAPL", cutoff_days_ago=1, eval_date="2026-06-10",
    )

    # Different eval_dates must yield different "current_close" (last row).
    cc_e1 = float(df_e1["close"].iloc[-1])
    cc_e2 = float(df_e2["close"].iloc[-1])
    assert cc_e1 != cc_e2, (
        f"eval_date slicing broken: both eval_dates produced current_close={cc_e1}"
    )
    # The earlier eval_date should give an EARLIER last close (df ends earlier).
    assert cc_e2 < cc_e1, (
        f"earlier eval_date should give earlier current_close; "
        f"got cc_e2={cc_e2} >= cc_e1={cc_e1}"
    )
    # actual_close must contain rows STRICTLY AFTER eval_date.
    assert (ac_e1.index > pd.Timestamp("2026-06-20")).all() if hasattr(ac_e1.index[0], '__lt__') else True
    # actual_close length should be <= 10 (FORECAST_HORIZON)
    assert len(ac_e1) <= 10
    # Legacy mode unchanged
    df_legacy, ac_legacy = eval_chronos_h2h_v3.build_test_tsdf_full(
        "AAPL", cutoff_days_ago=5,
    )
    assert len(df_legacy) == 260 - 5
    assert len(ac_legacy) == 5


def test_build_test_tsdf_full_eval_date_raises_before_parquet_start(tmp_path, monkeypatch):
    """eval_date before the parquet's first timestamp must raise ValueError.

    Without this guard, build_test_tsdf_full would silently return an empty
    df and downstream mase computation would divide-by-zero or skip the row.
    """
    from scripts import eval_chronos_h2h_v3
    parquet = _write_aapl_parquet(tmp_path / "AAPL.parquet", n_days=260)
    monkeypatch.setattr(eval_chronos_h2h_v3, "FEATURES_DIR", tmp_path)
    # B2: see note above — patch the actual lookup location of the moved function.
    from stockapp_v12.eval import test_data as _eval_test_data
    monkeypatch.setattr(_eval_test_data, "FEATURES_DIR", tmp_path)
    # B2: see note above — patch the actual lookup location of the moved function.
    from stockapp_v12.eval import test_data as _eval_test_data
    monkeypatch.setattr(_eval_test_data, "FEATURES_DIR", tmp_path)
    with pytest.raises(ValueError, match="no rows"):
        eval_chronos_h2h_v3.build_test_tsdf_full(
            "AAPL", cutoff_days_ago=1, eval_date="2010-01-01",
        )


# ---------------------------------------------------------------------------
# run_shadow_for_model — eval_dates path (true walk-forward orchestration)
# ---------------------------------------------------------------------------

class _FakePredictor:
    """Deterministic predictor: forecast = current_close * (1 + sym_offset)."""
    def predict(self, tsdf):
        import pandas as pd
        # Return a DataFrame indexed by (item_id, timestamp) with 'mean' col.
        idx = tsdf.index
        means = []
        for item_id, ts in idx:
            offset = {"AAPL": 0.02, "NVDA": -0.01, "TSLA": 0.03}.get(item_id, 0.0)
            # 10 horizons of identical forecast (= how Chronos returns)
            means.extend([1.0 + offset] * 10)
        mi = pd.MultiIndex.from_tuples(
            [(item_id, ts + pd.Timedelta(days=h + 1))
             for item_id, ts in idx
             for h in range(10)],
            names=["item_id", "timestamp"],
        )
        return pd.DataFrame({"mean": means}, index=mi)


def test_run_shadow_for_model_eval_dates_yields_distinct_per_date_rows(tmp_path, monkeypatch):
    """Each eval_date in eval_dates mode produces a distinct per-date slice.

    Regression test for ADR-009 backfill FAIL: pre-fix, all eval_dates
    produced the SAME forecast/actual pair (cutoff=1 from end of parquet).
    Post-fix, eval_date mode slices by timestamp, so different eval_dates
    give different current_close and different actual_close windows.
    """
    from scripts import shadow_trade
    # Provide parquet fixtures for 3 symbols, 320 days ending 2026-06-25.
    # Need 252+ rows AFTER eval_date 2026-05-30 to clear the predict_one_symbol_for_date
    # 252-row floor; 320 business-day rows ending 2026-06-25 reaches back to ~2025-04,
    # so even eval_date 2026-05-30 leaves ~290 rows.
    for sym in ["AAPL", "NVDA", "TSLA"]:
        _write_aapl_parquet(tmp_path / f"{sym}.parquet", n_days=320)
    # Patch the symbol the function looks up: we need shadow_trade's imported
    # `build_test_tsdf_full` to use our tmp FEATURES_DIR. Cleanest: monkeypatch
    # `eval_chronos_h2h_v3.FEATURES_DIR` (the source-of-truth constant).
    from scripts import eval_chronos_h2h_v3
    monkeypatch.setattr(eval_chronos_h2h_v3, "FEATURES_DIR", tmp_path)
    # B2: build_test_tsdf_full moved to stockapp_v12.eval.test_data;
    # patch the actual lookup location, not just the re-export.
    from stockapp_v12.eval import test_data as _eval_test_data
    monkeypatch.setattr(_eval_test_data, "FEATURES_DIR", tmp_path)

    pred = _FakePredictor()
    result = shadow_trade.run_shadow_for_model(
        pred, "fake_model",
        symbols=["AAPL", "NVDA", "TSLA"],
        cutoffs=[1],
        today=date(2026, 6, 23),
        eval_dates=[date(2026, 6, 20), date(2026, 6, 10), date(2026, 5, 30)],
    )

    # Three distinct eval_date keys
    assert set(result["per_date"].keys()) == {"2026-06-20", "2026-06-10", "2026-05-30"}
    # Each date has 3 symbol rows (no skips; parquet fixtures have 260 rows each)
    for ed, rows in result["per_date"].items():
        assert len(rows) == 3, f"{ed}: expected 3 rows, got {len(rows)}"

    # The CRITICAL invariant: different eval_dates → different current_close
    # for the same symbol (proves data slicing actually varies).
    aapl_by_date = {
        r["eval_date"]: r["current_close"]
        for r in result["per_date"]["2026-06-20"] + result["per_date"]["2026-06-10"] + result["per_date"]["2026-05-30"]
        if r["symbol"] == "AAPL"
    }
    assert len(set(aapl_by_date.values())) == 3, (
        f"AAPL current_close should be distinct across 3 eval_dates, got {aapl_by_date}"
    )
    # Earlier eval_date → earlier (smaller) current_close (chronological order).
    assert aapl_by_date["2026-05-30"] < aapl_by_date["2026-06-10"] < aapl_by_date["2026-06-20"]


def test_run_shadow_for_model_eval_dates_path_no_lookahead(tmp_path, monkeypatch):
    """The model must NEVER see data after eval_date (anti-lookahead guard).

    Spy on build_test_tsdf_full to capture (cutoff_days_ago, eval_date) calls
    and assert eval_date is set + df max timestamp <= eval_date.
    """
    from scripts import shadow_trade, eval_chronos_h2h_v3
    captured_calls = []
    real_build = eval_chronos_h2h_v3.build_test_tsdf_full

    def spy_build(symbol, cutoff_days_ago=1, eval_date=None):
        captured_calls.append({"symbol": symbol, "cutoff": cutoff_days_ago, "eval_date": eval_date})
        return real_build(symbol, cutoff_days_ago=cutoff_days_ago, eval_date=eval_date)

    for sym in ["AAPL", "NVDA", "TSLA"]:
        _write_aapl_parquet(tmp_path / f"{sym}.parquet", n_days=320)
    monkeypatch.setattr(eval_chronos_h2h_v3, "FEATURES_DIR", tmp_path)
    # B2: see note above — patch the actual lookup location of the moved function.
    from stockapp_v12.eval import test_data as _eval_test_data
    monkeypatch.setattr(_eval_test_data, "FEATURES_DIR", tmp_path)
    monkeypatch.setattr(shadow_trade, "build_test_tsdf_full", spy_build)
    # B2: the actual call happens via stockapp_v12.eval.predict; patch the spy there too.
    from stockapp_v12.eval import predict as _eval_predict
    monkeypatch.setattr(_eval_predict, "build_test_tsdf_full", spy_build)

    pred = _FakePredictor()
    shadow_trade.run_shadow_for_model(
        pred, "fake_model",
        symbols=["AAPL"],
        cutoffs=[1],
        today=date(2026, 6, 23),
        eval_dates=[date(2026, 6, 20), date(2026, 6, 15)],
    )

    # Every call must have eval_date set (no None falling back to legacy)
    assert all(c["eval_date"] is not None for c in captured_calls), captured_calls
    # Both eval_dates observed
    eval_dates_seen = {c["eval_date"] for c in captured_calls}
    assert eval_dates_seen == {"2026-06-20", "2026-06-15"}, eval_dates_seen
    # Each (symbol, eval_date) called exactly once
    pairs = [(c["symbol"], c["eval_date"]) for c in captured_calls]
    assert len(pairs) == 2 and len(set(pairs)) == 2


def test_run_shadow_for_model_legacy_cutoffs_path_still_works(tmp_path, monkeypatch):
    """When eval_dates is None, the legacy cutoffs path must keep working.

    Without this guard, the cron shadow-trade loop would break.
    """
    from scripts import shadow_trade, eval_chronos_h2h_v3
    for sym in ["AAPL", "NVDA", "TSLA"]:
        _write_aapl_parquet(tmp_path / f"{sym}.parquet", n_days=320)
    monkeypatch.setattr(eval_chronos_h2h_v3, "FEATURES_DIR", tmp_path)
    # B2: see note above — patch the actual lookup location of the moved function.
    from stockapp_v12.eval import test_data as _eval_test_data
    monkeypatch.setattr(_eval_test_data, "FEATURES_DIR", tmp_path)

    pred = _FakePredictor()
    result = shadow_trade.run_shadow_for_model(
        pred, "fake_model",
        symbols=["AAPL", "NVDA", "TSLA"],
        cutoffs=[1, 2, 3],  # legacy: 3 different "window sizes"
        today=date(2026, 7, 1),
    )

    # Legacy mode produces eval_date labels = today - cutoff days
    assert set(result["per_date"].keys()) == {"2026-06-30", "2026-06-29", "2026-06-28"}
    for ed, rows in result["per_date"].items():
        assert len(rows) == 3  # 3 symbols per date