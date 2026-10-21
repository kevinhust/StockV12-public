"""
CatalystScorer — L2 of the Three-Layer Funnel

Reads tier1 from Redis, fetches Polygon news via SentimentAnalyst,
calls LLM for catalyst analysis, applies regime multiplier,
and writes tier2 to Redis.

Features:
- News hash cache: skip LLM call if headlines unchanged since last scoring
- Strict "no catalyst" expulsion: remove symbol from tier2 entirely
- Regime-aware scoring with multiplier
- Kafka event fire on completion
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import redis
import yaml
from dotenv import load_dotenv
from loguru import logger

# ── Path setup ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # src/services/selection/ → repo root
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")

# ── Constants ─────────────────────────────────────────────────────────────
TIER1_KEY = "watchlist:tier1"
TIER1_DET = "watchlist:tier1:details"
TIER2_KEY = "watchlist:tier2"
TIER2_DET = "watchlist:tier2:details"
TIER2_SCORED = "watchlist:tier2:last_scored"
NEWS_HASH_PREFIX = "watchlist:tier2:last_news_hash:"


def _load_regime_multipliers(funnel_mode: str = 'long_only') -> Dict[str, float]:
    """Load regime multipliers from config/risk_regimes.yaml, respecting funnel_mode."""
    try:
        cfg_path = PROJECT_ROOT / "config" / "risk_regimes.yaml"
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        key = 'l2_catalyst_multipliers' if funnel_mode == 'long_only' else 'l2_catalyst_multipliers_bidirectional'
        multipliers = cfg.get(key, {})
        if multipliers:
            return {k.upper(): v for k, v in multipliers.items()}
    except Exception as e:
        logger.warning(f"Could not load regime multipliers from config: {e}")
    # Fallback: FusionEngine 6-schema (long_only defaults)
    return {
        "BULL_LONG": 1.2,
        "BULL_SHORT": 1.1,
        "NEUTRAL": 1.0,
        "BEAR_SHORT": 0.9,
        "BEAR_LONG": 0.8,
        "PANIC": 0.5,
    }


CATALYST_PROMPT = """\
You are a strict stock catalyst analyst. Your job is to VETO stocks that lack a genuine near-term catalyst.

A "catalyst" is a CONCRETE, IDENTIFIABLE event likely to drive price action in 1-5 trading days. Pure technical bounces, sector drift, or vague macro sentiment do NOT count.

Strong catalysts (has_catalyst=true, score 6-10):
- Earnings: beat/miss, guidance raise, revenue surprise
- FDA/regulatory: drug approval, clinical trial results, FDA panel
- M&A: acquisition, merger, activist stake, spinoff
- Product: major launch, contract win, partnership
- Analyst: upgrade/downgrade with price target change

Weak catalysts (has_catalyst=false, score 0-3):
- "Momentum play", "technical bounce", "sector rotation"
- Generic macro headlines unrelated to this specific stock
- Old news already priced in (>2 days stale)

News for {symbol}:
{headlines}

Technical context: price=${price}, gap={gap_pct}%, relative_volume={rv}x, beta={beta}

Respond ONLY with valid JSON:
{{
  "has_catalyst": true/false,
  "catalyst_type": "one of: earnings, fda, manda, product, analyst, sector, macro, none",
  "score": <float 0.0-10.0>,
  "summary": "<one sentence: name the specific catalyst event, or state why there is none>"
}}
"""

CATALYST_PROMPT_BIDIRECTIONAL = """\
You are a strict stock catalyst analyst evaluating BOTH bullish AND bearish catalysts. Your job is to identify concrete near-term events that could drive price action in EITHER direction within 1-5 trading days. Pure technical bounces, sector drift, or vague macro sentiment do NOT count.

Bullish catalysts (has_catalyst=true, score 6-10):
- Earnings: beat, guidance raise, revenue surprise
- FDA/regulatory: drug approval, clinical trial success, FDA panel positive
- M&A: acquisition target, merger, activist stake, spinoff
- Product: major launch, contract win, partnership
- Analyst: upgrade with price target raise

Bearish catalysts (has_catalyst=true, score 6-10, note negative direction):
- Earnings: miss, guidance cut, revenue shortfall
- FDA/regulatory: drug rejection, clinical trial failure, FDA warning letter
- M&A: failed acquisition, hostile takeover risk, spinoff uncertainty
- Analyst: downgrade with price target cut
- Sector: regulatory crackdown, antitrust action
- Legal: lawsuit, SEC investigation, patent invalidation

Weak catalysts (has_catalyst=false, score 0-3):
- "Technical support break", "sector weakness"
- Generic macro headlines unrelated to this specific stock
- Old news already priced in (>2 days stale)

