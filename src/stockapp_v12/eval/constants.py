"""
Constants shared across the V12 evaluation package.

Moved from `v12/scripts/eval_chronos_h2h_v3.py` (B2, post-launch refactor).
"""

from __future__ import annotations

# Default 10-symbol OOS panel — covers mega-cap + index (broad regime mix).
DEFAULT_TEST_SYMBOLS: list[str] = [
    "AAPL", "NVDA", "TSLA", "JPM", "MSFT",
    "AMZN", "AMD", "META", "GOOGL", "SPY",
]

# Stage 1.1 smoke variant — 3 fast-loading mega-caps + 1 cutoff.
# Designed for PR-gate CI: <5 min runtime on GPU, <1 min wall without GPU.
SMOKE_SYMBOLS: list[str] = ["AAPL", "NVDA", "TSLA"]
SMOKE_CUTOFFS: list[int] = [10]

# Forecast horizon in trading days. Matches the AutoGluon Chronos training
# horizon (10 trading days = 2 calendar weeks). Used by predict.py to clip
# the held-out actual window.
FORECAST_HORIZON: int = 10

# |forecast_ret_pct| threshold above which shadow-trade takes a position.
# Matches the SR-01 / SR-02 rules in the signal rule engine.
DEFAULT_SIGNAL_THRESHOLD_PCT: float = 0.5

# Backfill horizon: same as FORECAST_HORIZON; eval_chronos_h2h_v3 used a
# local copy to keep the H2H path independent of shadow_trade. We now
# promote it to a shared constant.
BACKFILL_HORIZON: int = 10