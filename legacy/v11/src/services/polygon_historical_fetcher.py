"""
Polygon Historical Data Fetcher
获取历史 K 线数据（免费订阅）

支持的时间间隔：
- 1 分钟: multiplier=1, timespan='minute'
- 5 分钟: multiplier=5, timespan='minute'
- 15 分钟: multiplier=15, timespan='minute'
- 1 小时: multiplier=1, timespan='hour'
- 1 天: multiplier=1, timespan='day'
"""

import os
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any
import redis

try:
    from polygon import RESTClient
except ImportError:
    RESTClient = None

logger = logging.getLogger(__name__)


class PolygonHistoricalFetcher:
    """Polygon 历史数据获取器"""

    def __init__(self, redis_client: redis.Redis = None):
        """
        初始化 Fetcher

        Args:
            redis_client: Redis 客户端（可选）
        """
        self.api_key = os.getenv('POLYGON_API_KEY', '')

        if not self.api_key:
            raise ValueError("POLYGON_API_KEY not found in environment")

        if RESTClient is None:
            raise ImportError("polygon package not installed")

        self.client = RESTClient(self.api_key)
        self.redis = redis_client

    def fetch_bars(
        self,
        symbol: str,
        multiplier: int = 5,
        timespan: str = 'day',
        from_date: str = None,
        to_date: str = None,
        limit: int = 100,
        adjusted: bool = True
    ) -> List[Dict[str, Any]]:
        """
        获取历史 K 线数据

        Args:
            symbol: 股票代码（如 'AAPL'）
            multiplier: 时间倍数（如 5 代表 5 分钟）
            timespan: 时间单位 ('minute', 'hour', 'day', 'week', 'month')
            from_date: 开始日期（YYYY-MM-DD）
            to_date: 结束日期（YYYY-MM-DD）
            limit: 最大返回数量

        Returns:
            K 线数据列表，每个元素包含:
            {
                'timestamp': 毫秒时间戳,
                'open': 开盘价,
                'high': 最高价,
                'low': 最低价,
                'close': 收盘价,
                'volume': 成交量,
                'vwap': 成交量加权平均价
            }
        """
        try:
            # 默认获取最近 1 天的数据
            if not to_date:
                to_date = datetime.now().strftime('%Y-%m-%d')

            if not from_date:
                # 默认获取足够的数据
                from_dt = datetime.now() - timedelta(days=5)
                from_date = from_dt.strftime('%Y-%m-%d')

            logger.info(f"Fetching bars for {symbol}: {multiplier} {timespan} from {from_date} to {to_date}")

            # 调用 Polygon API
            aggs = self.client.list_aggs(
                ticker=symbol,
                multiplier=multiplier,
                timespan=timespan,
                from_=from_date,
                to=to_date,
                adjusted=adjusted,
                sort='asc'
            )

            if not aggs:
                logger.warning(f"No bars found for {symbol}")
                return []

            # 转换为统一格式
            bars = []
            for agg in aggs:
                bar = {
                    'timestamp': agg.timestamp,
                    'open': agg.open,
                    'high': agg.high,
                    'low': agg.low,
                    'close': agg.close,
                    'volume': agg.volume,
                    'vwap': getattr(agg, 'vwap', 0),
                    'symbol': symbol
                }
                bars.append(bar)

            logger.info(f"Fetched {len(bars)} bars for {symbol}")

            # 存储 Redis（如果可用）
            if self.redis:
                self._store_bars_redis(symbol, bars)

            return bars

        except Exception as e:
            logger.error(f"Error fetching bars for {symbol}: {e}")
            raise

    def fetch_latest_bars(
        self,
        symbol: str,
        count: int = 100,
        timespan: str = 'minute',
        multiplier: int = 5,
        adjusted: bool = True
    ) -> List[Dict[str, Any]]:
        """
        获取最新 N 条 K 线

        Args:
            symbol: 股票代码
            count: 条数
            timespan: 时间单位
            multiplier: 时间倍数
            adjusted: 是否调整价格（split/dividend）

        Returns:
            K 线数据列表（最新的在最后）
        """
        # 计算需要的日期范围（拉足够多的数据）
        # 预估：每个交易日 ~78 个 5-min bars (6.5 小时 * 12)
        # 需要的天数 = count / 78
        days_needed = max(5, int(count / 78) + 2)

        from_dt = datetime.now() - timedelta(days=days_needed)
        from_date = from_dt.strftime('%Y-%m-%d')
        to_date = datetime.now().strftime('%Y-%m-%d')

        bars = None
        polygon_failed = False
        try:
            bars = self.fetch_bars(
                symbol=symbol,
                multiplier=multiplier,
                timespan=timespan,
                from_date=from_date,
                to_date=to_date,
                limit=count * 3,  # 多获取一些，确保有足够数据
                adjusted=adjusted
            )
        except Exception as e:
            polygon_failed = True
            logger.warning(f"Polygon failed for {symbol} — using yfinance fallback")

        # Polygon failed or returned insufficient data — try yfinance as fallback
        if polygon_failed or not bars or len(bars) < 20:
            bars = self._fetch_yfinance_bars(symbol, multiplier=multiplier, timespan=timespan)

        # 返回最新的 N 条
        return bars[-count:] if bars and len(bars) >= count else (bars or [])

    def _store_bars_redis(self, symbol: str, bars: List[Dict[str, Any]]):
        """存储 K 线数据到 Redis"""
        try:
            key = f"bars:{symbol}"

            # 转换为 JSON
            import json
            value = json.dumps(bars)

            # 存储，TTL 24 小时
            self.redis.setex(key, 86400, value)

            logger.debug(f"Stored {len(bars)} bars to Redis: {key}")

        except Exception as e:
            logger.warning(f"Failed to store bars to Redis: {e}")

    def _fetch_yfinance_bars(
        self,
        symbol: str,
        multiplier: int = 5,
        timespan: str = 'minute',
    ) -> List[Dict[str, Any]]:
        """Fallback: get historical bars from yfinance when Polygon is rate-limited.

        P1: Uses period='1mo' then tail() to avoid weekend/holiday data gaps
        that cause insufficient bars with period='5d'.
        """
        try:
            import yfinance as yf

            interval = f'{multiplier}m' if timespan == 'minute' else '1h'
            # P1: Use 1 month to guarantee enough bars even on weekends/holidays
            df = yf.Ticker(symbol).history(period='1mo', interval=interval)
            if not df.empty and timespan == 'minute':
                # Take last 5 trading days' worth of bars for minute data
                df = df.tail(5 * 78)  # ~78 5-min bars per trading day

            if df.empty:
                logger.warning(f"yfinance: no bars for {symbol}")
                return []

            # Convert DataFrame to list of dicts matching Polygon format
            result = []
            for ts, row in df.iterrows():
                result.append({
                    'timestamp': int(ts.timestamp() * 1000),
                    'open': float(row['Open']),
                    'high': float(row['High']),
                    'low': float(row['Low']),
                    'close': float(row['Close']),
                    'volume': int(row['Volume']),
                    'vwap': 0,
                    'symbol': symbol,
                })

            logger.info(f"yfinance fallback: got {len(result)} bars for {symbol}")
            return result
        except Exception as e:
            logger.warning(f"yfinance fallback failed for {symbol}: {e}")
            return []

    def get_bars_from_redis(self, symbol: str) -> List[Dict[str, Any]]:
        """从 Redis 获取 K 线数据"""
        try:
            key = f"bars:{symbol}"
            value = self.redis.get(key)

            if not value:
                return None

            import json
            bars = json.loads(value)
            logger.debug(f"Retrieved {len(bars)} bars from Redis: {key}")
            return bars

        except Exception as e:
            logger.warning(f"Failed to retrieve bars from Redis: {e}")
            return None


