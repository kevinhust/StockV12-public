"""Champion role persistence — halt_swap.json + champion_config.json.

Spec: docs/superpowers/specs/2026-07-10-champion-model-selection-design.md
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class HaltSwap:
    """Record of a champion↔challenger swap triggered by HaltGate."""
    active_champion: str
    halted_champion: str
    swapped_at: str
    reason: str
    halt_3day_accuracy: float


@dataclass(frozen=True)
class ChampionConfig:
    """Current champion + challenger assignment with promotion metadata."""
    champion: str
    challenger: str
    promoted_at: str
    promotion_rationale: str
    next_review_date: str


def write_halt_swap(path: Path, swap: HaltSwap) -> None:
    """Atomic write of halt_swap.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(swap), indent=2))
    tmp.replace(path)


def read_halt_swap(path: Path) -> HaltSwap | None:
    """Read halt_swap.json. Returns None if file doesn't exist."""
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return HaltSwap(**data)


def write_champion_config(path: Path, cfg: ChampionConfig) -> None:
    """Atomic write of champion_config.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2))
    tmp.replace(path)


def read_champion_config(path: Path) -> ChampionConfig:
    """Read champion_config.json. Raises FileNotFoundError if missing."""
    if not path.exists():
        raise FileNotFoundError(f"champion_config not found at {path}")
    data = json.loads(path.read_text())
    return ChampionConfig(**data)
