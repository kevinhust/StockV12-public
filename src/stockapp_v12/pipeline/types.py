"""
pipeline.types — shared dataclasses for the entry pipeline.

P2-boundaries (council 2026-07-09): EntryContext and EntryResult were
originally defined in ``services/entry_orchestrator.py`` and
re-imported by ``pipeline.stages``, creating a circular import that
required a local (deferred) import in entry_orchestrator. Moving them
here breaks the cycle cleanly.

Phase 2 (2026-07-26): SR/YOLO DTOs live in ``schemas`` so this module has
**zero** dependency on ``services`` (runtime or TYPE_CHECKING).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Set

import pandas as pd

from stockapp_v12.pipeline.analyst import AnalystOutput
from stockapp_v12.schemas.forecast import ForecastOutput
from stockapp_v12.schemas.signal import SignalRuleResult
from stockapp_v12.schemas.yolo import YoloFilterDecision


@dataclass
class EntryContext:
    """Inputs needed to evaluate one symbol for entry."""
    symbol: str
    bars: pd.DataFrame  # daily OHLCV ending today, must have 'close', optionally 'atr_14' or 'atr_pct'
    market_regime: str
    actionable_symbols: Set[str]
    equity: float  # current NAV for sizing
    mode: str = "paper_ibkr"


@dataclass
class EntryResult:
    decision: str  # "BUY" | "PASS" | "DENY"
    reason_code: str
    size_pct: Optional[float] = None
    qty: Optional[int] = None
    forecast: Optional[ForecastOutput] = None
    sr_result: Optional[SignalRuleResult] = None
    analyst: Optional[AnalystOutput] = None
    parent_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    # Wave 9: YOLO no-trade filter verdict (None if filter not enabled).
    # Surfaces in EntryResult so backtests can audit per-symbol filter hits.
    yolo_decision: Optional[YoloFilterDecision] = None


__all__ = ["EntryContext", "EntryResult"]
