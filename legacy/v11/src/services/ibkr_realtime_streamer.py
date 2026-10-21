"""
IBKR Realtime Streamer
实时行情推送（使用 IB Gateway）

功能：
- 订阅股票实时行情
- 监听价格/成交量更新
- 更新 Redis 缓存
- 自动重连 + 主动探测（is_alive）
"""

import os
import logging
import asyncio
import time
import json
import redis
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional
from datetime import datetime
from ib_insync import *

import numpy as np
from dataclasses import dataclass

from core.ib import IBConnection

logger = logging.getLogger(__name__)


@dataclass
class IBConfig:
    """IB Gateway 配置"""
    import os
    host: str = os.getenv('IB_HOST', '127.0.0.1')
    port: int = int(os.getenv('IB_PORT', 4002))
    client_id: int = int(os.getenv('IB_CLIENT_ID', 110))  # CID 110 for streamer


class IBKRRealtimeStreamer:
    """IB 实时行情推送器（使用 IBConnection 封装）"""

    def __init__(
        self,
        config: IBConfig = None,
        redis_client: redis.Redis = None
    ):
        """
        初始化 Streamer

        Args:
            config: IB Gateway 配置
            redis_client: Redis 客户端
        """
        self.config = config or IBConfig()
        self.redis = redis_client

        # IBConnection wraps ib_insync.IB with proper lifecycle management:
        # - Automatic reconnection with exponential backoff
        # - is_alive() active liveness probe (fixes isConnected() false positive)
        # - Proper socket cleanup (prevents CLOSE_WAIT on Gateway)
        self._ib_conn = IBConnection(
            host=self.config.host,
            port=self.config.port,
            client_id=self.config.client_id,
            name='streamer',
            timeout=20,
            reconnect=True,
            probe_interval=60,
        )
        # Expose raw ib_insync IB instance for ticker subscriptions
        self.ib: IB = self._ib_conn.ib
        self.connected = False
        self.tickers: Dict[str, Ticker] = {}
        self._last_data_time = time.time()
        self._connection_lost = False

        # Persistent ib_insync event loop thread (prevents socket close on loop exit)
        self._ib_thread: Optional[threading.Thread] = None
        self._ib_loop: Optional[asyncio.AbstractEventLoop] = None

        # Wire IBConnection disconnect event to streamer's state
        self._ib_conn.ib.disconnectedEvent += self._on_ib_disconnected
        self._init_done = True  # Guard: suppress spurious logs during __init__

        # Thread pool for offloading Redis I/O from the IB callback thread
        self._redis_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ibkr_redis")

        logger.info(f"IBKR Realtime Streamer initialized: {self.config.host}:{self.config.port} (CID={self.config.client_id})")

    async def connect(self) -> bool:
        """连接到 IB Gateway via IBConnection (thread-safe).

        Creates a persistent background thread with a dedicated event loop for
        ib_insync. This is the only pattern that works reliably:
        - The loop must stay alive (run_forever) to keep the asyncio transport open
        - Closing the loop closes the socket and kills all ticker callbacks
        - Callbacks fire in the ib thread, we offload Redis I/O to ThreadPoolExecutor
        """
        try:
            def _ib_thread_target():
                """Persistent thread: loop must stay alive for asyncio transport."""
                self._ib_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(self._ib_loop)
                try:
                    ok = self._ib_conn.connect()
                    if ok:
                        self._ib_loop.run_forever()  # Keep alive = keep socket open
                except Exception as e:
                    logger.error(f"IB thread error: {e}")
                finally:
                    self._ib_loop.close()

            # Always create a fresh thread for connect — avoids deadlock from
            # trying to run run_until_complete() on a loop already running run_forever()
            self._ib_thread = threading.Thread(
                target=_ib_thread_target,
                daemon=True,
                name="ibkr_ib_thread",
            )
            self._ib_thread.start()

            # Wait for connection to establish
            for _ in range(40):  # up to 4s
                await asyncio.sleep(0.1)
                if self._ib_conn.ib.isConnected():
                    break
            else:
                logger.error("IB Gateway connection timed out")
                return False

            self.connected = True
            self._connection_lost = False
            self._init_done = True
            self._last_data_time = time.time()

            md_type = int(os.getenv('IB_MARKET_DATA_TYPE', 3))
            md_label = self._ib_conn.get_market_data_type() or f"Delayed"
            logger.info(f"✅ Connected to IB Gateway: {self.config.host}:{self.config.port} MD={md_label}")

            # P3: Log market data type for operational visibility
            md_type_names = {1: "LIVE", 2: "DELAYED", 3: "DELAYED_FROZEN", 4: "DELAYED"}
            md_type_name = md_type_names.get(md_type, f"UNKNOWN({md_type})")
            logger.info(f"📊 Market data type: {md_type_name} (IB_MARKET_DATA_TYPE={md_type})")

            return True

        except Exception as e:
            logger.error(f"❌ Failed to connect to IB Gateway: {e}")
            self.connected = False
            return False

    async def subscribe(self, symbols: List[str]) -> Dict[str, Ticker]:
        """
        订阅股票实时行情

        Args:
            symbols: 股票代码列表

        Returns:
            symbol -> Ticker 字典
        """
        if not self.connected:
            logger.error("Not connected to IB Gateway")
            return {}

        self.tickers = {}

        for symbol in symbols:
            try:
                contract = Stock(symbol, 'SMART', 'USD')
                ticker = self.ib.reqMktData(contract, '', False, False)
                self.tickers[symbol] = ticker

                # Capture symbol by default argument to avoid closure capture issue
                ticker.updateEvent += lambda t, s=symbol: self._on_ticker_update(t, s)
                logger.info(f"✅ Subscribed to {symbol}")

            except Exception as e:
                logger.error(f"❌ Failed to subscribe to {symbol}: {e}")

        return self.tickers

    def _on_ticker_update(self, ticker: Ticker, symbol: str):
        """处理 Ticker 更新事件 (运行在 IB 回调线程，非 asyncio 线程).

        Redis 操作通过 ThreadPoolExecutor 异步执行，避免阻塞 IB Gateway socket.

        P0: Price fallback chain: last > marketPrice() > bid-ask mid > skip.
        Never fall back to ticker.close (previous day's close) during intraday.
        """
        try:
            self._last_data_time = time.time()

            # P0: Correct price fallback chain
            bid = ticker.bid if not np.isnan(ticker.bid) else None
            ask = ticker.ask if not np.isnan(ticker.ask) else None
            volume = ticker.volume if not np.isnan(ticker.volume) else None

            price = None
            if not np.isnan(ticker.last) and ticker.last > 0:
                price = ticker.last
            elif hasattr(ticker, 'marketPrice') and ticker.marketPrice() and not np.isnan(ticker.marketPrice()):
                price = ticker.marketPrice()
            elif bid is not None and ask is not None and bid > 0 and ask > 0:
                price = (bid + ask) / 2

            if not self.redis or price is None:
                return

            data = {
                'symbol': symbol,
                'price': float(price),
                'bid': float(bid) if bid is not None else None,
                'ask': float(ask) if ask is not None else None,
                'volume': int(volume) if volume is not None else None,
                'timestamp': time.time(),
                'source': 'ibkr',
            }

            self._redis_executor.submit(self._write_price_to_redis, symbol, data)
            logger.debug(f"Queued Redis write for {symbol}: ${price:.2f}")

        except Exception as e:
            logger.error(f"Error handling ticker update for {symbol}: {e}")

    def _write_price_to_redis(self, symbol: str, data: dict):
        """同步写入 Redis（在 ThreadPoolExecutor 线程中运行）.

        P0: Dynamic TTL — 60s intraday (09:30-16:00 ET), 300s otherwise.
        """
        try:
            key = f"price:{symbol}:current"
            # Dynamic TTL based on market hours (ET)
            from zoneinfo import ZoneInfo
            now_et = datetime.now(ZoneInfo("America/New_York"))
            is_intraday = 9 <= now_et.hour < 16 or (now_et.hour == 9 and now_et.minute >= 30)
            ttl = 60 if is_intraday else 300
            self.redis.setex(key, ttl, json.dumps(data))
            self.redis.publish("market_data:updates", json.dumps(data))
        except Exception as e:
            logger.error(f"Redis write failed for {symbol}: {e}")

    def _on_ib_disconnected(self):
        """Callback: ib_insync reports disconnection via event.

        Only logs when init is done AND we were actually connected.
        Suppresses spurious logs from __init__ and IBConnection's own events.
        """
        if (getattr(self, '_init_done', False) and self.connected
                and not getattr(self._ib_conn, '_shutdown', False)):
            logger.warning("🔌 IB disconnected — will reconnect")

    def get_current_price(self, symbol: str) -> Optional[float]:
        """获取当前价格. P0: Same fallback chain as _on_ticker_update."""
        ticker = self.tickers.get(symbol)
        if not ticker:
            return None
        if not np.isnan(ticker.last) and ticker.last > 0:
            return ticker.last
        if hasattr(ticker, 'marketPrice') and ticker.marketPrice() and not np.isnan(ticker.marketPrice()):
            return ticker.marketPrice()
        bid = ticker.bid if not np.isnan(ticker.bid) else None
        ask = ticker.ask if not np.isnan(ticker.ask) else None
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            return (bid + ask) / 2
        return None

    def disconnect(self):
        """断开连接 — stops the ib thread and cleans up the socket."""
        # Stop the event loop so the thread exits cleanly
        if self._ib_loop and self._ib_loop.is_running():
            self._ib_loop.call_soon_threadsafe(self._ib_loop.stop)

        self._ib_conn.disconnect()
        self.connected = False
        self._connection_lost = False
        logger.info("👋 Disconnected from IB Gateway")


