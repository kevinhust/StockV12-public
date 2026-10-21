"""
Gap Risk Manager V2.1
Handles overnight gap risk for US equities.
Enhanced by Gemini Deep Review Round 2.

Features:
- Gap detection (5% threshold)
- 15-minute observation mode
- Trend-following vs Reversal gap classification
- Volatility stabilization check
"""

import time
import logging
from loguru import logger
from typing import Optional, Dict, Tuple
from datetime import datetime, time as datetime_time
from dataclasses import dataclass
from enum import Enum


class GapType(Enum):
    """跳空类型"""
    NONE = "none"
    TREND_FOLLOWING = "trend_following"  # 顺势跳空 (方向一致)
    REVERSAL = "reversal"               # 逆势跳空 (方向相反)
    EXHAUSTION = "exhaustion"           # 缺口跳空 (流动性枯竭)


@dataclass
class GapEvent:
    """跳空事件数据"""
    symbol: str
    gap_pct: float
    gap_type: GapType
    prev_close: float
    open_price: float
    timestamp: float
    observation_end_time: float
    volatility_ratio: Optional[float] = None  # ATR 比率


class GapRiskManager:
    """
    跳空风险管理器 V2.1

    在美股开盘时检测隔夜跳空，对于大幅跳空 (>5%) 进入观察模式，
    防止 RL 模型在数据分布异常时失效。
    """

    # 阈值配置
    GAP_THRESHOLD_PCT = 5.0        # 5% 跳空触发观察
    EXTREME_GAP_PCT = 10.0       # 10% 极端跳空，延长观察
    OBSERVE_DURATION_SEC = 900   # 15 分钟观察期
    EXTREME_OBSERVE_SEC = 1800  # 30 分钟极端观察期

    # 波动率阈值
    VOLATILITY_RATIO_HIGH = 2.0   # ATR 是平均 2 倍以上视为高波动
    VOLATILITY_RATIO_LOW = 0.8    # ATR 是平均 0.8 倍以下视为收敛

    def __init__(self):
        self.gapped_symbols: Dict[str, GapEvent] = {}
        self.atr_baseline: Dict[str, float] = {}  # symbol -> baseline ATR

    def detect_gap(
        self,
        symbol: str,
        prev_close: float,
        open_price: float,
        current_atr: Optional[float] = None
    ) -> Tuple[bool, GapEvent]:
        """
        检测开盘跳空

        Args:
            symbol: 股票代码
            prev_close: 前一日收盘价
            open_price: 今日开盘价
            current_atr: 当前 ATR (可选，用于波动率判断)

        Returns:
            tuple: (is_significant_gap, gap_event)
        """
        if prev_close <= 0:
            return False, None

        gap_pct = abs(open_price - prev_close) / prev_close * 100
        gap_direction = 1 if open_price > prev_close else -1

        # 判断跳空类型
        if gap_pct < self.GAP_THRESHOLD_PCT:
            return False, None

        # 确定跳空类型
        if gap_pct >= self.EXTREME_GAP_PCT:
            gap_type = GapType.EXHAUSTION
            observe_duration = self.EXTREME_OBSERVE_SEC
            logger.warning(f"🚨 EXTREME GAP detected for {symbol}: {gap_pct:.2f}%")
        else:
            gap_type = GapType.TREND_FOLLOWING  # 默认为顺势
            observe_duration = self.OBSERVE_DURATION_SEC
            logger.warning(f"⚠️ GAP detected for {symbol}: {gap_pct:.2f}%")

        # 记录基线 ATR
        if current_atr is not None:
            self.atr_baseline[symbol] = current_atr

        gap_event = GapEvent(
            symbol=symbol,
            gap_pct=gap_pct,
            gap_type=gap_type,
            prev_close=prev_close,
            open_price=open_price,
            timestamp=time.time(),
            observation_end_time=time.time() + observe_duration,
            volatility_ratio=None
        )

        self.gapped_symbols[symbol] = gap_event

        return True, gap_event

    def classify_gap_direction(self, symbol: str, rl_direction: int) -> GapType:
        """
        根据 RL 信号方向重新分类跳空类型

        Args:
            symbol: 股票代码
            rl_direction: RL 信号方向 (1=BUY, -1=SELL, 0=HOLD)

        Returns:
            GapType: 更新后的跳空类型
        """
        if symbol not in self.gapped_symbols:
            return GapType.NONE

        gap_event = self.gapped_symbols[symbol]
        gap_direction = 1 if gap_event.open_price > gap_event.prev_close else -1

        # 判断顺/逆势
        if rl_direction == 0:
            return GapType.NONE  # HOLD 不需要判断

        if rl_direction == gap_direction:
            # RL 方向与跳空方向一致 = 顺势
            gap_event.gap_type = GapType.TREND_FOLLOWING
            logger.info(f"📊 {symbol}: Trend-following gap (RL aligned)")
        else:
            # RL 方向与跳空方向相反 = 逆势
            gap_event.gap_type = GapType.REVERSAL
            logger.warning(f"⚠️ {symbol}: Reversal gap detected (RL contra-gap)")

        return gap_event.gap_type

    def is_in_observe_mode(self, symbol: str) -> Tuple[bool, Optional[float]]:
        """
        检查是否仍在观察期

        Returns:
            tuple: (is_observing, remaining_seconds)
        """
        if symbol not in self.gapped_symbols:
            return False, None

        gap_event = self.gapped_symbols[symbol]
        elapsed = time.time() - gap_event.timestamp

        if elapsed < (gap_event.observation_end_time - gap_event.timestamp):
            remaining = gap_event.observation_end_time - time.time()
            logger.info(f"🔍 {symbol} in OBSERVE mode, {remaining:.0f}s remaining")
            return True, remaining

        # 观察期结束
        logger.info(f"✅ {symbol} OBSERVE mode ended")
        del self.gapped_symbols[symbol]
        return False, None

    def check_volatility_stabilization(
        self,
        symbol: str,
        current_atr: float
    ) -> Tuple[bool, Optional[float]]:
        """
        检查波动率是否开始收敛

        在观察期内，如果 ATR 从基线的 2 倍以上降到 0.8 倍以下，
        可以提前结束观察期。

        Args:
            symbol: 股票代码
            current_atr: 当前 ATR

        Returns:
            tuple: (is_stabilized, volatility_ratio)
        """
        if symbol not in self.gapped_symbols:
            return True, None

        gap_event = self.gapped_symbols[symbol]
        baseline_atr = self.atr_baseline.get(symbol, current_atr)

        if baseline_atr <= 0:
            return True, None

        volatility_ratio = current_atr / baseline_atr
        gap_event.volatility_ratio = volatility_ratio

        # 检查是否收敛
        if volatility_ratio < self.VOLATILITY_RATIO_LOW:
            logger.info(f"✅ {symbol} volatility stabilized (ratio: {volatility_ratio:.2f}x)")
            # 提前结束观察期
            del self.gapped_symbols[symbol]
            del self.atr_baseline[symbol]
            return True, volatility_ratio

        # 检查是否仍然高波动
        if volatility_ratio > self.VOLATILITY_RATIO_HIGH:
            logger.warning(f"⚡ {symbol} still high volatility (ratio: {volatility_ratio:.2f}x)")

        return False, volatility_ratio

    def get_gap_adjusted_size(self, symbol: str, base_size: float) -> float:
        """
        根据跳空情况调整仓位大小

        Args:
            symbol: 股票代码
            base_size: 基础仓位 (0.0 ~ 1.0)

        Returns:
            float: 调整后的仓位
        """
        is_observing, remaining = self.is_in_observe_mode(symbol)

        if is_observing:
            return 0.0  # 观察期内不交易

        if symbol not in self.gapped_symbols:
            return base_size  # 无跳空，正常仓位

        gap_event = self.gapped_symbols[symbol]

        # 根据跳空类型调整
        if gap_event.gap_type == GapType.REVERSAL:
            return base_size * 0.3  # 逆势跳空，仓位降至 30%
        elif gap_event.gap_type == GapType.EXHAUSTION:
            return base_size * 0.2  # 极端跳空，仓位降至 20%
        elif gap_event.gap_type == GapType.TREND_FOLLOWING:
            return base_size * 0.7  # 顺势跳空，仓位降至 70%

        return base_size

    def should_delay_trading(self, symbol: str) -> bool:
        """
        检查是否应该延迟交易

        在以下情况下延迟交易:
        1. 仍在观察期内
        2. 波动率尚未收敛
        """
        is_observing, _ = self.is_in_observe_mode(symbol)
        if is_observing:
            return True

        # 检查波动率
        if symbol in self.gapped_symbols:
            gap_event = self.gapped_symbols[symbol]
            if gap_event.volatility_ratio and gap_event.volatility_ratio > self.VOLATILITY_RATIO_HIGH:
                return True

        return False

    def get_status_report(self) -> Dict:
        """获取所有跳空股票的状态报告"""
        report = {
            "total_gapped": len(self.gapped_symbols),
            "symbols": {}
        }

        for symbol, gap_event in self.gapped_symbols.items():
            is_observing, remaining = self.is_in_observe_mode(symbol)
            report["symbols"][symbol] = {
                "gap_pct": gap_event.gap_pct,
                "gap_type": gap_event.gap_type.value,
                "is_observing": is_observing,
                "remaining_sec": remaining,
                "volatility_ratio": gap_event.volatility_ratio
            }

        return report


# Singleton instance
gap_risk_manager = GapRiskManager()
