# tests/test_champion_evaluation.py
import json
from pathlib import Path

import pytest

from scripts.champion_evaluation import (
    DEFAULT_MIN_TRADES,
    OBSERVATION_MODELS,
    build_multiwindow_report,
    compute_metrics_for_model,
    compute_window_snapshot,
    filter_rows_last_n_dates,
    load_all_metrics,
    load_model_rows,
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
        _row("A", forecast=2.0, actual=1.0),
        _row("B", forecast=2.0, actual=-0.5),
        _row("C", forecast=0.3, actual=0.3),
    ]
    metrics = compute_metrics_for_model("x", rows, hyperparam_family="fam")
    assert metrics is not None
    assert metrics.name == "x"
    assert metrics.n_trades_30d == 2
    assert metrics.win_rate_30d == 0.5
    assert metrics.pnl_30d == pytest.approx(0.5)
    assert metrics.n_dates_30d == 1


def test_compute_metrics_no_actionable_returns_none():
    rows = [_row("A", forecast=0.3, actual=0.1)]
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


def test_filter_rows_last_n_dates_keeps_newest_only():
    rows = [
        {**_row("A", 2.0, 1.0), "eval_date": "2026-07-20"},
        {**_row("A", 2.0, 1.0), "eval_date": "2026-07-21"},
        {**_row("A", 2.0, 1.0), "eval_date": "2026-07-22"},
        {**_row("B", 2.0, -1.0), "eval_date": "2026-07-22"},
    ]
    out = filter_rows_last_n_dates(rows, n_dates=2)
    dates = sorted({r["eval_date"] for r in out})
    assert dates == ["2026-07-21", "2026-07-22"]
    assert len(out) == 3


def test_compute_window_snapshot_insufficient_when_below_min_trades():
    rows = [
        {**_row("A", 2.0, 1.0), "eval_date": "2026-07-20"},
        {**_row("B", 2.0, -0.5), "eval_date": "2026-07-20"},
    ]
    snap = compute_window_snapshot(rows, min_trades=5)
    assert snap["n_trades"] == 2
    assert snap["sufficient"] is False
    assert snap["rankable"] is False
    assert snap["pnl_pct"] == pytest.approx(0.5)


def test_compute_window_snapshot_rankable_when_enough_trades():
    rows = [
        {**_row("A", 2.0, 1.0), "eval_date": f"2026-07-{20 + i:02d}"}
        for i in range(5)
    ]
    # 5 dates × 1 trade each
    snap = compute_window_snapshot(rows, min_trades=5)
    assert snap["n_trades"] == 5
    assert snap["sufficient"] is True
    assert snap["rankable"] is True
    assert snap["win_rate"] == pytest.approx(1.0)
    assert snap["hit_rate"] == pytest.approx(1.0)


def test_build_multiwindow_report_marks_insufficient(tmp_path: Path):
    shadow = tmp_path / "_shadow"
    # Only 2 trades total — below default min for 10d/20d ranking
    for d in ("2026-07-20", "2026-07-21"):
        _write_shadow_json(
            shadow / "phase2_v2" / f"{d}.json",
            [{**_row("A", 2.0, 1.0), "eval_date": d}],
            eval_date=d,
        )
    report = build_multiwindow_report(
        shadow,
        models=["phase2_v2"],
        windows={"full": None, "10d": 10, "20d": 20},
        min_trades=5,
    )
    assert report["min_trades"] == 5
    assert "phase2_v2" in report["models"]
    full = report["models"]["phase2_v2"]["windows"]["full"]
    assert full["n_trades"] == 2
    assert full["sufficient"] is False
    assert report["rankings"]["full"] == []  # nothing rankable


def test_observation_models_constant_matches_adr010():
    assert "zeroshot_bolt_small" not in OBSERVATION_MODELS
    assert "phase2_v2" in OBSERVATION_MODELS
    assert "phase2_wf_20260724" in OBSERVATION_MODELS
    assert "E07_long_ctx" in OBSERVATION_MODELS
    assert DEFAULT_MIN_TRADES >= 1