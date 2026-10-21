"""
ForecastService — Chronos wrapper producing ForecastOutput only (no LONG/SHORT).
Replaces direct use of ChronosService.detect() in the V12 entry path.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Callable, Dict, Optional

import pandas as pd

from stockapp_v12.contracts.chronos_backend import ChronosBackend  # re-export shim (commit 1)
from stockapp_v12.schemas.forecast import ForecastOutput, build_forecast_output

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "chronos-2-zero"


class ForecastService:
    """
    Daily-bar forecast adapter. Does not emit trade signals.

    LLM is not part of this service — see TRADING_PIPELINE Stage 4 (optional narrative only).
    """

    def __init__(
        self,
        *,
        backend: Optional[ChronosBackend] = None,
        model_id: str = DEFAULT_MODEL_ID,
        horizon_days: int = 10,
        regime_hint_fn: Optional[Callable[[str], Optional[str]]] = None,
    ):
        self._backend = backend
        self.model_id = model_id
        self.horizon_days = horizon_days
        self._regime_hint_fn = regime_hint_fn
        self._resolved_backend: Optional[ChronosBackend] = None  # lazy + cached

    def _resolve_backend(self) -> Optional[ChronosBackend]:
        if self._backend is not None:
            return self._backend
        if self._resolved_backend is not None:
            return self._resolved_backend
        # Archive Chronos is caged behind LegacyChronosAdapter (Task 6 /
        # ADR-011). ForecastService must not call ensure_v11 or import
        # src.services.chronos_service directly.
        from stockapp_v12.adapters.legacy_v11.chronos import (
            build_legacy_chronos_backend,
        )

        self._resolved_backend = build_legacy_chronos_backend(
            prediction_length=self.horizon_days,
        )
        return self._resolved_backend

    def forecast_daily(
        self,
        symbol: str,
        df: pd.DataFrame,
        *,
        as_of: Optional[date] = None,
        model_id: Optional[str] = None,
    ) -> Optional[ForecastOutput]:
        """
        Run Chronos on a daily OHLCV frame; return ForecastOutput or None on hard failure.
        """
        backend = self._resolve_backend()
        if backend is None:
            logger.warning(
                f"[forecast_service] {symbol} backend=None (ChronosService failed to load or no CHRONOS_MODEL_PATH); "
                f"returning degraded forecast (forecast_mean=current_price → SR-01 short-circuit)"
            )
            return self._degraded_output(symbol, df, as_of=as_of, model_id=model_id)
        if df is None or df.empty:
            logger.warning(
                f"[forecast_service] {symbol} empty/None bars frame; returning degraded forecast"
            )
            return self._degraded_output(symbol, df, as_of=as_of, model_id=model_id)

        signals = backend.detect(df)
        if not signals:
            logger.warning(
                f"[forecast_service] {symbol} backend.detect() returned no signals; "
                f"returning degraded forecast"
            )
            return self._degraded_output(symbol, df, as_of=as_of, model_id=model_id)

        meta = signals[0].get("meta") or {}
        current = float(meta.get("current_price") or df.iloc[-1].get("close") or 0)
        future = float(meta.get("forecast_price") or current)
        if current <= 0:
            return None

        q10 = meta.get("forecast_q10")
        q90 = meta.get("forecast_q90")
        hint = self._regime_hint_fn(symbol) if self._regime_hint_fn else None

        return build_forecast_output(
            symbol=symbol,
            as_of=as_of,
            horizon_days=int(meta.get("horizon") or self.horizon_days),
            current_price=current,
            forecast_mean=future,
            forecast_q10=float(q10) if q10 is not None else None,
            forecast_q90=float(q90) if q90 is not None else None,
            model_id=model_id or self.model_id,
            degraded=False,
            regime_hint=hint,
        )

    def forecast_daily_batch(
        self,
        frames: Dict[str, pd.DataFrame],
        *,
        as_of: Optional[date] = None,
        model_id: Optional[str] = None,
    ) -> Dict[str, Optional["ForecastOutput"]]:
        """
        Batch inference: one predict() call for all symbols. Falls back to
        per-symbol degraded output when backend is unavailable.

        Returns: {symbol: ForecastOutput | None}
        """
        out: Dict[str, Optional[ForecastOutput]] = {}
        if not frames:
            return out

        backend = self._resolve_backend()
        if backend is None or not hasattr(backend, "detect_batch"):
            reason = "backend=None" if backend is None else "backend has no detect_batch"
            logger.warning(
                f"[forecast_service] batch fallback to degraded for {len(frames)} symbols "
                f"(reason={reason}); each will SR-01 short-circuit at expected_return=0"
            )
            # Fallback to degraded for every symbol
            for sym, df in frames.items():
                out[sym] = self._degraded_output(sym, df, as_of=as_of, model_id=model_id)
            return out

        try:
            raw = backend.detect_batch(frames)
        except Exception as e:
            logger.warning(
                f"[forecast_service] batch detect_batch raised {type(e).__name__}: {e}; "
                f"falling back to per-symbol degraded for {len(frames)} symbols"
            )
            for sym, df in frames.items():
                out[sym] = self._degraded_output(sym, df, as_of=as_of, model_id=model_id)
            return out

        for sym, df in frames.items():
            signals = raw.get(sym) or []
            if not signals:
                logger.warning(
                    f"[forecast_service] {sym} detect_batch returned no signals; "
                    f"per-symbol degraded fallback"
                )
                out[sym] = self._degraded_output(sym, df, as_of=as_of, model_id=model_id)
                continue

            meta = signals[0].get("meta") or {}
            current = float(meta.get("current_price") or df.iloc[-1].get("close") or 0)
            future = float(meta.get("forecast_price") or current)
            if current <= 0:
                out[sym] = None
                continue

            q10 = meta.get("forecast_q10")
            q90 = meta.get("forecast_q90")
            hint = self._regime_hint_fn(sym) if self._regime_hint_fn else None
            out[sym] = build_forecast_output(
                symbol=sym,
                as_of=as_of,
                horizon_days=int(meta.get("horizon") or self.horizon_days),
                current_price=current,
                forecast_mean=future,
                forecast_q10=float(q10) if q10 is not None else None,
                forecast_q90=float(q90) if q90 is not None else None,
                model_id=model_id or self.model_id,
                degraded=False,
                regime_hint=hint,
            )
        return out

    def _degraded_output(
        self,
        symbol: str,
        df: Optional[pd.DataFrame],
        *,
        as_of: Optional[date],
        model_id: Optional[str],
    ) -> Optional[ForecastOutput]:
        if df is None or df.empty or "close" not in df.columns:
            logger.warning(
                f"[forecast_service] {symbol} cannot build degraded forecast: "
                f"df={'None' if df is None else 'empty' if df.empty else 'missing close col'}"
            )
            return None
        price = float(df.iloc[-1]["close"])
        if price <= 0:
            logger.warning(
                f"[forecast_service] {symbol} cannot build degraded forecast: "
                f"non-positive last close ({price})"
            )
            return None
        logger.warning(
            f"[forecast_service] {symbol} returning DEGRADED forecast "
            f"(forecast_mean=current_price={price:.2f}, ±2% bands); "
            f"downstream SR-01 will short-circuit at expected_return=0"
        )
        return build_forecast_output(
            symbol=symbol,
            as_of=as_of or datetime.now(timezone.utc).date(),
            horizon_days=self.horizon_days,
            current_price=price,
            forecast_mean=price,
            forecast_q10=price * 0.98,
            forecast_q90=price * 1.02,
            model_id=model_id or self.model_id,
            degraded=True,
            regime_hint=self._regime_hint_fn(symbol) if self._regime_hint_fn else None,
        )