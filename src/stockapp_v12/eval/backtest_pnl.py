"""
Per-trade P&L simulation from per-symbol forecast vs actual returns.

Moved from `v12/scripts/shadow_trade.py::simulate_pnl` (B2, post-launch
refactor). Behavior is byte-identical to the original; the only change
is the source of the `sign` helper and `DEFAULT_SIGNAL_THRESHOLD_PCT`.
"""

from __future__ import annotations

from stockapp_v12.eval.constants import DEFAULT_SIGNAL_THRESHOLD_PCT
from stockapp_v12.eval.metrics import sign


def simulate_pnl(
    forecast_pcts: list[float], actual_pcts: list[float],
    threshold: float = DEFAULT_SIGNAL_THRESHOLD_PCT,
) -> dict:
    """Simulate a long/short/flat daily P&L from per-symbol forecast vs actual.

    Rules (deliberately simple, matches SR-01 / SR-02):
      - If |forecast_pct| > threshold: take the trade (long if forecast>0,
        short if forecast<0). Per-trade pnl = sign(forecast) × actual_pct.
      - Else: flat, pnl = 0.
    """
    if len(forecast_pcts) != len(actual_pcts):
        raise ValueError(
            f"simulate_pnl: forecast/actual length mismatch "
            f"({len(forecast_pcts)} vs {len(actual_pcts)})"
        )
    pnls: list[float] = []
    for f, a in zip(forecast_pcts, actual_pcts):
        if abs(f) > threshold:
            pnls.append(sign(f) * a)
        # else: flat, no PnL contribution
    n_trades = len(pnls)
    if n_trades == 0:
        return {
            "total_pnl_pct": 0.0,
            "avg_pnl_per_trade_pct": 0.0,
            "n_trades": 0,
            "n_wins": 0,
            "win_rate": None,
            "threshold": threshold,
        }
    n_wins = sum(1 for p in pnls if p > 0)
    total = float(sum(pnls))
    return {
        "total_pnl_pct": round(total, 4),
        "avg_pnl_per_trade_pct": round(total / n_trades, 4),
        "n_trades": n_trades,
        "n_wins": n_wins,
        "win_rate": round(n_wins / n_trades, 4),
        "threshold": threshold,
    }