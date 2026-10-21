"""
FredMacroFetcher — FRED Macro Covariate Engine

Fetches key macroeconomic series from FRED (Federal Reserve Economic Data),
computes a weighted "slow signal" bias, and caches results in Redis.

Used by MacroAnchorService as the structural macro baseline (30% weight)
to complement the fast signal (VIX/SPY/sectors, 70% weight).

Series tracked:
  - T10Y2Y: 10Y-2Y Treasury Spread (recession signal)
  - UNRATE: Unemployment Rate
  - CPIAUCSL: CPI (YoY% computed)
  - FEDFUNDS: Federal Funds Rate
  - ICSA: Initial Jobless Claims
  - BAMLH0A0HYM2: ICE BofA US High Yield OAS Spread

Cache: Redis key `macro_covariates:fred`, TTL 6 hours
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import redis

logger = logging.getLogger("v8.fred_macro")

# ── Constants ─────────────────────────────────────────────────────────────

FRED_CACHE_KEY = "macro_covariates:fred"
FRED_CACHE_TTL = 21600  # 6 hours

# ── Series Configuration ──────────────────────────────────────────────────

FRED_SERIES_CONFIG = {
    "T10Y2Y": {
        "description": "10Y-2Y Treasury Spread",
        "frequency": "daily",
        "weight": 0.25,
        "bullish_threshold": 1.0,   # Spread > 1% = healthy
        "bearish_threshold": 0.0,   # Inverted yield curve
        "direction": "higher_is_bullish",
    },
    "UNRATE": {
        "description": "Unemployment Rate",
        "frequency": "monthly",
        "weight": 0.20,
        "bullish_threshold": 4.0,
        "bearish_threshold": 5.5,
        "direction": "lower_is_bullish",
        "lookback_months": 3,
    },
    "CPIAUCSL": {
        "description": "CPI (All Urban Consumers)",
        "frequency": "monthly",
        "weight": 0.15,
        "bullish_threshold": 2.5,   # YoY %
        "bearish_threshold": 4.0,
        "direction": "lower_is_bullish",
        "transform": "yoy_percent",
    },
    "FEDFUNDS": {
        "description": "Federal Funds Rate",
        "frequency": "monthly",
        "weight": 0.15,
        "bullish_threshold": 2.0,   # Accommodative
        "bearish_threshold": 5.0,   # Restrictive
        "direction": "lower_is_bullish",
        "lookback_months": 3,
    },
    "ICSA": {
        "description": "Initial Jobless Claims",
        "frequency": "weekly",
        "weight": 0.15,
        "bullish_threshold": 220000,
        "bearish_threshold": 300000,
        "direction": "lower_is_bullish",
    },
    "BAMLH0A0HYM2": {
        "description": "ICE BofA US High Yield OAS",
        "frequency": "daily",
        "weight": 0.10,
        "bullish_threshold": 3.5,
        "bearish_threshold": 5.0,
        "direction": "lower_is_bullish",
    },
}


class FredMacroFetcher:
    """
    Fetches and caches FRED macroeconomic covariates.

    Encapsulates all FRED API interaction. Consumers should never
    call FRED directly — always go through this class which
    handles caching, staleness tracking, and graceful degradation.
    """

    def __init__(self, redis_client: redis.Redis = None):
        self.redis = redis_client
        self._fred_client = None
        self._api_key_warned = False

    # ── Lazy Init ──────────────────────────────────────────────────────

    def _get_fred_client(self):
        """Lazy-init fredapi client. Returns None if FRED_API_KEY missing."""
        if self._fred_client is not None:
            return self._fred_client

        api_key = os.getenv("FRED_API_KEY")
        if not api_key:
            if not self._api_key_warned:
                logger.warning("FRED_API_KEY not set — macro covariates disabled")
                self._api_key_warned = True
            return None

        try:
            from fredapi import Fred
            self._fred_client = Fred(api_key=api_key)
            return self._fred_client
        except ImportError:
            logger.warning("fredapi not installed — pip install fredapi")
            return None

    # ── Public API ─────────────────────────────────────────────────────

    def get_covariates(self, force_refresh: bool = False) -> Dict:
        """
        Main entry point. Returns cached snapshot or fetches fresh.

        Returns a dict with:
            slow_signal_bias: float in [-0.3, +0.3]
            slow_signal_regime_hint: "bullish" | "neutral" | "bearish"
            series: {series_id: {value, date, change_1m, change_3m, available}}
            timestamp: ISO format
            fetch_errors: list of error strings
        """
        # Try cache first
        if not force_refresh:
            cached = self._read_cache()
            if cached:
                logger.debug("FRED covariates: cache hit")
                return cached

        # Check if FRED is available
        client = self._get_fred_client()
        if client is None:
            return self._neutral_fallback()

        # Fetch all series
        start_time = time.time()
        series_data: Dict[str, Dict] = {}
        fetch_errors: List[str] = []

        for series_id, config in FRED_SERIES_CONFIG.items():
            try:
                result = self._fetch_series(series_id, config)
                series_data[series_id] = result
            except Exception as e:
                logger.warning(f"FRED fetch failed for {series_id}: {e}")
                series_data[series_id] = {"available": False, "value": None, "date": None}
                fetch_errors.append(f"{series_id}: {str(e)[:80]}")

                # Track error metric
                try:
                    from core.metrics import fred_fetch_errors_total
                    fred_fetch_errors_total.labels(series_id=series_id).inc()
                except Exception:
                    pass

        # Compute slow signal
        slow_bias, regime_hint = self._compute_slow_signal(series_data)

        # Build snapshot
        snapshot = {
            "timestamp": datetime.utcnow().isoformat(),
            "series": series_data,
            "slow_signal_bias": round(slow_bias, 4),
            "slow_signal_regime_hint": regime_hint,
            "fetch_errors": fetch_errors,
            "fetch_latency_ms": round((time.time() - start_time) * 1000, 0),
        }

        # Write cache
        self._write_cache(snapshot)

        # Track latency metric
        try:
            from core.metrics import fred_fetch_latency_seconds
            fred_fetch_latency_seconds.observe(time.time() - start_time)
        except Exception:
            pass

        # Track staleness metrics
        try:
            from core.metrics import fred_series_staleness_hours
            staleness = self._assess_staleness(series_data)
            for sid, hours in staleness.items():
                fred_series_staleness_hours.labels(series_id=sid).set(hours)
        except Exception:
            pass

        available_count = sum(1 for v in series_data.values() if v.get("available"))
        logger.info(
            f"[FredMacro] Fetched {available_count}/{len(FRED_SERIES_CONFIG)} series "
            f"→ bias={slow_bias:+.3f} ({regime_hint}) in {snapshot['fetch_latency_ms']:.0f}ms"
        )

        return snapshot

    # ── Series Fetch ───────────────────────────────────────────────────

    def _fetch_series(self, series_id: str, config: Dict) -> Dict:
        """
        Fetch a single FRED series with lookback for change calculation.
        """
        client = self._fred_client

        # Special handling for YoY transform (CPI)
        if config.get("transform") == "yoy_percent":
            return self._fetch_yoy_series(series_id, config)

        # Standard series fetch
        # Get recent observations (enough for 3-month lookback)
        series = client.get_series(
            series_id,
            observation_start=(datetime.utcnow() - timedelta(days=120)).strftime("%Y-%m-%d"),
        )

        if series is None or series.empty:
            return {"available": False, "value": None, "date": None}

        latest_val = float(series.iloc[-1])
        latest_date = str(series.index[-1].date())

        result = {
            "value": round(latest_val, 4),
            "date": latest_date,
            "available": True,
            "description": config["description"],
        }

        # Compute changes
        if len(series) >= 2:
            # ~1 month change
            lookback_1m = min(22, len(series) - 1)  # ~22 trading days
            result["change_1m"] = round(float(series.iloc[-1] - series.iloc[-lookback_1m]), 4)
        else:
            result["change_1m"] = None

        if len(series) >= 5:
            # ~3 month change
            lookback_3m = min(66, len(series) - 1)  # ~66 trading days
            result["change_3m"] = round(float(series.iloc[-1] - series.iloc[-lookback_3m]), 4)
        else:
            result["change_3m"] = None

        return result

    def _fetch_yoy_series(self, series_id: str, config: Dict) -> Dict:
        """
        Fetch a level series (CPI) and compute year-over-year percent change.
        """
        client = self._fred_client

        # Fetch 15 months of data to compute YoY
        series = client.get_series(
            series_id,
            observation_start=(datetime.utcnow() - timedelta(days=450)).strftime("%Y-%m-%d"),
        )

        if series is None or series.empty or len(series) < 2:
            return {"available": False, "value": None, "date": None}

        latest_val = float(series.iloc[-1])
        latest_date = str(series.index[-1].date())

        # Find value from ~12 months ago
        target_date = series.index[-1] - timedelta(days=365)
        # Find closest observation to target date
        older_mask = series.index <= target_date
        if not older_mask.any():
            return {
                "value": round(latest_val, 4),
                "date": latest_date,
                "available": True,
                "description": config["description"],
                "change_1m": None,
                "change_3m": None,
                "yoy_pct": None,
            }

        older_idx = series.index[older_mask][-1]
        older_val = float(series.loc[older_idx])

        if older_val == 0:
            yoy_pct = 0.0
        else:
            yoy_pct = round((latest_val - older_val) / older_val * 100, 2)

        # For CPI, the YoY% is what we use for scoring, not the raw index level
        return {
            "value": round(latest_val, 4),
            "date": latest_date,
            "available": True,
            "description": config["description"],
            "yoy_pct": yoy_pct,
            "yoy_date": str(older_idx.date()),
            "change_1m": None,
            "change_3m": None,
        }

    # ── Slow Signal Computation ────────────────────────────────────────

    def _compute_slow_signal(self, series_data: Dict[str, Dict]) -> Tuple[float, str]:
        """
        Compute weighted slow signal bias from all FRED series.

        Each series contributes a score in [-1, +1] based on where
        its value sits relative to bullish/bearish thresholds.
        Final bias is scaled to [-0.3, +0.3].

        Returns:
            (bias, regime_hint) where bias ∈ [-0.3, +0.3]
        """
        scores: List[Tuple[float, float]] = []  # (weight, score)

        for series_id, config in FRED_SERIES_CONFIG.items():
            data = series_data.get(series_id, {})
            if not data.get("available"):
                continue

            # Get the value to score
            if config.get("transform") == "yoy_percent":
                val = data.get("yoy_pct")
            else:
                val = data.get("value")

            if val is None:
                continue

            score = self._score_series(val, config)
            if score is not None:
                scores.append((config["weight"], score))

        if not scores:
            return 0.0, "neutral"

        # Renormalize weights for available series only
        total_weight = sum(w for w, _ in scores)
        if total_weight == 0:
            return 0.0, "neutral"

        weighted_avg = sum(w * s for w, s in scores) / total_weight

        # Scale to [-0.3, +0.3]
        bias = round(max(-0.3, min(0.3, weighted_avg * 0.3)), 4)

        if bias > 0.05:
            hint = "bullish"
        elif bias < -0.05:
            hint = "bearish"
        else:
            hint = "neutral"

        return bias, hint

    @staticmethod
    def _score_series(value: float, config: Dict) -> Optional[float]:
        """
        Map a series value to a score in [-1, +1].

        For higher_is_bullish: value above bullish_threshold → +1, below bearish → -1
        For lower_is_bullish: value below bullish_threshold → +1, above bearish → -1
        """
        bull_thresh = config["bullish_threshold"]
        bear_thresh = config["bearish_threshold"]
        direction = config["direction"]

        if direction == "higher_is_bullish":
            if value >= bull_thresh:
                score = 1.0
            elif value <= bear_thresh:
                score = -1.0
            else:
                # Linear interpolation between bearish and bullish thresholds
                range_size = bull_thresh - bear_thresh
                if range_size == 0:
                    score = 0.0
                else:
                    score = (value - bear_thresh) / range_size * 2.0 - 1.0
        else:  # lower_is_bullish
            if value <= bull_thresh:
                score = 1.0
            elif value >= bear_thresh:
                score = -1.0
            else:
                range_size = bear_thresh - bull_thresh
                if range_size == 0:
                    score = 0.0
                else:
                    score = 1.0 - (value - bull_thresh) / range_size * 2.0

        return round(max(-1.0, min(1.0, score)), 4)

    # ── Staleness ──────────────────────────────────────────────────────

    @staticmethod
    def _assess_staleness(series_data: Dict[str, Dict]) -> Dict[str, float]:
        """Calculate hours since last observation for each series."""
        staleness = {}
        now = datetime.utcnow()

        for series_id, data in series_data.items():
            if not data.get("available") or not data.get("date"):
                staleness[series_id] = -1  # Unavailable
                continue

            try:
                obs_date = datetime.strptime(data["date"], "%Y-%m-%d")
                hours = (now - obs_date).total_seconds() / 3600
                staleness[series_id] = round(hours, 1)
            except (ValueError, TypeError):
                staleness[series_id] = -1

        return staleness

    # ── Cache ──────────────────────────────────────────────────────────

    def _write_cache(self, snapshot: Dict) -> None:
        """Write snapshot to Redis with TTL."""
        if not self.redis:
            return
        try:
            self.redis.setex(
                FRED_CACHE_KEY,
                FRED_CACHE_TTL,
                json.dumps(snapshot, default=str),
            )
        except Exception as e:
            logger.warning(f"FRED cache write failed: {e}")

    def _read_cache(self) -> Optional[Dict]:
        """Read snapshot from Redis. Returns None if missing/expired."""
        if not self.redis:
            return None
        try:
            raw = self.redis.get(FRED_CACHE_KEY)
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.debug(f"FRED cache read failed: {e}")
        return None

    # ── Fallback ───────────────────────────────────────────────────────

    @staticmethod
    def _neutral_fallback() -> Dict:
        """Return neutral snapshot when FRED is unavailable."""
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "series": {},
            "slow_signal_bias": 0.0,
            "slow_signal_regime_hint": "neutral",
            "fetch_errors": ["FRED_API_KEY not configured"],
            "fetch_latency_ms": 0,
        }


# ── Standalone runner ─────────────────────────────────────────────────────

def run_fred_fetch():
    """Entry point for lifecycle scheduler / manual testing."""
    r = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=0, decode_responses=True,
    )
    fetcher = FredMacroFetcher(redis_client=r)
    result = fetcher.get_covariates(force_refresh=True)
    available = sum(1 for v in result["series"].values() if v.get("available"))
    print(f"FRED: {available} series fetched, bias={result['slow_signal_bias']:+.3f} ({result['slow_signal_regime_hint']})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_fred_fetch()