def get_watchlist_from_redis(redis_client: redis.Redis, top_n: int = 10) -> list:
    """
    从 Redis 获取 watchlist

    Priority:
    1. watchlist:ranked (Market Analyst 每日扫描结果)
    2. watchlist:current (当前活跃 watchlist)
    3. Default fallback

    Args:
        redis_client: Redis 客户端
        top_n: 取前 N 个股票

    Returns:
        股票代码列表
    """
    try:
        # 1. Try watchlist:tier2 first (live selection pipeline)
        tier2 = redis_client.zrevrange("watchlist:tier2", 0, top_n - 1)
        if tier2:
            logger.info(f"✅ Found watchlist:tier2 — {tier2}")
            return list(tier2)

        # 2. Try watchlist:ranked (bridge output)
        ranked = redis_client.get("watchlist:ranked")
        if ranked:
            logger.info("✅ Found watchlist:ranked")
            ranked_data = json.loads(ranked)
            if isinstance(ranked_data, dict):
                watchlist = ranked_data.get("watchlist", [])
                symbols = [item.get("symbol") for item in watchlist[:top_n]]
                logger.info(f"📊 Top {len(symbols)} symbols from ranked watchlist: {symbols}")
                return symbols

        # 3. Try watchlist:current (bridge output)
        current = redis_client.get("watchlist:current")
        if current:
            logger.info("✅ Found watchlist:current")
            current_data = json.loads(current)
            if isinstance(current_data, list):
                symbols = [item.get("symbol") if isinstance(item, dict) else item
                          for item in current_data[:top_n]]
                logger.info(f"📊 Top {len(symbols)} symbols from current watchlist: {symbols}")
                return symbols

        # 4. Default fallback
        logger.warning("⚠️ No watchlist found in Redis, using default")
        return ["NVDA", "TSLA", "AAPL", "MSFT", "GOOGL"]

    except Exception as e:
        logger.error(f"❌ Failed to get watchlist from Redis: {e}")
        return ["NVDA", "TSLA", "AAPL", "MSFT", "GOOGL"]


