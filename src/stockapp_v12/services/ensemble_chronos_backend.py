"""
EnsembleChronosBackend — N-model averaging ChronosBackend for ForecastService.

Background (2026-07-01 council, Round 3 closure):
  Variant A and Variant B are both fine-tunes of the same frozen
  chronos-bolt-small backbone. They diverge only in the fine-tune recipe
  (ctx=512 vs 1024, LR 1e-5 vs 1e-4, etc.), so an arithmetic mean of
  forecast_price should reduce per-symbol noise without introducing a
  different point estimate.

Fallback contract (validated against v12/scripts/validate_ensemble_gate.py):
  When per-model |forecast_ret_pct| spread exceeds `fallback_spread_pct`
  (default 3.0%), the ensemble falls back to the FIRST model's forecast
  rather than averaging. Rationale: a >3% spread between Variants A and
  B on the same input means one model has gone rogue; we should trust
  Variant A (the higher-MASE, more-conservative seed we promoted in
  Round 2) rather than the average of two contradictory signals.

Why this class lives next to forecast_service.py:
  ForecastService already supports a backend= protocol injection
  (ChronosBackend Protocol in forecast_service.py:30). This class
  implements that protocol. The wiring happens in lifecycle_scheduler.py
  when CHRONOS_ENSEMBLE_MODEL_PATHS env var is set (colon-separated
  list of model directory paths).

Hardware: GPU-only (AutoGluon Chronos on RTX 3060). On Mac, this module
imports but `detect()` requires torch+CUDA at predictor.predict() time.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from stockapp_v12.eval.quantiles import extract_quantile_band

logger = logging.getLogger(__name__)

DEFAULT_FALLBACK_SPREAD_PCT = 3.0
ENSEMBLE_ENV_VAR = "CHRONOS_ENSEMBLE_MODEL_PATHS"
FALLBACK_SPREAD_ENV_VAR = "CHRONOS_ENSEMBLE_FALLBACK_SPREAD_PCT"
ENSEMBLE_MODEL_ID_DEFAULT = "chronos-ensemble"


class EnsembleChronosBackend:
    """Average forecast_price across N Chronos checkpoints.

    Implements the ChronosBackend Protocol (detect + detect_batch) used by
    ForecastService.

    For each symbol:
      1. Run each predictor's predict() on the same input df.
      2. Compute per-model forecast_price (mean over first 10 horizons).
      3. Compute per-model forecast_ret_pct and the spread = max - min.
      4. If spread > fallback_spread_pct: use FIRST model's forecast.
         Otherwise: use the arithmetic mean of all forecasts.
      5. Always emit a single signal (CHRONOS_LONG if avg > 0, SHORT if
         avg < 0). ForecastService's downstream SR-01 short-circuits at
         expected_return_pct < 0.5%, so emitting below-threshold forecasts
         is safe — they get filtered at SR-01.
    """

    def __init__(
        self,
        model_paths: List[str],
        *,
        prediction_length: int = 10,
        fallback_spread_pct: float = DEFAULT_FALLBACK_SPREAD_PCT,
        ensemble_model_id: str = ENSEMBLE_MODEL_ID_DEFAULT,
    ):
        if not model_paths:
            raise ValueError("EnsembleChronosBackend requires at least one model path")
        self.model_paths = list(model_paths)
        self.prediction_length = int(prediction_length)
        self.fallback_spread_pct = float(fallback_spread_pct)
        self.ensemble_model_id = ensemble_model_id
        self._predictors: Optional[list] = None  # lazy
        self._model_labels: Optional[list] = None  # parallel list of short labels

    @property
    def n_models(self) -> int:
        return len(self.model_paths)

    @property
    def model_labels(self) -> list[str]:
        """Short labels per model (basename of the path) for logging/audit."""
        if self._model_labels is None:
            self._model_labels = [
                os.path.basename(os.path.normpath(p)) or p for p in self.model_paths
            ]
        return self._model_labels

    def _ensure_loaded(self) -> list:
        """Lazy-load all predictors on first call. Idempotent."""
        if self._predictors is not None:
            return self._predictors
        try:
            from autogluon.timeseries import TimeSeriesPredictor
        except ImportError as e:
            raise RuntimeError(
                f"EnsembleChronosBackend: AutoGluon unavailable ({e}); "
                f"cannot load models. Ensure GPU env is active."
            ) from e
        predictors = []
        for path in self.model_paths:
            try:
                predictors.append(TimeSeriesPredictor.load(path))
            except Exception as e:
                logger.error(
                    f"[ensemble_backend] failed to load predictor from {path}: {e}"
                )
                raise
        self._predictors = predictors
        logger.info(
            f"[ensemble_backend] loaded {len(predictors)} models: {self.model_labels}"
        )
        return predictors

    @staticmethod
    def _prep_tsdf(df: pd.DataFrame) -> Any:
        """Build a TimeSeriesDataFrame from a single-symbol bars frame.

        Mirrors the prep logic in `src/services/chronos_service.py:detect()`
        and `v12/scripts/eval_chronos_h2h_v3.py:evaluate_one_model()` so
        the input contract is bit-exact with what each predictor was
        trained on. AutoGluon requires tz-naive timestamps; we strip tz
        to UTC-naive.
        """
        from autogluon.timeseries import TimeSeriesDataFrame

        if df is None or df.empty:
            raise ValueError("empty df")
        ag_df = df.copy()
        # AutoGluon TimeSeriesDataFrame wants an item_id column.
        if "item_id" not in ag_df.columns:
            # Single-symbol frame — fabricate a constant item_id
            # (forecast_service.py callers pass per-symbol dfs, so this
            # path is the only one we need to handle here).
            ag_df["item_id"] = "TICKER"
        # Resolve timestamp column name
        ts_col = None
        for cand in ("timestamp", "time"):
            if cand in ag_df.columns:
                ts_col = cand
                break
        if ts_col is None:
            raise ValueError("df must contain 'timestamp' or 'time' column")
        ag_df[ts_col] = pd.to_datetime(ag_df[ts_col])
        # AutoGluon rejects tz-aware timestamps
        try:
            if hasattr(ag_df[ts_col].dt, "tz") and ag_df[ts_col].dt.tz is not None:
                ag_df[ts_col] = ag_df[ts_col].dt.tz_convert(None)
        except (AttributeError, TypeError):
            pass
        return TimeSeriesDataFrame.from_data_frame(
            ag_df, id_column="item_id", timestamp_column=ts_col
        )

    def _predict_one_predictor(
        self, predictor, df: pd.DataFrame
    ) -> Optional[Dict[str, float]]:
        """Run one predictor on one df; return per-model forecast dict.

        Returns None if predictor fails (so callers can decide whether to
        fall back to single-model or skip the symbol entirely).
        """
        tsdf = self._prep_tsdf(df)
        try:
            pred = predictor.predict(tsdf)
        except Exception as e:
            logger.warning(
                f"[ensemble_backend] predictor failed: {type(e).__name__}: {e}"
            )
            return None
        try:
            forecast_mean, forecast_q10, forecast_q90 = extract_quantile_band(
                pred, self.prediction_length
            )
        except Exception as e:
            logger.warning(
                f"[ensemble_backend] extract_quantile_band failed: "
                f"{type(e).__name__}: {e}"
            )
            forecast_mean = float(pred.iloc[: self.prediction_length].mean().mean())
            forecast_q10 = forecast_mean * 0.95
            forecast_q90 = forecast_mean * 1.05
        return {
            "forecast_mean": forecast_mean,
            "forecast_q10": forecast_q10,
            "forecast_q90": forecast_q90,
        }

    def _resolve_current_price(self, df: pd.DataFrame) -> float:
        """Get the current price from the last bar. Tries 'close' first."""
        if df is None or df.empty:
            raise ValueError("empty df — cannot resolve current price")
        if "close" in df.columns:
            return float(df["close"].iloc[-1])
        # Fallback: any price-like column
        for cand in ("adj_close", "price", "last"):
            if cand in df.columns:
                return float(df[cand].iloc[-1])
        raise ValueError(f"no price column found in df (cols={list(df.columns)})")

    def _build_signal(
        self,
        df: pd.DataFrame,
        per_model: List[Dict[str, Any]],
        current_price: float,
    ) -> List[Dict[str, Any]]:
        """Aggregate per-model dicts into one Chronos-shaped signal list.

        per_model entries:
          - {"label": str, "forecast_mean": float, "forecast_q10": float,
             "forecast_q90": float, "degraded_reason": Optional[str]}
          - A degraded_reason indicates this model failed; the forecast
            numbers are still present (NaN) so we can compute the spread
            without it.
        """
        if not per_model:
            return []
        valid = [
            m for m in per_model if m.get("forecast_mean") is not None
            and not np.isnan(m["forecast_mean"])
        ]
        if not valid:
            logger.warning(
                f"[ensemble_backend] all {len(per_model)} models returned invalid forecasts"
            )
            return []

        # Compute per-model ret_pct (used for spread decision)
        for m in valid:
            if current_price > 0:
                m["forecast_ret_pct"] = (
                    (m["forecast_mean"] - current_price) / current_price * 100.0
                )
            else:
                m["forecast_ret_pct"] = 0.0

        ret_pcts = [m["forecast_ret_pct"] for m in valid]
        spread = float(max(ret_pcts) - min(ret_pcts))

        # Decide fallback
        used_fallback = spread > self.fallback_spread_pct
        if used_fallback:
            chosen = valid[0]
            chosen_label = valid[0]["label"]
            logger.info(
                f"[ensemble_backend] spread={spread:.3f}% > "
                f"fallback_spread_pct={self.fallback_spread_pct:.3f}%; "
                f"falling back to single-model {chosen_label}"
            )
        else:
            # Average forecast_mean / q10 / q90
            chosen = {
                "label": "ensemble_avg",
                "forecast_mean": float(np.mean([m["forecast_mean"] for m in valid])),
                "forecast_q10": float(np.mean([m["forecast_q10"] for m in valid])),
                "forecast_q90": float(np.mean([m["forecast_q90"] for m in valid])),
            }
            chosen_label = "ensemble_avg"

        forecast_price = chosen["forecast_mean"]
        forecast_q10 = chosen["forecast_q10"]
        forecast_q90 = chosen["forecast_q90"]
        if current_price > 0:
            pct_change = (forecast_price - current_price) / current_price
            forecast_ret_pct_pct = pct_change * 100.0
        else:
            pct_change = 0.0
            forecast_ret_pct_pct = 0.0

        # Chronos-shaped signal — same as ChronosService.detect() output.
        # We always emit (even below-threshold) so downstream SR-01 + SR-07
        # see the actual forecast magnitude.
        sig_type = "CHRONOS_LONG" if pct_change >= 0 else "CHRONOS_SHORT"
        return [{
            "type": sig_type,
            "confidence": min(abs(pct_change) * 100, 1.0),
            "bbox": [0, 0, 0, 0],
            "meta": {
                "current_price": current_price,
                "forecast_price": float(forecast_price),
                "forecast_q10": float(forecast_q10),
                "forecast_q90": float(forecast_q90),
                "pct_change": pct_change,
                "horizon": self.prediction_length,
                # Ensemble audit fields (not used by ChronosService but
                # surfaced in ForecastOutput via ForecastService if it
                # reads meta; currently they're recorded in journal logs
                # via the orchestrator's reason_detail flow).
                "_ensemble_meta": {
                    "n_models": len(valid),
                    "model_labels": [m["label"] for m in valid],
                    "per_model_forecast_ret_pct": {
                        m["label"]: round(m["forecast_ret_pct"], 3) for m in valid
                    },
                    "per_model_spread_pct": round(spread, 3),
                    "fallback_used": used_fallback,
                    "fallback_spread_threshold_pct": self.fallback_spread_pct,
                    "ensemble_model_id": self.ensemble_model_id,
                },
            },
        }]

    def detect(self, df: pd.DataFrame) -> List[Dict[str, Any]]:
        """ChronosBackend protocol: one-symbol batch inference."""
        predictors = self._ensure_loaded()
        current_price = self._resolve_current_price(df)
        per_model: List[Dict[str, Any]] = []
        for label, pred in zip(self.model_labels, predictors):
            res = self._predict_one_predictor(pred, df)
            if res is None:
                per_model.append({
                    "label": label,
                    "forecast_mean": float("nan"),
                    "forecast_q10": float("nan"),
                    "forecast_q90": float("nan"),
                    "degraded_reason": "predict_failed",
                })
            else:
                res["label"] = label
                per_model.append(res)
        return self._build_signal(df, per_model, current_price)

    def detect_batch(self, frames: Dict[str, pd.DataFrame]) -> Dict[str, List[Dict[str, Any]]]:
        """ChronosBackend protocol: multi-symbol batch inference.

        Runs each predictor sequentially over each symbol. Total GPU work
        = N_models × N_symbols (no batched multi-symbol predict for now —
        AutoGluon's predictor.predict is single-pass; we follow the same
        sequential pattern as ChronosService.detect_batch).
        """
        if not frames:
            return {}
        out: Dict[str, List[Dict[str, Any]]] = {}
        for sym, df in frames.items():
            try:
                out[sym] = self.detect(df)
            except Exception as e:
                logger.warning(
                    f"[ensemble_backend] {sym} detect() failed: {type(e).__name__}: {e}; "
                    f"returning empty signal list (forecast_service will degrade)"
                )
                out[sym] = []
        return out


def make_ensemble_backend_from_env() -> Optional[EnsembleChronosBackend]:
    """Factory: build an EnsembleChronosBackend from env vars, or return None.

    Env contract:
      CHRONOS_ENSEMBLE_MODEL_PATHS — colon-separated list of model dirs.
        If unset/empty: returns None (callers fall back to single-model
        ChronosService).
      CHRONOS_ENSEMBLE_FALLBACK_SPREAD_PCT — optional, default 3.0.

    Returns None (not raise) when the env var is unset so callers can
    cleanly branch:
        backend = make_ensemble_backend_from_env() or ChronosService(...)
    """
    paths_str = os.getenv(ENSEMBLE_ENV_VAR, "").strip()
    if not paths_str:
        return None
    paths = [p.strip() for p in paths_str.split(":") if p.strip()]
    if not paths:
        return None
    spread_env = os.getenv(FALLBACK_SPREAD_ENV_VAR, "").strip()
    try:
        spread = float(spread_env) if spread_env else DEFAULT_FALLBACK_SPREAD_PCT
    except (TypeError, ValueError):
        spread = DEFAULT_FALLBACK_SPREAD_PCT
    logger.info(
        f"[ensemble_backend] env-mode: {len(paths)} models, "
        f"fallback_spread_pct={spread}"
    )
    return EnsembleChronosBackend(
        model_paths=paths,
        fallback_spread_pct=spread,
    )