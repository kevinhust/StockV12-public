"""
Intraday High-Volatility Stock Scanner

Focused on actionable intraday trading candidates with core metrics:
- ATR% > 5.5% (volatility)
- Beta > 1.65 (market sensitivity)
- Avg Dollar Volume > 35M (liquidity)
- Price $10-$200 (tradeable range)
- Relative Volume > 1.8x (activity)

Data Source: Polygon.io (primary), with rate limiting for free tier (5 req/min).
Outputs to Redis for real-time consumption by ShadowRunner and other services.

Usage:
    scanner = IntradayScanner()
    await scanner.initialize()
    candidates = await scanner.scan(universe)
    await scanner.publish_watchlist(candidates)

Rate Limiting (Polygon Free Tier):
- 5 requests per minute max
- Each request needs ~13-15 second interval
- Batch mode: process symbols sequentially with sleep
"""

import asyncio
import os
import sys
import json
import time
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Dict, List, Optional, Any

import yaml
import redis
import pandas as pd
import numpy as np
import psycopg2
from kafka import KafkaProducer
from dataclasses import dataclass, asdict, field
from loguru import logger
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor

# Add src root to path; load .env from repo root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
REPO_ROOT = PROJECT_ROOT.parent
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(dotenv_path=REPO_ROOT / ".env")

# Try importing data source connectors
try:
    from polygon import RESTClient as PolygonClient
    POLYGON_AVAILABLE = True
except ImportError:
    POLYGON_AVAILABLE = False
    logger.warning("Polygon SDK not available. Install with: pip install polygon-api-client")

from services.longbridge_data_service import get_longbridge_service


@dataclass
class ScanCandidate:
    """Represents a scanned stock candidate with all metrics"""
    symbol: str
    price: float
    atr: float
    atr_percent: float
    beta: float
    avg_dollar_volume: float
    relative_volume: float
    gap_percent: float
    composite_score: float
    scan_time: str = field(default_factory=lambda: datetime.now().isoformat())
    
    # Additional metadata (note: at pre-market scan time, these are last trading day's values)
    volume_20d_avg: float = 0.0
    today_volume: float = 0.0   # actually last trading day's volume (no live data at 04:00)
    prev_close: float = 0.0     # last trading day's close
    today_open: float = 0.0     # = prev_close (no pre-market quote available)


class RateLimiter:
    """
    Rate limiter for Polygon API (Free Tier: 5 requests/minute)
    
    Ensures we don't exceed the rate limit by tracking request times
    and sleeping when necessary.
    """
    
    def __init__(self, requests_per_minute: int = 5, buffer_seconds: float = 1.0):
        self.requests_per_minute = requests_per_minute
        self.interval = 60.0 / requests_per_minute + buffer_seconds  # ~13s for 5 rpm
        self.last_request_time = 0.0
        self.request_count = 0
        
    async def wait_if_needed(self):
        """Wait if we need to respect rate limit"""
        now = time.time()
        elapsed = now - self.last_request_time
        
        if elapsed < self.interval:
            sleep_time = self.interval - elapsed
            logger.debug(f"Rate limit: sleeping {sleep_time:.1f}s")
            await asyncio.sleep(sleep_time)
        
        self.last_request_time = time.time()
        self.request_count += 1


