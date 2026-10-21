"""
MacroAnchorService — Quantitative Regime Engine (Phase 2 + FRED)

Slow signal (30% weight):
 yield curve, unemployment, CPI, Fed rate, etc.)
Regime output uses the canonical 6-schema:
  BULL_LONG / BULL_SHORT / NEUTRAL / BEAR_SHORT / BEAR_LONG / PANIC
"""

import os
import json
import logging
from datetime import date, timedelta, datetime
from typing import Dict, List, Optional, Tuple

import redis

logger = logging.getLogger("v8.macro_anchor")

# ── Longbridge data service (lazy) ──────────────────────────────────────────
_longbridge_svc = None


def _get_longbridge_svc():
    global _longbridge_svc
    if _longbridge_svc is not None:
        return _longbridge_svc
    try:
        from services.longbridge_data_service import get_longbridge_service
        _longbridge_svc = get_longbridge_service()
    except Exception:
        pass
    return _longbridge_svc

# ── Polygon client (lazy init) ──────────────────────────────────────────────
_polygon_client = None


def _get_polygon_client():
    global _polygon_client
    if _polygon_client is not None:
        return _polygon_client
    api_key = os.getenv("POLYGON_API_KEY")
    if not api_key:
        return None
    try:
        from polygon import RESTClient
        _polygon_client = RESTClient(api_key=api_key)
        return _polygon_client
    except ImportError:
        logger.warning("polygon-api-client not installed, macro anchor will use fallback")
        return None
# ── Sector ETFs ─────────────────────────────────────────────────────────
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLY"]
INDEX_SYMBOLS = {"SPY", "QQQ"}
# ── Regime Thresholds ──────────────────────────────────────────────────────
VIX_BULL   = 18
VIX_NEUTRAL = 25
VIX_PANIC  = 35
VIX_EXTREME = 50
SPY_TREND_DAYS = 20


