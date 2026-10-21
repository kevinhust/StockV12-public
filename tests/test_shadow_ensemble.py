"""Tests for shadow_ensemble.py pure functions.

These tests cover the ensemble-specific aggregation logic without spinning
up AutoGluon / GPU. The AutoGluon predict path is exercised separately in
test_ensemble_chronos_backend.py via mocked TimeSeriesPredictor.

Functions tested:
  - aggregate_ensemble_row  (per-symbol fallback vs average decision)
  - aggregate_ensemble_per_date_jsons (multi-day accumulation w/ spread stats)
  - predict_one_symbol_for_date  (per-model predict wrapper — mocked predictor)
  - run_shadow_ensemble  (end-to-end write artifacts, mocked predictors)
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# aggregate_ensemble_row — the core fallback decision
# ---------------------------------------------------------------------------


def test_aggregate_no_fallback_when_spread_below_threshold():
    """spread <= threshold → arithmetic mean of all model forecasts."""
    from scripts.shadow_ensemble import aggregate_ensemble_row
    a = {"symbol": "AAPL", "current_close": 300.0, "forecast_mean": 303.0,
         "forecast_ret_pct": 1.0, "actual_mean": 301.0, "actual_ret_pct": 0.33, "mase": 1.5}
    b = {"symbol": "AAPL", "current_close": 300.0, "forecast_mean": 304.5,
         "forecast_ret_pct": 1.5, "actual_mean": 302.0, "actual_ret_pct": 0.67, "mase": 1.6}
    row = aggregate_ensemble_row([a, b], ["var_a", "var_b"], fallback_spread_pct=3.0)
    assert row["used_fallback"] is False
    assert row["fallback_reason"] is None
    # Arithmetic mean: (303.0 + 304.5) / 2 = 303.75
    assert row["forecast_mean"] == 303.75
    # forecast_ret_pct derived from mean vs current_close
    assert row["forecast_ret_pct"] == pytest.approx(1.25, abs=0.01)
    assert row["per_model_spread_pct"] == 0.5
    assert row["n_models_valid"] == 2


def test_aggregate_fallback_when_spread_exceeds_threshold():
    """spread > threshold → fall back to FIRST valid model."""
    from scripts.shadow_ensemble import aggregate_ensemble_row
    a = {"symbol": "TSLA", "current_close": 400.0, "forecast_mean": 396.0,
         "forecast_ret_pct": -1.0, "actual_mean": 401.0, "actual_ret_pct": 0.25, "mase": 2.0}
    b = {"symbol": "TSLA", "current_close": 400.0, "forecast_mean": 416.0,
         "forecast_ret_pct": 4.0, "actual_mean": 403.0, "actual_ret_pct": 0.75, "mase": 1.8}
    row = aggregate_ensemble_row([a, b], ["var_a", "var_b"], fallback_spread_pct=3.0)
    assert row["used_fallback"] is True
    # Falls back to var_a (first in label order)
    assert row["forecast_mean"] == 396.0
    assert row["forecast_ret_pct"] == -1.0
    assert row["per_model_spread_pct"] == 5.0
    assert "spread" in row["fallback_reason"].lower()
    assert "var_a" in row["fallback_reason"]


def test_aggregate_exact_threshold_is_no_fallback():
    """spread == threshold (not >) → no fallback. Match > not >= to be strict."""
    from scripts.shadow_ensemble import aggregate_ensemble_row
    a = {"symbol": "NVDA", "current_close": 1000.0, "forecast_mean": 985.0,
         "forecast_ret_pct": -1.5, "actual_mean": 1005.0, "actual_ret_pct": 0.5, "mase": 1.8}
    b = {"symbol": "NVDA", "current_close": 1000.0, "forecast_mean": 1015.0,
         "forecast_ret_pct": 1.5, "actual_mean": 1007.0, "actual_ret_pct": 0.7, "mase": 1.7}
    # spread = 1.5 - (-1.5) = 3.0, threshold = 3.0
    row = aggregate_ensemble_row([a, b], ["var_a", "var_b"], fallback_spread_pct=3.0)
    assert row["used_fallback"] is False
    assert row["forecast_mean"] == 1000.0  # mean of 985 + 1015
    assert row["per_model_spread_pct"] == 3.0


def test_aggregate_skip_none_models():
    """Models returning None are excluded from spread / average."""
    from scripts.shadow_ensemble import aggregate_ensemble_row
    a = {"symbol": "META", "current_close": 500.0, "forecast_mean": 505.0,
         "forecast_ret_pct": 1.0, "actual_mean": 502.0, "actual_ret_pct": 0.4, "mase": 1.4}
    row = aggregate_ensemble_row([a, None], ["var_a", "var_b"], fallback_spread_pct=3.0)
    assert row["used_fallback"] is False
    assert row["n_models_valid"] == 1
    assert row["forecast_mean"] == 505.0  # only var_a contributes
    assert row["per_model_forecast_ret_pct"] == {"var_a": 1.0}


def test_aggregate_all_none_returns_none():
    """If all models return None, aggregate_ensemble_row returns None."""
    from scripts.shadow_ensemble import aggregate_ensemble_row
    assert aggregate_ensemble_row([None, None], ["a", "b"], fallback_spread_pct=3.0) is None
    assert aggregate_ensemble_row([], [], fallback_spread_pct=3.0) is None


def test_aggregate_skips_models_with_missing_forecast_ret_pct():
    """Model rows missing forecast_ret_pct (e.g. data fetch failed) are skipped."""
    from scripts.shadow_ensemble import aggregate_ensemble_row
    a = {"symbol": "AMZN", "current_close": 200.0, "forecast_mean": 202.0,
         "forecast_ret_pct": 1.0, "actual_mean": 201.0, "actual_ret_pct": 0.5, "mase": 1.2}
    b = {"symbol": "AMZN", "current_close": 200.0, "forecast_mean": None,
         "forecast_ret_pct": None, "actual_mean": None, "actual_ret_pct": None, "mase": None}
    row = aggregate_ensemble_row([a, b], ["var_a", "var_b"], fallback_spread_pct=3.0)
    assert row["n_models_valid"] == 1
    assert row["forecast_mean"] == 202.0


def test_aggregate_first_model_label_used_for_fallback_reason():
    """Fallback message names the chosen model explicitly (audit trail)."""
    from scripts.shadow_ensemble import aggregate_ensemble_row
    a = {"symbol": "GOOGL", "current_close": 150.0, "forecast_mean": 148.0,
         "forecast_ret_pct": -1.33, "actual_mean": 149.0, "actual_ret_pct": -0.67, "mase": 1.3}
    b = {"symbol": "GOOGL", "current_close": 150.0, "forecast_mean": 155.0,
         "forecast_ret_pct": 3.33, "actual_mean": 151.0, "actual_ret_pct": 0.67, "mase": 1.4}
    row = aggregate_ensemble_row([a, b], ["phase2_v2", "E05_p3_coldstart_seed42"],
                                 fallback_spread_pct=3.0)
    assert row["used_fallback"] is True
    assert "phase2_v2" in row["fallback_reason"]


# ---------------------------------------------------------------------------
# aggregate_ensemble_per_date_jsons — multi-day accumulation + spread stats
# ---------------------------------------------------------------------------


def _write_fake_per_date_jsons(ensemble_dir: Path, dates: list[str],
                                rows_per_date: list[list[dict]] = None,
                                **kwargs) -> None:
    """Helper: write N per-date JSONs into ensemble_dir for testing.

    Accepts both keyword styles used in different tests:
      - rows_per_date=  (list of lists, one inner list per date)
      - rows=           (single list of rows for a single date)
    """
    if rows_per_date is None and "rows" in kwargs:
        # `rows=` is a flat list of row dicts for ONE date — use directly
        rows_per_date = [kwargs["rows"]]
    if rows_per_date is None:
        raise TypeError("must pass either rows_per_date= or rows=")
    ensemble_dir.mkdir(parents=True, exist_ok=True)
    for d, rows in zip(dates, rows_per_date):
        payload = {
            "ensemble_name": "A_B_ensemble",
            "eval_date": d,
            "rows": rows,
        }
        (ensemble_dir / f"{d}.json").write_text(json.dumps(payload))


def _row(symbol: str, fcst_ret: float, actual_ret: float, spread: float,
         used_fb: bool, mase: float = 1.5) -> dict:
    return {
        "symbol": symbol, "current_close": 100.0,
        "forecast_mean": 100.0 + fcst_ret, "forecast_ret_pct": fcst_ret,
        "actual_mean": 100.0 + actual_ret, "actual_ret_pct": actual_ret,
        "mase": mase,
        "per_model_spread_pct": spread, "used_fallback": used_fb,
        "n_models_valid": 2,
    }


def test_aggregate_per_date_empty_dir():
    """No per-date JSONs → n_dates=0, aggregate=None."""
    from scripts.shadow_ensemble import aggregate_ensemble_per_date_jsons
    with pytest.TempdirFactory() if False else __import__("tempfile").TemporaryDirectory() as td:
        result = aggregate_ensemble_per_date_jsons(Path(td))
    assert result["n_dates"] == 0
    assert result["aggregate"] is None


def test_aggregate_per_date_spread_stats_p50_p95_max():
    """p50, p95, max are computed from per-row spread_pcts."""
    from scripts.shadow_ensemble import aggregate_ensemble_per_date_jsons
    with __import__("tempfile").TemporaryDirectory() as td:
        ed = Path(td)
        # 3 days, varied spreads: 0.5, 1.5, 5.0
        rows_d1 = [_row("AAPL", 1.0, 0.5, 0.5, False)]
        rows_d2 = [_row("AAPL", 1.0, 0.5, 1.5, False),
                   _row("NVDA", 0.5, 0.3, 1.5, False)]
        rows_d3 = [_row("AAPL", 1.0, 0.5, 5.0, True)]
        _write_fake_per_date_jsons(
            ed,
            dates=["2026-07-01", "2026-07-02", "2026-07-03"],
            rows_per_date=[rows_d1, rows_d2, rows_d3],
        )
        result = aggregate_ensemble_per_date_jsons(ed)
        agg = result["aggregate"]
        assert result["n_dates"] == 3
        assert agg["n_rows"] == 4
        # 4 spread values: [0.5, 1.5, 1.5, 5.0]
        # p50 = 1.5, max = 5.0, p95 between 1.5 and 5.0 (linear interp)
        assert agg["spread_stats"]["max"] == 5.0
        assert agg["spread_stats"]["p50"] == 1.5
        assert agg["spread_stats"]["p95"] >= 1.5
        # 1 fallback out of 4 rows
        assert agg["fallback_count"] == 1
        assert agg["fallback_rate"] == pytest.approx(0.25)


def test_aggregate_per_date_skips_rows_missing_spread():
    """Rows without per_model_spread_pct are excluded from spread stats."""
    from scripts.shadow_ensemble import aggregate_ensemble_per_date_jsons
    with __import__("tempfile").TemporaryDirectory() as td:
        ed = Path(td)
        row_with = _row("AAPL", 1.0, 0.5, 3.5, True)
        row_without = _row("NVDA", 0.5, 0.3, 1.0, False)
        row_without.pop("per_model_spread_pct")
        _write_fake_per_date_jsons(ed, dates=["2026-07-01"],
                                    rows_per_date=[[row_with, row_without]])
        result = aggregate_ensemble_per_date_jsons(ed)
        agg = result["aggregate"]
        assert agg["spread_stats"]["n"] == 1
        assert agg["spread_stats"]["max"] == 3.5


def test_aggregate_per_date_hit_rate_uses_only_paired_rows():
    """hit_rate skips rows missing forecast_ret_pct or actual_ret_pct."""
    from scripts.shadow_ensemble import aggregate_ensemble_per_date_jsons
    with __import__("tempfile").TemporaryDirectory() as td:
        ed = Path(td)
        good = _row("AAPL", 1.0, 0.5, 1.0, False)  # matched sign → hit
        no_actual = _row("NVDA", 1.0, 0.5, 1.0, False)
        no_actual["actual_ret_pct"] = None
        _write_fake_per_date_jsons(ed, dates=["2026-07-01"],
                                    rows_per_date=[[good, no_actual]])
        result = aggregate_ensemble_per_date_jsons(ed)
        agg = result["aggregate"]
        # n_pairs should be 1 (good row only), n_rows = 2
        assert agg["n_rows"] == 2
        assert agg["n_pairs"] == 1


def test_aggregate_per_date_handles_backfill_subdir():
    """Subdirectories of ensemble_dir (e.g. _backfill/) are NOT walked.

    Unlike shadow_trade.py's aggregate_all_per_date_jsons which walks both
    `model_dir` and `model_dir/_backfill/`, this function globs only the
    top-level ensemble_dir. Backfill data would need to be placed at
    top-level (or a separate aggregator written for it).
    """
    from scripts.shadow_ensemble import aggregate_ensemble_per_date_jsons
    with __import__("tempfile").TemporaryDirectory() as td:
        ed = Path(td)
        backfill_dir = ed / "_backfill"
        backfill_dir.mkdir(parents=True, exist_ok=True)
        # Top-level (cron) — should be picked up
        _write_fake_per_date_jsons(ed, dates=["2026-07-01"],
                                    rows=[_row("AAPL", 1.0, 0.5, 1.0, False)])
        # Backfill subdir — should NOT be picked up by this aggregator
        _write_fake_per_date_jsons(backfill_dir, dates=["2026-06-15"],
                                    rows=[_row("NVDA", 0.5, 0.3, 0.8, False)])
        result = aggregate_ensemble_per_date_jsons(ed)
        # Only cron row counted; backfill row ignored
        assert result["n_dates"] == 1
        assert result["aggregate"]["n_rows"] == 1


# ---------------------------------------------------------------------------
# predict_one_symbol_for_date — wrapper around eval helpers
# ---------------------------------------------------------------------------


class _MockPredictor:
    """Minimal stand-in for AutoGluon TimeSeriesPredictor used in shadow_ensemble.predict.

    Mocks both `pred["mean"].iloc[:N].mean()` and the fallback
    `pred.iloc[:N].mean().mean()` paths in predict_one_symbol_for_date.

    Key trick: `iloc` is a property that returns a subscriptable _Sliceable
    (so `pred["mean"].iloc[:N]` works as expected — a method would fail with
    "'method' object is not subscriptable").
    """

    def __init__(self, forecast_mean: float):
        self._forecast_mean = forecast_mean

    def predict(self, tsdf):  # noqa: ARG002
        class _MeanVal:
            def __init__(self, v): self._v = v
            def mean(self_inner): return self_inner._v
            def mean_twice(self_inner): return self_inner._v

        class _Sliceable:
            """Supports `_sliceable[:N]` (returns _MeanVal) and `_sliceable.mean()`."""
            def __init__(self, v): self._v = v
            def __getitem__(self_inner, _slice): return _MeanVal(self_inner._v)
            def mean(self_inner): return self_inner._v

        class _Col:
            """A column-like object: `pred["mean"]` returns this."""
            def __init__(self, v): self._v = v
            @property
            def iloc(self_inner): return _Sliceable(self_inner._v)
            def mean(self_inner): return self_inner._v

        class _PredDF:
            """A DataFrame-like object supporting `pred["mean"]` and `pred.iloc[:N]`."""
            def __init__(self, v): self._v = v
            def __getitem__(self_inner, _key): return _Col(self_inner._v)
            @property
            def iloc(self_inner): return _Sliceable(self_inner._v)

        return _PredDF(self._forecast_mean)


def test_predict_one_symbol_skips_short_history(monkeypatch):
    """Symbols with <252 rows are skipped (not enough history for 10-day forecast)."""
    from scripts import shadow_ensemble

    # Patch build_test_tsdf_full to return a small df
    def fake_build(symbol, **kwargs):
        idx = pd.date_range("2026-01-01", periods=100, freq="D")
        df = pd.DataFrame({
            "symbol": symbol,
            "close": np.linspace(100, 110, 100),
            "timestamp": idx,
        })
        return df, pd.Series([], dtype=float)

    monkeypatch.setattr(shadow_ensemble, "build_test_tsdf_full", fake_build)
    # B2: predict_one_symbol_for_date lives in stockapp_v12.eval.predict and
    # calls build_test_tsdf_full from stockapp_v12.eval.test_data at module
    # load. Patch the lookup location so the spy reaches the real call site.
    from stockapp_v12.eval import predict as _eval_predict
    monkeypatch.setattr(_eval_predict, "build_test_tsdf_full", fake_build)
    row = shadow_ensemble.predict_one_symbol_for_date(
        _MockPredictor(forecast_mean=110.0), "SHORT",
    )
    assert row is None  # 100 rows < 252 → skip


def test_predict_one_symbol_returns_per_symbol_dict(monkeypatch):
    """Normal case: returns dict with forecast_mean / forecast_ret_pct / actual_* fields."""
    from scripts import shadow_ensemble

    def fake_build(symbol, **kwargs):
        idx = pd.date_range("2024-01-01", periods=300, freq="D")
        df = pd.DataFrame({
            "symbol": symbol,
            "close": np.linspace(100, 110, 300),
            "timestamp": idx,
        })
        actual = pd.Series(np.linspace(111, 113, 10))
        return df, actual

    monkeypatch.setattr(shadow_ensemble, "build_test_tsdf_full", fake_build)
    # B2: see note above — patch the actual lookup location of the moved function.
    from stockapp_v12.eval import predict as _eval_predict
    monkeypatch.setattr(_eval_predict, "build_test_tsdf_full", fake_build)
    row = shadow_ensemble.predict_one_symbol_for_date(
        _MockPredictor(forecast_mean=115.0), "AAPL", cutoff_days_ago=1,
    )
    assert row is not None
    assert row["symbol"] == "AAPL"
    assert row["forecast_mean"] == 115.0
    # forecast_ret_pct = (115 - 110) / 110 * 100 = 4.545...
    assert row["forecast_ret_pct"] == pytest.approx(4.545, abs=0.01)
    assert row["actual_mean"] is not None
    assert row["mase"] is not None


# ---------------------------------------------------------------------------
# run_shadow_ensemble — end-to-end artifact writing (mocked predictors)
# ---------------------------------------------------------------------------


def test_run_shadow_ensemble_seat_refuses_decision_dir(monkeypatch, tmp_path):
    """ADR-010: trade_path=False cannot write into production ensemble/."""
    from scripts import shadow_ensemble

    monkeypatch.setattr(shadow_ensemble, "SHADOW_ROOT", tmp_path)
    fake_a = tmp_path / "var_a"
    fake_a.mkdir()
    with pytest.raises(ValueError, match="trade_path=False"):
        shadow_ensemble.run_shadow_ensemble(
            model_paths=[fake_a],
            symbols=["AAPL"],
            today=date(2026, 7, 1),
            ensemble_dir_name="ensemble",
            trade_path=False,
        )


def test_run_shadow_ensemble_seat_writes_sidecar_not_ensemble(monkeypatch, tmp_path):
    """E-SEAT writes only under ensemble_seat_* and leaves ensemble/ empty."""
    from scripts import shadow_ensemble
    import numpy as np
    import pandas as pd

    monkeypatch.setattr(shadow_ensemble, "SHADOW_ROOT", tmp_path)

    def fake_build(symbol, **kwargs):
        idx = pd.date_range("2024-01-01", periods=300, freq="D")
        df = pd.DataFrame({
            "symbol": symbol,
            "close": np.linspace(100, 110, 300),
            "timestamp": idx,
        })
        actual = pd.Series(np.linspace(111, 113, 10))
        return df, actual

    monkeypatch.setattr(shadow_ensemble, "load_model",
                         lambda p: _MockPredictor(forecast_mean=110.0))
    fake_a = tmp_path / "phase2_v2"
    fake_b = tmp_path / "E09_higher_lr"
    fake_a.mkdir()
    fake_b.mkdir()
    monkeypatch.setattr(shadow_ensemble, "build_test_tsdf_full", fake_build)
    from stockapp_v12.eval import predict as _eval_predict
    monkeypatch.setattr(_eval_predict, "build_test_tsdf_full", fake_build)

    # Pre-create production decision artifact that must not be touched
    decision_dir = tmp_path / "ensemble"
    decision_dir.mkdir()
    marker = decision_dir / "MARKER.json"
    marker.write_text('{"keep": true}')

    summary = shadow_ensemble.run_shadow_ensemble(
        model_paths=[fake_a, fake_b],
        symbols=["AAPL"],
        today=date(2026, 7, 24),
        cutoff_days_ago=1,
        ensemble_dir_name="ensemble_seat_e09",
        ensemble_name="seat_phase2_v2_E09",
        trade_path=False,
    )

    seat_dir = tmp_path / "ensemble_seat_e09"
    assert (seat_dir / "2026-07-24.json").exists()
    assert (seat_dir / "SUMMARY.json").exists()
    assert summary["trade_path"] is False
    assert summary["ensemble_dir_name"] == "ensemble_seat_e09"
    # Decision path untouched
    assert marker.read_text() == '{"keep": true}'
    assert not (decision_dir / "2026-07-24.json").exists()
    assert not (decision_dir / "SUMMARY.json").exists()


def test_run_shadow_ensemble_writes_artifacts(monkeypatch, tmp_path):
    """End-to-end: writes per-date JSON + per-model JSONs + SUMMARY.json."""
    from scripts import shadow_ensemble

    # Override SHADOW_ROOT to tmp_path so we don't pollute the repo
    monkeypatch.setattr(shadow_ensemble, "SHADOW_ROOT", tmp_path)
    monkeypatch.setattr(shadow_ensemble, "ENSEMBLE_DIR_NAME", "ensemble_test")

    # Mock build_test_tsdf_full to return synthetic data
    def fake_build(symbol, **kwargs):
        idx = pd.date_range("2024-01-01", periods=300, freq="D")
        df = pd.DataFrame({
            "symbol": symbol,
            "close": np.linspace(100, 110, 300),
            "timestamp": idx,
        })
        actual = pd.Series(np.linspace(111, 113, 10))
        return df, actual

    # Mock load_model so the run_shadow_ensemble thinks it loaded predictors
    monkeypatch.setattr(shadow_ensemble, "load_model",
                         lambda p: _MockPredictor(forecast_mean=110.0))

    # Create fake model paths so the FileNotFoundError check passes
    fake_a = tmp_path / "var_a"
    fake_b = tmp_path / "var_b"
    fake_a.mkdir()
    fake_b.mkdir()

    monkeypatch.setattr(shadow_ensemble, "build_test_tsdf_full", fake_build)
    # B2: see note above — patch the actual lookup location of the moved function.
    from stockapp_v12.eval import predict as _eval_predict
    monkeypatch.setattr(_eval_predict, "build_test_tsdf_full", fake_build)

    summary = shadow_ensemble.run_shadow_ensemble(
        model_paths=[fake_a, fake_b],
        symbols=["AAPL", "TSLA"],
        today=date(2026, 7, 1),
        cutoff_days_ago=1,
        fallback_spread_pct=3.0,
        threshold=0.5,
    )

    # Verify artifact paths.
    # CRITICAL (cron path, matches shadow_trade.py --predict-today):
    # single-cutoff (cutoff_days_ago=1) must label TODAY as eval_date,
    # not today-1. The daily cron writes today's filename so n_dates
    # advances by 1 per trading day instead of alternating.
    ensemble_dir = tmp_path / "ensemble_test"
    assert (ensemble_dir / "2026-07-01.json").exists(), (
        "single-cutoff cron path must write <today>.json, not <today-1>.json; "
        "this is the live shadow_ensemble cron regression covered by "
        "test_run_shadow_ensemble_single_cutoff_cron_labels_today"
    )
    assert not (ensemble_dir / "2026-06-30.json").exists(), (
        "single-cutoff cron path must NOT write today-1 filename"
    )
    assert (ensemble_dir / "SUMMARY.json").exists()
    assert (ensemble_dir / "per_model" / "var_a" / "2026-07-01.json").exists()
    assert (ensemble_dir / "per_model" / "var_b" / "2026-07-01.json").exists()
    # Verify the JSON's eval_date field is today, not today-1
    payload = json.loads((ensemble_dir / "2026-07-01.json").read_text())
    assert payload["eval_date"] == "2026-07-01"
    assert payload["today"] == "2026-07-01"

    # Verify summary shape
    assert summary["ensemble_name"] == "A_B_ensemble"
    assert sorted(summary["ensemble_models"]) == ["var_a", "var_b"]
    assert summary["fallback_spread_pct"] == 3.0
    assert summary["today"] == "2026-07-01"
    assert "current_run_aggregate" in summary
    assert "accumulated" in summary
    # accumulated.n_dates should be 1 (today's run)
    assert summary["accumulated"]["n_dates"] == 1


def test_run_shadow_ensemble_accumulates_across_days(monkeypatch, tmp_path):
    """Second run with a different day appends to accumulated stats."""
    from scripts import shadow_ensemble

    monkeypatch.setattr(shadow_ensemble, "SHADOW_ROOT", tmp_path)
    monkeypatch.setattr(shadow_ensemble, "ENSEMBLE_DIR_NAME", "ensemble_test")

    def fake_build(symbol, **kwargs):
        idx = pd.date_range("2024-01-01", periods=300, freq="D")
        df = pd.DataFrame({
            "symbol": symbol,
            "close": np.linspace(100, 110, 300),
            "timestamp": idx,
        })
        actual = pd.Series(np.linspace(111, 113, 10))
        return df, actual

    monkeypatch.setattr(shadow_ensemble, "load_model",
                         lambda p: _MockPredictor(forecast_mean=110.0))
    monkeypatch.setattr(shadow_ensemble, "build_test_tsdf_full", fake_build)
    # B2: see note above — patch the actual lookup location of the moved function.
    from stockapp_v12.eval import predict as _eval_predict
    monkeypatch.setattr(_eval_predict, "build_test_tsdf_full", fake_build)

    fake_a = tmp_path / "var_a"
    fake_b = tmp_path / "var_b"
    fake_a.mkdir()
    fake_b.mkdir()

    # Run for two different days
    shadow_ensemble.run_shadow_ensemble(
        model_paths=[fake_a, fake_b],
        symbols=["AAPL"],
        today=date(2026, 7, 1),
        fallback_spread_pct=3.0,
    )
    shadow_ensemble.run_shadow_ensemble(
        model_paths=[fake_a, fake_b],
        symbols=["AAPL"],
        today=date(2026, 7, 2),
        fallback_spread_pct=3.0,
    )

    ensemble_dir = tmp_path / "ensemble_test"
    # Both runs use single-cutoff cron semantics (default cutoff_days_ago=1),
    # so each run labels its own today, not today-1.
    assert (ensemble_dir / "2026-07-01.json").exists()
    assert (ensemble_dir / "2026-07-02.json").exists()
    assert not (ensemble_dir / "2026-06-30.json").exists()

    summary = json.loads((ensemble_dir / "SUMMARY.json").read_text())
    assert summary["accumulated"]["n_dates"] == 2
    # Each day collapses N per-model rows into 1 ensemble row per symbol.
    # 1 symbol × 2 days = 2 ensemble rows total.
    assert summary["accumulated"]["aggregate"]["n_rows"] == 2


def test_run_shadow_ensemble_single_cutoff_cron_labels_today(monkeypatch, tmp_path):
    """Single-cutoff cron path (cutoff_days_ago=1) MUST label TODAY as eval_date.

    Regression for: today=2026-07-13 was producing ensemble/2026-07-12.json
    (today - cutoff_days_ago), causing HaltGate on the next day to read the
    wrong artifact and emit insufficient_trades noise for the ensemble row.

    Mirrors shadow_trade.py --predict-today semantics: cron writes today's
    filename regardless of cutoff, so n_dates advances by 1 per trading day
    instead of alternating between today and today-1.

    The predict() call MUST use the legacy cutoff branch (eval_date=None)
    for data slicing so parquet-tail slicing stays "drop last N rows",
    matching shadow_trade.py line 516. Walk-forward mode (eval_date set)
    would silently switch to "data <= eval_date" slicing and produce
    empty actuals when parquet stops short of eval_date.
    """
    from scripts import shadow_ensemble

    monkeypatch.setattr(shadow_ensemble, "SHADOW_ROOT", tmp_path)
    monkeypatch.setattr(shadow_ensemble, "ENSEMBLE_DIR_NAME", "ensemble_test")

    def fake_build(symbol, **kwargs):
        idx = pd.date_range("2024-01-01", periods=300, freq="D")
        df = pd.DataFrame({
            "symbol": symbol,
            "close": np.linspace(100, 110, 300),
            "timestamp": idx,
        })
        actual = pd.Series(np.linspace(111, 113, 10))
        return df, actual

    monkeypatch.setattr(shadow_ensemble, "load_model",
                         lambda p: _MockPredictor(forecast_mean=110.0))
    monkeypatch.setattr(shadow_ensemble, "build_test_tsdf_full", fake_build)
    from stockapp_v12.eval import predict as _eval_predict
    monkeypatch.setattr(_eval_predict, "build_test_tsdf_full", fake_build)

    # Spy on predict_one_symbol_for_date to capture its (cutoff, eval_date) args
    predict_calls: list[tuple] = []
    real_predict = shadow_ensemble.predict_one_symbol_for_date

    def spy_predict(pred, sym, cutoff_days_ago, eval_date=None):
        predict_calls.append((cutoff_days_ago, eval_date))
        return real_predict(pred, sym, cutoff_days_ago=cutoff_days_ago,
                            eval_date=eval_date)

    monkeypatch.setattr(shadow_ensemble, "predict_one_symbol_for_date", spy_predict)

    fake_a = tmp_path / "var_a"
    fake_b = tmp_path / "var_b"
    fake_a.mkdir()
    fake_b.mkdir()

    summary = shadow_ensemble.run_shadow_ensemble(
        model_paths=[fake_a, fake_b],
        symbols=["AAPL", "NVDA"],
        today=date(2026, 7, 13),  # Monday in our scenario
        cutoff_days_ago=1,
        fallback_spread_pct=3.0,
    )

    # Filename = today, NOT today - 1
    ensemble_dir = tmp_path / "ensemble_test"
    expected = ensemble_dir / "2026-07-13.json"
    assert expected.exists(), (
        f"Expected {expected} but it was not written. "
        "single-cutoff cron path must label TODAY as eval_date."
    )
    wrong = ensemble_dir / "2026-07-12.json"
    assert not wrong.exists(), (
        f"{wrong} should NOT be written by single-cutoff cron path "
        "(this is the off-by-one bug being regression-tested)"
    )

    # JSON contents
    payload = json.loads(expected.read_text())
    assert payload["eval_date"] == "2026-07-13"
    assert payload["today"] == "2026-07-13"

    # SUMMARY.json's today label
    assert summary["today"] == "2026-07-13"

    # Per-model per-date JSONs also use today as filename
    assert (ensemble_dir / "per_model" / "var_a" / "2026-07-13.json").exists()
    assert (ensemble_dir / "per_model" / "var_b" / "2026-07-13.json").exists()

    # predict() was called with eval_date=None (legacy cutoff branch),
    # matching shadow_trade.py line ~516. Every (symbol, model) tuple = 2*2 = 4 calls.
    assert len(predict_calls) == 4
    for cutoff, ed in predict_calls:
        assert cutoff == 1, f"unexpected cutoff_days_ago={cutoff}"
        assert ed is None, (
            f"predict must be called with eval_date=None in single-cutoff cron "
            f"path (legacy cutoff branch). Got eval_date={ed!r}. Passing today as "
            f"eval_date would silently switch to walk-forward slicing (data <= eval_date) "
            f"and produce empty actuals when parquet stops short of today."
        )


def test_run_shadow_ensemble_multi_cutoff_walk_forward_uses_today_minus_cutoff(
    monkeypatch, tmp_path
):
    """Multi-cutoff path (cutoff_days_ago > 1) preserves historical walk-forward
    semantics: eval_date label = today - cutoff, and predict() is called with
    eval_date set so build_test_tsdf_full uses 'data <= eval_date' slicing.
    """
    from scripts import shadow_ensemble

    monkeypatch.setattr(shadow_ensemble, "SHADOW_ROOT", tmp_path)
    monkeypatch.setattr(shadow_ensemble, "ENSEMBLE_DIR_NAME", "ensemble_test")

    def fake_build(symbol, **kwargs):
        idx = pd.date_range("2024-01-01", periods=300, freq="D")
        df = pd.DataFrame({
            "symbol": symbol,
            "close": np.linspace(100, 110, 300),
            "timestamp": idx,
        })
        actual = pd.Series(np.linspace(111, 113, 10))
        return df, actual

    monkeypatch.setattr(shadow_ensemble, "load_model",
                         lambda p: _MockPredictor(forecast_mean=110.0))
    monkeypatch.setattr(shadow_ensemble, "build_test_tsdf_full", fake_build)
    from stockapp_v12.eval import predict as _eval_predict
    monkeypatch.setattr(_eval_predict, "build_test_tsdf_full", fake_build)

    predict_calls: list[tuple] = []
    real_predict = shadow_ensemble.predict_one_symbol_for_date

    def spy_predict(pred, sym, cutoff_days_ago, eval_date=None):
        predict_calls.append((cutoff_days_ago, eval_date))
        return real_predict(pred, sym, cutoff_days_ago=cutoff_days_ago,
                            eval_date=eval_date)

    monkeypatch.setattr(shadow_ensemble, "predict_one_symbol_for_date", spy_predict)

    fake_a = tmp_path / "var_a"
    fake_b = tmp_path / "var_b"
    fake_a.mkdir()
    fake_b.mkdir()

    shadow_ensemble.run_shadow_ensemble(
        model_paths=[fake_a, fake_b],
        symbols=["AAPL"],
        today=date(2026, 7, 13),
        cutoff_days_ago=3,
        fallback_spread_pct=3.0,
    )

    ensemble_dir = tmp_path / "ensemble_test"
    # Walk-forward mode: filename = today - cutoff_days_ago
    expected = ensemble_dir / "2026-07-10.json"
    assert expected.exists()
    payload = json.loads(expected.read_text())
    assert payload["eval_date"] == "2026-07-10"
    assert payload["today"] == "2026-07-13"

    # predict() is called with eval_date set to walk-forward mode
    # (build_test_tsdf_full will slice data <= eval_date)
    assert len(predict_calls) == 2  # 1 symbol × 2 models
    for cutoff, ed in predict_calls:
        assert cutoff == 3
        assert ed == "2026-07-10", (
            f"multi-cutoff path must pass eval_date={today - cutoff} to "
            f"predict() (walk-forward branch). Got eval_date={ed!r}."
        )


def test_constants_match_ensemble_backend():
    """shadow_ensemble constants must match EnsembleChronosBackend defaults."""
    from scripts import shadow_ensemble
    from stockapp_v12.services.ensemble_chronos_backend import (
        DEFAULT_FALLBACK_SPREAD_PCT as BACKEND_DEFAULT,
        ENSEMBLE_ENV_VAR as BACKEND_ENV,
        FALLBACK_SPREAD_ENV_VAR as BACKEND_FALLBACK_ENV,
    )
    assert shadow_ensemble.DEFAULT_FALLBACK_SPREAD_PCT == BACKEND_DEFAULT
    assert shadow_ensemble.ENSEMBLE_ENV_VAR == BACKEND_ENV
    assert shadow_ensemble.FALLBACK_SPREAD_ENV_VAR == BACKEND_FALLBACK_ENV