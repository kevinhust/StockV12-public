"""
Unit tests for CatalystScorer — L2 of the three-layer funnel.
"""

import json
import time

import pytest

from src.services.selection.catalyst_scorer import (
    CATALYST_PROMPT,
    NEWS_HASH_PREFIX,
    REGIME_MULTIPLIERS,
    TIER1_DET,
    TIER1_KEY,
    TIER2_DET,
    TIER2_KEY,
    CatalystScorer,
)


# ── _hash_headlines ──────────────────────────────────────────────────────


class TestHashHeadlines:
    def test_deterministic(self):
        headlines = ["AAPL earnings beat", "TSLA new model"]
        h1 = CatalystScorer._hash_headlines(headlines)
        h2 = CatalystScorer._hash_headlines(headlines)
        assert h1 == h2

    def test_order_independent(self):
        """Sorted before hashing → order doesn't matter."""
        h1 = CatalystScorer._hash_headlines(["A", "B", "C"])
        h2 = CatalystScorer._hash_headlines(["C", "A", "B"])
        assert h1 == h2

    def test_case_insensitive(self):
        h1 = CatalystScorer._hash_headlines(["AAPL Earnings"])
        h2 = CatalystScorer._hash_headlines(["aapl earnings"])
        assert h1 == h2

    def test_empty_headlines(self):
        h = CatalystScorer._hash_headlines([])
        assert isinstance(h, str) and len(h) == 64  # SHA-256 hex digest


# ── _is_news_cached ──────────────────────────────────────────────────────


class TestIsNewsCached:
    def test_returns_true_when_hash_matches(self, fake_redis):
        headlines = ["AAPL earnings beat"]
        h = CatalystScorer._hash_headlines(headlines)
        fake_redis.set(f"{NEWS_HASH_PREFIX}AAPL", h)

        scorer = CatalystScorer(redis_client=fake_redis)
        assert scorer._is_news_cached("AAPL", headlines) is True

    def test_returns_false_when_no_cache(self, fake_redis):
        scorer = CatalystScorer(redis_client=fake_redis)
        assert scorer._is_news_cached("AAPL", ["some headline"]) is False

    def test_returns_false_when_hash_differs(self, fake_redis):
        fake_redis.set(f"{NEWS_HASH_PREFIX}AAPL", "old_hash")
        scorer = CatalystScorer(redis_client=fake_redis)
        assert scorer._is_news_cached("AAPL", ["new headline"]) is False


# ── _parse_llm_response ──────────────────────────────────────────────────


class TestParseLlmResponse:
    def test_handles_markdown_code_blocks(self):
        raw = '```json\n{"has_catalyst": true, "score": 8.5, "catalyst_type": "earnings", "summary": "Beat by 15%"}\n```'
        result = CatalystScorer._parse_llm_response(raw)
        assert result is not None
        assert result["has_catalyst"] is True
        assert result["score"] == 8.5

    def test_plain_json(self):
        raw = '{"has_catalyst": false, "score": 2.0}'
        result = CatalystScorer._parse_llm_response(raw)
        assert result is not None
        assert result["has_catalyst"] is False
        assert result["score"] == 2.0
        assert result["catalyst_type"] == "none"  # default
        assert result["summary"] == ""  # default

    def test_returns_none_for_invalid_json(self):
        raw = "I cannot determine the catalyst for this stock."
        assert CatalystScorer._parse_llm_response(raw) is None

    def test_returns_none_for_missing_required_fields(self):
        raw = '{"some_field": true}'
        assert CatalystScorer._parse_llm_response(raw) is None

    def test_extracts_json_embedded_in_text(self):
        raw = 'Here is my analysis:\n{"has_catalyst": true, "score": 7.0}\nThat is all.'
        result = CatalystScorer._parse_llm_response(raw)
        assert result is not None
        assert result["has_catalyst"] is True


# ── _get_regime ──────────────────────────────────────────────────────────


class TestGetRegime:
    def test_reads_regime_from_redis(self, fake_redis):
        fake_redis.set(
            "market_regime:current",
            json.dumps({"regime": "BULL_LONG"}),
        )
        scorer = CatalystScorer(redis_client=fake_redis)
        assert scorer._get_regime() == "BULL_LONG"

    def test_fallback_market_regime_key(self, fake_redis):
        fake_redis.set(
            "market_regime:current",
            json.dumps({"market_regime": "BEAR_LONG"}),
        )
        scorer = CatalystScorer(redis_client=fake_redis)
        assert scorer._get_regime() == "BEAR_LONG"

    def test_defaults_to_neutral(self, fake_redis):
        scorer = CatalystScorer(redis_client=fake_redis)
        assert scorer._get_regime() == "NEUTRAL"


# ── REGIME_MULTIPLIERS ──────────────────────────────────────────────────


class TestRegimeMultipliers:
    def test_all_six_regimes_present(self):
        expected = {"BULL_LONG", "BULL_SHORT", "NEUTRAL", "BEAR_SHORT", "BEAR_LONG", "PANIC"}
        assert expected == set(REGIME_MULTIPLIERS.keys())

    def test_bull_long_greater_than_one(self):
        assert REGIME_MULTIPLIERS["BULL_LONG"] > 1.0

    def test_panic_less_than_one(self):
        assert REGIME_MULTIPLIERS["PANIC"] < 1.0

    def test_neutral_is_one(self):
        assert REGIME_MULTIPLIERS["NEUTRAL"] == 1.0


# ── Cached path applies regime multiplier ────────────────────────────────


class TestCachedPathRegimeMultiplier:
    def test_regime_multiplier_applied_on_cached_path(self, fake_redis, regime_seed):
        """
        Phase 5 bug fix: cached path must apply regime multiplier.
        When headlines are unchanged (cached), the composite score should
        still reflect the current regime multiplier.
        """
        fake_redis.set(
            "market_regime:current",
            json.dumps(regime_seed),
        )
        headlines = ["AAPL beat earnings"]
        fake_redis.set(
            f"{NEWS_HASH_PREFIX}AAPL",
            CatalystScorer._hash_headlines(headlines),
        )

        # We need to mock the sentiment analyst
        scorer = CatalystScorer(redis_client=fake_redis)
        # Patch _get_sentiment to return controlled data
        scorer._sentiment = type(
            "MockSentiment",
            (),
            {"analyze_symbol": lambda self, *a, **kw: {"key_headlines": headlines, "sentiment_score": 0.6}},
        )()

        composite = scorer.score_symbol("AAPL", 0.8, {"price": 195.0})
        assert composite is not None
        # BULL_LONG multiplier = 1.2 → score should be boosted
        # Without multiplier: min(0.8*0.7 + 0.6*0.3, 1.0) = 0.74
        # With BULL_LONG: 0.74 * 1.2 = 0.888
        assert composite > 0.74  # multiplier applied


# ── Redis key consistency ────────────────────────────────────────────────


class TestRedisKeys:
    def test_tier_keys_are_consistent(self):
        assert TIER1_KEY == "watchlist:tier1"
        assert TIER1_DET == "watchlist:tier1:details"
        assert TIER2_KEY == "watchlist:tier2"
        assert TIER2_DET == "watchlist:tier2:details"

    def test_news_hash_prefix(self):
        assert NEWS_HASH_PREFIX == "watchlist:tier2:last_news_hash:"
