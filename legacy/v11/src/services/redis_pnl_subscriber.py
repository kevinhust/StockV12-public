import json
import logging
import threading
from typing import Any
from src.infrastructure.redis_client import get_redis_client

logger = logging.getLogger(__name__)

class RedisPnLSubscriber:
    """
    Subscribes to Redis market data updates and triggers 
    TradeRecorder to recalculate MtM and PnL.
    """
    def __init__(self, recorder: Any, channel: str = "market_data:updates"):
        self.recorder = recorder
        self.channel = channel
        self.redis = get_redis_client()
        self.pubsub = self.redis.pubsub()
        self._stop_event = threading.Event()
        self._worker_thread = None

    def start(self):
        """Starts the subscription loop in a separate thread."""
        if self._worker_thread and self._worker_thread.is_alive():
            logger.warning("RedisPnLSubscriber is already running.")
            return

        self._stop_event.clear()
        self.pubsub.subscribe(self.channel)
        
        self._worker_thread = threading.Thread(target=self._run, daemon=True)
        self._worker_thread.start()
        logger.info(f"📡 RedisPnLSubscriber started on channel: {self.channel}")

    def stop(self):
        """Stops the subscription loop."""
        self._stop_event.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=5)
        self.pubsub.unsubscribe(self.channel)
        logger.info("📡 RedisPnLSubscriber stopped.")

    def _run(self):
        """Main loop for processing Redis messages."""
        while not self._stop_event.is_set():
            try:
                # listen() is blocking, but we use a timeout to check _stop_event
                message = self.pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if not message:
                    continue

                if message['type'] == 'message':
                    self._process_payload(message['data'])
                    
            except Exception as e:
                logger.error(f"❌ Error in RedisPnLSubscriber loop: {e}")
                import time
                time.sleep(2) # Prevent tight loop on error

    def _process_payload(self, data: str):
        """Parses market data payload and triggers MtM update."""
        try:
            payload = json.loads(data)
            # Support both single update and batch update format
            updates = payload if isinstance(payload, list) else [payload]
            
            for update in updates:
                symbol = update.get("symbol")
                price = update.get("price") or update.get("close") # Fallback to close
                
                if symbol and price:
                    self.recorder.update_mtm(symbol, float(price))
                else:
                    logger.debug(f"⚠️ Incomplete market data update: {update}")
                    
        except json.JSONDecodeError:
            logger.error(f"❌ Failed to decode market data JSON: {data}")
        except Exception as e:
            logger.error(f"❌ Error processing market data payload: {e}")

if __name__ == "__main__":
    # Example usage for manual testing
    import os
    logging.basicConfig(level=logging.INFO)
    
    # Mock recorder
    class MockRecorder:
        def update_mtm(self, symbol, price):
            print(f"DEBUG: update_mtm called for {symbol} @ {price}")

    recorder = MockRecorder()
    subscriber = RedisPnLSubscriber(recorder)
    subscriber.start()
    
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        subscriber.stop()
