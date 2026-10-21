"""
Execution Service - 负责幂等性的订单执行
确保同一个事件 ID 不会被执行两次。
"""

import json
import logging
import time
import redis
from typing import Optional, Dict, Any
from core.interfaces.broker import BaseBroker

logger = logging.getLogger("v8.execution_service")

class ExecutionService:
    def __init__(self, broker: BaseBroker, redis_client: redis.Redis, mode: str = "paper"):
        self.broker = broker
        self.redis = redis_client
        self.mode = mode
        self.key_prefix = f"execution:{mode}"

    def _generate_client_order_id(self, event_id: str, symbol: str, action: str) -> str:
        """生成唯一的 Client Order ID (Flow A)"""
        return f"v8_{event_id}_{symbol}_{action.lower()}"

    def execute_decision(self, event_id: str, symbol: str, action: str, qty: float, price: Optional[float] = None) -> Dict[str, Any]:
        """
        幂等执行交易决策
        """
        # 1. 幂等性检查 (Flow A)
        idempotency_key = f"{self.key_prefix}:event:{event_id}"
        existing = self.redis.get(idempotency_key)
        if existing:
            try:
                logger.warning(f"⚠️  Idempotency Triggered: Event {event_id} already executed. Skipping.")
                return json.loads(existing)
            except (json.JSONDecodeError, ValueError) as e:
                # Corrupted record — delete and retry (prevent infinite loop)
                logger.error(f"Corrupted idempotency record for {event_id}: {e} — deleting and retrying.")
                self.redis.delete(idempotency_key)

        # 2. 检查仓位 (简单的防重入保护)
        # 这里可以扩展更复杂的预检查逻辑

        # 3. 生成 Client Order ID
        client_order_id = self._generate_client_order_id(event_id, symbol, action)
        
        # 4. 执行下单
        try:
            logger.info(f"🚀 Execution: Placing {action} {qty} {symbol} (Event: {event_id})")
            
            # 使用 LIMIT 订单带 Buffer (假设默认 LMT 逻辑)
            order_type = "LMT" if price else "MKT"
            
            # 记录下单开始
            execution_record = {
                "event_id": event_id,
                "client_order_id": client_order_id,
                "symbol": symbol,
                "action": action,
                "qty": qty,
                "limit_price": price,
                "status": "SUBMITTED",
                "timestamp": time.time()
            }
            
            result = self.broker.place_order(
                symbol=symbol,
                action=action,
                qty=qty,
                price=price or 0.0,
                order_type=order_type
            )

            # 5. 持久化状态 — broker_result 必须可 JSON 序列化，否则用 repr()
            try:
                execution_record["broker_result"] = result if isinstance(result, (dict, list, str, int, float, bool, type(None))) else repr(result)
                self.redis.setex(idempotency_key, 86400, json.dumps(execution_record))
            except Exception as json_err:
                # 序列化失败也设置幂等键，防止重复下单
                logger.error(f"Failed to serialize broker result: {json_err} — setting idempotency key with error state")
                execution_record["broker_result"] = f"unserializable:{repr(result)}"
                self.redis.setex(idempotency_key, 86400, json.dumps(execution_record))

            # 记录待处理订单以便 Recovery Sync (Flow B) 使用
            pending_key = f"orders:pending:{self.mode}:{client_order_id}"
            self.redis.setex(pending_key, 86400, json.dumps(execution_record))

            return execution_record

        except Exception as e:
            logger.error(f"❌ Execution Failed for {symbol}: {e}")
            # 即使 broker 调用失败也设置幂等键，防止无限重试
            self.redis.setex(idempotency_key, 86400, json.dumps({
                "event_id": event_id,
                "client_order_id": client_order_id,
                "symbol": symbol,
                "action": action,
                "qty": qty,
                "status": "FAILED",
                "error": str(e),
                "timestamp": time.time()
            }))
            return {"event_id": event_id, "success": False, "error": str(e)}

    def cancel_all_pending(self, symbol: Optional[str] = None):
        """取消所有挂单 (紧急避险)"""
        # 实际实现中需要遍历 broker 的 openOrders
        pass
