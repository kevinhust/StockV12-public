#!/usr/bin/env python3
"""
Standalone Scanner Runner

Run this script to:
1. Load universe from config
2. Run intraday scan using Polygon.io
3. Publish results to Redis (single source of truth)

Usage:
    # Single run (pre-market or on-demand)
    python run_scanner.py
    
    # Continuous mode (scan every 30 min during market hours)
    python run_scanner.py --continuous
    
    # Continuous with custom interval
    python run_scanner.py --continuous --interval 45
    
    # Quick test with small universe
    python run_scanner.py --test

Rate Limiting Notes:
    Polygon Free Tier: 5 requests/minute
    With 80 symbols, scan takes ~17 minutes
    With 200 symbols, scan takes ~43 minutes
    
Schedule Recommendation:
    - Pre-market (08:15 ET): Full scan
    - Open+30 (10:00 ET): Top 50 refresh  
    - Midday (12:00 ET): Top 50 refresh
    - Afternoon (14:30 ET): Top 50 refresh
"""

import asyncio
import argparse
import json
import os
import sys
from datetime import datetime, time, timedelta, date
from pathlib import Path
from typing import List, Set

from loguru import logger

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from services.selection.intraday_scanner import IntradayScanner, ScanCandidate

# Configure logging
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | <cyan>{message}</cyan>",
    level="INFO"
)
logger.add(
    PROJECT_ROOT / "logs" / "scanner.log",
    rotation="1 day",
    retention="7 days",
    level="DEBUG"
)


def load_universe(universe_path: str = None) -> List[str]:
    """
    Load stock universe from config file.
    
    Tries multiple paths in order:
    1. Provided path
    2. v8_system/config/universe.json
    3. v7_structure/services/selection/config/universe.json
    """
    paths_to_try = []
    
    if universe_path:
        paths_to_try.append(Path(universe_path))
    
    # V8 path
    paths_to_try.append(PROJECT_ROOT / "config" / "universe.json")
    
    # V7 fallback
    paths_to_try.append(PROJECT_ROOT.parent / "v7_structure" / "services" / "selection" / "config" / "universe.json")
    
    for path in paths_to_try:
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                universe = data.get("practical_universe", [])
                logger.info(f"📋 Loaded {len(universe)} symbols from {path.name}")
                return universe
            except Exception as e:
                logger.warning(f"Failed to load {path}: {e}")
    
    logger.error("❌ No universe file found!")
    return []


def get_default_universe() -> List[str]:
    """
    Return default high-volatility universe if no config found.

    This is a curated list of typically high-volatile US stocks.
    """
    return [
        # Tech / AI
        "NVDA", "AMD", "SMCI", "ARM", "PLTR", "SNOW", "NET", "CRWD",
        # EV / Clean Energy
        "TSLA", "RIVN", "LCID", "NIO", "PLUG", "ENPH", "FSLR",
        # Crypto / Fintech
        "COIN", "MARA", "RIOT", "SOFI", "HOOD", "UPST",
        # Biotech
        "MRNA", "BNTX", "NVAX", "EDIT",
        # Meme / High Vol
        "GME", "AMC", "BBBY",
        # Semiconductors
        "INTC", "MU", "MRVL", "QCOM",
        # Growth Tech
        "META", "SHOP", "SQ", "ABNB", "UBER",
        # Leveraged ETFs (high vol by design)
        "SOXL", "TQQQ", "TSLL",
        # Other High Vol
        "RKLB", "ASTS", "CLSK", "WULF"
    ]


