"""
Signal Expiry Checker V2.1
Validates signal freshness during LLM latency gap.
Enhanced by Gemini Deep Review Round 2.

Features:
- Baseline capture (YOLO conf, RL target)
- Confidence drift validation (signal decay)
- Direction reversal detection (momentum shift)
- Hard time limit (10s max)
"""

import time
import logging
from loguru import logger
from typing import Optional, Dict, Tuple
from dataclasses import dataclass
from enum import Enum


class ExpiryAction(Enum):
    """信号过期后的动作"""
    EXECUTE = "execute"       # 信号仍然有效
    DOWNGRADE = "downgrade"     # 信号衰减，降级处理
    ABORT = "abort"           # 信号失效，终止执行


    REVERSE = "reverse"         # 方向反转，反向操作


@dataclass
class SignalBaseline:
    """信号基线数据"""
    yolo_confidence: float
    rl_target: float
    yolo_pattern: str
    timestamp: float
    symbol: str


@dataclass
class SignalValidationResult:
    """信号验证结果"""
    action: ExpiryAction
    current_yolo_conf: float
    current_rl_target: float
    confidence_drift: float
    direction_reversed: bool
    signal_age_ms: float
    reason: str


    # 调整后的参数 (如果 action != ABORT)
    adjusted_size_multiplier: Optional[float] = None
    adjusted_confidence: Optional[float] = None


