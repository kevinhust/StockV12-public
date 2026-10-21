"""
Longbridge Realtime Streamer (via longport SDK)
主力实时行情推送 — Nasdaq Basic 实时数据

功能：
- longport QuoteContext WebSocket → on_quote callback → Redis
- 复用 IBKR streamer 的 Redis 数据格式
- 自动重连 + 数据过期检测
- 每 5 分钟动态调整订阅列表
"""

import os
import logging
import time
import json
import redis
import signal
import sys
from typing import List, Optional

from longport.openapi import Config, QuoteContext, SubType, PushQuote

logger = logging.getLogger(__name__)

# Always-streamed symbols for regime detection and monitoring.
# SPY: data-freshness canary + regime detection.
# VIXY: monitoring reference only — NOT a substitute for ^VIX index (different scale, roll decay).
CANARY_SYMBOLS = ["SPY", "VIXY"]

# Reuse the same watchlist resolution logic as IBKR streamer
from services.ibkr_realtime_streamer import get_watchlist_from_redis


class LongbridgeRealtimeStreamer:
    """Longport (Longbridge) real-time quote streamer → Redis."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self.ctx: Optional[QuoteContext] = None
        self._last_data_time = time.time()
        self._subscribed_symbols: List[str] = []
        self._shutdown = False

    def connect(self) -> bool:
        """Create QuoteContext from env vars and set callback.

        Uses LONGBRIDGE_* env vars (same as LongbridgeExecutor) and passes them
        to the Config constructor explicitly. Config.from_env() reads LONGPORT_*
        prefix which doesn't match our .env.nuc convention.
        """
        try:
            # Clean up previous context if reconnecting
            if self.ctx and self._subscribed_symbols:
                try:
                    self.unsubscribe(self._subscribed_symbols)
                except Exception:
                    pass
            self.ctx = None

            app_key = os.getenv("LONGBRIDGE_APP_KEY")
            app_secret = os.getenv("LONGBRIDGE_APP_SECRET")
            access_token = os.getenv("LONGBRIDGE_ACCESS_TOKEN")

            if not (app_key and app_secret and access_token):
                logger.error("Missing LONGBRIDGE_APP_KEY/SECRET/ACCESS_TOKEN env vars")
                return False

            config = Config(app_key, app_secret, access_token)
            self.ctx = QuoteContext(config)
            self.ctx.set_on_quote(self._on_quote)
            logger.info("Connected to Longbridge QuoteContext (WebSocket)")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Longbridge: {e}")
            return False

    def _on_quote(self, symbol: str, event: PushQuote):
        """WebSocket push callback — writes price to Redis.

        Runs in the SDK's internal thread, so Redis writes must be fast.
        PushQuote fields: last_done, volume, timestamp, open, high, low, turnover
        Basic quote does NOT include bid/ask.
        """
        try:
            self._last_data_time = time.time()

            price = event.last_done
            if price is None or price <= 0:
                return

            # Strip .US suffix for internal use (e.g. "NVDA.US" → "NVDA")
            symbol_clean = symbol.replace(".US", "")

            data = {
                "symbol": symbol_clean,
                "price": float(price),
                "bid": None,
                "ask": None,
                "volume": int(event.volume) if event.volume else None,
                "timestamp": time.time(),
                "source": "longbridge",
            }

            self._write_price_to_redis(symbol_clean, data)
            logger.debug(f"Longbridge quote: {symbol_clean} ${price:.2f}")

        except Exception as e:
            logger.error(f"Error in on_quote for {symbol}: {e}")

    def _write_price_to_redis(self, symbol: str, data: dict):
        """Write price to Redis — same format as IBKR streamer.

        TTL: 60s (real-time data is always fresh within seconds).
        """
        try:
            key = f"price:{symbol}:current"
            self.redis.setex(key, 60, json.dumps(data))
            self.redis.publish("market_data:updates", json.dumps(data))
        except Exception as e:
            logger.error(f"Redis write failed for {symbol}: {e}")

    def subscribe(self, symbols: List[str]):
        """Subscribe to real-time quotes. Symbols should NOT have .US suffix."""
        if not self.ctx:
            logger.error("QuoteContext not initialized")
            return

        # Longport uses "SYMBOL.US" format for US stocks
        lb_symbols = [f"{s}.US" for s in symbols]

        try:
            self.ctx.subscribe(lb_symbols, [SubType.Quote])
            self._subscribed_symbols = symbols
            logger.info(f"Subscribed to {len(symbols)} symbols: {symbols}")
        except Exception as e:
            logger.error(f"Subscribe failed: {e}")

    def unsubscribe(self, symbols: List[str]):
        """Unsubscribe from quotes."""
        if not self.ctx:
            return
        lb_symbols = [f"{s}.US" for s in symbols]
        try:
            self.ctx.unsubscribe(lb_symbols, [SubType.Quote])
        except Exception as e:
            logger.error(f"Unsubscribe failed: {e}")

    def shutdown(self):
        """Graceful shutdown — unsubscribe and release QuoteContext."""
        self._shutdown = True
        if self.ctx and self._subscribed_symbols:
            try:
                self.unsubscribe(self._subscribed_symbols)
            except Exception:
                pass
        self.ctx = None
        logger.info("Longbridge streamer shutting down")


def main():
    """Service loop — runs until SIGTERM/SIGINT."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    logger.info("Starting Longbridge Realtime Streamer Service...")

    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_port = int(os.getenv("REDIS_PORT", 6379))
    redis_client = redis.Redis(host=redis_host, port=redis_port, db=0, decode_responses=True)

    top_n = int(os.getenv("WATCHLIST_TOP_N", "10"))

    STALE_THRESHOLD = 300     # 5 min — no data → reconnect
    HEARTBEAT_INTERVAL = 30   # Check every 30s
    WATCHLIST_REFRESH = 300   # Re-check watchlist every 5 min

    streamer = LongbridgeRealtimeStreamer(redis_client)

    # Graceful shutdown on SIGTERM (Docker stop) / SIGINT (Ctrl-C)
    def _signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        streamer.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    while not streamer._shutdown:
        if not streamer.connect():
            logger.warning("Connection failed, retrying in 10s...")
            time.sleep(10)
            continue

        # Initial subscription — always include canary symbols
        symbols = get_watchlist_from_redis(redis_client, top_n=top_n)
        symbols = list(set(symbols + CANARY_SYMBOLS))
        streamer.subscribe(symbols)

        last_watchlist_refresh = time.time()

        try:
            while not streamer._shutdown:
                time.sleep(HEARTBEAT_INTERVAL)
                stale_secs = time.time() - streamer._last_data_time

                # Check if we need to reconnect (stale data)
                if stale_secs > STALE_THRESHOLD:
                    logger.error(
                        f"DATA STALE: No Longbridge updates for {stale_secs:.0f}s "
                        f"(threshold={STALE_THRESHOLD}s) — forcing reconnect"
                    )
                    break

                if stale_secs > 120:
                    logger.warning(
                        f"Data gap: no Longbridge updates for {stale_secs:.0f}s"
                    )
                else:
                    logger.debug(f"Heartbeat OK (last data {stale_secs:.0f}s ago)")

                # Periodic watchlist refresh
                now = time.time()
                if now - last_watchlist_refresh >= WATCHLIST_REFRESH:
                    new_symbols = get_watchlist_from_redis(redis_client, top_n=top_n)
                    new_symbols = list(set(new_symbols + CANARY_SYMBOLS))
                    old_set = set(streamer._subscribed_symbols)
                    new_set = set(new_symbols)
                    if new_set != old_set:
                        added = new_set - old_set
                        removed = old_set - new_set
                        logger.info(
                            f"Watchlist changed: +{list(added)} -{list(removed)}"
                        )
                        if removed:
                            streamer.unsubscribe(list(removed))
                        if added:
                            streamer.subscribe(list(added))
                        # Keep full list for next diff
                        streamer._subscribed_symbols = new_symbols
                    last_watchlist_refresh = now

        except Exception as e:
            logger.error(f"Longbridge streamer error: {e}")

        if not streamer._shutdown:
            logger.warning("Connection lost, retrying in 10s...")
            time.sleep(10)


if __name__ == "__main__":
    main()