News for {symbol}:
{headlines}

Technical context: price=${price}, gap={gap_pct}%, relative_volume={rv}x, beta={beta}

Respond ONLY with valid JSON:
{{
  "has_catalyst": true/false,
  "catalyst_type": "one of: earnings, fda, manda, product, analyst, sector, macro, none",
  "direction": "bullish or bearish — only if has_catalyst=true",
  "score": <float 0.0-10.0>,
  "summary": "<one sentence: name the specific catalyst event and direction, or state why there is none>"
}}
"""


class CatalystScorer:
    """
    L2 Catalyst Scorer — reads tier1, scores via LLM + news, writes tier2.
    """

    def __init__(self, redis_client: redis.Redis = None):
        host = os.getenv("REDIS_HOST", "localhost")
        port = int(os.getenv("REDIS_PORT", 6379))
        self.r = redis_client or redis.Redis(host=host, port=port, decode_responses=True)

        # Read funnel_mode from scanner_config.yaml
        self.funnel_mode = self._read_funnel_mode()
        self.regime_multipliers = _load_regime_multipliers(self.funnel_mode)
        self.catalyst_prompt = (
            CATALYST_PROMPT_BIDIRECTIONAL if self.funnel_mode == 'bidirectional'
            else CATALYST_PROMPT
        )

        self._llm = None
        self._sentiment = None
        self._kafka_producer = None
        self._llm_contexts: Dict[str, Dict] = {}  # symbol → last LLM result for tier2 detail

    # ── Lazy init helpers ──────────────────────────────────────────────────

    @staticmethod
    def _read_funnel_mode() -> str:
        """Read funnel_mode from scanner_config.yaml, defaulting to long_only."""
        try:
            cfg_path = PROJECT_ROOT / "config" / "scanner_config.yaml"
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f)
            mode = cfg.get("funnel_mode", "long_only")
            if mode not in ("long_only", "bidirectional"):
                logger.warning(f"Unknown funnel_mode '{mode}', falling back to long_only")
                return "long_only"
            return mode
        except Exception as e:
            logger.warning(f"Could not read funnel_mode from config: {e}")
            return "long_only"

    def _get_llm(self):
        """Lazy-init LLM provider (DeepSeek — cheap, good for batch scoring)."""
        if self._llm is not None:
            return self._llm

        from core.llm.factory import LLMFactory

        # Prefer DeepSeek (cheapest), fall back to configured provider
        provider = os.getenv("CATALYST_LLM_PROVIDER", "deepseek").lower()
        api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
        model = os.getenv("CATALYST_LLM_MODEL", "deepseek-chat")

        if not api_key:
            raise RuntimeError("No LLM API key found (DEEPSEEK_API_KEY or OPENAI_API_KEY)")

        self._llm = LLMFactory.create_provider(
            provider_name=provider,
            api_key=api_key,
            model=model,
        )
        logger.info(f"CatalystScorer LLM: {provider}/{model}")
        return self._llm

    def _get_sentiment(self):
        """Lazy-init SentimentAnalyst (Polygon news)."""
        if self._sentiment is not None:
            return self._sentiment

        from services.sentiment_analyst import SentimentAnalyst
        self._sentiment = SentimentAnalyst(redis_client=self.r)
        return self._sentiment

    # ── News Hash Cache ────────────────────────────────────────────────────

    @staticmethod
    def _hash_headlines(headlines: List[str]) -> str:
        """Canonicalize headlines → SHA-256 hash."""
        normalized = "|".join(sorted(h.lower().strip() for h in headlines if h))
        return hashlib.sha256(normalized.encode()).hexdigest()

    def _is_news_cached(self, symbol: str, headlines: List[str]) -> bool:
        """Return True if headlines unchanged since last scoring → skip LLM."""
        new_hash = self._hash_headlines(headlines)
        cache_key = f"{NEWS_HASH_PREFIX}{symbol}"
        old_hash = self.r.get(cache_key)
        return old_hash == new_hash

    def _update_news_hash(self, symbol: str, headlines: List[str]):
        """Store news hash with 24h TTL."""
        cache_key = f"{NEWS_HASH_PREFIX}{symbol}"
        self.r.set(cache_key, self._hash_headlines(headlines), ex=86400)

    # ── Capital Flow ──────────────────────────────────────────────────────

    def _get_capital_flow_score(self, symbol: str) -> Optional[float]:
        """Read capital flow score from Redis (set by scanner enrichment).

        Returns 0.0-1.0 score or None if no data available.
        CRITICAL: Returns None (not 0.0) when missing — 0.0 would mean "extremely bearish".
        """
        try:
            raw = self.r.get(f"capital_flow:{symbol}")
            if raw is not None:
                score = float(raw)
                if 0.0 <= score <= 1.0:
                    return score
        except (ValueError, TypeError):
            pass
        return None

    # ── Regime ─────────────────────────────────────────────────────────────

    def _get_regime(self) -> str:
        """Read current market regime from Redis with freshness check.

        P1: If regime data is >6h old, falls back to NEUTRAL.
        """
        try:
            raw = self.r.get("market_regime:current")
            if raw:
                data = json.loads(raw)
                # Check freshness via timestamp field
                ts = data.get("timestamp")
                if ts:
                    from datetime import datetime as _dt, timezone as _tz
                    try:
                        ts_dt = _dt.fromisoformat(ts)
                        # Ensure both sides are timezone-aware for comparison
                        now = _dt.now(_tz.utc)
                        if ts_dt.tzinfo is None:
                            ts_dt = ts_dt.replace(tzinfo=_tz.utc)
                        else:
                            ts_dt = ts_dt.astimezone(_tz.utc)
                        age_hours = (now - ts_dt).total_seconds() / 3600
                        if age_hours > 6:
                            logger.warning(
                                f"⚠️ Regime data stale ({age_hours:.1f}h > 6h), "
                                f"falling back to NEUTRAL"
                            )
                            return "NEUTRAL"
                    except (ValueError, TypeError):
                        pass  # Can't parse timestamp — proceed
                # Writer uses "regime" key; fallback to "market_regime" for compat
                return data.get("regime", data.get("market_regime", "NEUTRAL")).upper()
        except Exception:
            pass
        return "NEUTRAL"

    # ── LLM Call (with fallback chain) ─────────────────────────────────────

    # Provider fallback chain: try each until one succeeds
    LLM_FALLBACK_CHAIN = [
        ("deepseek", "DEEPSEEK_API_KEY", "deepseek-chat"),
        ("openai",   "OPENAI_API_KEY",   "gpt-4o-mini"),
        ("xai",      "XAI_API_KEY",      "grok-3-mini-fast"),
    ]

    def _call_llm_with_fallback(self, symbol: str, headlines: List[str], tech_ctx: Dict) -> Optional[Dict]:
        """
        Call LLM with automatic fallback across providers.
        Tries DeepSeek (cheapest) → OpenAI → xAI.
        """
        prompt = self.catalyst_prompt.format(
            symbol=symbol,
            headlines="\n".join(f"- {h}" for h in headlines[:10]),
            price=tech_ctx.get("price", 0),
            gap_pct=round(tech_ctx.get("gap_percent", 0) * 100, 2),
            rv=tech_ctx.get("relative_volume", 0),
            beta=tech_ctx.get("beta", 0),
        )

        for provider_name, env_key, model in self.LLM_FALLBACK_CHAIN:
            api_key = os.getenv(env_key)
            if not api_key:
                continue
            try:
                from core.llm.factory import LLMFactory
                llm = LLMFactory.create_provider(
                    provider_name=provider_name,
                    api_key=api_key,
                    model=os.getenv("CATALYST_LLM_MODEL", model),
                )
                raw = llm.invoke(prompt, temperature=0.3, max_tokens=300)
                result = self._parse_llm_response(raw)
                if result:
                    logger.debug(f"LLM ({provider_name}) succeeded for {symbol}")
                    return result
            except Exception as e:
                logger.warning(f"LLM ({provider_name}) failed for {symbol}: {e}")
                continue

        logger.warning(f"All LLM providers failed for {symbol}")
        return None

    @staticmethod
    def _parse_llm_response(raw: str) -> Optional[Dict]:
        """Extract JSON from LLM response (handles markdown code blocks)."""
        # Strip markdown code fences if present
        text = re.sub(r"```json\s*", "", raw)
        text = re.sub(r"```\s*", "", text)
        text = text.strip()

        # Try to find JSON object in the response
        match = re.search(r"\{[^{}]+\}", text, re.DOTALL)
        if not match:
            return None

        try:
            data = json.loads(match.group())
            # Validate required fields
            if "has_catalyst" in data and "score" in data:
                data["has_catalyst"] = bool(data["has_catalyst"])
                data["score"] = float(data["score"])
                data.setdefault("catalyst_type", "none")
                data.setdefault("summary", "")
                return data
        except (json.JSONDecodeError, ValueError):
            pass

        return None

    # ── Single Symbol Scoring ──────────────────────────────────────────────

    def score_symbol(self, symbol: str, tech_score: float, tech_detail: Dict) -> Optional[float]:
        """
        Score a single symbol. Returns composite score (0.0-1.0) or None if expelled.

        Steps:
        1. Fetch news via SentimentAnalyst (Polygon, cached 60min)
        2. Check news hash cache → skip LLM if unchanged
        3. Call LLM for catalyst analysis
        4. Apply regime multiplier
        5. Expel if no catalyst
        """
        # 1. Fetch news
        try:
            sentiment = self._get_sentiment()
            news_result = sentiment.analyze_symbol(symbol, use_cache=True, fallback_to_search=False)
            headlines = news_result.get("key_headlines", [])
            sentiment_score = news_result.get("sentiment_score", 0.5)
        except Exception as e:
            logger.warning(f"News fetch failed for {symbol}: {e}")
            headlines = []
            sentiment_score = 0.5

        # 2. News hash cache check
        if headlines and self._is_news_cached(symbol, headlines):
            logger.debug(f"{symbol}: news unchanged, keeping tier1 tech score")
            composite = min(tech_score * 0.7 + sentiment_score * 0.3, 1.0)
            regime = self._get_regime()
            composite *= self.regime_multipliers.get(regime, 1.0)
            return round(min(composite, 1.0), 4)

        # 3. No headlines at all — score purely on tech + sentiment
        if not headlines:
            logger.debug(f"{symbol}: no news, using tech + regime only")
            composite = min(tech_score * 0.8 + sentiment_score * 0.2, 1.0)
            regime = self._get_regime()
            composite *= self.regime_multipliers.get(regime, 1.0)
            return round(min(composite, 1.0), 4)

        # 4. Call LLM
        llm_result = self._call_llm_with_fallback(symbol, headlines, tech_detail)

        if llm_result is None:
            # LLM failed — keep symbol with tech score, don't expel
            logger.warning(f"{symbol}: LLM failed, keeping with tech score")
            composite = min(tech_score * 0.75 + sentiment_score * 0.25, 1.0)
            return round(composite, 4)

        # 5. Soft handling — no catalyst: keep with reduced score (tech-dominant)
        if not llm_result.get("has_catalyst", False):
            logger.info(f"CatalystScorer: {symbol} no catalyst → kept with tech-dominant score")
            self._update_news_hash(symbol, headlines)
            composite = min(tech_score * 0.70 + sentiment_score * 0.30, 1.0)
            regime = self._get_regime()
            composite *= self.regime_multipliers.get(regime, 1.0)
            # Store minimal context
            self._llm_contexts[symbol] = {
                "has_catalyst": False,
                "catalyst_type": "none",
                "score": 0.0,
                "summary": llm_result.get("summary", "No catalyst identified"),
            }
            return round(min(composite, 1.0), 4)

        # 6. Compute composite: blend LLM catalyst score with tech score + capital flow
        catalyst_score_norm = min(llm_result["score"] / 10.0, 1.0)
        cf_score = self._get_capital_flow_score(symbol)
        if cf_score is not None:
            composite = (
                tech_score * 0.40 +
                catalyst_score_norm * 0.30 +
                sentiment_score * 0.15 +
                cf_score * 0.15
            )
        else:
            composite = (
                tech_score * 0.45 +
                catalyst_score_norm * 0.35 +
                sentiment_score * 0.20
            )

        # 7. Apply regime multiplier
        regime = self._get_regime()
        composite *= self.regime_multipliers.get(regime, 1.0)
        composite = round(min(composite, 1.0), 4)

        # 8. Update news hash cache
        self._update_news_hash(symbol, headlines)

        # 9. Store LLM context for tier2 detail enrichment (used by L3 agent)
        self._llm_contexts[symbol] = llm_result

        logger.info(
            f"CatalystScorer: {symbol} → {composite:.3f} "
            f"(catalyst={llm_result['catalyst_type']}, score={llm_result['score']:.1f}, "
            f"regime={regime})"
        )
        return composite

    # ── Batch Pipeline ─────────────────────────────────────────────────────

    def run(self) -> int:
        """
        Run full L2 scoring pipeline. Returns number of symbols written to tier2.

        1. Read tier1 from Redis
        2. Score each symbol
        3. Write tier2
        4. Fire Kafka event
        """
        logger.info("CatalystScorer: starting L2 pipeline...")

        # 1. Read tier1
        tier1_raw = self.r.zrevrange(TIER1_KEY, 0, 39, withscores=True)
        if not tier1_raw:
            logger.warning("CatalystScorer: tier1 empty, nothing to score")
            return 0

        logger.info(f"CatalystScorer: scoring {len(tier1_raw)} tier1 symbols...")

        # 2. Score each symbol
        tier2_entries: List[Tuple[str, float]] = []
        expelled: List[str] = []
        llm_calls = 0

        for symbol, tech_score in tier1_raw:
            tech_score = float(tech_score)

            # Read tier1 details for tech context
            detail_raw = self.r.hget(TIER1_DET, symbol)
            tech_detail = json.loads(detail_raw) if detail_raw else {}

            composite = self.score_symbol(symbol, tech_score, tech_detail)

            if composite is None:
                expelled.append(symbol)
                continue

            tier2_entries.append((symbol, composite))

            # Rate limit: ~0.5s between symbols to avoid LLM rate limit
            time.sleep(0.3)

        # 3. Write tier2 to Redis
        if tier2_entries:
            pipe = self.r.pipeline()
            pipe.delete(TIER2_KEY)
            pipe.delete(TIER2_DET)

            for symbol, score in tier2_entries:
                pipe.zadd(TIER2_KEY, {symbol: score})

                # Build tier2 detail
                detail_raw = self.r.hget(TIER1_DET, symbol)
                detail = json.loads(detail_raw) if detail_raw else {}
                detail["catalyst_score"] = round(score * 10, 2)  # 0-10 scale
                detail["regime"] = self._get_regime()
                # Inject LLM analysis context for L3 agent consumption
                llm_ctx = self._llm_contexts.get(symbol, {})
                detail["catalyst_type"] = llm_ctx.get("catalyst_type", "none")
                detail["has_catalyst"] = llm_ctx.get("has_catalyst", False)
                detail["llm_summary"] = llm_ctx.get("summary", "")
                detail["llm_raw_score"] = llm_ctx.get("score", 0)
                pipe.hset(TIER2_DET, symbol, json.dumps(detail))

            pipe.set(TIER2_SCORED, datetime.now().isoformat(), ex=86400)
            pipe.expire(TIER2_KEY, 28800)   # 8h TTL
            pipe.expire(TIER2_DET, 28800)
            pipe.execute()

        # 4. Fire Kafka event
        self._fire_kafka_event(len(tier2_entries), [s for s, _ in tier2_entries[:8]])

        # 5. Bridge tier2 → watchlist:current + watchlist:ranked for downstream consumers
        self._publish_final_watchlist()

        logger.info(
            f"CatalystScorer: done — {len(tier2_entries)} in tier2, "
            f"{len(expelled)} expelled"
        )
        if expelled:
            logger.info(f"  Expelled: {expelled}")

        return len(tier2_entries)

    def _publish_final_watchlist(self, top_n: int = 10):
        """Bridge: tier2 → watchlist:current + watchlist:ranked for consumers."""
        ranked = self.r.zrevrange(TIER2_KEY, 0, top_n - 1, withscores=True)
        if not ranked:
            return

        # watchlist:current — JSON list of dicts (format ibkr_streamer expects)
        current_list = [{"symbol": sym} for sym, _ in ranked]
        self.r.set("watchlist:current", json.dumps(current_list), ex=28800)

        # watchlist:ranked — JSON dict with "watchlist" array (format ibkr_streamer expects)
        ranked_list = [{"symbol": sym, "score": float(score)} for sym, score in ranked]
        ranked_payload = {"watchlist": ranked_list, "updated_at": datetime.now().isoformat()}
        self.r.set("watchlist:ranked", json.dumps(ranked_payload), ex=28800)

        logger.info(f"Bridge: tier2 → watchlist:current ({len(current_list)} symbols)")

    def _get_kafka_producer(self):
        """Lazy-init a single KafkaProducer, reused across calls."""
        if self._kafka_producer is not None:
            return self._kafka_producer
        try:
            from kafka import KafkaProducer
            self._kafka_producer = KafkaProducer(
                bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"),
                value_serializer=lambda v: json.dumps(v).encode("utf-8"),
                acks="all",
            )
            return self._kafka_producer
        except Exception as e:
            logger.warning(f"KafkaProducer init failed: {e}")
            return None

    def _fire_kafka_event(self, count: int, top_symbols: List[str]):
        """Fire events.watchlist.tier2_ready via Kafka (reuses long-lived producer)."""
        kp = self._get_kafka_producer()
        if not kp:
            return
        try:
            kp.send("events.watchlist.tier2_ready", {
                "timestamp": datetime.now().isoformat(),
                "count": count,
                "top_symbols": top_symbols,
            })
            logger.info(f"Kafka → events.watchlist.tier2_ready ({count} symbols)")
        except Exception as e:
            logger.warning(f"Kafka tier2_ready event failed: {e}")
            self._kafka_producer = None  # Reset so next call re-initializes
