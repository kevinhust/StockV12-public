
import os
import sys
import json
import pandas as pd
from collections import deque
from typing import Dict, List, Deque
from loguru import logger
from threading import Lock

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.bus import EventBus
from core.redpanda_bus import RedpandaEventBus
from services.pattern_service import PatternService

class PatternMonitor:
    def __init__(self, 
                 bus: EventBus, 
                 pattern_service: PatternService, 
                 window_size: int = 64):
        self.bus = bus
        self.service = pattern_service
        self.window_size = window_size
        
        # Buffer: Symbol -> Deque of candles
        self.buffers: Dict[str, Deque] = {}
        self.lock = Lock()
        
    def start(self):
        logger.info("Starting Pattern Monitor...")
        self.bus.subscribe("market_data.candles.1m", self.on_candle)
        self.bus.start()
        
    def on_candle(self, message: Dict):
        """
        Callback for new 1m candle.
        Message: {symbol, open, high, low, close, volume, time}
        """
        symbol = message.get('symbol')
        if not symbol:
            return

        # Snapshot buffer state inside lock, then release before processing
        with self.lock:
            if symbol not in self.buffers:
                self.buffers[symbol] = deque(maxlen=self.window_size)

            candle = {
                'open': float(message['open']),
                'high': float(message['high']),
                'low': float(message['low']),
                'close': float(message['close']),
                'volume': float(message.get('volume', 0)),
                'time': message.get('time')
            }

            self.buffers[symbol].append(candle)

            # Only run inference if we have enough data
            if len(self.buffers[symbol]) >= self.window_size:
                # Copy the deque contents while holding the lock, then release it
                buffer_snapshot = list(self.buffers[symbol])
            else:
                buffer_snapshot = None

        # Process outside the lock — avoid holding the lock across network I/O (Kafka publish)
        if buffer_snapshot is not None:
            self._process_symbol(symbol, buffer_snapshot)
                
    def _process_symbol(self, symbol: str, buffer_snapshot: List[dict]):
        # Use the pre-captured buffer snapshot — avoids copying inside the lock
        df = pd.DataFrame(buffer_snapshot)
        
        # Run Inference
        try:
            patterns = self.service.detect(df)
            
            if patterns:
                logger.info(f"✨ Detected {len(patterns)} patterns for {symbol}")
                for p in patterns:
                    event = {
                        "type": "PATTERN_DETECTED",
                        "symbol": symbol,
                        "pattern": p['type'],
                        "confidence": p['confidence'],
                        "timestamp": p['timestamp'],
                        "bbox": p['bbox']
                    }
                    self.bus.publish("events.pattern", event)
                    
        except Exception as e:
            logger.error(f"Error processing patterns for {symbol}: {e}")

if __name__ == "__main__":
    # Standalone Runner

    # 1. Config
    models_dir = os.path.join(PROJECT_ROOT, "models")
    MODEL_PATH = os.path.join(models_dir, "yolo11_detection", "yolo11s_pattern_v1_20251229_2023.pt")

    # Check Model
    if not os.path.exists(MODEL_PATH):
        # Fallback search if timestamp changed
        import glob
        pattern_dir = os.path.join(models_dir, "yolo11_detection")
        models = glob.glob(os.path.join(pattern_dir, "*.pt"))
        if models:
            MODEL_PATH = models[0]
        else:
            logger.error("No model found!")
            sys.exit(1)
            
    # 2. Init Components
    bus = RedpandaEventBus(bootstrap_servers="localhost:9092")
    service = PatternService(MODEL_PATH, conf_threshold=0.5)
    
    monitor = PatternMonitor(bus, service)
    
    # 3. Connect & Run
    try:
        bus.connect()
        monitor.start()
        
        # Keep alive
        import time
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Stopping...")
        bus.stop()
    except Exception as e:
        logger.error(f"Fatal Error: {e}")
        bus.stop()
