"""
V12 evaluation package.

Public surface (B2, post-launch refactor — all moved from
`v12/scripts/eval_chronos_h2h_v3.py` + `shadow_trade.py` +
`shadow_ensemble.py`):

  Pure metrics:
    - `naive_errors_from_actual`
    - `compute_mase`
    - `sign`, `sign_match`
    - `hit_rate`, `signal_hit_rate`
    - `summarize_forecasts`, `rank_models`

  Test data:
    - `build_test_tsdf_full`

  Backtest:
    - `simulate_pnl`

  Halt gate (Round 4 council paper-trade instrument):
    - `HaltGate`, `HaltDecision` — see `stockapp_v12.eval.halt_gate`

  Prediction:
    - `predict_one_symbol_for_date` (unified; was duplicated in shadow_trade
       and shadow_ensemble with divergent copies)

  Constants:
    - `DEFAULT_TEST_SYMBOLS`, `SMOKE_SYMBOLS`, `FORECAST_HORIZON`,
      `DEFAULT_SIGNAL_THRESHOLD_PCT`, `BACKFILL_HORIZON`

  ADR gate (existing, from A3):
    - `Adr008Thresholds`, `GateCheck`, `GateResult`, `evaluate`,
      `evaluate_file` — see `stockapp_v12.eval.adr008_gate`

  Champion selection (Topic 1 of brainstorm):
    - `ModelMetrics`, `GateResult`, `ChampionSelection`,
      `check_promotion_gates`, `select_champion`, `select_challenger`
      — see `stockapp_v12.eval.champion_selector`
"""

from stockapp_v12.eval.backtest_pnl import simulate_pnl
from stockapp_v12.eval.halt_gate import HaltDecision, HaltGate
from stockapp_v12.eval.constants import (
    BACKFILL_HORIZON,
    DEFAULT_SIGNAL_THRESHOLD_PCT,
    DEFAULT_TEST_SYMBOLS,
    FORECAST_HORIZON,
    SMOKE_CUTOFFS,
    SMOKE_SYMBOLS,
)
from stockapp_v12.eval.metrics import (
    SIGN_EPSILON,
    compute_mase,
    hit_rate,
    naive_errors_from_actual,
    rank_models,
    sign,
    sign_match,
    signal_hit_rate,
    summarize_forecasts,
)
from stockapp_v12.eval.predict import predict_one_symbol_for_date
from stockapp_v12.eval.test_data import FEATURES_DIR, REPO, build_test_tsdf_full
from stockapp_v12.eval.champion_selector import (
    ChampionSelection,
    GateResult,
    ModelMetrics,
    check_promotion_gates,
    check_research_scorecard,
    RESEARCH_ONLY,
    select_champion,
    select_challenger,
)

__all__ = [
    # metrics
    "SIGN_EPSILON",
    "compute_mase",
    "hit_rate",
    "naive_errors_from_actual",
    "rank_models",
    "sign",
    "sign_match",
    "signal_hit_rate",
    "summarize_forecasts",
    # test_data
    "FEATURES_DIR",
    "REPO",
    "build_test_tsdf_full",
    # backtest
    "simulate_pnl",
    # halt_gate (Round 4 council paper-trade instrument)
    "HaltDecision",
    "HaltGate",
    # predict
    "predict_one_symbol_for_date",
    # constants
    "BACKFILL_HORIZON",
    "DEFAULT_SIGNAL_THRESHOLD_PCT",
    "DEFAULT_TEST_SYMBOLS",
    "FORECAST_HORIZON",
    "SMOKE_CUTOFFS",
    "SMOKE_SYMBOLS",
    # champion selection — research-only scorecard (Gate Arch layer D)
    "ChampionSelection",
    "GateResult",
    "ModelMetrics",
    "RESEARCH_ONLY",
    "check_promotion_gates",
    "check_research_scorecard",
    "select_champion",
    "select_challenger",
]