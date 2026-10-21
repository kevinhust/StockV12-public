"""
Pattern Detector
基于技术指标检测交易形态

支持的 Pattern:
- volume_spike: 成交量突增
- oversold_bounce: RSI 超卖反弹
- overbought_reversal: RSI 超买反转
- bullish_breakout: 看涨突破
- bearish_breakout: 看跌突破
- golden_cross: MACD 金叉
- death_cross: MACD 死叉
"""

import logging
import json
from typing import List, Dict, Any, Optional
from datetime import datetime
from .technical_indicators import TechnicalIndicators
from prometheus_client import Counter

logger = logging.getLogger(__name__)

# Metrics
PATTERN_PUBLISHED = Counter('patterns_published_total', 'Patterns successfully published to Kafka', ['pattern_type'])
PATTERN_PUBLISH_FAILED = Counter('patterns_publish_failed_total', 'Pattern publish failures')


class PatternDetector:
    """Pattern 检测器"""

    # Pattern 定义
    PATTERNS = {
        'volume_spike': {
            'name': '成交量突增',
            'description': '成交量突然增加，可能预示价格变动',
            'weight': 0.8
        },
        'oversold_bounce': {
            'name': '超卖反弹',
            'description': 'RSI 低于 30，可能出现反弹',
            'weight': 0.7
        },
        'overbought_reversal': {
            'name': '超买反转',
            'description': 'RSI 高于 70，可能出现回调',
            'weight': 0.7
        },
        'bullish_breakout': {
            'name': '看涨突破',
            'description': '价格突破阻力位',
            'weight': 0.75
        },
        'bearish_breakout': {
            'name': '看跌突破',
            'description': '价格跌破支撑位',
            'weight': 0.75
        },
        'golden_cross': {
            'name': 'MACD 金叉',
            'description': 'MACD 线上穿信号线，看涨信号',
            'weight': 0.6
        },
        'death_cross': {
            'name': 'MACD 死叉',
            'description': 'MACD 线下穿信号线，看跌信号',
            'weight': 0.6
        }
    }

    def __init__(self, kafka_producer=None, redis_client=None):
        """
        初始化 Pattern Detector

        Args:
            kafka_producer: Kafka Producer（可选）
            redis_client: Redis 客户端（可选）
        """
        self.kafka = kafka_producer
        self.redis = redis_client

    def detect_patterns(
        self,
        symbol: str,
        bars: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        检测 Pattern

        Args:
            symbol: 股票代码
            bars: K 线数据列表

        Returns:
            Pattern 列表，每个 Pattern 包含:
            {
                'type': pattern_type,
                'name': pattern_name,
                'price': 当前价格,
                'confidence': 置信度 (0-1),
                'timestamp': 时间戳,
                'details': 详细信息
            }
        """
        if not bars or len(bars) < 20:
            logger.warning(f"Not enough bars for {symbol} to detect patterns")
            return []

        # 计算技术指标
        indicators = TechnicalIndicators.calculate_all_indicators(bars)

        patterns = []

        # 1. 成交量突增
        vs = indicators['volume_spike']
        if vs['spike']:
            patterns.append({
                'type': 'volume_spike',
                'name': self.PATTERNS['volume_spike']['name'],
                'price': indicators['current_price'],
                'confidence': max(0.0, min(0.9, 0.6 + (vs['ratio'] - 2.0) * 0.1)),
                'timestamp': int(datetime.now().timestamp()),
                'details': {
                    'current_volume': vs['current_volume'],
                    'avg_volume': vs['avg_volume'],
                    'ratio': vs['ratio']
                }
            })

        # 2. RSI 超卖反弹
        rsi = indicators['rsi']
        if rsi is not None:
            if rsi < 30:
                patterns.append({
                    'type': 'oversold_bounce',
                    'name': self.PATTERNS['oversold_bounce']['name'],
                    'price': indicators['current_price'],
                    'confidence': min(0.85, (30 - rsi) / 30 * 0.7 + 0.4),
                    'timestamp': int(datetime.now().timestamp()),
                    'details': {
                        'rsi': rsi,
                        'oversold_level': 30
                    }
                })

            # 3. RSI 超买反转
            elif rsi > 70:
                patterns.append({
                    'type': 'overbought_reversal',
                    'name': self.PATTERNS['overbought_reversal']['name'],
                    'price': indicators['current_price'],
                    'confidence': min(0.85, (rsi - 70) / 30 * 0.7 + 0.4),
                    'timestamp': int(datetime.now().timestamp()),
                    'details': {
                        'rsi': rsi,
                        'overbought_level': 70
                    }
                })

        # 4. 价格突破
        bo = indicators['breakout']
        if bo['bullish_breakout']:
            patterns.append({
                'type': 'bullish_breakout',
                'name': self.PATTERNS['bullish_breakout']['name'],
                'price': indicators['current_price'],
                'confidence': 0.75,
                'timestamp': int(datetime.now().timestamp()),
                'details': {
                    'current_price': indicators['current_price'],
                    'resistance': bo['resistance']
                }
            })

        elif bo['bearish_breakout']:
            patterns.append({
                'type': 'bearish_breakout',
                'name': self.PATTERNS['bearish_breakout']['name'],
                'price': indicators['current_price'],
                'confidence': 0.75,
                'timestamp': int(datetime.now().timestamp()),
                'details': {
                    'current_price': indicators['current_price'],
                    'support': bo['support']
                }
            })

        # 5. MACD 金叉/死叉 (true crossover detection — requires prev bars)
        macd = indicators['macd']
        if macd['macd'] is not None and macd['signal'] is not None:
            prev_macd = macd.get('prev_macd')
            prev_signal = macd.get('prev_signal')

            # Golden cross: MACD crosses ABOVE signal line (was below, now above)
            if (prev_macd is not None and prev_signal is not None
                    and prev_macd <= prev_signal
                    and macd['macd'] > macd['signal']):
                patterns.append({
                    'type': 'golden_cross',
                    'name': self.PATTERNS['golden_cross']['name'],
                    'price': indicators['current_price'],
                    'confidence': min(0.8, macd['histogram'] + 0.5),
                    'timestamp': int(datetime.now().timestamp()),
                    'details': {
                        'macd': macd['macd'],
                        'signal': macd['signal'],
                        'histogram': macd['histogram']
                    }
                })

            # Death cross: MACD crosses BELOW signal line (was above, now below)
            elif (prev_macd is not None and prev_signal is not None
                    and prev_macd >= prev_signal
                    and macd['macd'] < macd['signal']):
                patterns.append({
                    'type': 'death_cross',
                    'name': self.PATTERNS['death_cross']['name'],
                    'price': indicators['current_price'],
                    'confidence': min(0.8, abs(macd['histogram']) + 0.5),
                    'timestamp': int(datetime.now().timestamp()),
                    'details': {
                        'macd': macd['macd'],
                        'signal': macd['signal'],
                        'histogram': macd['histogram']
                    }
                })

        logger.info(f"Detected {len(patterns)} patterns for {symbol}: {', '.join([p['type'] for p in patterns])}")

        return patterns

    def publish_pattern(
        self,
        symbol: str,
        pattern: Dict[str, Any],
        topic: str = 'events.pattern'
    ) -> bool:
        """
        发布 Pattern 到 Kafka

        Args:
            symbol: 股票代码
            pattern: Pattern 数据
            topic: Kafka topic

        Returns:
            是否发布成功
        """
        try:
            # 构建 Kafka 消息
            message = {
                'symbol': symbol,
                'pattern': pattern['type'],
                'name': pattern['name'],
                'price': pattern['price'],
                'confidence': pattern['confidence'],
                'timestamp': pattern['timestamp'],
                'details': pattern['details']
            }

            # 发送到 Kafka
            if self.kafka:
                # 检查是否是 RedpandaEventBus（有 publish 方法）
                if hasattr(self.kafka, 'publish'):
                    self.kafka.publish(topic, message)
                else:
                    # 标准 Kafka Producer
                    import json
                    self.kafka.send(
                        topic=topic,
                        key=symbol.encode('utf-8'),
                        value=json.dumps(message).encode('utf-8')
                    )
                logger.info(f"Published pattern {pattern['type']} for {symbol} to {topic}")
                PATTERN_PUBLISHED.labels(pattern_type=pattern['type']).inc()
                return True
            else:
                # 如果没有 Kafka，打印到日志
                logger.info(f"Pattern {pattern['type']} for {symbol}: {json.dumps(message, indent=2)}")
                return False

        except Exception as e:
            logger.error(f"Failed to publish pattern for {symbol}: {e}")
            PATTERN_PUBLISH_FAILED.inc()
            return False

    def store_patterns_redis(
        self,
        symbol: str,
        patterns: List[Dict[str, Any]],
        ttl: int = 300
    ):
        """
        存储 Pattern 到 Redis

        Args:
            symbol: 股票代码
            patterns: Pattern 列表
            ttl: TTL（秒）
        """
        if not self.redis:
            return

        try:
            key = f"patterns:{symbol}"

            # 转换为 JSON
            value = json.dumps(patterns)

            # 存储
            self.redis.setex(key, ttl, value)

            logger.debug(f"Stored {len(patterns)} patterns to Redis: {key}")

        except Exception as e:
            logger.warning(f"Failed to store patterns to Redis: {e}")

    def detect_and_publish(
        self,
        symbol: str,
        bars: List[Dict[str, Any]],
        topic: str = 'events.pattern'
    ) -> int:
        """
        检测 Pattern 并发布

        Args:
            symbol: 股票代码
            bars: K 线数据列表
            topic: Kafka topic

        Returns:
            发布的 Pattern 数量
        """
        # 检测 Pattern
        patterns = self.detect_patterns(symbol, bars)

        if not patterns:
            return 0

        # 存储 Redis
        self.store_patterns_redis(symbol, patterns)

        # 发布到 Kafka
        published = 0
        for pattern in patterns:
            if self.publish_pattern(symbol, pattern, topic):
                published += 1

        return published


# 测试代码
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    # 模拟 K 线数据
    bars = []
    for i in range(30):
        bars.append({
            'open': 100 + i * 0.3,
            'high': 100 + i * 0.3 + 2,
            'low': 100 + i * 0.3 - 2,
            'close': 100 + i * 0.3,
            'volume': 1000 + i * 100,
            'symbol': 'TEST'
        })

    # 添加成交量突增
    bars[-1]['volume'] = 5000

    # 创建 Detector
    detector = PatternDetector()

    # 检测 Pattern
    patterns = detector.detect_patterns('TEST', bars)

    print("=" * 70)
    print("🎯 Pattern 检测结果")
    print("=" * 70)
    print()

    print(f"检测到 {len(patterns)} 个 Pattern:")
    print()

    for pattern in patterns:
        print(f"📊 {pattern['name']} (置信度: {pattern['confidence']:.2f})")
        print(f"   类型: {pattern['type']}")
        print(f"   价格: ${pattern['price']:.2f}")
        print(f"   详情: {json.dumps(pattern['details'], indent=4)}")
        print()