class SignalExpiryChecker:
    """
    信号过期检查器 V2.1

    在 LLM 延迟 (5-15s) 期间，验证信号是否仍然有效。
    防止执行过时的交易决策。
    """

    # 阈值配置
    MAX_SIGNAL_AGE_MS = 10_000      # 10 秒硬性上限
    CONFIDENCE_DRIFT_THRESHOLD = 0.2   # YOLO 置信度漂移阈值
    DIRECTION_REVERSAL_THRESHOLD = 0.3  # RL 方向反转阈值 (e.g., 0.5 -> -0.3)

    EXTREME_VOLATILITY_MS = 30_000  # 极端波动环境，放宽阈值

    # 动态阈值调整
    VOLATILITY_ADJUSTMENT_FACTOR = 0.5  # 高波动时阈值放宽 50%

    def __init__(self):
        self.baselines: Dict[str, SignalBaseline] = {}
        self.last_validation_time: Dict[str, float] = {}

    def capture_baseline(
        self,
        symbol: str,
        yolo_confidence: float,
        rl_target: float,
        yolo_pattern: str
    ) -> SignalBaseline:
        """
        在发送 LLM 请求前捕获信号基线

        必须在 LLM 调用前立即调用此方法！
        """
        baseline = SignalBaseline(
            symbol=symbol,
            yolo_confidence=yolo_confidence,
            rl_target=rl_target,
            yolo_pattern=yolo_pattern,
            timestamp=time.time()
        )

        self.baselines[symbol] = baseline
        self.last_validation_time[symbol] = baseline.timestamp

        logger.info(f"📍 Baseline captured for {symbol}: yolo={yolo_confidence:.2f}, rl={rl_target:.2f}")

        return baseline

    def validate(
        self,
        symbol: str,
        current_yolo_conf: float,
        current_rl_target: float,
        current_volatility: Optional[float] = None
    ) -> SignalValidationResult:
        """
        验证信号是否仍然有效

        应在 LLM 返回后、 Fusion 决策前调用此方法。

        Args:
            symbol: 股票代码
            current_yolo_conf: 当前 YOLO 置信度
            current_rl_target: 当前 RL 目标仓位
            current_volatility: 当前波动率 (ATR)，用于动态调整阈值

        Returns:
            SignalValidationResult: 验证结果
        """
        if symbol not in self.baselines:
            logger.warning(f"⚠️ No baseline found for {symbol}")
            return SignalValidationResult(
                action=ExpiryAction.ABORT,
                current_yolo_conf=current_yolo_conf,
                current_rl_target=current_rl_target,
                confidence_drift=1.0,
                direction_reversed=False,
                signal_age_ms=0,
                reason="No baseline captured"
            )

        baseline = self.baselines[symbol]
        signal_age_ms = (time.time() - baseline.timestamp) * 1000

        # 1. 硬性时间上限检查
        max_age = self._get_adjusted_max_age(current_volatility)
        if signal_age_ms > max_age:
            logger.warning(f"⏰ Signal EXPIRED for {symbol}: age={signal_age_ms:.0f}ms > {max_age:.0f}ms")
            return SignalValidationResult(
                action=ExpiryAction.ABORT,
                current_yolo_conf=current_yolo_conf,
                current_rl_target=current_rl_target,
                confidence_drift=0,
                direction_reversed=False,
                signal_age_ms=signal_age_ms,
                reason=f"Signal age {signal_age_ms:.0f}ms exceeds limit {max_age:.0f}ms"
            )

        # 2. YOLO 置信度漂移检查
        confidence_drift = abs(current_yolo_conf - baseline.yolo_confidence)
        drift_threshold = self._get_adjusted_drift_threshold(current_volatility)

        if confidence_drift > drift_threshold:
            logger.warning(f"📉 YOLO confidence drifted for {symbol}: {confidence_drift:.2f} > {drift_threshold:.2f}")
            return SignalValidationResult(
                action=ExpiryAction.DOWNGRADE,
                current_yolo_conf=current_yolo_conf,
                current_rl_target=current_rl_target,
                confidence_drift=confidence_drift,
                direction_reversed=False,
                signal_age_ms=signal_age_ms,
                reason=f"YOLO confidence drifted by {confidence_drift:.2f}",
                adjusted_size_multiplier=0.5,  # 降级：仓位减半
                adjusted_confidence=current_yolo_conf
            )

        # 3. RL 方向反转检查
        baseline_direction = self._get_direction(baseline.rl_target)
        current_direction = self._get_direction(current_rl_target)
        direction_reversed = False
        if baseline_direction != current_direction:
            # 方向反转 (例如从做多转为做空)
            if baseline_direction != "HOLD" and current_direction != "HOLD":
                direction_reversed = True
                logger.warning(f"🔄 Direction REVERSED for {symbol}: {baseline_direction} -> {current_direction}")
                return SignalValidationResult(
                    action=ExpiryAction.REVERSE,
                    current_yolo_conf=current_yolo_conf,
                    current_rl_target=current_rl_target,
                    confidence_drift=confidence_drift,
                    direction_reversed=True,
                    signal_age_ms=signal_age_ms,
                    reason=f"RL direction reversed from {baseline_direction} to {current_direction}",
                    adjusted_size_multiplier=0.3,  # 反向操作：仓位降至 30%
                    adjusted_confidence=0.3
                )

        # 4. 信号仍然有效
        logger.info(f"✅ Signal VALID for {symbol}: age={signal_age_ms:.0f}ms, drift={confidence_drift:.2f}")
        return SignalValidationResult(
            action=ExpiryAction.EXECUTE,
            current_yolo_conf=current_yolo_conf,
            current_rl_target=current_rl_target,
            confidence_drift=confidence_drift,
            direction_reversed=False,
            signal_age_ms=signal_age_ms,
            reason="Signal validated successfully"
        )

    def _get_direction(self, rl_target: float) -> str:
        """获取 RL 方向"""
        if rl_target > 0.1:
            return "BUY"
        elif rl_target < -0.1:
            return "SELL"
        return "HOLD"

    def _get_adjusted_max_age(self, volatility: Optional[float]) -> int:
        """根据波动率动态调整最大信号年龄"""
        base_max_age = self.MAX_SIGNAL_AGE_MS
        if volatility and volatility > 0:
            # 高波动环境放宽阈值
            adjusted = base_max_age * (1 - self.VOLATILITY_ADJUSTMENT_FACTOR * min(0.5, volatility / 100))
            return int(base_max_age * adjusted)
        return base_max_age

    def _get_adjusted_drift_threshold(self, volatility: Optional[float]) -> float:
        """根据波动率动态调整漂移阈值"""
        base_threshold = self.CONFIDENCE_DRIFT_THRESHOLD
        if volatility and volatility > 0:
            # 高波动环境放宽阈值
            return base_threshold * (1 + self.VOLATILITY_ADJUSTMENT_FACTOR * min(0.5, volatility / 100))
        return base_threshold

    def clear_baseline(self, symbol: str):
        """清除信号基线 (交易完成后调用)"""
        if symbol in self.baselines:
            del self.baselines[symbol]
            del self.last_validation_time[symbol]
            logger.debug(f"🧹 Baseline cleared for {symbol}")

    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            "active_baselines": len(self.baselines),
            "symbols": list(self.baselines.keys()),
            "oldest_baseline_age_ms": max(
                [time.time() - b.timestamp for b in self.baselines.values()]
            ) if self.baselines else 0
        }


# Singleton instance
signal_expiry_checker = SignalExpiryChecker()