class IntradayScanner:
    """
    High-Volatility Intraday Stock Scanner
    
    Scans universe of stocks and filters based on:
    1. Price range ($10-$200)
    2. ATR% > 5.5%
    3. Beta > 1.65
    4. Avg Dollar Volume > 35M
    5. Relative Volume > 1.8x (when available)
    
    Uses Polygon.io as primary data source with proper rate limiting.
    Ranks by composite score and publishes to Redis.
    """
    
    def __init__(self, config_path: str = None):
        self.config = self._load_config(config_path)
        self.redis_client: Optional[redis.Redis] = None
        self.polygon_client: Optional[Any] = None
        self.rate_limiter = RateLimiter(requests_per_minute=5, buffer_seconds=1.0)
        self.executor = ThreadPoolExecutor(max_workers=1)  # Single thread for sync calls
        self._initialized = False
        self._daily_cache: Dict[str, pd.DataFrame] = {}  # Cache to reduce API calls
        self._macro_anchor = None  # Lazy-init in initialize()

        # ── TimescaleDB (for Beta lookup) ────────────────────────────────────
        self._db_dsn: str = os.getenv(
            "DATABASE_URL",
            f"postgresql://{os.getenv('DB_USER','stock_app')}:{os.getenv('DB_PASS','')}"
            f"@{os.getenv('DB_HOST','localhost')}:{os.getenv('DB_PORT','5435')}/{os.getenv('DB_NAME','v8_db')}"
        )

        # ── Kafka (for tier1_ready event) ─────────────────────────────────────
        self._kafka_producer: Optional[KafkaProducer] = None
        self._kafka_bootstrap: str = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
        
    def _load_config(self, config_path: str = None) -> dict:
        """Load scanner configuration from YAML"""
        if config_path is None:
            config_path = Path(__file__).resolve().parent.parent.parent.parent / "config" / "scanner_config.yaml"
        
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            logger.info(f"✅ Loaded scanner config from {config_path}")
            return config
        except Exception as e:
            logger.error(f"Failed to load config: {e}, using defaults")
            return self._default_config()
    
    def _default_config(self) -> dict:
        """Return default configuration if YAML not found"""
        return {
            'filters': {
                'price_range': {'min': 10.0, 'max': 1000.0},
                'avg_dollar_volume_20d': {'min': 25000000},
                'atr_percent': {'min': 0.030},
                'beta_1y': {'min': 1.0},
                'gap': {
                    'soft_penalty_threshold': -0.10,
                    'black_swan_threshold': -0.20,
                },
            },
            'vix_adaptation': {
                'high_vix_threshold': 25,
                'high_vix_atr_min': 0.030,
                'normal_atr_min': 0.035
            },
            'regime_adaptation': {
                'panic_block_all': True,
                'presets': {
                    'BULL_LONG': {'atr_percent': 0.030, 'min_dollar_volume': 20000000},
                    'BULL_SHORT': {'atr_percent': 0.030, 'min_dollar_volume': 20000000},
                    'NEUTRAL': {'atr_percent': 0.035, 'min_dollar_volume': 25000000},
                    'BEAR_SHORT': {'atr_percent': 0.045, 'min_dollar_volume': 40000000},
                    'BEAR_LONG': {'atr_percent': 0.050, 'min_dollar_volume': 50000000},
                    'PANIC': {'atr_percent': 0.060, 'min_dollar_volume': 100000000},
                }
            },
            'ranking': {
                'weights': {
                    'relative_volume': 0.40,
                    'atr_percent': 0.25,
                    'beta': 0.20,
                    'gap_percent': 0.15
                },
                'top_n': 40,
                'long_only': {
                    'gap_bonus': {
                        'monster': {'threshold': 0.10, 'multiplier': 1.15},
                        'strong': {'threshold': 0.05, 'multiplier': 1.08},
                    },
                    'gap_penalty': {
                        'threshold': -0.10,
                        'value': -0.25,
                    },
                    'gap_black_swan': -0.20,
                },
            },
            'redis': {
                'watchlist_key': 'watchlist:tier1',
                'ranked_key': 'watchlist:ranked',
                'event_channel': 'events:watchlist',
                'ttl_seconds': 36000
            }
        }
    
    async def initialize(self):
        """Initialize connections to data sources and Redis"""
        if self._initialized:
            return
            
        logger.info("🔧 Initializing Intraday Scanner...")
        
        # Initialize Redis
        try:
            self.redis_client = redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                db=int(os.getenv("REDIS_DB", 0)),
                decode_responses=True
            )
            self.redis_client.ping()
            logger.info("✅ Redis connected")
        except Exception as e:
            logger.warning(f"⚠️ Redis connection failed: {e}. Will output to JSON only.")
            self.redis_client = None
        
        # Initialize Polygon
        polygon_key = os.getenv("POLYGON_API_KEY")
        if POLYGON_AVAILABLE and polygon_key:
            try:
                self.polygon_client = PolygonClient(api_key=polygon_key)
                logger.info("✅ Polygon client initialized (Free tier: 5 req/min)")
            except Exception as e:
                logger.error(f"❌ Polygon init failed: {e}")
                self.polygon_client = None
        else:
            logger.error("❌ Polygon API key not found or SDK not installed")

        # Initialize Kafka producer (for events.watchlist.tier1_ready)
        try:
            self._kafka_producer = KafkaProducer(
                bootstrap_servers=self._kafka_bootstrap,
                value_serializer=lambda v: json.dumps(v).encode('utf-8'),
                acks='all',
            )
            logger.info(f"✅ Kafka producer connected: {self._kafka_bootstrap}")
        except Exception as e:
            logger.warning(f"⚠️ Kafka producer failed: {e} — tier1_ready events will not be published")

        # Initialize MacroAnchorService for VIX waterfall (shared instance)
        try:
            from services.macro_anchor import MacroAnchorService
            self._macro_anchor = MacroAnchorService(redis_client=self.redis_client)
            logger.info("✅ MacroAnchorService initialized for VIX waterfall")
        except Exception as e:
            logger.warning(f"⚠️ MacroAnchorService init failed: {e} — VIX will use fallback")

        self._initialized = True
        logger.info("✅ Intraday Scanner initialized")
    
    async def scan(self, universe: List[str]) -> List[ScanCandidate]:
        """
        Run full scan on universe of symbols.
        
        This is a two-phase scan to minimize API calls:
        1. Phase 1: Fetch daily bars for all symbols (slow, ~13s per request)
        2. Phase 2: For symbols that pass initial filter, compute final scores
        
        Args:
            universe: List of stock symbols to scan
            
        Returns:
            List of ScanCandidate objects, ranked by composite score
        """
        if not self._initialized:
            await self.initialize()
        
        if not self.polygon_client:
            logger.error("❌ Polygon client not available, cannot scan")
            return []
        
        logger.info(f"🔍 Starting scan on {len(universe)} symbols...")
        logger.info(f"   ⏱️ Estimated time: {len(universe) * 13 / 60:.1f} minutes (Polygon rate limit)")
        start_time = datetime.now()
        
        # Get VIX for adaptive thresholds
        vix_level = await self._get_vix_level()

        # Self-healing: if market_regime:current is stale/missing, compute lite regime
        if self.redis_client:
            try:
                regime_raw = self.redis_client.get("market_regime:current")
                if not regime_raw:
                    logger.info("🔄 market_regime:current missing, computing lite regime for scan")
                    await self._ensure_regime_lite(vix_level)
            except Exception:
                pass

        filters = self._get_adaptive_filters(vix_level)

        # PANIC guard: skip scanning entirely to conserve API calls
        if filters.get("_regime") == "PANIC" and self.config.get("regime_adaptation", {}).get("panic_block_all", True):
            logger.warning("⚠️ PANIC regime — scan aborted, returning empty list")
            return []

        logger.info(f"📊 VIX Level: {vix_level:.1f} | ATR min: {filters['atr_min']:.1%}")
        
        # Phase 1: Fetch and analyze all symbols
        candidates = []
        failed_count = 0
        skipped_count = 0
        
        for i, symbol in enumerate(universe):
            try:
                # Progress logging every 10 symbols
                if (i + 1) % 10 == 0:
                    elapsed = (datetime.now() - start_time).total_seconds()
                    eta = (elapsed / (i + 1)) * (len(universe) - i - 1)
                    logger.info(f"  📈 Progress: {i+1}/{len(universe)} | ETA: {eta/60:.1f} min")
                
                result = await self._analyze_symbol(symbol, filters)
                
                if result:
                    candidates.append(result)
                    logger.debug(f"  ✅ {symbol}: ATR%={result.atr_percent:.1%}, Score={result.composite_score:.3f}")
                else:
                    skipped_count += 1
                    
            except Exception as e:
                logger.warning(f"  ❌ Failed {symbol}: {e}")
                failed_count += 1
        
        # Rank by composite score
        candidates.sort(key=lambda x: x.composite_score, reverse=True)
        top_n = self.config['ranking']['top_n']
        
        elapsed = (datetime.now() - start_time).total_seconds()
        logger.info(f"\n✅ Scan complete in {elapsed/60:.1f} minutes")
        logger.info(f"   📊 Analyzed: {len(universe)} | Passed: {len(candidates)} | Skipped: {skipped_count} | Failed: {failed_count}")
        logger.info(f"   🎯 Top {min(top_n, len(candidates))} selected")
        
        return candidates[:top_n]
    
    async def _analyze_symbol(self, symbol: str, filters: dict) -> Optional[ScanCandidate]:
        """
        Analyze a single symbol against filters.
        
        Returns ScanCandidate if passes all filters, None otherwise.
        """
        # Fetch historical data — try Longbridge first (no rate limit), fall back to Polygon
        bars = await self._fetch_daily_bars_longbridge(symbol, days=30)
        if bars is None:
            bars = await self._fetch_daily_bars_polygon(symbol, days=30)
        
        if bars is None or len(bars) < 14:
            return None
        
        # Basic price filter
        price = bars['close'].iloc[-1]
        if not (filters['price_min'] <= price <= filters['price_max']):
            return None
        
        # Calculate ATR (14-day)
        atr = self._calculate_atr(bars, period=14)
        atr_percent = atr / price
        
        if atr_percent < filters['atr_min']:
            logger.debug(f"  ⏭️ {symbol}: ATR% {atr_percent:.2%} < {filters['atr_min']:.2%}")
            return None
        
        # Calculate average dollar volume (20-day)
        avg_volume = bars['volume'].tail(20).mean()
        avg_dollar_volume = avg_volume * price
        
        if avg_dollar_volume < filters['min_dollar_volume']:
            return None
        
        # ── Beta: try TimescaleDB first, fallback to ATR% estimation ─────────
        beta = self._get_beta_from_db(symbol)
        if beta is None:
            beta = self._estimate_beta(atr_percent)
            logger.debug(f"{symbol}: Beta not in DB, using ATR%-estimation → {beta}")

        # ── Volume metrics ──────────────────────────────────────────────────
        # At pre-market (04:00), iloc[-1] is yesterday's bar — no today bar exists.
        # Use last trading day's volume as proxy for "today".
        last_day_volume = bars['volume'].iloc[-1]
        relative_volume = last_day_volume / avg_volume if avg_volume > 0 else 1.0

        # ── Gap calculation ─────────────────────────────────────────────────
        # Pre-market: iloc[-1] = yesterday's complete bar.  No live quote available.
        # price == bars['close'].iloc[-1] always (both are yesterday's close).
        # Therefore gap_percent must be 0.0 — we have no pre-market quote source.
        prev_close = bars['close'].iloc[-1]    # yesterday's close
        today_open = prev_close                # no live quote; assume open ≈ prev_close
        gap_percent = 0.0                      # cannot compute real gap pre-market

        # ── Gap% Filters (profile-aware via funnel_mode) ───────────────────────
        funnel_mode = self.config.get('funnel_mode', 'long_only')
        profile = self.config.get('ranking', {}).get(funnel_mode, {})

        # Hard block: black swan gap-down (profile-specific threshold)
        gap_black_swan = profile.get('gap_black_swan',
                          self.config['filters'].get('gap', {}).get('black_swan_threshold', -0.20))
        if gap_percent < gap_black_swan:
            logger.debug(f"  ⏭️ {symbol}: gap% {gap_percent:.1%} < {gap_black_swan:.0%}, excluded")
            return None

        # ── Composite Score ────────────────────────────────────────────────────
        weights = self.config['ranking']['weights']

        # Normalize each factor to 0–1
        rel_vol_norm = min(relative_volume / 3.0, 1.0)   # Cap at 3x RV
        atr_norm     = min(atr_percent  / 0.15,  1.0)     # Cap at 15% ATR
        beta_norm    = min(beta        / 3.0,   1.0)     # Cap at Beta 3.0

        # Pre-market: gap_percent=0.0 (no quote), redistribute gap weight proportionally
        if gap_percent == 0.0:
            w_rl = weights.get('relative_volume', 0.40)
            w_at = weights.get('atr_percent',     0.25)
            w_be = weights.get('beta',             0.20)
            total_w = w_rl + w_at + w_be
            composite_score = (
                rel_vol_norm * (w_rl / total_w) +
                atr_norm     * (w_at / total_w) +
                beta_norm    * (w_be / total_w)
            )
        else:
            gap_norm    = min(abs(gap_percent) / 0.20,  1.0)
            composite_score = (
                rel_vol_norm * weights.get('relative_volume', 0.40) +
                atr_norm     * weights.get('atr_percent',     0.25) +
                beta_norm    * weights.get('beta',             0.20) +
                gap_norm     * weights.get('gap_percent',      0.15)
            )
        composite_score = min(max(composite_score, 0.0), 1.0)

        # ── Gap-Down Soft Penalty (profile-dependent) ──────────────────────────
        gap_penalty_cfg = profile.get('gap_penalty')
        if gap_penalty_cfg and gap_percent < gap_penalty_cfg.get('threshold', -0.10):
            penalty_val = gap_penalty_cfg.get('value', -0.25)
            composite_score = max(composite_score + penalty_val, 0.0)
            logger.debug(f"  ⚠️ {symbol}: gap-down penalty ({gap_percent:.1%}), {penalty_val} composite")

        # ── Gap-Up Bonus (from profile) ────────────────────────────────────────
        gap_bonus = profile.get('gap_bonus', {})
        monster_thr = gap_bonus.get('monster', {}).get('threshold', 999)
        strong_thr  = gap_bonus.get('strong', {}).get('threshold', 999)
        if gap_percent > monster_thr:
            composite_score = min(composite_score * gap_bonus['monster']['multiplier'], 1.0)
        elif gap_percent > strong_thr:
            composite_score = min(composite_score * gap_bonus['strong']['multiplier'], 1.0)

        # ── Bidirectional only: gap-down bonus (short opportunity signal) ───────
        if funnel_mode == 'bidirectional':
            gap_short = profile.get('gap_short_bonus', {})
            short_monster_thr = gap_short.get('monster', {}).get('threshold', 0)
            short_strong_thr  = gap_short.get('strong', {}).get('threshold', 0)
            if gap_percent < short_monster_thr:
                composite_score = min(composite_score * gap_short['monster']['multiplier'], 1.0)
            elif gap_percent < short_strong_thr:
                composite_score = min(composite_score * gap_short['strong']['multiplier'], 1.0)
        
        return ScanCandidate(
            symbol=symbol,
            price=round(price, 2),
            atr=round(atr, 4),
            atr_percent=round(atr_percent, 4),
            beta=round(beta, 2),
            avg_dollar_volume=round(avg_dollar_volume, 0),
            relative_volume=round(relative_volume, 2),
            gap_percent=round(gap_percent, 4),
            composite_score=round(composite_score, 4),
            volume_20d_avg=round(avg_volume, 0),
            today_volume=round(last_day_volume, 0),  # actually last trading day's volume
            prev_close=round(prev_close, 2),
            today_open=round(today_open, 2)
        )
    
    async def _fetch_daily_bars_longbridge(self, symbol: str, days: int = 30) -> Optional[pd.DataFrame]:
        """Fetch daily bars from Longbridge (no rate limit). Returns None on failure."""
        # Check cache first (same cache as Polygon)
        cache_key = f"{symbol}_{date.today().isoformat()}"
        if cache_key in self._daily_cache:
            return self._daily_cache[cache_key]

        try:
            lb_svc = get_longbridge_service()
            loop = asyncio.get_running_loop()
            bars = await loop.run_in_executor(
                self.executor, lb_svc.get_daily_candles, symbol, days
            )
            if bars is not None and len(bars) >= 14:
                self._daily_cache[cache_key] = bars
                logger.debug(f"  Longbridge bars: {symbol} ({len(bars)} days)")
                return bars
        except Exception as e:
            logger.debug(f"Longbridge bars failed for {symbol}: {e}")
        return None

    async def _fetch_daily_bars_polygon(self, symbol: str, days: int = 30) -> Optional[pd.DataFrame]:
        """
        Fetch daily OHLCV bars from Polygon.io with rate limiting.
        
        Free tier: 5 requests per minute, so we sleep ~13s between requests.
        """
        if not self.polygon_client:
            return None
        
        # Check cache first
        cache_key = f"{symbol}_{date.today().isoformat()}"
        if cache_key in self._daily_cache:
            return self._daily_cache[cache_key]
        
        # Rate limit
        await self.rate_limiter.wait_if_needed()
        
        to_date = date.today().isoformat()
        from_date = (date.today() - timedelta(days=days + 10)).isoformat()
        
        try:
            # Polygon client is synchronous, run in executor
            loop = asyncio.get_running_loop()
            
            def fetch():
                aggs = list(self.polygon_client.list_aggs(
                    symbol, 1, "day", from_date, to_date, limit=50000, adjusted=True
                ))
                return aggs
            
            aggs = await loop.run_in_executor(self.executor, fetch)
            
            if not aggs or len(aggs) < 14:
                return None
            
            df = pd.DataFrame([{
                'open': a.open,
                'high': a.high,
                'low': a.low,
                'close': a.close,
                'volume': a.volume,
                'date': datetime.fromtimestamp(a.timestamp / 1000)
            } for a in aggs])
            
            df.set_index('date', inplace=True)
            df = df.tail(days)
            
            # Cache the result
            self._daily_cache[cache_key] = df
            
            return df
            
        except Exception as e:
            logger.debug(f"Polygon fetch failed for {symbol}: {e}")
            return None
    
    def _calculate_atr(self, bars: pd.DataFrame, period: int = 14) -> float:
        """Calculate Average True Range"""
        high = bars['high'] if 'high' in bars.columns else bars['close']
        low = bars['low'] if 'low' in bars.columns else bars['close']
        close = bars['close']
        
        # True Range components
        tr1 = high - low
        tr2 = abs(high - close.shift(1))
        tr3 = abs(low - close.shift(1))
        
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]
        
        return atr if not pd.isna(atr) else 0.0
    
    def _get_beta_from_db(self, symbol: str) -> Optional[float]:
        """
        Fetch pre-calculated 120-day rolling Beta from TimescaleDB.
        Falls back to ATR%-estimation if the DB record is missing or stale.
        """
        if not self._db_dsn:
            return None
        try:
            with psycopg2.connect(self._db_dsn, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT beta FROM stock_beta WHERE symbol = %s",
                        (symbol,)
                    )
                    row = cur.fetchone()
            if row and row[0] is not None:
                return float(row[0])
        except Exception as e:
            logger.debug(f"Beta DB lookup failed for {symbol}: {e}")
        return None

    def _estimate_beta(self, atr_percent: float) -> float:
        """
        Estimate beta from ATR% — fallback only, used when stock_beta table has no record.

        Market average ATR% ≈ 1–1.5%.  Higher ATR% → higher beta.
        """
        if atr_percent > 0.10:
            return 2.5
        elif atr_percent > 0.08:
            return 2.0
        elif atr_percent > 0.06:
            return 1.7
        elif atr_percent > 0.04:
            return 1.4
        else:
            return 1.1
    
    async def _get_vix_level(self) -> float:
        """Get current VIX level via zero-cost waterfall."""
        if not self._macro_anchor:
            logger.debug("MacroAnchorService not available, using VIX fallback")
            return 20.0
        try:
            loop = asyncio.get_running_loop()
            vix, _source = await asyncio.wait_for(
                loop.run_in_executor(self.executor, self._macro_anchor._fetch_vix),
                timeout=15.0
            )
            return vix
        except asyncio.TimeoutError:
            logger.warning("VIX waterfall timed out (15s), using fallback")
        except Exception as e:
            logger.warning(f"VIX waterfall failed: {e}")
        return 20.0

    async def _ensure_regime_lite(self, vix_level: float):
        """Self-heal: write a lightweight regime classification if missing."""
        if vix_level < 18:
            regime = "BULL_LONG"
        elif vix_level < 25:
            regime = "NEUTRAL"
        elif vix_level < 35:
            regime = "BEAR_LONG"
        else:
            regime = "PANIC"
        try:
            self.redis_client.set("market_regime:current", json.dumps({
                "regime": regime,
                "confidence": 0.5,
                "source": "scanner_vix_fallback",
                "vix": vix_level,
                "timestamp": datetime.now().isoformat(),
            }), ex=18000)  # 5h TTL
            logger.info(f"🔄 Wrote fallback regime: {regime} (VIX={vix_level:.1f})")
        except Exception as e:
            logger.warning(f"Failed to write fallback regime: {e}")

    def _get_adaptive_filters(self, vix_level: float) -> dict:
        """Regime-first adaptive filters. Falls back to VIX-based logic."""
        config = self.config['filters']
        regime_config = self.config.get('regime_adaptation', {})

        # 1. Try regime-based presets (primary path)
        regime_name = None
        if self.redis_client:
            try:
                regime_raw = self.redis_client.get("market_regime:current")
                if regime_raw:
                    regime_data = json.loads(regime_raw)
                    regime_name = regime_data.get('regime', '').upper()
            except Exception:
                pass

        if regime_name and 'presets' in regime_config:
            preset = regime_config['presets'].get(regime_name, regime_config['presets'].get('NEUTRAL'))
            if preset:
                logger.info(f"🧠 Scanner using {regime_name} preset: ATR>{preset['atr_percent']:.1%}, Vol>${preset['min_dollar_volume']/1e6:.0f}M")
                return {
                    'price_min': config['price_range']['min'],
                    'price_max': config['price_range']['max'],
                    'min_dollar_volume': preset['min_dollar_volume'],
                    'atr_min': preset['atr_percent'],
                    'beta_min': config['beta_1y']['min'],
                    '_regime': regime_name,
                }

        # 2. Fallback: VIX-based logic (backward compatible)
        vix_config = self.config.get('vix_adaptation', {})
        atr_min = config['atr_percent']['min']
        if vix_level > vix_config.get('high_vix_threshold', 25):
            atr_min = vix_config.get('high_vix_atr_min', 0.045)
            logger.info(f"📈 High VIX ({vix_level:.1f}), ATR min to {atr_min:.1%}")

        return {
            'price_min': config['price_range']['min'],
            'price_max': config['price_range']['max'],
            'min_dollar_volume': config['avg_dollar_volume_20d']['min'],
            'atr_min': atr_min,
            'beta_min': config['beta_1y']['min'],
            '_regime': regime_name or 'UNKNOWN',
        }
    
    async def publish_watchlist(self, candidates: List[ScanCandidate]):
        """
        Publish tier1 watchlist to Redis + fire Kafka event.

        Stores:
        - watchlist:tier1        (ZSET — symbol → composite_score, TTL=10h)
        - watchlist:tier1:details (HASH — symbol → full metrics JSON, TTL=10h)
        - watchlist:tier1:last_scan (STRING — ISO timestamp, TTL=24h)

        Fires: events.watchlist.tier1_ready → Redpanda (L2 subscribes)
        """
        if not self.redis_client:
            logger.warning("⚠️ Redis not available, skipping publish")
            return

        TIER1_KEY  = "watchlist:tier1"
        TIER1_DET  = "watchlist:tier1:details"
        TIER1_SCAN = "watchlist:tier1:last_scan"
        TTL_SHORT  = 36000   # 10 hours
        TTL_SCAN   = 86400   # 24 hours

        try:
            pipe = self.redis_client.pipeline()

            # Clear old data (idempotent)
            pipe.delete(TIER1_KEY)
            pipe.delete(TIER1_DET)

            # watchlist:tier1 — ZSET sorted by composite_score
            for c in candidates:
                pipe.zadd(TIER1_KEY, {c.symbol: float(c.composite_score)})

            # watchlist:tier1:details — HASH with full candidate data
            for c in candidates:
                pipe.hset(TIER1_DET, c.symbol, json.dumps(asdict(c)))

            # Last-scan timestamp
            pipe.set(TIER1_SCAN, datetime.now().isoformat(), ex=TTL_SCAN)

            # TTL on tier1 keys
            pipe.expire(TIER1_KEY, TTL_SHORT)
            pipe.expire(TIER1_DET, TTL_SHORT)

            pipe.execute()

            logger.info(f"✅ Published {len(candidates)} candidates → watchlist:tier1")
            logger.info(f"   Top 5: {[c.symbol for c in candidates[:5]]}")

        except Exception as e:
            logger.error(f"❌ Redis publish failed: {e}")

        # ── Fire Kafka event (L2 CatalystScorer subscribes) ────────────────────
        if self._kafka_producer:
            try:
                payload = {
                    "timestamp": datetime.now().isoformat(),
                    "count": len(candidates),
                    "top_symbols": [c.symbol for c in candidates[:8]],
                }
                self._kafka_producer.send("events.watchlist.tier1_ready", payload)
                logger.info("   Kafka event → events.watchlist.tier1_ready fired")
            except Exception as e:
                logger.warning(f"⚠️ Kafka publish failed: {e}")

        # ── Capital flow enrichment (from Longbridge minute candles) ──────────
        await self._enrich_capital_flow(candidates)
    
    # Note: save_to_json removed - Redis is the only output
    # Use get_watchlist_from_redis() to retrieve data

    async def _enrich_capital_flow(self, candidates: List[ScanCandidate]):
        """Fetch minute candles for top candidates, compute capital flow, write to Redis."""
        if not self.redis_client:
            return
        lb_svc = get_longbridge_service()
        loop = asyncio.get_running_loop()

        enriched = 0
        for c in candidates[:20]:  # Top 20 only
            try:
                bars = await loop.run_in_executor(
                    self.executor, lb_svc.get_minute_candles, c.symbol, 390
                )
                if bars is None or len(bars) < 10:
                    continue
                flow_score = self._estimate_capital_flow(bars)
                if flow_score is not None:
                    self.redis_client.setex(
                        f"capital_flow:{c.symbol}", 300, str(round(flow_score, 4))
                    )
                    enriched += 1
            except Exception as e:
                logger.debug(f"Capital flow enrichment failed for {c.symbol}: {e}")

        if enriched:
            logger.info(f"   Capital flow enriched: {enriched}/{min(len(candidates), 20)} symbols")
        elif candidates:
            logger.warning(f"   Capital flow: 0/{min(len(candidates), 20)} enriched — Longbridge minute data unavailable")

    def _estimate_capital_flow(self, bars: pd.DataFrame) -> Optional[float]:
        """
        Estimate net capital flow from minute bars.
        flow = volume * sign(close - open) per bar, summed over recent bars.
        Returns normalised 0.0-1.0 score via z-score, or None if insufficient data.
        """
        if len(bars) < 10:
            return None
        try:
            signed_flow = bars["volume"] * np.sign(bars["close"] - bars["open"])
            recent = signed_flow.tail(60)

            # Per-bar average flow for z-score (consistent with rolling per-bar mean)
            avg_flow = recent.mean()

            # Z-score normalisation against rolling window
            rolling_mean = signed_flow.rolling(60, min_periods=10).mean()
            rolling_std = signed_flow.rolling(60, min_periods=10).std()
            current_mean = rolling_mean.iloc[-1]
            current_std = rolling_std.iloc[-1]

            if pd.isna(current_std) or current_std == 0:
                # Fallback: simple normalisation by absolute volume
                total_vol = bars["volume"].tail(60).sum()
                if total_vol == 0:
                    return None
                net_ratio = recent.sum() / total_vol
                return max(0.0, min(1.0, 0.5 + net_ratio))

            z = (avg_flow - current_mean) / current_std
            # Map z-score to 0-1: sigmoid-like transform
            score = 1.0 / (1.0 + np.exp(-z))
            return float(score)
        except Exception as e:
            logger.debug(f"Capital flow estimation error: {e}")
            return None

    def get_watchlist_from_redis(self) -> List[str]:
        """Retrieve current watchlist from Redis"""
        if not self.redis_client:
            return []
        
        ranked_key = self.config.get('redis', {}).get('ranked_key', 'watchlist:ranked')
        
        try:
            symbols = self.redis_client.zrevrange(ranked_key, 0, -1)
            return list(symbols)
        except Exception as e:
            logger.error(f"Failed to get watchlist from Redis: {e}")
            return []
    
    def clear_cache(self):
        """Clear the daily data cache"""
        self._daily_cache.clear()
        logger.info("🗑️ Cache cleared")


