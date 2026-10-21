"""Tests for champion role persistence (champion_state module).

Covers halt_swap.json (T8 writes, T12 reads) and champion_config.json
(T11 writes, deploy artifacts read). Atomic tmp+rename writes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from stockapp_v12.eval.champion_state import (
    ChampionConfig,
    HaltSwap,
    read_champion_config,
    read_halt_swap,
    write_champion_config,
    write_halt_swap,
)


def test_halt_swap_roundtrip(tmp_path: Path):
    path = tmp_path / "halt_swap.json"
    write_halt_swap(
        path,
        HaltSwap(
            active_champion="zeroshot_bolt_small",
            halted_champion="E07_long_ctx",
            swapped_at="2026-07-15T16:20:00-04:00",
            reason="halt_triggered",
            halt_3day_accuracy=0.38,
        ),
    )
    result = read_halt_swap(path)
    assert result is not None
    assert result.active_champion == "zeroshot_bolt_small"
    assert result.halted_champion == "E07_long_ctx"


def test_halt_swap_returns_none_when_missing(tmp_path: Path):
    assert read_halt_swap(tmp_path / "nope.json") is None


def test_champion_config_roundtrip(tmp_path: Path):
    path = tmp_path / "champion_config.json"
    write_champion_config(
        path,
        ChampionConfig(
            champion="E07_long_ctx",
            challenger="zeroshot_bolt_small",
            promoted_at="2026-07-10",
            promotion_rationale="Sharpe=3.5",
            next_review_date="2026-09-25",
        ),
    )
    cfg = read_champion_config(path)
    assert cfg.champion == "E07_long_ctx"
    assert cfg.challenger == "zeroshot_bolt_small"
    assert cfg.next_review_date == "2026-09-25"


def test_champion_config_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        read_champion_config(tmp_path / "nope.json")
