# tests/test_retrain_phase2_vn.py
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from scripts.retrain_phase2_vn import (
    PROTECTED_BASELINE,
    build_plan,
    filter_features_by_cutoff,
    load_recipe,
    output_dir_name,
    previous_trading_day,
)


def test_output_dir_name_default_uses_cutoff():
    assert output_dir_name(date(2026, 7, 24)) == "phase2_wf_20260724"


def test_output_dir_name_rejects_protected_baseline():
    with pytest.raises(ValueError, match="protected"):
        output_dir_name(date(2026, 7, 24), name=PROTECTED_BASELINE)


def test_previous_trading_day():
    assert previous_trading_day(date(2026, 7, 20)) == date(2026, 7, 17)


def test_filter_features_by_cutoff():
    df = pd.DataFrame(
        {
            "symbol": ["AAPL", "AAPL", "AAPL"],
            "timestamp": pd.to_datetime(
                ["2026-07-22", "2026-07-23", "2026-07-24"]
            ).tz_localize("America/New_York"),
            "close": [1.0, 2.0, 3.0],
        }
    )
    out = filter_features_by_cutoff(df, date(2026, 7, 23))
    assert len(out) == 2
    assert out["close"].tolist() == [1.0, 2.0]


def test_load_recipe_and_build_plan(tmp_path: Path):
    recipe_path = Path("config/chronos_phase2_v2_recipe.yaml")
    if not recipe_path.exists():
        pytest.skip("recipe not in workspace")
    recipe = load_recipe(recipe_path)
    plan = build_plan(
        recipe,
        cutoff=date(2026, 7, 24),
        name=None,
        features_dir=tmp_path / "feat",
        models_root=tmp_path / "models",
    )
    assert plan["out_name"] == "phase2_wf_20260724"
    assert plan["hyperparameters"]["fine_tune_steps"] == 1000
    assert plan["hyperparameters"]["fine_tune_lr"] == 1e-5
    assert plan["hyperparameters"]["fine_tune_batch_size"] == 32
    assert plan["protected_baseline"] == "phase2_v2"
