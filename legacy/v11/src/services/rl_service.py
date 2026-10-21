import os
import sys
import numpy as np
import pandas as pd
from loguru import logger
import time
from datetime import datetime
from sb3_contrib import RecurrentPPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize
from stable_baselines3.common.monitor import Monitor
import gymnasium as gym
from gymnasium import spaces

# Add project root and source path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
SRC_ROOT = os.path.join(PROJECT_ROOT, "src")
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

# V18 Model Paths
MODEL_PATH = os.path.join(PROJECT_ROOT, "models", "v18_recppo", "recppo_v18_1000000_steps.zip")
VEC_PATH = os.path.join(PROJECT_ROOT, "models", "v18_recppo", "recppo_v18_vecnormalize_1000000_steps.pkl")

class DummyV18Env(gym.Env):
    """Dummy environment to provide the correct observation space for VecNormalize loading."""
    def __init__(self):
        super().__init__()
        # V18 expects 32 features
        self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=(32,), dtype=np.float32)
        self.action_space = spaces.Box(low=-1, high=1, shape=(1,), dtype=np.float32)
    def reset(self): return np.zeros((32,), dtype=np.float32)
    def step(self, action): return np.zeros((32,), dtype=np.float32), 0.0, False, {}

class RLService:
    def __init__(self, model_path: str = MODEL_PATH, vec_path: str = VEC_PATH):
        self.model_path = model_path
        self.vec_path = vec_path
        self.model = None
        self.env = None
        # Per-symbol LSTM state management
        self.lstm_states = {}
        self.episode_starts = {}
        self._load_model()
        
    def _load_model(self):
        try:
            if os.path.exists(self.model_path) and os.path.exists(self.vec_path):
                # 1. Create dummy env and load VecNormalize
                def make_env():
                    return Monitor(DummyV18Env())
                
                venv = DummyVecEnv([make_env])
                self.env = VecNormalize.load(self.vec_path, venv)
                self.env.training = False     # Stop mean/var updates
                self.env.norm_reward = False  # Don't normalize rewards during inference
                
                # 2. Load RecurrentPPO model
                self.model = RecurrentPPO.load(self.model_path, env=self.env, device="cpu")
                logger.info(f"✅ Loaded RL Model V18 RecPPO: {self.model_path}")
            else:
                if not os.path.exists(self.model_path):
                    logger.error(f"❌ Model missing at {self.model_path}")
                if not os.path.exists(self.vec_path):
                    logger.error(f"❌ VecNormalize missing at {self.vec_path}")
        except Exception as e:
            logger.error(f"❌ Failed to load V18 models: {e}")
            import traceback
            traceback.print_exc()

    def predict(self, df_5min: pd.DataFrame, symbol: str, position_info: dict = None) -> dict:
        if not self.model:
            return {"error": "Model not loaded"}

        try:
            # ── V18 Data Pipeline ──────────────────────────────────────────
            # Try to construct the 32-dim observation from df_5min.
            # If df_5min lacks required columns, raw_obs will be zeros — return HOLD.
            raw_obs = self._construct_v18_obs(df_5min, position_info)

            # If all zeros (pipeline unavailable), explicitly return HOLD
            if raw_obs is None or np.allclose(raw_obs, 0.0):
                logger.warning(f"V18 pipeline returned zeros for {symbol} — returning HOLD")
                self._reset_lstm_state(symbol)
                return {
                    "symbol": symbol,
                    "target_position": 0.0,
                    "action": "V18_HOLD_NO_SIGNAL",
                    "timestamp": time.time(),
                    "model_version": "V18_RecPPO_1M",
                    "is_recurrent": True,
                    "note": "pipeline_zeros"
                }

            # ── LSTM State Reset at Trading Day Boundary ──────────────────
            self._manage_lstm_state(symbol, df_5min)

            # 1. Normalize observation
            norm_obs = self.env.normalize_obs(raw_obs.reshape(1, -1))

            # 2. RecurrentPPO Prediction
            action, new_lstm_states = self.model.predict(
                norm_obs,
                state=self.lstm_states[symbol],
                episode_start=self.episode_starts[symbol],
                deterministic=True
            )

            # 3. Update LSTM states (episodic — reset on new trading day)
            self.lstm_states[symbol] = new_lstm_states
            # episode_start stays False within a trading session
            # (will be reset True by _manage_lstm_state at session boundary)

            # V18 returns continuous position in [-1.0, 1.0]
            target_pos = float(np.clip(action[0][0], -1.0, 1.0))

            return {
                "symbol": symbol,
                "target_position": round(target_pos, 4),
                "action": "V18_CONTINUOUS",
                "timestamp": time.time(),
                "model_version": "V18_RecPPO_1M",
                "is_recurrent": True
            }

        except Exception as e:
            logger.error(f"RL Prediction failed for {symbol}: {e}")
            import traceback
            traceback.print_exc()
            self._reset_lstm_state(symbol)
            return {"error": str(e), "symbol": symbol}

    # ─────────────────────────────────────────────────────────────────────────────
    def _construct_v18_obs(self, df_5min: pd.DataFrame, position_info: dict = None) -> np.ndarray:
        """
        Construct 32-dim V18 observation vector from a 5-min bar DataFrame.

        Returns 32-dim np.ndarray on success, None on failure.
        """
        try:
            df = df_5min.copy()
            if 'timestamp' in df.columns:
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                df = df.sort_values('timestamp').reset_index(drop=True)

            # ── Inline microstructure features (VWAP, volume imbalance, ATR vol) ──
            close = df['close'].values.astype(float)
            high = df['high'].values.astype(float)
            low = df['low'].values.astype(float)
            volume = df['volume'].values.astype(float)

            # VWAP
            typical = (high + low + close) / 3.0
            cum_pv = pd.Series(typical * volume).cumsum().values
            cum_v = pd.Series(volume).cumsum().values
            vwap = cum_pv / (cum_v + 1e-8)
            df['vwap_distance'] = (close - vwap) / (vwap + 1e-8)

            # Volume imbalance
            vol_ma = pd.Series(volume).rolling(20, min_periods=5).mean().values
            df['volume_imbalance'] = (volume - vol_ma) / (vol_ma + 1e-8)

            # ATR-based price volatility (dims 3-4)
            tr1 = high[1:] - low[1:]
            tr2 = np.abs(high[1:] - close[:-1])
            tr3 = np.abs(low[1:] - close[:-1])
            tr = np.vstack([tr1, tr2, tr3]).max(axis=0)
            tr = np.concatenate([[np.nan], tr])
            atr_15 = pd.Series(tr).rolling(15, min_periods=15).mean().values
            atr_60 = pd.Series(tr).rolling(60, min_periods=60).mean().values
            df['vol_15m'] = atr_15 / (close + 1e-8)
            df['vol_60m'] = atr_60 / (close + 1e-8)

            # Last row only
            row = df.iloc[-1]

            # Ensure YOLO columns exist (yolo_prob_0 … yolo_prob_6)
            for i in range(7):
                col = f'yolo_prob_{i}'
                if col not in df.columns:
                    df[col] = 0.0
            if 'sentiment' not in df.columns:
                df['sentiment'] = 0.0

            # Map to 32-dim observation space
            obs = np.zeros(32, dtype=np.float32)
            closes = df['close'].values
            rets = np.diff(closes) / closes[:-1]
            obs[0] = np.clip(rets[-1] * 100, -3, 3) if len(rets) > 0 else 0  # ret_5m
            obs[1] = np.clip(np.mean(rets[-3:]) * 100, -3, 3) if len(rets) >= 3 else 0  # ret_15m
            obs[2] = np.clip(np.mean(rets[-12:]) * 100, -3, 3) if len(rets) >= 12 else 0  # ret_60m
            # ATR-based price volatility (dims 3-4)
            obs[3] = np.clip(row.get('vol_15m', 0) * 500, 0, 5)
            obs[4] = np.clip(row.get('vol_60m', 0) * 500, 0, 5)
            obs[5] = (row.get('rsi_14', 50) - 50) / 25  # RSI centered at 0
            obs[6] = np.tanh(row.get('dist_sma20', 0) * 100)
            obs[7] = np.tanh(row.get('dist_sma50', 0) * 100)
            obs[8] = np.tanh(row.get('dist_sma200', 0) * 100)
            obs[9] = np.tanh(row.get('vwap_distance', 0))
            obs[10] = row.get('regime', 0)
            obs[11] = row.get('is_high_vol', 0)
            for i in range(7):
                obs[12 + i] = row.get(f'yolo_prob_{i}', 0.0)
            obs[19] = row.get('sentiment', 0.0)
            obs[20] = np.tanh(row.get('dist_sma20', 0) * 50)
            obs[21] = np.tanh(row.get('dist_sma50', 0) * 50)
            obs[22] = position_info.get('current_position', 0.0) if position_info else 0.0
            obs[23] = position_info.get('unrealized_pnl', 0.0) * 10 if position_info else 0.0
            obs[24] = np.tanh(row.get('volume_imbalance', 0))
            # obs[25-31] remain zero (padding)
            return obs
        except Exception:
            return None  # Pipeline failed

    def _manage_lstm_state(self, symbol: str, df_5min: pd.DataFrame):
        """Reset LSTM state at trading day boundary (9:30 EST or 4:00 EST close)."""
        if symbol not in self.lstm_states:
            self.lstm_states[symbol] = None
            self.episode_starts[symbol] = np.ones((1,), dtype=bool)
            return

        # Detect session start/end from timestamp
        if 'timestamp' in df_5min.columns and len(df_5min) > 0:
            ts_col = pd.to_datetime(df_5min['timestamp'])
            last_ts = ts_col.iloc[-1].time() if hasattr(ts_col.iloc[-1], 'time') else None
            if last_ts is not None:
                from datetime import time as t
                # Reset at market open (9:30 EST) or after close (16:00 EST)
                if last_ts == t(9, 30) or (hasattr(self, '_last_ts') and getattr(self, '_last_ts') == t(16, 0)):
                    self._reset_lstm_state(symbol)
        self._last_ts = last_ts if 'last_ts' not in dir() else getattr(self, '_last_ts', None)

    def _reset_lstm_state(self, symbol: str):
        """Hard-reset LSTM hidden state for a symbol."""
        self.lstm_states[symbol] = None
        self.episode_starts[symbol] = np.ones((1,), dtype=bool)

# Singleton instance
rl_service = RLService()
