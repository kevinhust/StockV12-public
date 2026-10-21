"""
Chronos forecast contract — prediction only, no trade actions.
Spec: v12/docs + Obsidian TSFM-Pipeline (ForecastOutput).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FORBIDDEN_ACTION_KEYS = frozenset({"action", "side", "signal", "order_side"})


class ForecastOutput(BaseModel):
    """Structured TSFM output. Must never encode BUY/SELL."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    as_of: date
    horizon_days: int = Field(ge=1, le=30)
    current_price: float = Field(gt=0)
    forecast_mean: float = Field(gt=0)
    forecast_q10: float = Field(gt=0)
    forecast_q90: float = Field(gt=0)
    expected_return_pct: float
    model_id: str
    degraded: bool = False
    regime_hint: Optional[str] = None

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, v: str) -> str:
        s = v.upper().strip()
        if not s:
            raise ValueError("symbol required")
        return s

    @field_validator("regime_hint")
    @classmethod
    def _normalize_regime_hint(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.upper().strip()
        return s or None

    @model_validator(mode="after")
    def _quantile_order(self) -> "ForecastOutput":
        if self.forecast_q10 > self.forecast_q90:
            raise ValueError("forecast_q10 must be <= forecast_q90")
        return self

    @property
    def uncertainty_pct(self) -> float:
        if self.current_price <= 0:
            return 0.0
        return (self.forecast_q90 - self.forecast_q10) / self.current_price

    def to_journal_dict(self) -> Dict[str, Any]:
        return self.model_dump(mode="json")


def build_forecast_output(
    *,
    symbol: str,
    as_of: Optional[date] = None,
    horizon_days: int,
    current_price: float,
    forecast_mean: float,
    forecast_q10: Optional[float] = None,
    forecast_q90: Optional[float] = None,
    model_id: str,
    degraded: bool = False,
    regime_hint: Optional[str] = None,
) -> ForecastOutput:
    """Build ForecastOutput; synthesize quantiles from mean if missing."""
    spread = abs(forecast_mean - current_price) * 0.5
    q10 = forecast_q10 if forecast_q10 is not None else max(forecast_mean - spread, current_price * 0.5)
    q90 = forecast_q90 if forecast_q90 is not None else forecast_mean + spread
    expected_return_pct = (forecast_mean - current_price) / current_price

    return ForecastOutput(
        symbol=symbol,
        as_of=as_of or datetime.now(timezone.utc).date(),
        horizon_days=horizon_days,
        current_price=current_price,
        forecast_mean=forecast_mean,
        forecast_q10=q10,
        forecast_q90=q90,
        expected_return_pct=expected_return_pct,
        model_id=model_id,
        degraded=degraded,
        regime_hint=regime_hint,
    )


def assert_no_trade_action_fields(payload: Dict[str, Any]) -> None:
    """Guardrail: forecast payloads must not smuggle order directives."""
    for key in FORBIDDEN_ACTION_KEYS:
        if key in payload:
            raise ValueError(f"forbidden forecast field: {key}")