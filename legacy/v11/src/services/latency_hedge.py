"""
Latency Hedge Manager V2.1
Handles network latency risks in Vaughan local Docker environment.

Features:
- EMA-smoothed latency monitoring
- 10s circuit breaker for critical spikes
- Dynamic position sizing based on latency
- Bid-Ask spread slippage factor
"""

import time
import logging
from loguru import logger
from typing import Optional


class LatencyHedgeManager:
    """
    延迟对冲管理器 V2.1

    在 Vaughan 本地 Docker 环境中监控网络延迟，
    动态调整仓位乘数以保护 RL 和 LLM 信号不被错位。
    """

    # 阈值配置
    CRITICAL_HALT_MS = 10_000   # >10s 立即触发断路器 (Circuit Breaker)
    HIGH_LATENCY_MS = 5_000     # >5s 显著减仓
    MEDIUM_LATENCY_MS = 3_000   # >3s 中度减仓
    STABLE_LATENCY_MS = 1_500   # >1.5s 轻微预警

    EMA_ALPHA = 0.2             # 延迟平滑系数 (0.1-0.3 较为平衡)

    def __init__(self):
        self.gateway_latency_ema = 0.0
        self.last_check_time = 0
        self.is_circuit_broken = False
        self._recovery_count = 0

    def update_latency(self, current_latency_ms: float) -> dict:
        """
        更新 EMA 延迟指标，并检查断路器状态

        Returns:
            dict with status info for logging/monitoring
        """
        result = {
            "ema_latency": self.gateway_latency_ema,
            "circuit_broken": False,
            "action_taken": None
        }

        # 1. 瞬时硬熔断检查 (Circuit Breaker)
        if current_latency_ms > self.CRITICAL_HALT_MS:
            logger.error(f"🚨 CIRCUIT BREAKER: Latency spike {current_latency_ms:.0f}ms > 10s!")
            self.is_circuit_broken = True
            result["circuit_broken"] = True
            result["action_taken"] = "HALT_ALL"
            return result

        # 2. EMA 平滑计算
        if self.gateway_latency_ema == 0:
            self.gateway_latency_ema = current_latency_ms
        else:
            self.gateway_latency_ema = (self.EMA_ALPHA * current_latency_ms) + \
                                       (1 - self.EMA_ALPHA) * self.gateway_latency_ema

        result["ema_latency"] = self.gateway_latency_ema

        # 3. 自动恢复逻辑 (连续 3 次正常延迟后恢复)
        if self.is_circuit_broken:
            if self.gateway_latency_ema < self.MEDIUM_LATENCY_MS:
                self._recovery_count += 1
                if self._recovery_count >= 3:
                    logger.info("✅ Latency stabilized, resetting circuit breaker.")
                    self.is_circuit_broken = False
                    self._recovery_count = 0
                    result["action_taken"] = "RECOVERY"
            else:
                self._recovery_count = 0

        self.last_check_time = time.time()
        return result

    def get_latency_multiplier(self) -> float:
        """
        根据 EMA 延迟返回仓位乘数

        Returns:
            float: 0.0 (熔断) to 1.0 (正常)
        """
        if self.is_circuit_broken:
            return 0.0  # 熔断期间禁止任何新开仓

        if self.gateway_latency_ema > self.HIGH_LATENCY_MS:
            return 0.3
        elif self.gateway_latency_ema > self.MEDIUM_LATENCY_MS:
            return 0.5
        elif self.gateway_latency_ema > self.STABLE_LATENCY_MS:
            return 0.8

        return 1.0

    def get_slippage_factor(self, bid: float, ask: float) -> float:
        """
        根据 Bid-Ask Spread 调节滑点因子

        逻辑：价差越大，执行成本越高，相应减小 Size

        Args:
            bid: 当前买一价
            ask: 当前卖一价

        Returns:
            float: 0.0 (极端价差) 到 1.0 (正常价差)
        """
        if bid <= 0 or ask <= 0:
            return 0.0

        spread_pct = (ask - bid) / ask * 100

        # 经验公式：价差每增加 0.1%，仓位降低 10%
        # 科技股通常 spread < 0.05%，如果 > 0.3% 说明流动性极差
        if spread_pct > 0.5:
            logger.warning(f"⚠️ Extreme spread detected: {spread_pct:.2f}%")
            return 0.3
        elif spread_pct > 0.3:
            return 0.5
        elif spread_pct > 0.1:
            return 0.8

        return 1.0

    def check_gateway_health(self, gateway_client) -> tuple[bool, float, dict]:
        """
        执行 Gateway 心跳检查

        Args:
            gateway_client: IBKR Gateway 客户端

        Returns:
            tuple: (is_healthy, latency_ms, metadata)
        """
        start = time.time()
        try:
            # 简单的 ping 检查 (需要根据实际 IBKR API 调整)
            # 这里假设 gateway_client 有一个 ping 方法
            if hasattr(gateway_client, 'ping'):
                gateway_client.ping()
            else:
                # 如果没有 ping，可以用一个轻量级 API 调用代替
                pass

            latency_ms = (time.time() - start) * 1000
            result = self.update_latency(latency_ms)

            is_healthy = not result["circuit_broken"]
            return is_healthy, latency_ms, result

        except Exception as e:
            logger.error(f"❌ Gateway health check failed: {e}")
            result = self.update_latency(99999)  # 视为极端延迟
            return False, 99999, result


# Singleton instance
latency_hedge = LatencyHedgeManager()
