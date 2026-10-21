"""L2 path-change evaluator (Gate Decision Architecture Accepted 2026-07-26)."""
from __future__ import annotations

from scripts.evaluate_path_change import (
    VERDICT_INSUFFICIENT,
    VERDICT_READY_FOR_HUMAN,
    VERDICT_REJECT,
    evaluate_path_change,
)


def _snap(**kw):
    base = {
        "n_trades": 20,
        "n_dates": 20,
        "hit_rate": 0.50,
        "mase_mean": 2.0,
        "pnl_pct": 10.0,
        "win_rate": 0.50,
        "sufficient": True,
    }
    base.update(kw)
    return base


def test_insufficient_when_challenger_trades_below_floor():
    r = evaluate_path_change(
        baseline=_snap(n_trades=20),
        challenger=_snap(n_trades=10, hit_rate=0.55),
        min_trades=15,
    )
    assert r["verdict"] == VERDICT_INSUFFICIENT
    assert any("n_trades" in x for x in r["reasons"])


def test_insufficient_when_n_dates_low():
    r = evaluate_path_change(
        baseline=_snap(n_dates=20),
        challenger=_snap(n_dates=10),
        min_trades=15,
        min_n_dates=15,
    )
    assert r["verdict"] == VERDICT_INSUFFICIENT


def test_reject_when_hit_worse():
    r = evaluate_path_change(
        baseline=_snap(hit_rate=0.50),
        challenger=_snap(hit_rate=0.40),
    )
    assert r["verdict"] == VERDICT_REJECT
    assert any("hit_rate" in x for x in r["reasons"])


def test_reject_when_mase_worse_than_ratio():
    r = evaluate_path_change(
        baseline=_snap(mase_mean=2.0, hit_rate=0.50),
        challenger=_snap(mase_mean=2.2, hit_rate=0.50),  # > 2.0 * 1.05
        max_mase_ratio=1.05,
    )
    assert r["verdict"] == VERDICT_REJECT
    assert any("mase" in x for x in r["reasons"])


def test_ready_when_not_worse_on_hit_and_mase():
    r = evaluate_path_change(
        baseline=_snap(hit_rate=0.50, mase_mean=2.0, pnl_pct=10.0),
        challenger=_snap(hit_rate=0.50, mase_mean=2.05, pnl_pct=8.0),  # mase within 1.05
    )
    assert r["verdict"] == VERDICT_READY_FOR_HUMAN
    assert r["checks"]["c2_relative_quality"] is True
    # PnL worse is informational only
    assert r["informational"]["delta_pnl_pct"] == -2.0
    assert r["auto_promote"] is False


def test_tie_hit_and_better_mase_ready():
    r = evaluate_path_change(
        baseline=_snap(hit_rate=0.45, mase_mean=2.5),
        challenger=_snap(hit_rate=0.45, mase_mean=2.3),
    )
    assert r["verdict"] == VERDICT_READY_FOR_HUMAN


def test_halt_reject_when_challenger_halts_more():
    r = evaluate_path_change(
        baseline=_snap(),
        challenger=_snap(hit_rate=0.55, mase_mean=1.9),
        baseline_halt_days=1,
        challenger_halt_days=4,
        max_extra_halt_days=1,
    )
    assert r["verdict"] == VERDICT_REJECT
    assert any("halt" in x for x in r["reasons"])


def test_halt_skipped_when_missing_counts():
    r = evaluate_path_change(
        baseline=_snap(),
        challenger=_snap(hit_rate=0.55, mase_mean=1.9),
        baseline_halt_days=None,
        challenger_halt_days=None,
    )
    assert r["verdict"] == VERDICT_READY_FOR_HUMAN
    assert r["checks"]["c3_halt"] == "skipped"


def test_never_auto_promote_flag():
    r = evaluate_path_change(baseline=_snap(), challenger=_snap(hit_rate=0.6, mase_mean=1.5))
    assert r["auto_promote"] is False
    assert r["layer"] == "L2"
