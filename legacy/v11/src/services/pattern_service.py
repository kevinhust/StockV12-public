
import os
import sys
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg') # Headless mode
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from ultralytics import YOLO
from PIL import Image
import io
from typing import List, Dict, Optional
from datetime import datetime
from loguru import logger

# Add project root and source path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
SRC_ROOT = os.path.join(PROJECT_ROOT, "src")
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

class PatternService:
    def __init__(self, model_path: str = None, conf_threshold: float = 0.5):
        """
        Initialize Pattern Service with YOLO11 model.
        """
        # Default to the new V18 Production model path
        default_model_path = os.path.join(PROJECT_ROOT, "models", "yolo11n_v18_production", "best.pt")
        final_model_path = model_path if model_path else default_model_path
        
        if not os.path.exists(final_model_path):
            logger.warning(f"⚠️ Model not found at {final_model_path}, trying to use provided model_path")
            if not os.path.exists(model_path) if model_path else True:
                if model_path:
                    raise FileNotFoundError(f"Model not found at {model_path}")
                else:
                    raise FileNotFoundError(f"Default model not found at {default_model_path}")
            final_model_path = model_path
            
        logger.info(f"Loading Pattern Model V18: {final_model_path}")
        self.model = YOLO(final_model_path)
        self.conf_threshold = conf_threshold
        # V18 Production Class mappings (7 core patterns)
        self.names = {
            0: "bull_pinbar_uptrend",
            1: "bull_pinbar_reversal",
            2: "bear_pinbar_downtrend",
            3: "bear_pinbar_reversal",
            4: "bull_engulfing",
            5: "bear_engulfing",
            6: "doji"
        }

    def detect(self, df: pd.DataFrame, save_dir: Optional[str] = None, symbol: str = "UNKNOWN") -> List[Dict]:
        """
        Detect patterns in the provided OHLC DataFrame.
        Returns a list of detected patterns.
        """
        if df.empty:
            return []

        # 1. Convert DF to Image (In-Memory)
        img = self._dataframe_to_image(df)
        
        # 2. Save Image if requested (Shadow Mode Data Collection)
        image_path = None
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{symbol}_{timestamp}.jpg"
            image_path = os.path.join(save_dir, filename)
            img.save(image_path)
            # logger.debug(f"Saved YOLO chart image to {image_path}")

        # 3. Inference
        results = self.model.predict(img, conf=self.conf_threshold, verbose=False)[0]
        
        patterns = []
        
        # 4. Parse Results
        img_width = img.width
        n_candles = len(df)
        
        for box in results.boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])
            xyxy = box.xyxy[0].tolist() # x1, y1, x2, y2
            
            x_center_px = (xyxy[0] + xyxy[2]) / 2
            
            # Map Pixel X to Candle Index (Approximate)
            x_idx_approx = (x_center_px / img_width) * (n_candles + 1) - 1
            x_idx = int(round(x_idx_approx))
            x_idx = max(0, min(n_candles - 1, x_idx))
            
            # Get Timestamp if available
            timestamp_val = df.index[x_idx] if isinstance(df.index, pd.DatetimeIndex) else x_idx
            
            pattern_name = self.names.get(cls_id, f"class_{cls_id}")
            
            patterns.append({
                "type": pattern_name,
                "confidence": round(conf, 4),
                "index": x_idx,
                "timestamp": str(timestamp_val),
                "bbox": [round(x, 2) for x in xyxy],
                "image_path": image_path
            })
            
        return patterns

    def _dataframe_to_image(self, df: pd.DataFrame) -> Image.Image:
        """
        Convert OHLC DataFrame to PIL Image suitable for YOLO.
        Matches the style of synthetic_chart_gen.py
        """
        IMG_SIZE = 640
        DPI = 100
        
        fig, ax = plt.subplots(figsize=(IMG_SIZE/DPI, IMG_SIZE/DPI), dpi=DPI)
        
        # Setup similar to generator
        ax.set_xlim(-1, len(df))
        all_high = df['high'].max()
        all_low = df['low'].min()
        rng = all_high - all_low
        if rng == 0: rng = 1.0
        ax.set_ylim(all_low - rng*0.05, all_high + rng*0.05)
        ax.axis('off')
        
        # Check column names (case-insensitive)
        df_cols = {c.lower(): c for c in df.columns}
        col_map = {
            'open': df_cols.get('open'),
            'high': df_cols.get('high'),
            'low': df_cols.get('low'),
            'close': df_cols.get('close')
        }

        # Draw
        for i, row in enumerate(df.itertuples()):
            o = getattr(row, col_map['open'])
            h = getattr(row, col_map['high'])
            l = getattr(row, col_map['low'])
            c = getattr(row, col_map['close'])
            
            color = 'green' if c >= o else 'red'
            
            # Wick
            ax.plot([i, i], [l, h], color='black', linewidth=1)
            
            # Body
            body_top = max(o, c)
            body_bottom = min(o, c)
            height = max(0.0001, body_top - body_bottom)
            
            rect = patches.Rectangle((i - 0.3, body_bottom), 0.6, height, facecolor=color, edgecolor='black', linewidth=0.5)
            ax.add_patch(rect)
            
        plt.tight_layout(pad=0)
        
        # Save to buffer
        buf = io.BytesIO()
        fig.savefig(buf, format='jpg')
        plt.close(fig)
        buf.seek(0)
        
        return Image.open(buf)
