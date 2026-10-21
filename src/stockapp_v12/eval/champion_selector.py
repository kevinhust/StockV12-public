"""Champion model selection — **research-only** scorecard (not live path).

Gate Decision Architecture (Accepted 2026-07-26):
  Live open / halt → L0 + L1
  Path change → L2 ``scripts/evaluate_path_change.py`` + human
  This module → layer **D (Diagnostic)** only

The historical 7-gate table remains for experiments and tests. It must
**not** alone approve a decision-path swap (conflicts with Round-4 PnL
moratorium and L2 relative rules).

Spec (historical): docs/superpowers/specs/2026-07-10-champion-model-selection-design.md
"""
from __future__ import annotations

from dataclasses import dataclass

# Explicit: not for live decision-path promotion (Gate Arch layer D).
RESEARCH_ONLY = True
LIVE_PATH_CHANGE_MODULE = "scripts.evaluate_path_change"


@dataclass(frozen=True)
class ModelMetrics:
    """Aggregate metrics for a single model over evaluation windows."""
    name: str
    pnl_30d: float
    sharpe_30d: float
    sharpe_7d_live: float
    win_rate_30d: float
    mase_30d: float
    n_dates_30d: int
    n_trades_30d: int
    hyperparam_family: str


@dataclass(frozen=True)
class GateResult:
    """Outcome of the research 7-gate scorecard (not L2 path-change)."""
    passed: bool
    failed_gates: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.passed


def check_promotion_gates(m: ModelMetrics) -> GateResult:
    """Research scorecard (layer D). Do **not** use as live path-change gate.

    Prefer ``evaluate_path_change`` (L2) for decision-path swaps.

    Thresholds (historical spec — research only):
      pnl_30d > 0
      sharpe_30d >= 1.0
      sharpe_7d_live >= 0.5
      win_rate_30d >= 0.55
      mase_30d <= 2.1181
      n_dates_30d >= 30
      n_trades_30d >= 150
    """
    failed: list[str] = []
    if m.pnl_30d <= 0:
        failed.append("pnl_30d")
    if m.sharpe_30d < 1.0:
        failed.append("sharpe_30d")
    if m.sharpe_7d_live < 0.5:
        failed.append("sharpe_7d_live")
    if m.win_rate_30d < 0.55:
        failed.append("win_rate_30d")
    if m.mase_30d > 2.1181:
        failed.append("mase_30d")
    if m.n_dates_30d < 30:
        failed.append("n_dates_30d")
    if m.n_trades_30d < 150:
        failed.append("n_trades_30d")
    return GateResult(passed=len(failed) == 0, failed_gates=tuple(failed))


def check_research_scorecard(m: ModelMetrics) -> GateResult:
    """Alias for :func:`check_promotion_gates` — preferred name (layer D)."""
    return check_promotion_gates(m)


@dataclass(frozen=True)
class ChampionSelection:
    """Result of running the selection pipeline across candidates.

    Notes
    -----
    `runner_up` is the informational second-place model in `eligible`
    (purely diagnostic — it is *not* a halt-backup). The actual
    halt-backup used by the eval loop is produced by `select_challenger()`
    (Task 4), which chooses the runner-up via a
    family-diversity tiebreaker. Do not confuse the two.
    """
    champion: str
    runner_up: str | None
    eligible: tuple[str, ...]


def select_champion(candidates: list[ModelMetrics]) -> ChampionSelection:
    """Research helper: highest-Sharpe model that passes the 7-gate scorecard.

    **Not** live path selection — use L2 ``evaluate_path_change`` + human.

    Raises ValueError if no model passes.
    """
    eligible: list[ModelMetrics] = []
    for m in candidates:
        if check_promotion_gates(m).passed:
            eligible.append(m)
    if not eligible:
        raise ValueError(
            f"No model passed all 7 gates (out of {len(candidates)} candidates)"
        )
    eligible.sort(key=lambda m: m.sharpe_30d, reverse=True)
    return ChampionSelection(
        champion=eligible[0].name,
        runner_up=eligible[1].name if len(eligible) > 1 else None,
        eligible=tuple(m.name for m in eligible),
    )


def select_challenger(
    candidates: list[ModelMetrics],
    champion: str,
) -> str:
    """Pick the best challenger: second-highest Sharpe, preferring low
    correlation (different hyperparam_family) with the champion.

    Raises ValueError if no challenger candidates exist.

    Note: This is the function called by the eval loop when the active
    champion halts (Task 8 onwards). It is distinct from ChampionSelection.runner_up
    (which is purely informational, see ChampionSelection docstring).
    """
    challengers = [m for m in candidates if m.name != champion]
    if not challengers:
        raise ValueError(f"No challenger candidates (champion={champion!r})")
    champion_family = next(
        m.hyperparam_family for m in candidates if m.name == champion
    )
    # Sort: by (same_family_last desc, sharpe desc) — i.e. ascend by
    # (same_family_flag, -sharpe). Different-family models first; within each
    # group, higher Sharpe first.
    challengers.sort(
        key=lambda m: (m.hyperparam_family == champion_family, -m.sharpe_30d)
    )
    return challengers[0].name