class MacroAnchorService:
    """
    Quantitative macro anchor: derives regime from VIX + SPY trend + sector breadth + FRED slow signal.
    """

    def __init__(self, redis_client: redis.Redis = None):
        self.redis = redis_client
        self._fred_fetcher = None

    def _get_fred_fetcher(self):
        """Lazy-init FRED macro fetcher."""
        if self._fred_fetcher is not None:
            return self._fred_fetcher
        try:
            from services.fred_macro_fetcher import FredMacroFetcher
            self._fred_fetcher = FredMacroFetcher(redis_client=self.redis)
        except ImportError:
            logger.warning("fred_macro_fetcher not available")
        return self._fred_fetcher
    # ─── Public API ─────────────────────────────────────────────────────────
    def compute_regime(self) -> Dict:
        """
        Run the full quantitative pipeline and return a regime dict
        compatible with the Redis `market_regime:current` schema.
        """
        vix, vix_source = self._fetch_vix()
        spy_trend, spy_return = self._fetch_spy_trend()
        sector_breadth = self._fetch_sector_breadth()
        # Fetch FRED slow signal (cached 6h, very fast on cache hit)
        fred_snapshot = {}
        fred_fetcher = self._get_fred_fetcher()
        if fred_fetcher:
            try:
                fred_snapshot = fred_fetcher.get_covariates()
            except Exception as e:
                logger.warning(f"FRED covariates fetch failed: {e}")
        regime, bias = self._classify_regime(
            vix, spy_trend, spy_return, sector_breadth, fred_snapshot
        )

        # Build result
        fred_hint = fred_snapshot.get("slow_signal_regime_hint", "n/a")
        fred_bias = fred_snapshot.get("slow_signal_bias", 0.0)
        now_est = self._now_est()
        data_source = vix_source
        if fred_snapshot.get("series"):
            data_source = f"{vix_source}+fred"
        result = {
            "timestamp": now_est.isoformat(),
            "overall_sentiment_bias": round(bias, 3),
            "market_regime": regime,
            "key_risks": self._assess_risks(regime, vix, spy_trend, sector_breadth, fred_snapshot),
            "sector_flow": sector_breadth,
            "macro_catalysts": [],
            "summary": (
                f"VIX={vix:.1f}, SPY {spy_return:+.1f}% ({SPY_TREND_DAYS}d), "
                f"Sectors: {sector_breadth.get('bullish_count', 0)}B/"
                f"{sector_breadth.get('bearish_count', 0)}B, "
                f"FRED={fred_hint}({fred_bias:+.2f}) → {regime}"
            ),
            "data_source": data_source,
            "vix_level": round(vix, 2),
            "spy_trend": spy_trend,
            "spy_return_pct": round(spy_return, 2),
            "fred_covariates": fred_snapshot,
            "next_update": now_est.timestamp() + 21600,
        }
        logger.info(
            f"[MacroAnchor] VIX={vix:.1f} SPY={spy_return:+.1f}% "
            f"FRED={fred_hint}({fred_bias:+.3f}) "
            f"→ {regime} (bias={bias:+.3f})"
        )
        return result

    def compute_regime_lite(self) -> Dict:
        """
        Lightweight regime — VIX waterfall + SPY trend + sector breadth only.
        Skips FRED slow signal entirely. Used by 03:55 AM pre-market baseline.
        Completes in ~5s (vs ~15s for full compute_regime).
        """
        vix, vix_source = self._fetch_vix()
        spy_trend, spy_return = self._fetch_spy_trend()
        sector_breadth = self._fetch_sector_breadth()
        regime, bias = self._classify_fast_signal(vix, spy_trend, spy_return, sector_breadth)
        now_est = self._now_est()
        result = {
            "timestamp": now_est.isoformat(),
            "overall_sentiment_bias": round(bias, 3),
            "market_regime": regime,
            "key_risks": self._assess_risks(regime, vix, spy_trend, sector_breadth),
            "sector_flow": sector_breadth,
            "macro_catalysts": [],
            "summary": (
                f"[LITE] VIX={vix:.1f}, SPY {spy_return:+.1f}% ({SPY_TREND_DAYS}d), "
                f"Sectors: {sector_breadth.get('bullish_count', 0)}B/"
                f"{sector_breadth.get('bearish_count', 0)}B → {regime}"
            ),
            "data_source": f"{vix_source}_lite",
            "vix_level": round(vix, 2),
            "spy_trend": spy_trend,
            "spy_return_pct": round(spy_return, 2),
            "fred_covariates": {},
            "next_update": now_est.timestamp() + 21600,
        }
        logger.info(f"[MacroAnchorLite] VIX={vix:.1f} SPY={spy_return:+.1f}% → {regime}")
        return result
    # ─── VIX Fetch (Zero-cost waterfall: CBOE → yfinance → Redis → 20.0) ───
    def _fetch_vix(self) -> Tuple[float, str]:
        """Zero-cost VIX waterfall: CBOE → yfinance → Redis → 20.0.
        Returns (vix_value, source_tag).
        """
        # Tier 1: CBOE official delayed JSON (free, no API key)
        vix = self._vix_from_cboe()
        if vix:
            return vix, "cboe_delayed"

        # Tier 2: yfinance
        vix = self._vix_from_yfinance()
        if vix:
            return vix, "yfinance"

        # Tier 3: Redis cache (previously saved VIX)
        vix = self._vix_from_redis()
        if vix:
            return vix, "redis_cache"

        # Tier 4: Hard fallback
        logger.warning("All VIX sources failed, using 20.0 fallback")
        return 20.0, "fallback_20"

    def _vix_from_cboe(self) -> Optional[float]:
        """Fetch VIX from CBOE official delayed quotes endpoint."""
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX",
                headers={"User-Agent": "RealtimeStockAnalysis/1.0"}
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status != 200:
                    logger.debug(f"CBOE returned HTTP {resp.status}")
                    return None
                data = json.loads(resp.read())
                price = data.get("data", {}).get("current_price")
                if price and price > 0:
                    logger.debug(f"VIX from CBOE: {price}")
                    return float(price)
        except Exception as e:
            logger.debug(f"CBOE VIX fetch failed: {e}")
        return None

    def _vix_from_yfinance(self) -> Optional[float]:
        """Fetch VIX via yfinance as fallback.

        P1: Guards against empty DataFrame and NaN values.
        """
        try:
            import yfinance as yf
            hist = yf.Ticker("^VIX").history(period="1d")
            if hist.empty:
                return None
            price = hist["Close"].iloc[-1]
            # P1: Check for NaN from yfinance
            if hasattr(price, '__iter__'):
                # Edge case: got a Series instead of scalar
                price = float(price.iloc[0]) if len(price) > 0 else None
            if price is not None and not (isinstance(price, float) and price != price) and price > 0:
                logger.debug(f"VIX from yfinance: {price}")
                return float(price)
        except Exception as e:
            logger.debug(f"yfinance VIX fetch failed: {e}")
        return None
    def _vix_from_redis(self) -> Optional[float]:
        """Try reading VIX from Redis cache (populated by scanner)."""
        if not self.redis:
            return None
        try:
            raw = self.redis.get("market_regime:current")
            if raw:
                return json.loads(raw).get("vix_level")
        except Exception:
            pass
        return None
    # ─── SPY Trend ──────────────────────────────────────────────────────────
    def _fetch_spy_trend(self) -> Tuple[str, float]:
        """
        Fetch SPY daily bars and compute 20-day return.
        Returns:
            (trend_str, return_pct) where trend_str is "bullish"/"bearish"/"neutral"
        """
        # Try Longbridge first (no rate limit)
        lb = _get_longbridge_svc()
        if lb:
            try:
                bars = lb.get_daily_candles("SPY", days=SPY_TREND_DAYS + 10)
                if bars is not None and len(bars) >= 5:
                    first_close = bars["close"].iloc[-min(SPY_TREND_DAYS, len(bars))]
                    last_close = bars["close"].iloc[-1]
                    ret_pct = (last_close - first_close) / first_close * 100
                    if ret_pct > 2.0:
                        trend = "bullish"
                    elif ret_pct < -2.0:
                        trend = "bearish"
                    else:
                        trend = "neutral"
                    logger.debug(f"SPY trend from Longbridge: {trend} ({ret_pct:+.1f}%)")
                    return trend, ret_pct
            except Exception as e:
                logger.debug(f"Longbridge SPY trend failed: {e}")

        # Fallback to Polygon
        client = _get_polygon_client()
        if not client:
            return "neutral", 0.0
        try:
            to_date = date.today().isoformat()
            from_date = (date.today() - timedelta(days=SPY_TREND_DAYS + 10)).isoformat()
            aggs = list(client.list_aggs(
                "SPY", 1, "day", from_date, to_date, limit=500, adjusted=True
            ))
            if len(aggs) < 5:
                return "neutral", 0.0
            first_close = aggs[-min(SPY_TREND_DAYS, len(aggs))].close
            last_close = aggs[-1].close
            ret_pct = (last_close - first_close) / first_close * 100
            if ret_pct > 2.0:
                trend = "bullish"
            elif ret_pct < -2.0:
                trend = "bearish"
            else:
                trend = "neutral"
            return trend, ret_pct
        except Exception as e:
            logger.warning(f"SPY trend fetch failed: {e}")
            return "neutral", 0.0
    # ─── Sector Breadth ─────────────────────────────────────────────────────
    def _fetch_sector_breadth(self) -> Dict:
        """
        Fetch 10-day returns for sector ETFs and classify bullish/bearish.
        Returns:
            {
                "bullish_sectors": ["XLK", ...],
                "bearish_sectors": ["XLE", ...],
                "bullish_count": 3,
                "bearish_count": 1,
                "neutral_count": 1,
                "details": {"XLK": +2.1, ...}
            }
        """
        defaults = {
            "bullish_sectors": [], "bearish_sectors": [],
            "bullish_count": 0, "bearish_count": 0, "neutral_count": len(SECTOR_ETFS),
            "details": {},
        }

        # Try Longbridge first (no rate limit — fetches all ETFs instantly)
        lb = _get_longbridge_svc()
        if lb:
            try:
                bullish, bearish, details = [], [], {}
                all_ok = True
                for etf in SECTOR_ETFS:
                    bars = lb.get_daily_candles(etf, days=15)
                    if bars is not None and len(bars) >= 5:
                        first = bars["close"].iloc[-min(10, len(bars))]
                        last = bars["close"].iloc[-1]
                        ret = (last - first) / first * 100
                        details[etf] = round(ret, 2)
                        if ret > 1.0:
                            bullish.append(etf)
                        elif ret < -1.0:
                            bearish.append(etf)
                    else:
                        details[etf] = 0.0
                        all_ok = False
                if all_ok or details:
                    return {
                        "bullish_sectors": bullish,
                        "bearish_sectors": bearish,
                        "bullish_count": len(bullish),
                        "bearish_count": len(bearish),
                        "neutral_count": len(SECTOR_ETFS) - len(bullish) - len(bearish),
                        "details": details,
                    }
            except Exception as e:
                logger.debug(f"Longbridge sector breadth failed: {e}")

        # Fallback to Polygon
        client = _get_polygon_client()
        if not client:
            return defaults
        bullish = []
        bearish = []
        details = {}
        to_date = date.today().isoformat()
        from_date = (date.today() - timedelta(days=15)).isoformat()
        for etf in SECTOR_ETFS:
            try:
                aggs = list(client.list_aggs(
                    etf, 1, "day", from_date, to_date, limit=500, adjusted=True
                ))
                if len(aggs) >= 5:
                    ret = (aggs[-1].close - aggs[-min(10, len(aggs))].close) / aggs[-min(10, len(aggs))].close * 100
                    details[etf] = round(ret, 2)
                    if ret > 1.0:
                        bullish.append(etf)
                    elif ret < -1.0:
                        bearish.append(etf)
            except Exception as e:
                logger.debug(f"Sector ETF {etf} fetch failed: {e}")
                details[etf] = 0.0
        return {
            "bullish_sectors": bullish,
            "bearish_sectors": bearish,
            "bullish_count": len(bullish),
            "bearish_count": len(bearish),
            "neutral_count": len(SECTOR_ETFS) - len(bullish) - len(bearish),
            "details": details,
        }
    # ─── Regime Classification ──────────────────────────────────────────────
    def _classify_regime(
        self, vix: float, spy_trend: str, spy_return: float,
        sector_breadth: Dict, fred_snapshot: Dict = None,
    ) -> Tuple[str, float]:
        """
        Regime classification with fast (70%) + slow (30%) signal blend.
        Safety invariants:
          - PANIC (VIX >= 50) is never overridden by FRED data
          - Slow signal can only shift regime 1 step from fast signal
          - Strong disagreement → nudge toward NEUTRAL
        """
        # Step 1: Fast signal (existing logic, unchanged)
        fast_regime, fast_bias = self._classify_fast_signal(
            vix, spy_trend, spy_return, sector_breadth
        )
        # Step 2: Get FRED slow signal
        fred_snapshot = fred_snapshot or {}
        slow_bias = fred_snapshot.get("slow_signal_bias", 0.0)
        slow_hint = fred_snapshot.get("slow_signal_regime_hint", "neutral")
        # Step 3: Safety — PANIC / VIX extreme cannot be overridden
        if vix >= VIX_EXTREME:
            return fast_regime, fast_bias
        # Step 4: No FRED data → return fast signal unchanged
        if not fred_snapshot.get("series") or slow_bias == 0.0:
            return fast_regime, fast_bias
        # Step 5: Blend (70% fast, 30% slow)
        FAST_WEIGHT = 0.7
        SLOW_WEIGHT = 0.3
        blended_bias = fast_bias * FAST_WEIGHT + slow_bias * SLOW_WEIGHT
        # Step 6: Detect strong disagreement
        fast_is_bull = fast_regime in ("BULL_LONG", "BULL_SHORT")
        fast_is_bear = fast_regime in ("BEAR_SHORT", "BEAR_LONG", "PANIC")
        if (fast_is_bull and slow_hint == "bearish") or \
           (fast_is_bear and slow_hint == "bullish"):
            # Strong disagreement → reduce confidence, nudge toward NEUTRAL
            blended_bias *= 0.7
        # Step 7: Map blended bias to final regime (1-step max shift)
        final_regime = self._regime_from_blend(fast_regime, blended_bias, fast_bias)
        return final_regime, round(blended_bias, 3)
    def _classify_fast_signal(
        self, vix: float, spy_trend: str, spy_return: float, sector_breadth: Dict
    ) -> Tuple[str, float]:
        """
        Pure deterministic fast signal classification (original logic).
        VIX + SPY trend + sector breadth → regime + bias.
        """
        if vix >= VIX_EXTREME:
            return "PANIC", -0.5
        elif vix >= VIX_PANIC:
            return "BEAR_LONG", -0.4
        elif vix >= VIX_NEUTRAL:
            if spy_trend == "bearish":
                return "BEAR_LONG", -0.35
            elif sector_breadth.get("bearish_count", 0) >= 3:
                return "BEAR_SHORT", -0.2
            else:
                return "BEAR_SHORT", -0.15
        elif vix < VIX_BULL:
            if spy_trend == "bullish":
                return "BULL_LONG", 0.35
            elif sector_breadth.get("bullish_count", 0) >= 3:
                return "BULL_SHORT", 0.2
            else:
                return "BULL_SHORT", 0.15
        else:
            # VIX 18-25: neutral zone
            if spy_trend == "bullish" and sector_breadth.get("bullish_count", 0) >= 3:
                return "BULL_SHORT", 0.2
            elif spy_trend == "bearish" and sector_breadth.get("bearish_count", 0) >= 3:
                return "BEAR_SHORT", -0.2
            else:
                if spy_return > 3.0:
                    return "BULL_SHORT", 0.15
                elif spy_return < -3.0:
                    return "BEAR_SHORT", -0.15
                return "NEUTRAL", 0.0
    @staticmethod
    def _regime_from_blend(fast_regime: str, blended_bias: float, fast_bias: float) -> str:
        """
        Determine final regime from blended bias.
        Can only shift 1 step from fast signal's classification.
        """
        REGIME_ORDER = ["BULL_LONG", "BULL_SHORT", "NEUTRAL", "BEAR_SHORT", "BEAR_LONG", "PANIC"]
        try:
            idx = REGIME_ORDER.index(fast_regime)
        except ValueError:
            return fast_regime
        # Determine shift direction from blend vs fast
        bias_delta = blended_bias - fast_bias
        if abs(bias_delta) < 0.03:
            # No meaningful shift
            return fast_regime
        elif bias_delta > 0 and idx > 0:
            # Slow signal more bullish → shift toward BULL
            return REGIME_ORDER[idx - 1]
        elif bias_delta < 0 and idx < len(REGIME_ORDER) - 1:
            # Slow signal more bearish → shift toward BEAR
            return REGIME_ORDER[idx + 1]
        return fast_regime
    # ─── Risk Assessment ────────────────────────────────────────────────────
    def _assess_risks(
        self, regime: str, vix: float, spy_trend: str,
        sector_breadth: Dict, fred_snapshot: Dict = None
    ) -> List[str]:
        """Generate human-readable risk list including FRED warnings."""
        risks = []
        if vix > 30:
            risks.append(f"Elevated volatility (VIX={vix:.1f})")
        if vix > 40:
            risks.append("Extreme fear — consider reducing exposure")
        if spy_trend == "bearish":
            risks.append("SPY in downtrend")
        bear_count = sector_breadth.get("bearish_count", 0)
        if bear_count >= 3:
            risks.append(f"Broad sector weakness ({bear_count}/{len(SECTOR_ETFS)} bearish)")
        if regime in ("BEAR_LONG", "PANIC"):
            risks.append("Defensive positioning recommended")
        # FRED slow signal warnings
        if fred_snapshot:
            slow_hint = fred_snapshot.get("slow_signal_regime_hint", "neutral")
            slow_bias = fred_snapshot.get("slow_signal_bias", 0.0)
            if slow_hint == "bearish" and slow_bias < -0.1:
                risks.append(
                    f"FRED macro: bearish slow signal (bias={slow_bias:+.2f})"
                )
            elif slow_hint == "bullish" and slow_bias > 0.1:
                risks.append(
                    f"FRED macro: bullish slow signal (bias={slow_bias:+.2f})"
                )
        return risks
    # ─── Helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _now_est() -> datetime:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
# ─── Standalone runner (for lifecycle_scheduler / cron) ─────────────────────────────
def run_macro_anchor():
    """
    Entry point for lifecycle scheduler.
    Runs quantitative anchor and writes regime to Redis.
    """
    from services.macro_news_analyst import MacroNewsAnalyst
    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=0, decode_responses=True
    )
    anchor = MacroAnchorService(redis_client=r)
    result = anchor.compute_regime()
    # Write via MacroNewsAnalyst's dual-write path
    analyst = MacroNewsAnalyst(redis_client=r)
    analyst.update_from_research(result)
    logger.info(f"[MacroAnchor] Written to Redis: {result['market_regime']}")
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_macro_anchor()
