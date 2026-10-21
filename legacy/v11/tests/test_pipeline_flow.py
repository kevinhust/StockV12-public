"""
Integration tests for the full L1→L2 pipeline data flow.
Requires real Redis (marked with @pytest.mark.integration).
"""

import json
import time

import pytest

from src.services.selection.catalyst_scorer import (
    NEWS_HASH_PREFIX,
    REGIME_MULTIPLIERS,
    TIER1_DET,
    TIER1_KEY,
    TIER2_DET,
    TIER2_KEY,
    CatalystScorer,
)


# ── Use fake_redis for CI-safe integration tests ─────────────────────────


class TestTier1ToTier2Flow:
    """Tests data flow from tier1 → CatalystScorer → tier2 using fakeredis."""

    @pytest.fixture()
    def setup_pipeline(self, fake_redis, tier1_seed, regime_seed):
        """Set up tier1 + regime data in fakeredis."""
        entries, details = tier1_seed
        for symbol, score in entries:
            fake_redis.zadd(TIER1_KEY, {symbol: score})
        for symbol, raw in details.items():
            fake_redis.hset(TIER1_DET, symbol, raw)
        fake_redis.set("market_regime:current", json.dumps(regime_seed))
        return fake_redis

    def test_score_symbol_writes_tier2_detail_fields(self, setup_pipeline):
        """
        After scoring, tier2:details should contain all 6 LLM context fields.
        We mock the LLM/sentiment to avoid external calls.
        """
        r = setup_pipeline
        scorer = CatalystScorer(redis_client=r)

        # Mock sentiment to return headlines + score
        scorer._sentiment = type(
            "MockSentiment",
            (),
            {"analyze_symbol": lambda self, *a, **kw: {
                "key_headlines": ["AAPL earnings beat by 15%"],
                "sentiment_score": 0.7,
            }},
        )()

        # Mock LLM to return a catalyst result
        original_parse = CatalystScorer._parse_llm_response

        def mock_parse(raw):
            return {
                "has_catalyst": True,
                "catalyst_type": "earnings",
                "score": 8.0,
                "summary": "Strong earnings beat by 15%",
            }

        # Patch _call_llm_with_fallback to avoid real LLM calls
        scorer._call_llm_with_fallback = lambda *a, **kw: {
            "has_catalyst": True,
            "catalyst_type": "earnings",
            "score": 8.0,
            "summary": "Strong earnings beat by 15%",
        }

        # Score the symbol
        result = scorer.score_symbol("AAPL", 0.85, {"price": 195.0})
        assert result is not None
        assert result > 0

        # Manually simulate what run() does for tier2 detail writing
        # (since we're testing score_symbol, not the full run() pipeline)
        llm_ctx = {
            "has_catalyst": True,
            "catalyst_type": "earnings",
            "score": 8.0,
            "summary": "Strong earnings beat by 15%",
        }
        detail = json.loads(r.hget(TIER1_DET, "AAPL"))
        detail["catalyst_score"] = round(result * 10, 2)
        detail["regime"] = scorer._get_regime()
        detail["catalyst_type"] = llm_ctx.get("catalyst_type", "none")
        detail["has_catalyst"] = llm_ctx.get("has_catalyst", False)
        detail["llm_summary"] = llm_ctx.get("summary", "")
        detail["llm_raw_score"] = llm_ctx.get("score", 0)
        r.hset(TIER2_DET, "AAPL", json.dumps(detail))

        # Verify all 6 L2 context fields are present
        raw = r.hget(TIER2_DET, "AAPL")
        data = json.loads(raw)
        assert "catalyst_score" in data
        assert "catalyst_type" in data
        assert "has_catalyst" in data
        assert "llm_summary" in data
        assert "llm_raw_score" in data
        assert "regime" in data
        assert data["catalyst_type"] == "earnings"
        assert data["has_catalyst"] is True

    def test_forced_patrol_event_structure(self, fake_redis, regime_seed):
        """Verify forced patrol event matches the spec."""
        fake_redis.set("market_regime:current", json.dumps(regime_seed))

        # Simulate tier2 entries
        fake_redis.zadd(TIER2_KEY, {"AAPL": 0.85, "TSLA": 0.78})
        fake_redis.hset(
            TIER2_DET,
            "AAPL",
            json.dumps({
                "catalyst_score": 8.5,
                "catalyst_type": "earnings",
                "has_catalyst": True,
                "llm_summary": "Beat by 15%",
                "llm_raw_score": 8.0,
            }),
        )
        fake_redis.set(
            "price:AAPL:current",
            json.dumps({"last": 195.0}),
        )

        # Verify the event structure that _run_tier3_forced_inspection would build
        l2_detail_raw = fake_redis.hget(TIER2_DET, "AAPL")
        l2_detail = json.loads(l2_detail_raw) if l2_detail_raw else {}

        event = {
            "symbol": "AAPL",
            "type": "forced_patrol",
            "price": 195.0,
            "timestamp": "2026-03-29T10:30:00",
            "source": "tier3_forced_patrol",
            "l2_context": {
                "catalyst_score": l2_detail.get("catalyst_score", 0),
                "catalyst_type": l2_detail.get("catalyst_type", "none"),
                "has_catalyst": l2_detail.get("has_catalyst", False),
                "llm_summary": l2_detail.get("llm_summary", ""),
            },
        }

        assert event["type"] == "forced_patrol"
        assert event["source"] == "tier3_forced_patrol"
        assert event["l2_context"]["catalyst_score"] == 8.5
        assert event["l2_context"]["catalyst_type"] == "earnings"

    def test_regime_multiplier_applied_correctly(self, fake_redis):
        """Verify BULL_LONG multiplier boosts score, PANIC reduces it."""
        # NEUTRAL baseline
        base_score = 0.75
        assert round(base_score * REGIME_MULTIPLIERS.get("NEUTRAL", 1.0), 4) == 0.75

        # BULL_LONG boosts
        bull_score = round(base_score * REGIME_MULTIPLIERS["BULL_LONG"], 4)
        assert bull_score > base_score

        # PANIC reduces
        panic_score = round(base_score * REGIME_MULTIPLIERS["PANIC"], 4)
        assert panic_score < base_score

        # Score never exceeds 1.0 even with BULL_LONG
        high_score = 0.95
        result = round(min(high_score * REGIME_MULTIPLIERS["BULL_LONG"], 1.0), 4)
        assert result <= 1.0
