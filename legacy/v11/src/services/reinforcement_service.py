import os
import logging
import json
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import psycopg2
from psycopg2.extras import RealDictCursor
# KnowledgeStore lives in V11 (archived). Optional import — degrade closed.
try:
    from src.knowledge.store import KnowledgeStore  # type: ignore[import-not-found]
    _KNOWLEDGE_STORE_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    KnowledgeStore = None  # type: ignore[assignment,misc]
    _KNOWLEDGE_STORE_AVAILABLE = False
    logging.getLogger("ReinforcementEngine").warning(
        "src.knowledge.store unavailable (V11 archived). "
        "ReinforcementEngine will run with knowledge_store=None (lesson weight updates skipped)."
    )
from src.services.polygon_historical_fetcher import create_polygon_fetcher

logger = logging.getLogger("ReinforcementEngine")

class ReinforcementEngine:
    """
    V2.2 Positive Reinforcement Engine.
    Validates 'avoided' trades (HOLD/REJECTED with lessons applied) 
    by checking if the price actually dropped (for avoided BUYS) 
    or rose (for avoided SELLS) after T+4h.
    """

    def __init__(self, db_url: str = None):
        self.db_url = db_url or os.getenv("DATABASE_URL")
        if not self.db_url:
            host = os.getenv("DB_HOST", "timescaledb")
            port = os.getenv("DB_PORT", "5432")
            user = os.getenv("DB_USER", "stock_app")
            password = os.getenv("DB_PASS")
            if not password:
                raise ValueError("DB_PASS environment variable is not set.")
            db_name = os.getenv("DB_NAME", "v8_db")
            self.db_url = f"postgresql://{user}:{password}@{host}:{port}/{db_name}"
        
        if _KNOWLEDGE_STORE_AVAILABLE and KnowledgeStore is not None:
            try:
                self.knowledge_store: Optional["KnowledgeStore"] = KnowledgeStore()
            except Exception as _ks_e:
                logging.getLogger("ReinforcementEngine").warning(
                    f"KnowledgeStore() init failed ({_ks_e}); lesson weight updates skipped"
                )
                self.knowledge_store = None
        else:
            self.knowledge_store = None
        self.fetcher = create_polygon_fetcher()

    def audit_successful_avoidance(self, lookback_hours: int = 24):
        """
        1. Query trade_events for was_lesson_applied = true
        2. Filter for entries where status is SHADOW_LOGGED or REJECTED
        3. Check T+4h price vs entry price
        4. If avoidance was 'correct', increment lesson weight.
        """
        logger.info(f"🛡️ Starting Reinforcement Audit (Lookback: {lookback_hours}h)...")
        
        avoided_trades = self._get_avoided_trades(lookback_hours)
        logger.info(f"🔍 Found {len(avoided_trades)} avoided trades to audit.")
        
        success_count = 0
        for trade in avoided_trades:
            if self._verify_trade_avoidance(trade):
                success_count += 1
                
        logger.info(f"✅ Reinforcement Audit Complete. {success_count} lessons reinforced.")
        return success_count

    def _get_avoided_trades(self, lookback_hours: int) -> List[Dict]:
        """Fetch trades from DB where lessons were applied but no entry occurred."""
        since = datetime.utcnow() - timedelta(hours=lookback_hours)
        
        sql = """
            SELECT 
                time, symbol, side, price as entry_price, 
                context_snapshot->>'applied_lesson_ids' as lesson_ids_json,
                trace_id
            FROM trade_events
            WHERE time > %s
            AND (context_snapshot->>'was_lesson_applied')::boolean = true
            AND status IN ('SHADOW_LOGGED', 'REJECTED')
        """
        
        try:
            with psycopg2.connect(self.db_url) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, (since,))
                    return cur.fetchall()
        except Exception as e:
            logger.error(f"Failed to fetch avoided trades: {e}")
            return []

    def _verify_trade_avoidance(self, trade: Dict) -> bool:
        """
        Checks if the decision to AVOID was correct.
        Strategy: For an avoided BUY, if Price(T+4h) < Price(T), the avoidance was SUCCESSFUL.
        """
        symbol = trade['symbol']
        entry_price = float(trade['entry_price'] or 0)
        side = trade['side']
        event_time = trade['time']
        lesson_ids = []
        
        if trade['lesson_ids_json']:
            try:
                lesson_ids = json.loads(trade['lesson_ids_json'])
            except:
                logger.warning(f"Failed to parse lesson_ids for trace {trade['trace_id']}")
        
        if not lesson_ids or entry_price == 0:
            return False

        # 1. Get T+4h Price
        target_time = event_time + timedelta(hours=4)
        # Polygon fetcher normally takes dates, but we need a specific point.
        # We'll fetch 5m bars for that day.
        try:
            bars = self.fetcher.fetch_bars(
                symbol=symbol,
                multiplier=5,
                timespan='minute',
                from_date=event_time.strftime('%Y-%m-%d'),
                to_date=target_time.strftime('%Y-%m-%d')
            )
            
            if not bars:
                return False
                
            # Find closest bar to T+4h
            target_ts_ms = int(target_time.timestamp() * 1000)
            closest_bar = min(bars, key=lambda x: abs(x['timestamp'] - target_ts_ms))
            future_price = closest_bar['close']
            
            # 2. Validation Logic
            is_success = False
            if side == 'BUY':
                # Avoided buying, price went down -> SUCCESS
                if future_price < entry_price:
                    is_success = True
                    logger.info(f"🌟 [SUCCESS] Avoided BUY on {symbol} @ {entry_price}. Price at T+4h: {future_price}")
            elif side == 'SELL':
                # Avoided selling/liquidating, price went up -> SUCCESS
                if future_price > entry_price:
                    is_success = True
                    logger.info(f"🌟 [SUCCESS] Avoided SELL on {symbol} @ {entry_price}. Price at T+4h: {future_price}")
            
            # 3. Apply Reinforcement
            if is_success:
                for lid in lesson_ids:
                    if self.knowledge_store is not None:
                        self.knowledge_store.increment_lesson_weight(lid)
                return True
                
        except Exception as e:
            logger.error(f"Reinforcement verification failed for {symbol}: {e}")
            
        return False

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    engine = ReinforcementEngine()
    engine.audit_successful_avoidance(lookback_hours=48)