# 测试代码
async def main():
    """Service Mode"""
    import os
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    logger.info("🚀 Starting IBKR Realtime Streamer Service...")

    # Create Redis Client
    redis_host = os.getenv('REDIS_HOST', 'localhost')
    redis_port = int(os.getenv('REDIS_PORT', 6379))
    redis_client = redis.Redis(host=redis_host, port=redis_port, db=0, decode_responses=True)

    # Create Streamer (uses IBConnection internally)
    streamer = IBKRRealtimeStreamer(redis_client=redis_client)

    top_n = int(os.getenv('WATCHLIST_TOP_N', '10'))
    logger.info(f"📊 Fetching top {top_n} stocks from watchlist...")

    STALE_THRESHOLD = 300   # 5 min — no data = force reconnect
    HEARTBEAT_INTERVAL = 30  # Check every 30s
    MD_TYPE_LOG_INTERVAL = 3600  # P3: Log market data type every hour

    while True:
        if await streamer.connect():
            symbols = get_watchlist_from_redis(redis_client, top_n=top_n)
            await streamer.subscribe(symbols)

            # Monitor with is_alive() (active probe) + data staleness
            last_md_log = time.time()
            try:
                while streamer._ib_conn.is_alive() and not streamer._connection_lost:
                    await asyncio.sleep(HEARTBEAT_INTERVAL)
                    stale_secs = time.time() - streamer._last_data_time

                    # P3: Periodic market data type logging (every hour)
                    now = time.time()
                    if now - last_md_log >= MD_TYPE_LOG_INTERVAL:
                        md_type = int(os.getenv('IB_MARKET_DATA_TYPE', 3))
                        md_type_names = {1: "LIVE", 2: "DELAYED", 3: "DELAYED_FROZEN", 4: "DELAYED"}
                        logger.info(f"📊 [hourly] Market data type: {md_type_names.get(md_type, 'UNKNOWN')}")
                        last_md_log = now

                    if stale_secs > STALE_THRESHOLD:
                        logger.error(
                            f"🔌 DATA STALE: No ticker updates for {stale_secs:.0f}s "
                            f"(threshold={STALE_THRESHOLD}s) — forcing reconnect"
                        )
                        streamer.disconnect()
                        break

                    if stale_secs > 120:
                        logger.warning(
                            f"⚠️ Data gap: no updates for {stale_secs:.0f}s — "
                            f"connection may be silently dead"
                        )
                    else:
                        logger.debug(f"💓 Heartbeat OK (last data {stale_secs:.0f}s ago)")

            except Exception as e:
                logger.error(f"Connection lost: {e}")

        # IBConnection handles exponential backoff for reconnection
        # But we still need to give it time before retry
        logger.warning("🔌 Connection lost or failed, retrying in 10s...")
        await asyncio.sleep(10)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Service stopped by user")
