"""
LongbridgeScreener — Server-side pre-filtering wrapper for V12 SwingScanner.

This is the production-grade implementation for the Longbridge Screener
optimization discussed in Component 2 (Scanning Layer).

Goals:
- Push as many hard filters as possible to Longbridge (server-side).
- Greatly reduce the number of symbols for which we pull 260-day bars
  and run local custom calculations (RS vs SPY, pivot proximity, volume
  breakout, 52w distance).
- Strong fallbacks, caching, and logging.
"""

import logging
from typing import List, Dict, Any, Optional

try:
    from longport.openapi import QuoteContext, AdjustType
except ImportError:
    QuoteContext = None
    AdjustType = None

logger = logging.getLogger(__name__)


class LongbridgeScreener:
    """
    Longbridge Screener wrapper.

    Designed to be used together with LongbridgeDataService.

    NOTE (stub status): The server-side screener API call
    (``_call_longbridge_screener_api``) is a deliberate stub as of
    2026-06-19. Until the real Longbridge SDK screener method is
    integrated, ``apply_hard_filters`` will always raise internally
    and fall back to the caller-provided full universe (returns ``[]``).
    This is a known migration debt item, not a bug.
    """

    def __init__(self, quote_ctx: Optional[QuoteContext] = None, redis_client=None):
        self.ctx = quote_ctx
        self.redis = redis_client
        self.cache_ttl = 3600 * 6  # 6h cache for bars

    def apply_hard_filters(
        self,
        price_min: float = 10,
        price_max: float = 500,
        min_dollar_volume_20d: float = 30_000_000,
        min_atr_percent_14d: float = 0.03,
        require_above_ema20: bool = True,
        max_distance_from_52w_high: float = 0.25,
        min_adx_14: float = 20.0,  # new for Path A validation
        rsi_min: float = 40.0,     # new for Path A validation
        rsi_max: float = 75.0,     # new for Path A validation
        market: str = "US",
        fallback_to_full_universe: bool = True,
    ) -> List[str]:
        """
        Server-side hard filter (preferred) + graceful fallback.
        Returns list of symbols that pass V12 hard filters.
        """
        logger.info(
            f"[LongbridgeScreener] hard filters: "
            f"price[{price_min}-{price_max}], "
            f"min_dv20={min_dollar_volume_20d}, "
            f"min_atr14={min_atr_percent_14d}, "
            f"above_ema20={require_above_ema20}, "
            f"min_adx14={min_adx_14}, "
            f"rsi[{rsi_min}-{rsi_max}]"
        )

        try:
            filter_conditions = {
                "price": f"{price_min}:{price_max}",
                "dollar_volume_20d": f"{min_dollar_volume_20d}:",
                "atr_percent_14d": f"{min_atr_percent_14d}:",
                "adx_14": f"{min_adx_14}:",
                "rsi": f"{rsi_min}:{rsi_max}",
            }
            if require_above_ema20:
                filter_conditions["above_ema20"] = "true"
            if max_distance_from_52w_high < 1.0:
                filter_conditions["distance_from_52w_high"] = f":{max_distance_from_52w_high}"

            result = self._call_longbridge_screener_api(filter_conditions, market)
            symbols = [item["symbol"] for item in result if item.get("symbol")]
            logger.info(f"[LongbridgeScreener] {len(symbols)} symbols after server-side filter")
            return symbols

        except Exception as e:
            logger.error(f"[LongbridgeScreener] failed: {e}")
            logger.warning("SCAN_DEGRADED: LongbridgeScreener failed, using fallback")
            if fallback_to_full_universe:
                logger.warning("[LongbridgeScreener] falling back to caller-provided universe")
                return []
            raise

    def _call_longbridge_screener_api(
        self, filters: Dict[str, str], market: str
    ) -> List[Dict[str, Any]]:
        """
        Placeholder for the real Longbridge Screener / Filter API call.

        Current status (as of 2026-06-19):
            This is a deliberate stub. No real Longbridge Screener call is
            implemented yet. The method always raises to trigger the
            graceful fallback logic in apply_hard_filters.

        When the exact Longbridge QuoteContext Screener method, filter format,
        and response structure become available, replace the body with the
        actual implementation while preserving the existing logging and
        exception contract.

        Args:
            filters: Structured filter conditions prepared by apply_hard_filters.
                     Example keys: "price", "dollar_volume_20d", "atr_percent_14d",
                     "adx_14", "rsi", "above_ema20", "distance_from_52w_high".
            market: Market identifier (e.g. "US").

        Returns:
            List of dictionaries. Each dict must contain at minimum a "symbol"
            key. Additional fields returned by the real API may be present.
        """
        logger.info(
            f"[LongbridgeScreener] _call_longbridge_screener_api invoked. "
            f"market={market}, filter_keys={list(filters.keys())}"
        )

        if self.ctx is None:
            logger.warning("[LongbridgeScreener] No QuoteContext provided for screener")
            raise RuntimeError("No QuoteContext for Screener")

        # TODO: Replace the following with the real Longbridge Screener call
        # once the official method and response format are known.
        # Suggested (example only — adjust to actual SDK):
        #   result = self.ctx.screener(market=market, filters=filters)
        #   return result or []
        #
        # The caller (apply_hard_filters) expects:
        #   - List[dict] with at least "symbol" in each item
        #   - Any exception here will be caught and trigger SCAN_DEGRADED fallback

        error_msg = (
            "_call_longbridge_screener_api is not yet implemented. "
            "Real Longbridge Screener API integration is pending. "
            "Falling back to local universe."
        )
        logger.error(f"[LongbridgeScreener] {error_msg}")
        raise NotImplementedError(error_msg)

    def get_daily_bars_with_cache(
        self,
        symbols: List[str],
        lookback_days: int = 260,
        adjust_type: Any = None,
    ) -> Dict[str, Any]:
        """Fetch daily bars for (already filtered) symbols with Redis caching."""
        if not symbols:
            return {}

        cache_key = f"lb:daily:{lookback_days}:{','.join(sorted(symbols)[:50])}"

        if self.redis:
            try:
                cached = self.redis.get(cache_key)
                if cached:
                    logger.debug(f"[LongbridgeScreener] cache hit for {len(symbols)} symbols")
                    return cached  # deserialize in real usage
            except Exception as e:
                logger.warning(
                    "[LongbridgeScreener] cache get failed for key=%s: %s: %s "
                    "— proceeding without cache",
                    cache_key, type(e).__name__, e,
                )

        bars: Dict[str, Any] = {}
        for symbol in symbols:
            try:
                lb_symbol = f"{symbol}.US"
                if self.ctx and hasattr(self.ctx, "candlesticks"):
                    data = self.ctx.candlesticks(
                        lb_symbol,
                        period="day",
                        count=lookback_days + 10,
                        adjust_type=adjust_type or (AdjustType.ForwardAdjust if AdjustType else None),
                    )
                    bars[symbol] = data
            except Exception as e:
                logger.warning(f"[LongbridgeScreener] bars for {symbol} failed: {e}")
                continue

        if self.redis and bars:
            try:
                self.redis.setex(cache_key, self.cache_ttl, bars)
            except Exception as e:
                logger.warning(
                    "[LongbridgeScreener] cache setex failed for key=%s: %s: %s "
                    "— next call will re-fetch",
                    cache_key, type(e).__name__, e,
                )

        return bars