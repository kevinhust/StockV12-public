import os
import sys
import json
import time
import logging
import psycopg2
from datetime import datetime
from typing import Dict, Any, List
from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

load_dotenv(os.path.join(PROJECT_ROOT, ".env"))

from core.execution.router import ExecutionRouter
from core.redpanda_bus import RedpandaEventBus
from core.config import ConfigManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("LiquidationService")

class LiquidationService:
    def __init__(self, dry_run: bool = False):
        self.db_url = os.getenv("DATABASE_URL")
        # Construct fallback if missing
        if not self.db_url:
            host = os.getenv("DB_HOST", "localhost")
            port = os.getenv("DB_PORT", "5435")
            user = os.getenv("DB_USER", "stock_app")
            password = os.getenv("DB_PASS")
            if not password:
                raise ValueError("DB_PASS environment variable is not set. Set DB_PASS before running liquidation.")
            db_name = os.getenv("DB_NAME", "v8_db")
            self.db_url = f"postgresql://{user}:{password}@{host}:{port}/{db_name}"
            
        self.dry_run = dry_run
        self.config_manager = ConfigManager()
        self.eod_config = {} # Loaded from config_manager below
        self._load_config()
        
        self.bus = RedpandaEventBus(
            bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
            group_id="liquidation_service"
        )
        self.bus.connect()
        
        # Redis Client for regime and score lookups
        import redis
        from src.infrastructure.redis_client import get_redis_client
        self.redis = get_redis_client()
        
        self.router = ExecutionRouter(
            config_manager=self.config_manager, 
            bus=self.bus,
            redis_client=self.redis
        )

    def _load_config(self):
        """Loads EOD config from trading_modes.yaml via ConfigManager effectively."""
        # ConfigManager might not have direct access to raw YAML sections easily if not mapped
        # So we manually read the section if needed, or assume it's in a specific place.
        # For now, let's just use defaults as discussed in the plan.
        self.eod_config = {
            "exempt_enabled": True,
            "exempt_regimes": ["BULL_LONG", "NEUTRAL"],
            "exempt_profit_pct": 0.08,
            "exempt_score_threshold": 0.75,
            "exempt_sentiment_threshold": 0.70,
            "exempt_leave_ratio": 0.30,
            "exempt_max_per_stock_pct": 0.05,
            "exempt_max_total_pct": 0.20,
            "exempt_max_symbols_per_day": 5,
            "exempt_max_consecutive_days": 3
        }
        # TODO: Dynamically load from trading_modes.yaml if section exists
        try:
            with open(os.path.join(PROJECT_ROOT, "config/trading_modes.yaml"), 'r') as f:
                import yaml
                full_cfg = yaml.safe_load(f)
                if 'eod_liquidation' in full_cfg:
                    self.eod_config.update(full_cfg['eod_liquidation'])
        except Exception as e:
            logger.warning(f"Could not load eod_liquidation from yaml, using defaults: {e}")

    def get_open_positions(self, mode: str = None) -> List[Dict]:
        """Fetch all open/partial lots from DB, optionally filtered by mode."""
        sql = """
            SELECT p.symbol, SUM(p.remaining_quantity) as total_qty, AVG(p.entry_price) as avg_price
            FROM open_positions_track p
            JOIN trade_events e ON p.entry_event_id = e.event_id
            WHERE p.status IN ('OPEN', 'PARTIAL')
        """
        params = []
        if mode:
            sql += " AND e.mode = %s"
            params.append(mode)
        
        sql += " GROUP BY p.symbol"
        
        positions = []
        try:
            with psycopg2.connect(self.db_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                    for row in rows:
                        symbol = row[0]
                        qty = float(row[1])
                        entry = float(row[2])
                        
                        # Fetch current price from Redis (MtM snapshot)
                        curr = entry
                        mtm_data = self.redis.get(f"open_position_mtm:{symbol}")
                        if mtm_data:
                            try:
                                curr = json.loads(mtm_data).get("current_price", entry)
                            except (json.JSONDecodeError, TypeError, ValueError) as e:
                                logger.warning(f"Failed to parse MTM data for {symbol}: {e} — using entry price {entry}")
                        
                        unrealized_pct = (curr - entry) / entry if entry > 0 else 0
                        
                        positions.append({
                            "symbol": symbol,
                            "quantity": qty,
                            "remaining_quantity": qty,
                            "avg_price": entry,
                            "current_price": curr,
                            "unrealized_pct": unrealized_pct
                        })
        except Exception as e:
            logger.error(f"Failed to fetch open positions: {e}")
        return positions

    def is_exempt(self, symbol: str, position: Dict, regime: str) -> (bool, str):
        """Quant Exemption Logic (Block 8.2)"""
        if not self.eod_config.get('exempt_enabled'):
            return False, "exempt_disabled"

        # 1. Regime Gating
        if regime not in self.eod_config.get('exempt_regimes', []):
            return False, f"regime_{regime}_not_safe"

        # 2. Profit Threshold
        if position['unrealized_pct'] < self.eod_config.get('exempt_profit_pct', 0.08):
            return False, f"profit_{position['unrealized_pct']:.2%}_low"

        # 3. Score & Sentiment from Redis (watchlist)
        watchlist_data = self.redis.hget("watchlist:current", symbol)
        if not watchlist_data:
            return False, "no_watchlist_data"
        
        try:
            data = json.loads(watchlist_data)
            fused_score = float(data.get("fused_score", 0))
            sentiment = float(data.get("sentiment_score", 0))
            
            if fused_score < self.eod_config.get('exempt_score_threshold', 0.75):
                return False, f"score_{fused_score:.2f}_low"
            if sentiment < self.eod_config.get('exempt_sentiment_threshold', 0.70):
                return False, f"sentiment_{sentiment:.2f}_low"
        except Exception as e:
            return False, f"score_parse_error_{str(e)}"

        # 4. Constraints: Consecutive Days
        exempt_days_key = f"eod_exempt_days:{symbol}"
        exempt_days = int(self.redis.get(exempt_days_key) or 0)
        if exempt_days >= self.eod_config.get('exempt_max_consecutive_days', 3):
            return False, f"consecutive_{exempt_days}_exceeded"

        # 5. Constraints: Portfolio Weights
        # Use portfolio_mtm:current for NAV
        portfolio_data = self.redis.get("portfolio_mtm:current")
        portfolio_nav = 100000.0 # Fallback
        if portfolio_data:
            try:
                # Assuming total_cost + total_mtm_dollar = current NAV (approx)
                p_data = json.loads(portfolio_data)
                portfolio_nav = p_data.get("total_cost", 100000.0) + p_data.get("total_mtm_dollar", 0.0)
            except: pass
            
        stock_value = position['remaining_quantity'] * position['current_price']
        weight = stock_value / portfolio_nav if portfolio_nav > 0 else 0
        if weight > self.eod_config.get('exempt_max_per_stock_pct', 0.05):
            return False, f"stock_weight_{weight:.2%}_too_high"

        current_exempt_value = self._get_current_exempt_value()
        if (current_exempt_value + stock_value) / portfolio_nav > self.eod_config.get('exempt_max_total_pct', 0.20):
            return False, "total_exempt_capacity_full"

        # 6. Constraints: Daily Symbol Limit
        date_str = datetime.now().strftime("%Y-%m-%d")
        daily_count_key = f"eod_exempt_count:{date_str}"
        daily_count = int(self.redis.get(daily_count_key) or 0)
        if daily_count >= self.eod_config.get('exempt_max_symbols_per_day', 5):
            return False, "daily_symbol_limit_reached"

        return True, "strong_performance_exempt"

    def _get_current_exempt_value(self) -> float:
        """Sum up current exempt value from Redis tracking."""
        # Simple implementation: use a specific Redis key we update during liquidation
        val = self.redis.get("eod_total_exempt_value_today")
        return float(val) if val else 0.0

    def liquidate_all(self, mode: str = "shadow_v8"):
        """Liquidate open positions with Quant Exemption support."""
        logger.info(f"🚨 Starting liquidation for mode: {mode} (Dry-run: {self.dry_run})")
        
        # Get Regime for gating
        regime_data = self.redis.get("market_regime:current")
        regime = "UNKNOWN"
        if regime_data:
            regime = json.loads(regime_data).get("regime", "UNKNOWN")
        
        positions = self.get_open_positions(mode=mode)
        if not positions:
            logger.info("✅ No open positions found to liquidate.")
            return

        date_str = datetime.now().strftime("%Y-%m-%d")
        
        # Reset daily exempt value if first run of the day
        # (Usually handled by TTL, but we can manage it here)

        for pos in positions:
            symbol = pos['symbol']
            full_qty = pos['quantity']

            # Skip if already closed by eod_closing at 15:45
            if self.redis.sismember("eod:closed_today", symbol):
                logger.info(f"Skipping {symbol}: already closed by eod_closing")
                continue

            # Check Exemption
            is_exempt, reason = self.is_exempt(symbol, pos, regime)
            
            if is_exempt:
                leave_ratio = self.eod_config.get('exempt_leave_ratio', 0.3)
                liquidate_qty = full_qty * (1 - leave_ratio)
                logger.info(f"✨ [EXEMPT] {symbol} qualifies: {reason}. Keeping {leave_ratio*100}%")
                
                # Tracking
                self.redis.incr(f"eod_exempt_count:{date_str}")
                self.redis.expire(f"eod_exempt_count:{date_str}", 86400)
                self.redis.incr(f"eod_exempt_days:{symbol}")
                
                # Track total exempt value using integer cents to avoid float accumulation error
                exempt_val = (full_qty * leave_ratio) * pos['current_price']
                exempt_val_cents = int(round(exempt_val * 100))
                self.redis.incrbyfloat("eod_total_exempt_value_cents_today", exempt_val_cents)
                self.redis.expire("eod_total_exempt_value_cents_today", 86400)
                
                liq_reason = f"EXEMPT_PARTIAL_CLOSE_{reason}"
            else:
                liquidate_qty = full_qty
                liq_reason = f"EOD_FULL_LIQUIDATION_{reason}"
                # Reset consecutive days if fully closed
                self.redis.delete(f"eod_exempt_days:{symbol}")

            if liquidate_qty <= 0:
                logger.info(f"⏭️ Skipping {symbol} (0 qty to liquidate)")
                continue

            logger.warning(f"📉 Liquidating {liquidate_qty} shares of {symbol} (Total: {full_qty}, Reason: {liq_reason})")
            
            if self.dry_run:
                logger.info(f"[DRY-RUN] Would send SELL order for {liquidate_qty} {symbol}")
                continue

            # Route Order
            try:
                res = self.router.route_order(
                    mode=mode,
                    symbol=symbol,
                    action='SELL',
                    quantity=liquidate_qty,
                    order_type='MKT',
                    agent_metadata={"reason": liq_reason}
                )
                logger.info(f"✅ Liquidation order sent for {symbol}: {res.get('status')}")
                
                if res.get('status') == 'SHADOW_FILLED':
                    self._emit_shadow_execution(symbol, liquidate_qty, mode, liq_reason)

            except Exception as e:
                logger.error(f"❌ Failed to liquidate {symbol}: {e}")

    def _emit_shadow_execution(self, symbol: str, qty: float, mode: str, reason: str):
        """Emits a shadow execution event to Kafka with actual market price from Redis.

        P0: Validates price freshness before using it. Rejects stale prices (>120s).
        """
        # Fetch current price from Redis (written by ibkr_streamer)
        price = 0.0
        price_stale = False
        try:
            import redis as _redis
            import json
            import os
            r = _redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                decode_responses=True
            )
            raw = r.get(f"price:{symbol}:current")
            if raw:
                data = json.loads(raw)
                price = float(data.get("price") or data.get("last", 0))
                # P0: Check freshness
                ts = float(data.get("timestamp", 0))
                if ts > 0:
                    age_sec = time.time() - ts
                    if age_sec > 120:
                        logger.warning(
                            f"⚠️ STALE PRICE for {symbol}: ${price:.2f} is {age_sec:.0f}s old "
                            f"(threshold=120s). Marking as stale."
                        )
                        price_stale = True
        except Exception as e:
            logger.warning(f"Could not fetch Redis price for {symbol}: {e}")

        if price <= 0:
            logger.error(f"Cannot emit shadow execution for {symbol}: no valid price (price={price})")
            return

        if price_stale:
            logger.error(f"Cannot emit shadow execution for {symbol}: price is stale")
            return

        payload = {
            "event_type": "TRADE_FILLED",
            "event_id": f"liq_{symbol}_{int(time.time())}",
            "timestamp": datetime.utcnow().isoformat(),
            "symbol": symbol,
            "side": "SELL",
            "quantity": qty,
            "fill_qty": qty,
            "price": price,
            "fill_price": price,
            "status": "EXECUTED",
            "mode": mode,
            "metadata": {"reason": reason}
        }
        self.bus.publish("events.trade_execution", payload)
        logger.info(f"Published shadow execution for {symbol} @ ${price}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="EOD Liquidation Service")
    parser.add_argument("--mode", default="shadow_v8", help="Trading mode to liquidate")
    parser.add_argument("--dry-run", action="store_true", help="Don't send real orders")
    args = parser.parse_args()

    service = LiquidationService(dry_run=args.dry_run)
    service.liquidate_all(mode=args.mode)
