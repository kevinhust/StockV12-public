"""
V12 risk and pipeline limits — single loader for runtime enforcement.
Spec: v12/docs/RISK_MANAGEMENT.md
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

from stockapp_v12.core.paths import REPO_CONFIG_DIR

DEFAULT_RISK_CONFIG = REPO_CONFIG_DIR / "risk_regimes.yaml"


@dataclass(frozen=True)
class V12RiskLimits:
    max_daily_trades: int = 3
    max_single_position_pct: float = 0.03
    max_portfolio_exposure_pct: float = 0.40
    daily_loss_limit_pct: float = 2.0
    max_drawdown_pct: float = 8.0
    symbol_cooldown_hours: int = 48
    min_hold_days: int = 1
    risk_per_trade_pct: float = 0.01
    min_entry_confidence: float = 0.70
    confidence_cap_discount_enabled: bool = False
    sl_atr_multiple_default: float = 1.5
    regime_sizing: Optional[Dict[str, float]] = None
    # Trailing stop parameters (from RISK_MANAGEMENT.md)
    trailing_activation_atr: float = 1.0
    trailing_distance_atr: float = 2.0

    def __post_init__(self) -> None:
        if self.regime_sizing is None:
            object.__setattr__(
                self,
                "regime_sizing",
                {
                    "BULL_LONG": 1.0,
                    "BULL_SHORT": 0.9,
                    "NEUTRAL": 0.8,
                    "BEAR_SHORT": 0.6,
                    "BEAR_LONG": 0.0,
                    "PANIC": 0.0,
                },
            )


@dataclass
class StageFallbacks:
    """Pipeline-level fallback values used when upstream data is missing.

    Lives in core/limits.py (not stages.py) per P2-spec (council 2026-07-09)
    so that operators can tune them via config rather than editing stage
    code, and so they can be unit-tested in isolation.
    """
    #: Confidence assigned when analyst output is None or errors out.
    analyst_confidence: float = 0.75
    #: ATR as fraction of current price when forecast has no ATR data.
    atr_pct: float = 0.05


def load_v12_risk_limits(config_path: Optional[Path] = None) -> V12RiskLimits:
    """Load V12 limits from repo config/risk_regimes.yaml (global_limits + v12_sizing)."""
    path = config_path or DEFAULT_RISK_CONFIG
    defaults = V12RiskLimits()

    if not path.exists():
        logger.warning(
            "[limits] risk config not found at %s; using built-in defaults", path,
        )
        return defaults

    try:
        with open(path) as f:
            raw: Dict[str, Any] = yaml.safe_load(f) or {}
    except Exception as e:
        logger.exception(
            "[limits] risk config YAML parse failed at %s; using defaults "
            "(regime multipliers / daily caps may differ from operator intent)", path,
        )
        # NB: we still return defaults rather than raise because callers
        # load limits at module import time; a startup crash would block
        # the whole pipeline. The WARNING above is the operator signal.
        return defaults

    gl = raw.get("global_limits") or {}
    sizing = raw.get("v12_sizing") or {}

    regime_sizing = dict(defaults.regime_sizing or {})
    if isinstance(sizing.get("regime_multipliers"), dict):
        for k, v in sizing["regime_multipliers"].items():
            regime_sizing[str(k).upper()] = float(v)

    daily_loss = gl.get("daily_loss_limit_pct", defaults.daily_loss_limit_pct)
    if isinstance(daily_loss, float) and daily_loss < 1:
        daily_loss = daily_loss * 100

    return V12RiskLimits(
        max_daily_trades=int(gl.get("max_daily_trades", defaults.max_daily_trades)),
        max_single_position_pct=float(
            gl.get("max_single_position_pct", defaults.max_single_position_pct)
        ),
        max_portfolio_exposure_pct=float(
            gl.get("max_portfolio_exposure_pct", defaults.max_portfolio_exposure_pct)
        ),
        daily_loss_limit_pct=float(daily_loss),
        max_drawdown_pct=float(gl.get("max_drawdown_pct", defaults.max_drawdown_pct)),
        symbol_cooldown_hours=int(
            gl.get("symbol_cooldown_hours", defaults.symbol_cooldown_hours)
        ),
        min_hold_days=int(gl.get("min_hold_days", defaults.min_hold_days)),
        risk_per_trade_pct=float(gl.get("risk_per_trade_pct", defaults.risk_per_trade_pct)),
        min_entry_confidence=float(
            gl.get("min_entry_confidence", defaults.min_entry_confidence)
        ),
        confidence_cap_discount_enabled=bool(
            sizing.get(
                "confidence_cap_discount_enabled",
                defaults.confidence_cap_discount_enabled,
            )
        ),
        sl_atr_multiple_default=float(
            sizing.get("sl_atr_multiple_default", defaults.sl_atr_multiple_default)
        ),
        regime_sizing=regime_sizing,
        trailing_activation_atr=float(
            gl.get("trailing_activation_atr", sizing.get("trailing_activation_atr", defaults.trailing_activation_atr))
        ),
        trailing_distance_atr=float(
            gl.get("trailing_distance_atr", sizing.get("trailing_distance_atr", defaults.trailing_distance_atr))
        ),
    )


def _env_truthy(env_var: str, default: str = "0") -> bool:
    """Unified truthy parser for environment variables.

    Accepts (case-insensitive): 1, true, yes, on.
    Everything else (including unset) → False.
    """
    return os.getenv(env_var, default).strip().lower() in ("1", "true", "yes", "on")


def v12_enabled() -> bool:
    return _env_truthy("STOCKAPP_V12_RISK", default="1")