# =============================================================================
# Standalone test
# =============================================================================
if __name__ == "__main__":
    async def test_scanner():
        logger.info("🧪 Testing IntradayScanner with Polygon.io...")
        
        # Test universe (small set for quick test)
        test_universe = [
            "NVDA", "TSLA", "AMD", "PLTR", "SOFI", "RIVN",
            "COIN", "MARA", "RIOT", "SMCI", "ARM", "META"
        ]
        
        scanner = IntradayScanner()
        await scanner.initialize()
        
        logger.info(f"\n⏱️ Estimated time: {len(test_universe) * 13 / 60:.1f} minutes")
        
        candidates = await scanner.scan(test_universe)
        
        print("\n" + "=" * 70)
        print(f"🎯 Top {len(candidates)} Candidates")
        print("=" * 70)
        
        for i, c in enumerate(candidates, 1):
            print(f"{i:2d}. {c.symbol:6s} ${c.price:<8.2f} "
                  f"ATR%: {c.atr_percent:5.1%} Beta: {c.beta:.1f} "
                  f"RelVol: {c.relative_volume:.1f}x Score: {c.composite_score:.3f}")
        
        print("=" * 70)
        
        # Publish to Redis (only output)
        await scanner.publish_watchlist(candidates)
    
    asyncio.run(test_scanner())
