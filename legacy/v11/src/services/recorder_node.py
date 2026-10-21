import os
import logging
import threading
import signal
import sys
from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from src.services.trade_recorder import TradeRecorder
from src.services.redis_pnl_subscriber import RedisPnLSubscriber
from core.redpanda_bus import RedpandaEventBus
from src.infrastructure.redis_client import get_redis_client

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("RecorderNode")

class RecorderNode:
    def __init__(self):
        self.db_url = os.getenv("DATABASE_URL")
        if not self.db_url:
            host = os.getenv("DB_HOST", "localhost")
            port = os.getenv("DB_PORT", "5435")
            user = os.getenv("DB_USER", "stock_app")
            password = os.getenv("DB_PASS")
            if not password:
                raise ValueError("DB_PASS environment variable is not set.")
            db_name = os.getenv("DB_NAME", "v8_db")
            self.db_url = f"postgresql://{user}:{password}@{host}:{port}/{db_name}"
        self.kafka_bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
        self.redis = get_redis_client()
        
        # 1. Initialize Bus
        self.bus = RedpandaEventBus(
            bootstrap_servers=self.kafka_bootstrap, 
            group_id="trade_recorder_node_v2"
        )
        self.bus.connect()
        
        # 2. Initialize Recorder
        self.recorder = TradeRecorder(
            db_url=self.db_url, 
            bus=self.bus, 
            redis_client=self.redis
        )
        
        # 3. Initialize subscriber
        self.subscriber = RedisPnLSubscriber(recorder=self.recorder)
        
        self._stop_event = threading.Event()

    def start(self):
        logger.info("🚀 Starting Recorder Node...")
        
        # Start Redis Subscriber in background
        self.subscriber.start()
        
        # Subscribe Recorder to trade execution events
        self.bus.subscribe("events.trade_execution", self.recorder.consume_event)
        
        # Start Kafka Bus (this might be blocking depending on implementation)
        # In our case, bus.start() usually runs consumers in background threads or blocks
        self.bus.start()
        
        logger.info("✅ Recorder Node is fully operational.")
        
        try:
            while not self._stop_event.is_set():
                import time
                time.sleep(1)
        except Exception as e:
            logger.error(f"Recorder Node error: {e}")
        finally:
            self.stop()

    def stop(self):
        logger.info("🛑 Stopping Recorder Node...")
        self._stop_event.set()
        if hasattr(self, 'subscriber'):
            self.subscriber.stop()
        if hasattr(self, 'bus'):
            self.bus.stop()
        logger.info("👋 Recorder Node stopped.")

def signal_handler(sig, frame):
    node.stop()
    sys.exit(0)

if __name__ == "__main__":
    node = RecorderNode()
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    node.start()
