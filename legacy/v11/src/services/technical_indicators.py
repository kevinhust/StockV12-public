"""
Technical Indicators Calculator
技术指标计算器：RSI, MACD, ATR, 布林带等
"""

import numpy as np
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class TechnicalIndicators:
    """技术指标计算器"""

    @staticmethod
    def calculate_sma(prices: List[float], period: int) -> List[float]:
        """
        计算简单移动平均线（SMA）

        Args:
            prices: 价格列表
            period: 周期

        Returns:
            SMA 列表
        """
        if len(prices) < period:
            return []

        sma = []
        for i in range(period - 1, len(prices)):
            avg = sum(prices[i - period + 1:i + 1]) / period
            sma.append(avg)

        return sma

    @staticmethod
    def calculate_ema(prices: List[float], period: int) -> List[float]:
        """
        计算指数移动平均线（EMA）

        Args:
            prices: 价格列表
            period: 周期

        Returns:
            EMA 列表
        """
        if len(prices) < period:
            return []

        # 初始 SMA
        ema = [sum(prices[:period]) / period]

        # 计算后续 EMA
        multiplier = 2 / (period + 1)
        for i in range(period, len(prices)):
            ema.append((prices[i] - ema[-1]) * multiplier + ema[-1])

        return ema

    @staticmethod
    def calculate_rsi(prices: List[float], period: int = 14) -> Optional[float]:
        """
        计算相对强弱指数（RSI）— Wilder 指数平滑法

        Args:
            prices: 价格列表
            period: 周期（默认 14）

        Returns:
            RSI 值（0-100），如果数据不足返回 None
        """
        if len(prices) < period + 1:
            return None

        # 计算价格变化
        deltas = np.diff(prices)

        # 分离涨跌
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        # Wilder 指数平滑: first avg = SMA of first `period` values
        avg_gain = float(np.mean(gains[:period]))
        avg_loss = float(np.mean(losses[:period]))

        # Then exponential smoothing for the rest
        alpha = 1.0 / period
        for i in range(period, len(gains)):
            avg_gain = avg_gain * (1 - alpha) + gains[i] * alpha
            avg_loss = avg_loss * (1 - alpha) + losses[i] * alpha

        # 避免除零
        if avg_loss == 0:
            return 100.0

        # 计算 RSI
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return round(rsi, 2)

    @staticmethod
    def calculate_macd(
        prices: List[float],
        fast_period: int = 12,
        slow_period: int = 26,
        signal_period: int = 9
    ) -> Dict[str, Optional[float]]:
        """
        计算 MACD（移动平均收敛散度）

        Args:
            prices: 价格列表
            fast_period: 快线周期（默认 12）
            slow_period: 慢线周期（默认 26）
            signal_period: 信号线周期（默认 9）

        Returns:
            {
                'macd': MACD 值,
                'signal': 信号线,
                'histogram': 柱状图（MACD - Signal）
            }
        """
        if len(prices) < slow_period:
            return {'macd': None, 'signal': None, 'histogram': None}

        # Compute full EMA series for both fast and slow periods
        fast_ema = TechnicalIndicators.calculate_ema(prices, fast_period)
        slow_ema = TechnicalIndicators.calculate_ema(prices, slow_period)

        if not fast_ema or not slow_ema:
            return {'macd': None, 'signal': None, 'histogram': None}

        # Align: fast_ema starts at index fast_period-1, slow_ema at slow_period-1.
        # Slice fast_ema to match slow_ema temporally (they overlap from
        # slow_period-1 onwards). fast_ema[14:] aligns with slow_ema[0:].
        offset = slow_period - fast_period  # = 14 for standard 12/26 MACD
        macd_series = [f - s for f, s in zip(fast_ema[offset:], slow_ema)]

        # Need at least signal_period bars to compute the signal EMA
        if len(macd_series) < signal_period:
            return {'macd': None, 'signal': None, 'histogram': None}

        # Signal line = EMA(signal_period) of the MACD series
        signal_ema = TechnicalIndicators.calculate_ema(macd_series, signal_period)

        macd_value = macd_series[-1]
        signal_value = signal_ema[-1] if signal_ema else macd_value * 0.8  # fallback only if insufficient data
        histogram = macd_value - signal_value

        # Previous bar values for crossover detection
        prev_macd = macd_series[-2] if len(macd_series) > 1 else None
        prev_signal = signal_ema[-2] if len(signal_ema) > 1 else None

        return {
            'macd': round(macd_value, 2),
            'signal': round(signal_value, 2),
            'histogram': round(histogram, 2),
            'prev_macd': round(prev_macd, 2) if prev_macd is not None else None,
            'prev_signal': round(prev_signal, 2) if prev_signal is not None else None,
        }

    @staticmethod
    def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
        """
        计算平均真实波动范围（ATR）

        Args:
            highs: 最高价列表
            lows: 最低价列表
            closes: 收盘价列表
            period: 周期（默认 14）

        Returns:
            ATR 值，如果数据不足返回 None
        """
        if len(closes) < period + 1:
            return None

        # 计算真实波动范围
        tr_values = []
        for i in range(1, len(closes)):
            high_low = highs[i] - lows[i]
            high_close_prev = abs(highs[i] - closes[i-1])
            low_close_prev = abs(lows[i] - closes[i-1])

            tr = max(high_low, high_close_prev, low_close_prev)
            tr_values.append(tr)

        # ATR = TR 的移动平均
        atr = np.mean(tr_values[-period:])

        return round(atr, 2)

    @staticmethod
    def calculate_bollinger_bands(
        prices: List[float],
        period: int = 20,
        std_dev: float = 2.0
    ) -> Dict[str, Optional[float]]:
        """
        计算布林带

        Args:
            prices: 价格列表
            period: 周期（默认 20）
            std_dev: 标准差倍数（默认 2.0）

        Returns:
            {
                'upper': 上轨,
                'middle': 中轨（SMA）,
                'lower': 下轨,
                'bandwidth': 带宽百分比
            }
        """
        if len(prices) < period:
            return {'upper': None, 'middle': None, 'lower': None, 'bandwidth': None}

        # 中轨 = SMA
        middle = sum(prices[-period:]) / period

        # 标准差
        std = np.std(prices[-period:])

        # 上下轨
        upper = middle + (std_dev * std)
        lower = middle - (std_dev * std)

        # 带宽
        if lower > 0:
            bandwidth = (upper - lower) / middle * 100
        else:
            bandwidth = 0

        return {
            'upper': round(upper, 2),
            'middle': round(middle, 2),
            'lower': round(lower, 2),
            'bandwidth': round(bandwidth, 2)
        }

    @staticmethod
    def detect_volume_spike(volumes: List[float], multiplier: float = 2.0, period: int = 20) -> Dict[str, Any]:
        """
        检测成交量突增

        Args:
            volumes: 成交量列表
            multiplier: 倍数（默认 2.0）
            period: 周期（默认 20）

        Returns:
            {
                'spike': bool,  # 是否突增
                'current_volume': 当前成交量,
                'avg_volume': 平均成交量,
                'ratio': 当前 / 平均
            }
        """
        if len(volumes) < period:
            return {
                'spike': False,
                'current_volume': 0,
                'avg_volume': 0,
                'ratio': 0
            }

        current_volume = volumes[-1]
        avg_volume = np.mean(volumes[-period:])

        # 避免除零
        if avg_volume == 0:
            ratio = 0
        else:
            ratio = current_volume / avg_volume

        spike = ratio >= multiplier

        return {
            'spike': spike,
            'current_volume': int(current_volume),
            'avg_volume': int(avg_volume),
            'ratio': round(ratio, 2)
        }

    @staticmethod
    def detect_breakout(prices: List[float], period: int = 20) -> Dict[str, Any]:
        """
        检测价格突破

        Args:
            prices: 价格列表
            period: 周期（默认 20）

        Returns:
            {
                'bullish_breakout': bool,  # 看涨突破
                'bearish_breakout': bool,  # 看跌突破
                'resistance': 阻力位,
                'support': 支撑位
            }
        """
        if len(prices) < period:
            return {
                'bullish_breakout': False,
                'bearish_breakout': False,
                'resistance': 0,
                'support': 0
            }

        current_price = prices[-1]
        resistance = max(prices[-period:-1])  # 上一周期的最高价（不包括当前）
        support = min(prices[-period:-1])  # 上一周期的最低价

        # 看涨突破：价格突破阻力位
        bullish_breakout = current_price > resistance

        # 看跌突破：价格跌破支撑位
        bearish_breakout = current_price < support

        return {
            'bullish_breakout': bullish_breakout,
            'bearish_breakout': bearish_breakout,
            'resistance': round(resistance, 2),
            'support': round(support, 2)
        }

    @staticmethod
    def calculate_all_indicators(bars: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        计算所有技术指标

        Args:
            bars: K 线数据列表

        Returns:
            所有指标的字典
        """
        if not bars:
            return {}

        # 提取价格和成交量
        closes = [bar['close'] for bar in bars]
        highs = [bar['high'] for bar in bars]
        lows = [bar['low'] for bar in bars]
        volumes = [bar['volume'] for bar in bars]

        # 计算所有指标
        indicators = {
            'timestamp': datetime.now().timestamp(),
            'symbol': bars[-1].get('symbol', 'UNKNOWN'),
            'current_price': closes[-1],

            # RSI
            'rsi': TechnicalIndicators.calculate_rsi(closes, period=14),

            # MACD
            'macd': TechnicalIndicators.calculate_macd(closes),

            # ATR
            'atr': TechnicalIndicators.calculate_atr(highs, lows, closes, period=14),

            # 布林带
            'bollinger': TechnicalIndicators.calculate_bollinger_bands(closes, period=20, std_dev=2.0),

            # 成交量突增
            'volume_spike': TechnicalIndicators.detect_volume_spike(volumes, multiplier=2.0, period=20),

            # 价格突破
            'breakout': TechnicalIndicators.detect_breakout(closes, period=20)
        }

        return indicators


# 测试代码
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    # 模拟 K 线数据
    bars = [
        {
            'open': 100, 'high': 105, 'low': 98, 'close': 102, 'volume': 1000
        }
        for _ in range(30)
    ]

    # 添加一些波动
    for i in range(30):
        bars[i]['close'] = 100 + i * 0.5
        bars[i]['high'] = bars[i]['close'] + 2
        bars[i]['low'] = bars[i]['close'] - 2
        bars[i]['volume'] = 1000 + i * 100

    # 计算指标
    indicators = TechnicalIndicators.calculate_all_indicators(bars)

    print("=" * 70)
    print("📊 技术指标结果")
    print("=" * 70)
    print()
    print(f"当前价格: ${indicators['current_price']:.2f}")
    print(f"RSI: {indicators['rsi']}")
    print()
    print("MACD:")
    print(f"  MACD: {indicators['macd']['macd']}")
    print(f"  Signal: {indicators['macd']['signal']}")
    print(f"  Histogram: {indicators['macd']['histogram']}")
    print()
    print("ATR:", indicators['atr'])
    print()
    print("布林带:")
    print(f"  上轨: ${indicators['bollinger']['upper']:.2f}")
    print(f"  中轨: ${indicators['bollinger']['middle']:.2f}")
    print(f"  下轨: ${indicators['bollinger']['lower']:.2f}")
    print(f"  带宽: {indicators['bollinger']['bandwidth']}%")
    print()
    print("成交量:")
    print(f"  突增: {indicators['volume_spike']['spike']}")
    print(f"  当前: {indicators['volume_spike']['current_volume']:,}")
    print(f"  平均: {indicators['volume_spike']['avg_volume']:,}")
    print(f"  比例: {indicators['volume_spike']['ratio']:.2f}x")
    print()
    print("突破:")
    print(f"  看涨突破: {indicators['breakout']['bullish_breakout']}")
    print(f"  看跌突破: {indicators['breakout']['bearish_breakout']}")
    print(f"  阻力位: ${indicators['breakout']['resistance']:.2f}")
    print(f"  支撑位: ${indicators['breakout']['support']:.2f}")