def create_polygon_fetcher(redis_client: redis.Redis = None) -> PolygonHistoricalFetcher:
    """
    创建 Polygon Fetcher 实例

    Args:
        redis_client: Redis 客户端

    Returns:
        PolygonHistoricalFetcher 实例
    """
    # 如果没有提供 Redis 客户端，创建一个
    if redis_client is None:
        # 尝试从环境变量读取配置
        redis_host = os.getenv('REDIS_HOST', 'redis')
        redis_port = int(os.getenv('REDIS_PORT', 6379))

        # 如果在 Docker 容器中运行，使用容器名
        redis_client = redis.Redis(
            host=redis_host,
            port=redis_port,
            db=0,
            decode_responses=False
        )

    return PolygonHistoricalFetcher(redis_client=redis_client)


# 测试代码
if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)

    # 创建 Redis 客户端
    redis_client = redis.Redis(
        host='localhost',
        port=6380,
        db=0,
        decode_responses=False
    )

    # 创建 Fetcher
    fetcher = create_polygon_fetcher(redis_client)

    # 测试获取 AAPL 5 分钟 K 线
    print("=" * 60)
    print("🧪 测试: 获取 AAPL 5 分钟 K 线")
    print("=" * 60)

    bars = fetcher.fetch_latest_bars('AAPL', count=50, multiplier=5)

    print(f"\n✅ 获取到 {len(bars)} 条 K 线")
    print("\n最近 5 条:")
    for bar in bars[-5:]:
        dt = datetime.fromtimestamp(bar['timestamp'] / 1000)
        print(f"  {dt}: Close ${bar['close']:.2f} | Vol {bar['volume']:,}")

    # 测试 NVDA
    print("\n" + "=" * 60)
    print("🧪 测试: 获取 NVDA 5 分钟 K 线")
    print("=" * 60)

    bars = fetcher.fetch_latest_bars('NVDA', count=50, multiplier=5)

    print(f"\n✅ 获取到 {len(bars)} 条 K 线")
    print("\n最近 5 条:")
    for bar in bars[-5:]:
        dt = datetime.fromtimestamp(bar['timestamp'] / 1000)
        print(f"  {dt}: Close ${bar['close']:.2f} | Vol {bar['volume']:,}")
