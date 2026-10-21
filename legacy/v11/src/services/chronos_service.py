import os
import pandas as pd
import numpy as np
from loguru import logger
from typing import List, Dict, Optional

try:
    from autogluon.timeseries import TimeSeriesPredictor, TimeSeriesDataFrame
except ImportError:
    logger.warning("AutoGluon not installed. ChronosService will fail.")
    TimeSeriesPredictor = None
    TimeSeriesDataFrame = None

class ChronosService:
    def __init__(self, model_path: str, context_length: int = 64, prediction_length: int = 16, threshold: Optional[float] = None):
        """
        Args:
            model_path: Path to the directory containing the AutoGluon model (predictor.pkl, etc)
            context_length: How many past steps to use (Chronos usually handles this, but we pass window)
            prediction_length: How far ahead to predict
            threshold: Percentage change required to trigger a signal (e.g. 0.01 = 1%).
                       If None, falls back to CHRONOS_FORECAST_THRESHOLD env var, then 0.01 default.
        """
        if threshold is None:
            try:
                threshold = float(os.getenv("CHRONOS_FORECAST_THRESHOLD", "0.01"))
            except (TypeError, ValueError):
                threshold = 0.01
        self.model_path = model_path
        self.prediction_length = prediction_length
        self.threshold = threshold
        self.predictor = None

        self._load_model()

    def _load_model(self):
        if not TimeSeriesPredictor:
            logger.error("AutoGluon dependency missing.")
            return

        try:
            if os.path.exists(self.model_path):
                # Load the predictor
                # Note: TimeSeriesPredictor.load expects the directory path
                self.predictor = TimeSeriesPredictor.load(self.model_path)
                logger.info(f"✅ Loaded Chronos Model from {self.model_path}")
                
                # Verify info or set strict params if needed
                # logger.info(self.predictor.info())
            else:
                logger.error(f"❌ Model path does not exist: {self.model_path}")
        except Exception as e:
            logger.error(f"❌ Failed to load Chronos model: {e}")

    def detect(self, df: pd.DataFrame) -> List[Dict]:
        """
        Generate signals based on Chronos forecast.
        Mimics PatternService.detect interface.
        
        Args:
            df: DataFrame with 'timestamp'/'time' and 'close' columns. 
                Should contain enough history for context.
        """
        if not self.predictor:
            return []

        # 1. Prepare Data for AutoGluon
        # TSDataFrame needs [item_id, timestamp, target]
        # We assume single item 'TICKER' for the inference window
        
        if 'time' in df.columns:
            timestamp_col = 'time'
        elif 'timestamp' in df.columns:
            timestamp_col = 'timestamp'
        else:
            logger.warning("No timestamp column found for Chronos inference")
            return []
            
        # Ensure we have data
        if df.empty:
            return []
            
        # Note: df.iloc[-1] is the LATEST bar in the DataFrame.
        # The time granularity (minute vs daily) depends on the caller's input.
        # For intraday signals, this is the most recent minute bar;
        # for daily, it's the last trading day's close.
        current_price = df.iloc[-1]['close']
        current_time = df.iloc[-1][timestamp_col]
        
        # Create a temp dataframe for AG
        # static_features might be needed if the model was trained with them
        # For pure Chronos (zero-shot or finetuned), usually just target is enough if simple
        
        ag_df = df.copy()
        ag_df['item_id'] = "TICKER"
        ag_df[timestamp_col] = pd.to_datetime(ag_df[timestamp_col])
        # AutoGluon TimeSeriesDataFrame rejects tz-aware timestamps; normalize to naive UTC.
        try:
            if hasattr(ag_df[timestamp_col].dt, "tz") and ag_df[timestamp_col].dt.tz is not None:
                ag_df[timestamp_col] = ag_df[timestamp_col].dt.tz_convert(None)
        except (AttributeError, TypeError):
            pass
        
        # Select relevant columns
        data = TimeSeriesDataFrame.from_data_frame(
            ag_df,
            id_column="item_id",
            timestamp_column=timestamp_col
        )
        
        try:
            # 2. Predict
            # predict() returns a DataFrame with mean, 0.1, 0.2 ... 0.9 quantiles usually
            # We want the next steps
            predictions = self.predictor.predict(data)

            # 3. Analyze Forecast
            # Get the forecast for 'TICKER'
            forecast = predictions.loc["TICKER"]

            # Look at the mean/median at the end of prediction horizon (or average over horizon?)
            # Let's assess the price at `prediction_length` steps ahead
            # 'mean' is usually the point forecast

            future_price = forecast.iloc[-1]['mean']
            future_q10 = forecast.iloc[-1].get('0.1') or (future_price * 0.95)
            future_q90 = forecast.iloc[-1].get('0.9') or (future_price * 1.05)

            # Simple Logic:
            # If future_price >> current_price => LONG
            # If future_price << current_price => SHORT

            pct_change = (future_price - current_price) / current_price

            signals = []

            if pct_change > self.threshold:
                signals.append({
                    "type": "CHRONOS_LONG",
                    "confidence": min(abs(pct_change) * 100, 1.0), # Scaling confidence loosely
                    "bbox": [0, 0, 0, 0], # Placeholder
                    "meta": {
                        "current_price": current_price,
                        "forecast_price": future_price,
                        "forecast_q10": float(future_q10),
                        "forecast_q90": float(future_q90),
                        "pct_change": pct_change,
                        "horizon": self.prediction_length
                    }
                })
            elif pct_change < -self.threshold:
                signals.append({
                    "type": "CHRONOS_SHORT",
                    "confidence": min(abs(pct_change) * 100, 1.0),
                    "bbox": [0, 0, 0, 0],
                    "meta": {
                        "current_price": current_price,
                        "forecast_price": future_price,
                        "forecast_q10": float(future_q10),
                        "forecast_q90": float(future_q90),
                        "pct_change": pct_change,
                        "horizon": self.prediction_length
                    }
                })

            return signals

        except Exception as e:
            logger.error(f"Chronos inference failed: {e}")
            return []

    def detect_batch(self, frames: Dict[str, pd.DataFrame]) -> Dict[str, List[Dict]]:
        """
        Batch inference: predict for multiple symbols in one predict() call.

        Args:
            frames: {symbol: df_with_timestamp_and_close_and_covariates}

        Returns:
            {symbol: [signal_dict, ...]} — same shape as detect()
        """
        if not self.predictor:
            return {sym: [] for sym in frames}

        if not frames:
            return {}

        # Determine timestamp column from first frame
        first_df = next(iter(frames.values()))
        if 'time' in first_df.columns:
            timestamp_col = 'time'
        elif 'timestamp' in first_df.columns:
            timestamp_col = 'timestamp'
        else:
            logger.warning("No timestamp column found for batch Chronos inference")
            return {sym: [] for sym in frames}

        # Concatenate all frames with item_id per symbol
        pieces = []
        for sym, df in frames.items():
            if df is None or df.empty:
                continue
            sub = df.copy()
            sub['item_id'] = str(sym)
            sub[timestamp_col] = pd.to_datetime(sub[timestamp_col])
            # Strip tz to satisfy AG
            try:
                if hasattr(sub[timestamp_col].dt, "tz") and sub[timestamp_col].dt.tz is not None:
                    sub[timestamp_col] = sub[timestamp_col].dt.tz_convert(None)
            except (AttributeError, TypeError):
                pass
            pieces.append(sub)
        if not pieces:
            return {sym: [] for sym in frames}

        big = pd.concat(pieces, ignore_index=True)
        try:
            data = TimeSeriesDataFrame.from_data_frame(
                big,
                id_column="item_id",
                timestamp_column=timestamp_col,
            )
        except Exception as e:
            logger.error(f"Batch Chronos TimeSeriesDataFrame build failed: {e}")
            return {sym: [] for sym in frames}

        try:
            predictions = self.predictor.predict(data)
        except Exception as e:
            logger.error(f"Batch Chronos predict failed: {e}")
            return {sym: [] for sym in frames}

        out: Dict[str, List[Dict]] = {sym: [] for sym in frames}
        for sym in frames:
            try:
                forecast = predictions.loc[str(sym)]
            except KeyError:
                continue
            if forecast is None or len(forecast) == 0:
                continue
            future_price = forecast.iloc[-1]['mean']
            future_q10 = forecast.iloc[-1].get('0.1') or (future_price * 0.95)
            future_q90 = forecast.iloc[-1].get('0.9') or (future_price * 1.05)
            # current price = last close in frame
            cur_df = frames[sym]
            current_price = float(cur_df.iloc[-1]['close'])
            pct_change = (future_price - current_price) / current_price

            sigs: List[Dict] = []
            if pct_change > self.threshold:
                sigs.append({
                    "type": "CHRONOS_LONG",
                    "confidence": min(abs(pct_change) * 100, 1.0),
                    "bbox": [0, 0, 0, 0],
                    "meta": {
                        "current_price": current_price,
                        "forecast_price": float(future_price),
                        "forecast_q10": float(future_q10),
                        "forecast_q90": float(future_q90),
                        "pct_change": pct_change,
                        "horizon": self.prediction_length,
                    },
                })
            elif pct_change < -self.threshold:
                sigs.append({
                    "type": "CHRONOS_SHORT",
                    "confidence": min(abs(pct_change) * 100, 1.0),
                    "bbox": [0, 0, 0, 0],
                    "meta": {
                        "current_price": current_price,
                        "forecast_price": float(future_price),
                        "forecast_q10": float(future_q10),
                        "forecast_q90": float(future_q90),
                        "pct_change": pct_change,
                        "horizon": self.prediction_length,
                    },
                })
            out[sym] = sigs
        return out
