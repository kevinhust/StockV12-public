"""Tests for champion model selection (champion_selector module).

Topic 1 (Champion Model Selection) of the 3-topic brainstorm session for
parallel model PnL testing. The champion_selector module defines the
ModelMetrics value object and (in future tasks) the 7-gate promotion
logic, select_champion, and select_challenger functions.

Note: the spec lists an 8th gate (cross_period_sign_consistency) but it
is deferred — adding it requires pnl_7d_live on ModelMetrics, which would
break Task 1's frozen dataclass. See the champion_selector module
docstring for details.

Task 1 scope: just the ModelMetrics dataclass — verify it can be
instantiated with the documented fields.
Task 2 scope: 7-gate promotion logic via check_promotion_gates().
"""
from __future__ import annotations

import pytest

from stockapp_v12.eval.champion_selector import (
    RESEARCH_ONLY,
    ChampionSelection,
    GateResult,
    ModelMetrics,
    check_promotion_gates,
    check_research_scorecard,
    select_champion,
    select_challenger,
)


def test_model_metrics_required_fields():
    m = ModelMetrics(
        name="E07_long_ctx",
        pnl_30d=296.09,
        sharpe_30d=3.5,
        sharpe_7d_live=1.2,
        win_rate_30d=0.604,
        mase_30d=1.5,
        n_dates_30d=30,
        n_trades_30d=230,
        hyperparam_family="long_ctx",
    )
    assert m.name == "E07_long_ctx"
    assert m.sharpe_30d == 3.5


# ---------------------------------------------------------------------------
# Task 2: 7-gate promotion logic
# ---------------------------------------------------------------------------


def _metrics(**overrides) -> ModelMetrics:
    base = dict(
        name="m",
        pnl_30d=100.0,
        sharpe_30d=3.5,
        sharpe_7d_live=1.2,
        win_rate_30d=0.60,
        mase_30d=1.5,
        n_dates_30d=30,
        n_trades_30d=200,
        hyperparam_family="x",
    )
    base.update(overrides)
    return ModelMetrics(**base)


def test_research_only_flag():
    """Gate Decision Architecture: 7-gate is layer D, not live L2."""
    assert RESEARCH_ONLY is True


def test_gate_passes_all_criteria():
    result = check_promotion_gates(_metrics())
    assert result.passed is True
    assert result.failed_gates == ()
    assert check_research_scorecard(_metrics()).passed is True


def test_gate_fails_on_negative_pnl():
    r = check_promotion_gates(_metrics(pnl_30d=-1.0))
    assert r.passed is False
    assert "pnl_30d" in r.failed_gates


def test_gate_fails_on_low_sharpe_30d():
    r = check_promotion_gates(_metrics(sharpe_30d=0.9))
    assert "sharpe_30d" in r.failed_gates


def test_gate_fails_on_low_sharpe_7d():
    r = check_promotion_gates(_metrics(sharpe_7d_live=0.4))
    assert "sharpe_7d_live" in r.failed_gates


def test_gate_fails_on_low_win_rate():
    r = check_promotion_gates(_metrics(win_rate_30d=0.50))
    assert "win_rate_30d" in r.failed_gates


def test_gate_fails_on_high_mase():
    r = check_promotion_gates(_metrics(mase_30d=2.5))
    assert "mase_30d" in r.failed_gates


def test_gate_fails_on_insufficient_n_dates():
    r = check_promotion_gates(_metrics(n_dates_30d=20))
    assert "n_dates_30d" in r.failed_gates


def test_gate_fails_on_insufficient_n_trades():
    r = check_promotion_gates(_metrics(n_trades_30d=100))
    assert "n_trades_30d" in r.failed_gates


def test_gate_result_bool_returns_passed():
    assert bool(GateResult(passed=True, failed_gates=())) is True
    assert bool(GateResult(passed=False, failed_gates=("pnl_30d",))) is False


# ---------------------------------------------------------------------------
# Task 3: select_champion ranks eligible models by Sharpe
# ---------------------------------------------------------------------------


def test_select_champion_picks_only_passing_model():
    a = _metrics(name="A", sharpe_30d=3.5)
    b = _metrics(name="B", sharpe_30d=2.0)
    result = select_champion([a, b])
    assert result.champion == "A"
    assert result.runner_up == "B"
    assert result.eligible == ("A", "B")  # ordered desc by Sharpe


def test_select_champion_with_single_eligible_has_no_runner_up():
    a = _metrics(name="A", sharpe_30d=3.5)
    bad = _metrics(name="B", pnl_30d=-1.0)  # fails the pnl_30d gate
    result = select_champion([a, bad])
    assert result.champion == "A"
    assert result.eligible == ("A",)
    assert result.runner_up is None


def test_select_champion_with_no_passing_models_raises():
    bad = _metrics(name="X", sharpe_30d=0.5)
    with pytest.raises(ValueError, match="No model passed all 7 gates"):
        select_champion([bad])


def test_select_champion_ranks_eligible_by_sharpe():
    a = _metrics(name="A", sharpe_30d=3.5)
    b = _metrics(name="B", sharpe_30d=2.5)
    c = _metrics(name="C", sharpe_30d=1.8)
    result = select_champion([a, b, c])
    assert result.champion == "A"
    assert result.eligible == ("A", "B", "C")  # sorted desc by Sharpe (tuple)


# ---------------------------------------------------------------------------
# Task 4: select_challenger with family tiebreaker
# ---------------------------------------------------------------------------


def test_select_challenger_picks_second_highest_sharpe():
    a = _metrics(name="A", sharpe_30d=3.5, hyperparam_family="long_ctx")
    b = _metrics(name="B", sharpe_30d=2.5, hyperparam_family="zeroshot")
    c = _metrics(name="C", sharpe_30d=1.8, hyperparam_family="long_ctx")
    result = select_challenger([a, b, c], champion="A")
    assert result == "B"  # second-highest Sharpe


def test_select_challenger_prefers_low_correlation_on_tie():
    # A and B tie at 2.5; Z has higher Sharpe and is the champion.
    # Champion Z is zeroshot family, so prefer A (long_ctx, different).
    a = _metrics(name="A", sharpe_30d=2.5, hyperparam_family="long_ctx")
    b = _metrics(name="B", sharpe_30d=2.5, hyperparam_family="zeroshot")
    z = _metrics(name="Z", sharpe_30d=3.5, hyperparam_family="zeroshot")
    result = select_challenger([a, b, z], champion="Z")
    assert result == "A"  # different family from Z


def test_select_challenger_skips_champion():
    a = _metrics(name="A", sharpe_30d=3.5)
    b = _metrics(name="B", sharpe_30d=2.5)
    result = select_challenger([a, b], champion="A")
    assert result == "B"


def test_select_challenger_raises_if_no_alternatives():
    a = _metrics(name="A", sharpe_30d=3.5)
    with pytest.raises(ValueError, match="No challenger candidates"):
        select_challenger([a], champion="A")