def _fetch_finnhub_premarket_movers(top_n: int = 60) -> set:
    """
    Fetch active symbols from Finnhub via earnings calendar + market news.
    Free-tier market-movers endpoint returns empty, so we combine:
      1. Earnings calendar (today) — catalyst-driven stocks
      2. Market news — extract mentioned tickers
    """
    import requests
    token = os.getenv("FINNHUB_API_KEY", "")
    if not token:
        logger.warning("FINNHUB_API_KEY not set — skipping Finnhub movers")
        return set()

    symbols = set()

    # 1. Earnings calendar — today + next trading day
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        url = "https://finnhub.io/api/v1/calendar/earnings"
        resp = requests.get(url, params={"from": today, "to": today, "token": token}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            for item in data.get("earningsCalendar", []):
                sym = item.get("symbol", "")
                if sym and sym.isupper():
                    symbols.add(sym)
            logger.info(f"Finnhub earnings: {len(symbols)} symbols")
    except Exception as e:
        logger.warning(f"Finnhub earnings fetch failed: {e}")

    # 2. Market news — extract tickers from headlines
    try:
        url = "https://finnhub.io/api/v1/news"
        resp = requests.get(url, params={"category": "general", "token": token}, timeout=10)
        if resp.status_code == 200:
            import re
            for item in resp.json()[:30]:
                related = item.get("related", "")
                headline = item.get("headline", "")
                # Extract $TICKER patterns from headline
                for match in re.findall(r'\$([A-Z]{2,5})\b', headline):
                    symbols.add(match)
                # Parse related tickers (comma-separated)
                for sym in related.split(","):
                    sym = sym.strip()
                    if sym and sym.isupper() and 2 <= len(sym) <= 5:
                        symbols.add(sym)
    except Exception as e:
        logger.warning(f"Finnhub news fetch failed: {e}")

    symbols = set(list(symbols)[:top_n])
    logger.info(f"Finnhub: {len(symbols)} active symbols (earnings + news)")
    return symbols


def _fetch_polygon_daily_movers(top_n: int = 40) -> set:
    """
    Fetch yesterday's top movers from Polygon grouped daily aggs.
    Single API call, returns ~12K stocks. Sort by |change%| x liquidity.
    Free tier compatible — uses previous trading day's data.
    """
    from polygon import RESTClient as PolygonClient

    api_key = os.getenv("POLYGON_API_KEY", "")
    if not api_key:
        logger.warning("POLYGON_API_KEY not set — skipping Polygon movers")
        return set()

    try:
        client = PolygonClient(api_key=api_key)
        # Find last trading day (skip weekends)
        target = date.today()
        for _ in range(5):  # max 5 days back
            target -= timedelta(days=1)
            if target.weekday() < 5:  # Mon-Fri
                break

        aggs = client.get_grouped_daily_aggs(date=target.isoformat(), adjusted=True)
        if not aggs:
            return set()

        movers = []
        for a in aggs:
            if (a.open and a.open >= 10 and a.open <= 1000
                and a.volume and a.volume >= 500000):
                change_pct = (a.close - a.open) / a.open
                if abs(change_pct) >= 0.025:  # |change| >= 2.5%
                    dollar_vol = a.volume * a.close
                    # Score: |change%| x sqrt(dollar_vol_M) x liquidity_floor
                    # Liquidity floor dampens score for dollar_vol < $100M
                    liq_floor = min(dollar_vol / 1e8, 1.0)
                    score = abs(change_pct) * (dollar_vol / 1e6) ** 0.5 * liq_floor
                    movers.append((a.ticker, score, change_pct))

        movers.sort(key=lambda x: x[1], reverse=True)
        symbols = {m[0] for m in movers[:top_n]}

        logger.info(f"Polygon movers: {len(symbols)} from {target.isoformat()} "
                     f"(top: {[m[0] for m in movers[:5]]})")
        return symbols

    except Exception as e:
        logger.warning(f"Polygon daily movers fetch failed: {e}")
        return set()


def build_universe(redis_client=None, min_size: int = 50) -> set:
    """
    Build the scanning universe from multiple sources, merged + deduplicated.

    Source 1 (Primary):  carryover:7d  — yesterday's top performers
    Source 2 (Primary):  Polygon grouped daily movers — |change%| x liquidity
    Source 3 (Primary):  Finnhub earnings + news tickers
    Fallback Padding:    get_default_universe() if too small

    Returns a set of ticker symbols.
    """
    universe: set = set()

    # Source 1: Redis carryover
    if redis_client:
        try:
            carryover = redis_client.smembers("carryover:7d") or set()
            universe.update(carryover)
            logger.info(f"Universe: {len(carryover)} from carryover:7d")
        except Exception as e:
            logger.warning(f"carryover:7d read failed: {e}")

    # Source 2: Polygon daily movers (technical momentum)
    poly_movers = _fetch_polygon_daily_movers(top_n=40)
    universe.update(poly_movers)
    logger.info(f"Universe: {len(poly_movers)} from Polygon movers")

    # Source 3: Finnhub earnings + news (catalyst-driven)
    finnhub_movers = _fetch_finnhub_premarket_movers(top_n=60)
    universe.update(finnhub_movers)
    logger.info(f"Universe: {len(finnhub_movers)} from Finnhub")

    # Padding: if universe still too small, pad with defaults
    if len(universe) < min_size:
        defaults = set(get_default_universe())
        universe.update(defaults)
        logger.warning(f"Universe too small ({len(universe)}), padded with {len(defaults)} defaults")

    logger.info(f"Universe built: {len(universe)} total symbols")
    return universe


def is_market_hours() -> bool:
    """
    Check if within US market hours (9:30 AM - 4:00 PM ET).
    
    Note: This uses local time and assumes system is in ET timezone.
    For production, use proper timezone handling.
    """
    now = datetime.now()
    market_open = time(9, 30)
    market_close = time(16, 0)
    
    # Check if weekday
    if now.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    
    return market_open <= now.time() <= market_close


def is_premarket() -> bool:
    """Check if in pre-market hours (8:00 AM - 9:30 AM ET)"""
    now = datetime.now()
    premarket_start = time(8, 0)
    market_open = time(9, 30)
    
    if now.weekday() >= 5:
        return False
    
    return premarket_start <= now.time() < market_open


async def run_single_scan(
    scanner: IntradayScanner,
    universe: List[str]
) -> List[ScanCandidate]:
    """
    Run a single scan iteration.
    
    Args:
        scanner: Initialized IntradayScanner instance
        universe: List of symbols to scan
        
    Returns:
        List of ScanCandidate objects
    """
    logger.info("=" * 60)
    logger.info(f"🚀 Starting scan at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"   Universe size: {len(universe)} symbols")
    logger.info("=" * 60)
    
    candidates = await scanner.scan(universe)
    
    if candidates:
        # Display top 10
        logger.info("\n🎯 Top 10 Candidates:")
        for i, c in enumerate(candidates[:10], 1):
            logger.info(
                f"   {i:2d}. {c.symbol:6s} ${c.price:<7.2f} "
                f"ATR%: {c.atr_percent:5.1%} Beta: {c.beta:.1f} "
                f"RV: {c.relative_volume:.1f}x Score: {c.composite_score:.3f}"
            )
        
        # Publish to Redis (the single source of truth)
        await scanner.publish_watchlist(candidates)
    else:
        logger.warning("⚠️ No candidates found matching criteria")
    
    return candidates


async def run_continuous(interval_minutes: int = 30, universe: List[str] = None):
    """
    Run scans continuously during market hours.
    
    Args:
        interval_minutes: Minutes between scans
        universe: List of symbols to scan
    """
    logger.info(f"🔄 Starting continuous scanning (every {interval_minutes} min)")
    logger.info("   Press Ctrl+C to stop")
    
    if universe is None:
        universe = load_universe()
        if not universe:
            universe = get_default_universe()
    
    scanner = IntradayScanner()
    await scanner.initialize()
    
    while True:
        try:
            current_time = datetime.now()
            
            if is_premarket():
                logger.info("📅 Pre-market period - running full scan")
                await run_single_scan(scanner, universe)
                
            elif is_market_hours():
                logger.info("📈 Market hours - running scan")
                await run_single_scan(scanner, universe)
                
            else:
                logger.info("😴 Outside market hours, sleeping...")
            
            # Calculate next run time
            next_run = current_time + timedelta(minutes=interval_minutes)
            sleep_seconds = (next_run - datetime.now()).total_seconds()
            
            if sleep_seconds > 0:
                logger.info(f"⏰ Next scan at {next_run.strftime('%H:%M:%S')}")
                await asyncio.sleep(sleep_seconds)
            
            # Clear cache between runs to get fresh data
            scanner.clear_cache()
            
        except KeyboardInterrupt:
            logger.info("🛑 Stopping continuous scanning...")
            break
        except Exception as e:
            logger.error(f"❌ Error during scan: {e}")
            await asyncio.sleep(60)  # Wait a minute before retry


async def run_test():
    """Run a quick test with small universe"""
    test_universe = [
        "NVDA", "TSLA", "AMD", "PLTR", "SOFI",
        "COIN", "MARA", "META", "SMCI", "ARM"
    ]
    
    logger.info("🧪 Running test scan with 10 symbols...")
    logger.info(f"   ⏱️ Estimated time: <30s (Longbridge primary, Polygon fallback)")
    
    scanner = IntradayScanner()
    await scanner.initialize()
    
    candidates = await run_single_scan(scanner, test_universe)
    
    if candidates:
        logger.info("\n✅ Test completed successfully!")
        
        # Verify Redis
        if scanner.redis_client:
            stored = scanner.get_watchlist_from_redis()
            logger.info(f"   Redis verification: {len(stored)} symbols stored")
    else:
        logger.warning("⚠️ Test completed but no candidates found")


async def main():
    parser = argparse.ArgumentParser(
        description="Intraday High-Volatility Stock Scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python run_scanner.py                    # Single scan with default universe
    python run_scanner.py --continuous       # Continuous mode (every 30 min)
    python run_scanner.py --continuous -i 45 # Custom interval (45 min)
    python run_scanner.py --test             # Quick test with 10 symbols
    python run_scanner.py --universe /path/to/universe.json
        """
    )
    
    parser.add_argument(
        "--continuous", "-c",
        action="store_true",
        help="Run continuously during market hours"
    )
    
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=30,
        help="Scan interval in minutes (default: 30)"
    )
    
    parser.add_argument(
        "--universe", "-u",
        type=str,
        default=None,
        help="Path to universe.json file"
    )
    
    parser.add_argument(
        "--test", "-t",
        action="store_true",
        help="Run quick test with small universe"
    )
    
    parser.add_argument(
        "--no-redis",
        action="store_true",
        help="Skip Redis publishing (JSON only)"
    )
    
    args = parser.parse_args()
    
    # Print banner
    print("\n" + "=" * 60)
    print("🔍 Intraday High-Volatility Stock Scanner v2.3")
    print("   Layer 1: Longbridge (primary) → Polygon (fallback) + Polygon movers + Finnhub + carryover:7d")
    print("   Output: watchlist:tier1 + capital_flow:* + events.watchlist.tier1_ready (Kafka)")
    print("=" * 60 + "\n")

    if args.test:
        await run_test()
        return

    # Build universe from sources (carryover → Polygon movers → Finnhub → defaults)
    import redis as redis_lib
    try:
        _r = redis_lib.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            decode_responses=True,
        )
        _r.ping()
        universe_set = build_universe(redis_client=_r, min_size=50)
        universe = sorted(universe_set)
    except Exception as e:
        logger.warning(f"Redis unavailable for universe build: {e} — using load_universe()")
        universe = load_universe(args.universe)
        if not universe:
            universe = get_default_universe()

    if args.continuous:
        await run_continuous(interval_minutes=args.interval, universe=universe)
    else:
        # Single run
        scanner = IntradayScanner()
        await scanner.initialize()
        await run_single_scan(scanner, universe)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Scanner stopped by user")
    except Exception as e:
        logger.exception(f"❌ Fatal error: {e}")
        sys.exit(1)
