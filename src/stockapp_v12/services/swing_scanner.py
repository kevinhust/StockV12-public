"""
SwingScanner — V12 Stage 1 SCAN (daily bars only).

Spec: v12/docs/TRADING_PIPELINE.md
- Hard filters (AND)
- Composite ranking (RS20, ATR%, vol breakout, pivot proximity)
- Writes watchlist:tier1 (ZSET score) + :details (HASH)
- Max output_top_n (default 20)
- Pure daily-bar, no news, no intraday patterns.

Idempotent per trading day (overwrite keys).
Failure mode handled by caller (degraded -> prior tier1).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import redis
import yaml
from zoneinfo import ZoneInfo

# Local V12 Longbridge Screener (server-side hard filter first-pass)
try:
    from .longbridge_screener import LongbridgeScreener
except ImportError:
    LongbridgeScreener = None  # graceful for standalone runs

# Project paths (allow running as module or from root)
# Use the canonical REPO_ROOT from core/paths (parents[3] from core/paths.py
# = project root post-migration 2026-07-09). P0-1 council fix: the previous
# arithmetic `_HERE.parent.parent.parent.parent` resolved to $HOME because the
# depth count was wrong, causing swing_scanner.yaml + universe.yaml to silently
# fall back to defaults / a 10-name hardcoded list.
from stockapp_v12.core.paths import REPO_ROOT as _REPO_ROOT


def _init_longbridge():
    """Load Longbridge via legacy cage (Phase 4 — no ensure_v11 in services)."""
    from stockapp_v12.adapters.legacy_v11.longbridge import (
        get_legacy_longbridge_service,
    )

    return get_legacy_longbridge_service()

try:
    from polygon import RESTClient as PolygonClient
    POLYGON_AVAILABLE = True
except ImportError:
    POLYGON_AVAILABLE = False
    PolygonClient = None  # type: ignore

logger = logging.getLogger(__name__)
EST = ZoneInfo("America/New_York")


@dataclass
class SwingCandidate:
    symbol: str
    price: float
    atr_percent: float
    relative_strength_20d: float
    avg_dollar_volume_20d: float
    volume_breakout_score: float
    pivot_proximity: float
    dist_52w_high: float
    composite_score: float
    scanned_at: str
    above_ema20: bool


class SwingScanner:
    """
    V12 daily-bar Swing Scanner.

    Usage:
        scanner = SwingScanner()
        count = scanner.run()   # fetches, filters, scores, writes Redis

    Optional ``market_data`` injects a MarketDataClient (Fake / Composite /
    custom). When omitted, default construction wires CompositeMarketDataClient
    with the same Longbridge-prefer + Polygon-fallback behavior as before.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        redis_client: Optional[redis.Redis] = None,
        *,
        market_data: Optional[Any] = None,
        watchlist_store: Optional[Any] = None,
    ):
        self.config = self._load_config(config_path)
        self.redis_client = redis_client or self._connect_redis()
        # Phase 3: optional injected WatchlistStore; else wrap current redis_client
        # on demand (so late redis_client assignment in tests still works).
        self._watchlist_injected = watchlist_store is not None
        self._watchlist = watchlist_store
        self._spy_bars: Optional[pd.DataFrame] = None  # cached

        if market_data is not None:
            # Injected path: skip archive/network provider init.
            self.lb_service = None
            self.polygon_client: Optional[Any] = None
            self.market_data = market_data
        else:
            self.lb_service = _init_longbridge()
            self.polygon_client = None
            self._init_polygon()
            data_cfg = self.config.get("data", {}) or {}
            from stockapp_v12.adapters.marketdata.composite import (
                CompositeMarketDataClient,
            )

            self.market_data = CompositeMarketDataClient(
                lb_service=self.lb_service,
                polygon_client=self.polygon_client,
                prefer_longbridge=data_cfg.get("prefer_longbridge", True),
                polygon_fallback=data_cfg.get("polygon_fallback", True),
            )

    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        if config_path is None:
            # Default to repo config/swing_scanner.yaml
            config_path = str(_REPO_ROOT / "config" / "swing_scanner.yaml")
        p = Path(config_path)
        if not p.exists():
            logger.warning(f"swing_scanner.yaml not found at {p}, using embedded defaults")
            return self._default_config()
        with open(p) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg

    def _default_config(self) -> Dict[str, Any]:
        return {
            "output_top_n": 20,
            "filters": {
                "price_range": {"min": 10, "max": 500},
                "min_avg_dollar_volume_20d": 30_000_000,
                "min_atr_percent_14d": 0.030,
                "min_relative_strength_20d": 0.0,
                "max_distance_from_52w_high": 0.25,
                "require_above_ema20": True,
                "min_adx_14": 20.0,
                "rsi": {"min": 40.0, "max": 75.0},
            },
            "scoring": {
                "weights": {
                    "relative_strength_20d": 0.30,
                    "atr_percent_14d": 0.25,
                    "volume_breakout_score": 0.25,
                    "pivot_proximity": 0.20,
                }
            },
            "redis": {
                "tier1_zset": "watchlist:tier1",
                "tier1_details": "watchlist:tier1:details",
                "ttl_seconds": 86400,
            },
            "data": {
                "prefer_longbridge": True,
                "polygon_fallback": True,
                "lookback_days": 260,
                "spy_symbol": "SPY",
            },
            "universe": {"source": "config/universe.yaml:static_universe", "fallback_min_size": 30},
        }

    def _connect_redis(self) -> Optional[redis.Redis]:
        try:
            r = redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                decode_responses=True,
            )
            r.ping()
            return r
        except Exception as e:
            logger.warning(f"Redis unavailable for SwingScanner: {e}")
            return None

    def _init_polygon(self):
        if not POLYGON_AVAILABLE:
            return
        key = os.getenv("POLYGON_API_KEY")
        if key:
            try:
                self.polygon_client = PolygonClient(api_key=key)
            except Exception as e:
                logger.warning(f"Polygon init failed: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # Universe
    # ──────────────────────────────────────────────────────────────────────
    def _load_universe(self) -> List[str]:
        # Try static from config/universe.yaml
        uni_path = _REPO_ROOT / "config" / "universe.yaml"
        try:
            if uni_path.exists():
                with open(uni_path) as f:
                    u = yaml.safe_load(f) or {}
                static = u.get("static_universe", [])
                if static:
                    logger.info(f"Loaded {len(static)} symbols from static_universe")
                    return [str(s).upper() for s in static]
        except Exception as e:
            logger.warning(f"Failed loading universe.yaml: {e}")

        # Fallback small liquid set (for robustness)
        fallback = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD", "AVGO", "SMCI"]
        logger.warning(f"Using fallback universe ({len(fallback)})")
        return fallback

    # ──────────────────────────────────────────────────────────────────────
    # Data fetching (daily bars) — always via MarketDataClient
    # ──────────────────────────────────────────────────────────────────────
    def _fetch_daily_bars(self, symbol: str, days: int = 260) -> Optional[pd.DataFrame]:
        """Delegate to injected/default MarketDataClient.

        Kept as an instance method so existing tests that monkeypatch
        ``_fetch_daily_bars`` continue to work without changes.
        """
        return self.market_data.fetch_daily_bars(symbol, days=days)

    def _fetch_spy_bars(self, days: int = 260) -> Optional[pd.DataFrame]:
        if self._spy_bars is not None:
            return self._spy_bars
        spy_sym = self.config.get("data", {}).get("spy_symbol", "SPY")
        self._spy_bars = self._fetch_daily_bars(spy_sym, days)
        return self._spy_bars

    # ──────────────────────────────────────────────────────────────────────
    # Indicators
    # ──────────────────────────────────────────────────────────────────────
    @staticmethod
    def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        high = df["high"]
        low = df["low"]
        close = df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs()
        ], axis=1).max(axis=1)
        # Simple moving ATR (matches common daily usage)
        atr = tr.rolling(window=period, min_periods=period).mean()
        return atr

    @staticmethod
    def _ema(series: pd.Series, span: int = 20) -> pd.Series:
        return series.ewm(span=span, adjust=False).mean()

    def _compute_indicators(self, df: pd.DataFrame, spy_df: Optional[pd.DataFrame]) -> Dict[str, float]:
        if len(df) < 30:
            return {}
        close = df["close"]
        high = df["high"]
        low = df["low"]
        vol = df["volume"]
        price = float(close.iloc[-1])

        # ATR%
        atr = self._atr(df, 14)
        atr_pct = float(atr.iloc[-1] / price) if not pd.isna(atr.iloc[-1]) and price > 0 else 0.0

        # 20d avg dollar vol
        dollar_vol = (close * vol).rolling(20).mean()
        avg_dv = float(dollar_vol.iloc[-1]) if not pd.isna(dollar_vol.iloc[-1]) else 0.0

        # RS 20d (excess vs SPY)
        rs = 0.0
        if len(close) >= 21:
            ret_stock = (close.iloc[-1] / close.iloc[-21]) - 1.0
            if spy_df is not None and len(spy_df) >= 21:
                spy_close = spy_df["close"]
                ret_spy = (spy_close.iloc[-1] / spy_close.iloc[-21]) - 1.0 if spy_close.iloc[-21] > 0 else 0.0
                rs = ret_stock - ret_spy
            else:
                rs = ret_stock

        # EMA20 above?
        ema20 = self._ema(close, 20)
        above_ema = bool(close.iloc[-1] > ema20.iloc[-1])

        # 52w high distance (use ~200 trading days)
        win = min(200, len(high) - 1)
        if win >= 20:
            high_52 = float(high.tail(win).max())
            dist = (high_52 - price) / high_52 if high_52 > 0 else 1.0
        else:
            dist = 1.0

        # Volume breakout (last / 20d avg)
        vol_avg = vol.rolling(20).mean()
        vratio = float(vol.iloc[-1] / vol_avg.iloc[-1]) if vol_avg.iloc[-1] > 0 else 1.0
        vol_break = min(vratio, 3.0)

        # Pivot proximity (near 20d high, 0-1)
        max20 = float(high.tail(20).max())
        min20 = float(df["low"].tail(20).min())
        range20 = max20 - min20 if max20 > min20 else 1.0
        prox_raw = (price - min20) / range20
        pivot_prox = max(0.0, min(1.0, prox_raw))

        # RSI(14)
        if len(close) >= 15:
            delta = close.diff()
            gain = delta.where(delta > 0, 0.0)
            loss = (-delta).where(delta < 0, 0.0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rsi_rs = avg_gain / avg_loss.replace(0, 1e-9)
            rsi = 100 - (100 / (1 + rsi_rs))
            rsi_14 = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0
        else:
            rsi_14 = 50.0

        # Simplified ADX(14)
        if len(df) >= 20:
            tr = pd.concat([
                high - low,
                (high - close.shift()).abs(),
                (low - close.shift()).abs()
            ], axis=1).max(axis=1)
            atr = tr.rolling(14).mean()
            plus_dm = (high.diff() > low.diff().abs()) * high.diff().clip(lower=0)
            minus_dm = (low.diff().abs() > high.diff()) * low.diff().abs().clip(lower=0)
            plus_di = 100 * plus_dm.rolling(14).mean() / atr.replace(0, 1e-9)
            minus_di = 100 * minus_dm.rolling(14).mean() / atr.replace(0, 1e-9)
            dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
            adx = dx.rolling(14).mean()
            adx_14 = float(adx.iloc[-1]) if not pd.isna(adx.iloc[-1]) else 15.0
        else:
            adx_14 = 15.0

        return {
            "price": price,
            "atr_percent": atr_pct,
            "relative_strength_20d": rs,
            "avg_dollar_volume_20d": avg_dv,
            "volume_breakout_score": vol_break,
            "pivot_proximity": pivot_prox,
            "dist_52w_high": dist,
            "above_ema20": above_ema,
            "adx_14": adx_14,
            "rsi_14": rsi_14,
        }

    # ──────────────────────────────────────────────────────────────────────
    # Filters & Scoring
    # ──────────────────────────────────────────────────────────────────────
    def _passes_filters(self, m: Dict[str, Any]) -> bool:
        f = self.config.get("filters", {})
        pr = f.get("price_range", {})
        if not (pr.get("min", 0) <= m.get("price", 0) <= pr.get("max", 1e9)):
            return False
        if m.get("avg_dollar_volume_20d", 0) < f.get("min_avg_dollar_volume_20d", 0):
            return False
        if m.get("atr_percent", 0) < f.get("min_atr_percent_14d", 0):
            return False
        if m.get("relative_strength_20d", -1) < f.get("min_relative_strength_20d", 0):
            return False
        if m.get("dist_52w_high", 1) > f.get("max_distance_from_52w_high", 0.25):
            return False
        if f.get("require_above_ema20", True) and not m.get("above_ema20", False):
            return False
        # Locked hard filters from config (ADX(14) and RSI(14))
        min_adx = f.get("min_adx_14", 20.0)
        rsi_cfg = f.get("rsi", {}) or {}
        rsi_min = rsi_cfg.get("min", 40.0)
        rsi_max = rsi_cfg.get("max", 75.0)
        if m.get("adx_14", 25.0) < min_adx:
            return False
        rsi = m.get("rsi_14", 50.0)
        if not (rsi_min <= rsi <= rsi_max):
            return False
        return True

    def _score(self, m: Dict[str, Any]) -> float:
        w = self.config.get("scoring", {}).get("weights", {})
        norm = self.config.get("scoring", {}).get("normalization", {})
        rs_cap = norm.get("rs_max", 0.15)
        atr_cap = norm.get("atr_max", 0.12)
        vcap = norm.get("vol_breakout_max", 3.0)

        rs = max(0.0, min(1.0, m.get("relative_strength_20d", 0) / max(rs_cap, 0.001)))
        atr = max(0.0, min(1.0, m.get("atr_percent", 0) / max(atr_cap, 0.001)))
        volb = max(0.0, min(1.0, m.get("volume_breakout_score", 1.0) / max(vcap, 1.0)))
        piv = max(0.0, min(1.0, m.get("pivot_proximity", 0.0)))

        score = (
            rs * w.get("relative_strength_20d", 0.30) +
            atr * w.get("atr_percent_14d", 0.25) +
            volb * w.get("volume_breakout_score", 0.25) +
            piv * w.get("pivot_proximity", 0.20)
        )
        return float(round(score, 6))

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────
    def scan(self) -> List[SwingCandidate]:
        """Run scan, return ranked candidates (does NOT write Redis).

        Two-phase pattern (Longbridge Screener optimization):
        1. Server-side hard filter (LongbridgeScreener) to shrink universe.
        2. Only fetch full bars + local custom scoring on the reduced set.
        """
        spy = self._fetch_spy_bars(self.config.get("data", {}).get("lookback_days", 260))
        candidates: List[SwingCandidate] = []

        # Phase 1: Longbridge Screener (server-side hard filters)
        if LongbridgeScreener is not None:
            # We pass a dummy ctx here; real injection happens from scheduler / service
            screener = LongbridgeScreener(quote_ctx=None)  # TODO: inject real ctx
            f = self.config.get("filters", {})
            rsi_cfg = f.get("rsi", {}) or {}
            screened = screener.apply_hard_filters(
                price_min=f.get("price_range", {}).get("min", 10),
                price_max=f.get("price_range", {}).get("max", 500),
                min_dollar_volume_20d=f.get("min_avg_dollar_volume_20d", 30_000_000),
                min_atr_percent_14d=f.get("min_atr_percent_14d", 0.03),
                require_above_ema20=f.get("require_above_ema20", True),
                min_adx_14=f.get("min_adx_14", 20.0),
                rsi_min=rsi_cfg.get("min", 40.0),
                rsi_max=rsi_cfg.get("max", 75.0),
            )
            universe = screened if screened else self._load_universe()
            logger.info(f"[SwingScanner] Screener phase reduced to {len(universe)} symbols")
        else:
            universe = self._load_universe()

        # Phase 2: Full bars + custom local indicators only on reduced universe
        for sym in universe:
            bars = self._fetch_daily_bars(sym, self.config.get("data", {}).get("lookback_days", 260))
            if bars is None or len(bars) < 40:
                continue
            ind = self._compute_indicators(bars, spy)
            if not ind:
                continue
            ind["symbol"] = sym
            if not self._passes_filters(ind):
                continue
            comp = self._score(ind)
            c = SwingCandidate(
                symbol=sym,
                price=ind["price"],
                atr_percent=ind["atr_percent"],
                relative_strength_20d=ind["relative_strength_20d"],
                avg_dollar_volume_20d=ind["avg_dollar_volume_20d"],
                volume_breakout_score=ind["volume_breakout_score"],
                pivot_proximity=ind["pivot_proximity"],
                dist_52w_high=ind["dist_52w_high"],
                composite_score=comp,
                scanned_at=datetime.now(EST).isoformat(),
                above_ema20=ind.get("above_ema20", False),
            )
            candidates.append(c)

        # rank desc, cap
        candidates.sort(key=lambda x: x.composite_score, reverse=True)
        top_n = int(self.config.get("output_top_n", 20))
        return candidates[:top_n]

    def _resolve_watchlist_store(self):
        if self._watchlist_injected:
            return self._watchlist
        if self.redis_client is not None:
            from stockapp_v12.adapters.stores.redis_stores import RedisWatchlistStore

            return RedisWatchlistStore(self.redis_client)
        return None

    def publish(self, candidates: List[SwingCandidate]) -> int:
        """Write tier1 ZSET + details HASH. Returns # written."""
        if not candidates:
            return 0
        store = self._resolve_watchlist_store()
        if store is None:
            logger.warning("No watchlist store — SwingScanner publish skipped")
            return 0

        tier1_key = self.config.get("redis", {}).get("tier1_zset", "watchlist:tier1")
        det_key = self.config.get("redis", {}).get("tier1_details", "watchlist:tier1:details")
        ttl = int(self.config.get("redis", {}).get("ttl_seconds", 86400))

        scores = {c.symbol: c.composite_score for c in candidates}
        details = {}
        for c in candidates:
            details[c.symbol] = json.dumps(
                {
                    "price": round(c.price, 4),
                    "atr_percent": round(c.atr_percent, 6),
                    "relative_strength": round(c.relative_strength_20d, 6),
                    "volume_breakout": round(c.volume_breakout_score, 4),
                    "pivot_proximity": round(c.pivot_proximity, 4),
                    "composite_score": c.composite_score,
                    "dist_52w_high": round(c.dist_52w_high, 4),
                    "scanned_at": c.scanned_at,
                }
            )
        store.replace_tier1(
            tier1_key, det_key, scores, details, ttl_seconds=ttl
        )

        logger.info(
            f"[SwingScanner] published {len(candidates)} to {tier1_key} "
            f"(top score {candidates[0].composite_score:.4f})"
        )
        return len(candidates)

    def run(self) -> int:
        """Full job: scan + publish. Returns count published.

        Recommended two-phase flow (Longbridge Screener optimization):
        1. Screener.apply_hard_filters (server-side when possible)
        2. Screener.get_daily_bars_with_cache (cached)
        3. Local custom indicators + scoring only on the reduced set.
        """
        # Example integration (uncomment/adapt when LongbridgeScreener is fully wired):
        # if hasattr(self, 'longbridge_screener') and self.longbridge_screener:
        #     candidates = self.longbridge_screener.apply_hard_filters(...)
        #     bars = self.longbridge_screener.get_daily_bars_with_cache(candidates)
        #     ... then use bars for _compute...
        cands = self.scan()
        return self.publish(cands)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = SwingScanner()
    n = s.run()
    print(f"SwingScanner run complete: {n} candidates")
