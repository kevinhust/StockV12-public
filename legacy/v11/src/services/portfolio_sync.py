import sys
import os
import time
import json
import logging
import argparse
import redis
from dotenv import load_dotenv

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from core.brokers.longbridge import LongbridgeBroker
from core.brokers.ibkr import IBKRBroker

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("portfolio_sync")

def recover_after_reconnect(broker, redis_client, mode: str):
    """
    Flow B: 恢复逻辑
    1. 查询 IBKR 当前活跃订单和最近成交
    2. 与 Redis 中记录的本地状态进行比对
    """
    logger.info("🔄 Running Post-Reconnection Recovery Sync...")
    try:
        open_orders = broker.get_open_orders()
        executions = broker.get_executions(days=1)
        
        # 获取 Redis 中记录的待处理订单 (这里假设 key 模式为 orders:pending:*)
        # 实际实现中需要根据 execution_service 的设计来对齐
        pending_keys = redis_client.keys(f"orders:pending:{mode}:*")
        
        logger.info(f"📊 Recovery Search: Found {len(open_orders)} open orders, {len(executions)} recent executions, {len(pending_keys)} pending in Redis.")
        
        # 此处可以扩展更复杂的对齐逻辑
        # 例如：如果在 Redis 是 PENDING，但在 IBKR 已经 FINISHED，则更新 Redis
        
    except Exception as e:
        logger.error(f"❌ Recovery failure: {e}")
    load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", type=str, required=True, choices=["shadow_longbridge", "ibkr_paper", "shadow_ibkr"])
    parser.add_argument("--interval", type=int, default=5)
    args = parser.parse_args()
    
    # 1. Select Broker
    if "longbridge" in args.mode:
        broker = LongbridgeBroker()
    elif "ibkr" in args.mode:
        broker = IBKRBroker()
    else:
        logger.error(f"Unknown mode: {args.mode}")
        return

    # 2. Connect
    logger.info(f"🚀 Starting Portfolio Sync Service | Mode: {args.mode}")
    if not broker.connect():
        logger.error("❌ Broker Connection Failed. Exiting.")
        return

    # 3. Setup Redis
    try:
        r = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", 6379)), db=0)
        r.ping()
        logger.info("✅ Connected to Redis")
    except Exception as e:
        logger.error(f"❌ Redis Connection Failed: {e}")
        return

    # 4. Loop
    was_connected = True
    while True:
        try:
            is_connected = broker.is_connected()
            
            # 检测重连
            if is_connected and not was_connected:
                recover_after_reconnect(broker, r, args.mode)
            
            was_connected = is_connected

            if not is_connected:
                logger.warning("🔌 Broker not connected. Attempting reconnection...")
                broker.connect()
                time.sleep(5)
                continue

            state = broker.get_account_state()
            if state:
                # Add Metadata
                state['last_updated'] = time.time()
                state['service_mode'] = args.mode
                
                # Write to Redis
                key = f"portfolio:snapshot:{args.mode}"
                r.set(key, json.dumps(state))
                
                # Calculate Risk Metrics (Simple)
                nav = state.get('nav', 0)
                cash = state.get('cash', 0)
                utilization = 1.0 - (cash / nav) if nav > 0 else 0
                
                logger.info(f"✅ Sync | NAV: ${nav:,.2f} | Cash: ${cash:,.2f} | Util: {utilization:.1%}")
            else:
                logger.warning("⚠️ Empty state received from broker")
        
        except Exception as e:
            logger.error(f"❌ Sync Loop Error: {e}")
        
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
