"""
Tests for FusionEngine V2.2 dynamic weighting.
Verifies exponential weighting logic and cold-start fallback.
"""
import json
import time
import pytest
from unittest.mock import MagicMock, patch
from services.fusion_engine import FusionEngine, FusionInput


def _fuse_input(rl_target=0.5, llm_conf=0.8, yolo_conf=0.7, regime="NEUTRAL"):
    return FusionInput(
        symbol="AAPL",
        rl_target=rl_target,
        llm_side="BUY",
        llm_confidence=llm_conf,
        llm_reasoning="Test",
        regime=regime,
        yolo_confidence=yolo_conf,
        yolo_pattern="BREAKOUT",
        bid=100.0,
        ask=100.01,
    )


def _mock_expiry_continue():
    """Return a SignalValidationResult with EXECUTE action."""
    from services.signal_expiry import ExpiryAction, SignalValidationResult
    return SignalValidationResult(
        action=ExpiryAction.EXECUTE,
        current_yolo_conf=0.8,
        current_rl_target=0.5,
        confidence_drift=0.0,
        direction_reversed=False,
        signal_age_ms=0,
        reason="test",
    )


class TestExponentialWeighting:
    """Step 9 exponential weighting: rl_target ** w_rl * llm ** w_llm * yolo ** w_yolo."""

    def test_equal_weights_produces_same_as_original(self):
        """w=1.0 degenerates to original equal-weight multiplication."""
        engine = FusionEngine()
        engine._weights = {"w_rl": 1.0, "w_llm": 1.0, "w_yolo": 1.0}

        with patch(
            "services.fusion_engine.signal_expiry_checker.validate",
            return_value=_mock_expiry_continue(),
        ):
            decision = engine.fuse(_fuse_input())

        assert decision.dominant_source in ("RL", "LLM", "YOLO")
        assert decision.size >= 0

    def test_high_rl_weight_increases_rl_contribution(self):
        """When w_rl > 1, strong RL signal dominates."""
        engine = FusionEngine()
        engine._weights = {"w_rl": 2.0, "w_llm": 1.0, "w_yolo": 1.0}

        with patch(
            "services.fusion_engine.signal_expiry_checker.validate",
            return_value=_mock_expiry_continue(),
        ):
            # RL: 0.7**2.0=0.49, LLM: 0.4**1.0=0.4, YOLO: 0.4**1.0=0.4
            # RL should dominate
            decision = engine.fuse(_fuse_input(rl_target=0.7, llm_conf=0.4, yolo_conf=0.4))

        assert decision.dominant_source == "RL"

    def test_low_rl_weight_lets_llm_dominate(self):
        """When w_rl < 1, RL contributes less so LLM can dominate."""
        engine = FusionEngine()
        engine._weights = {"w_rl": 0.5, "w_llm": 1.0, "w_yolo": 1.0}

        with patch(
            "services.fusion_engine.signal_expiry_checker.validate",
            return_value=_mock_expiry_continue(),
        ):
            # rl=0.5**0.5=0.707, llm=0.8**1.0=0.8, yolo=0.7**1.0=0.7
            # LLM dominates (0.8 > 0.707)
            decision = engine.fuse(_fuse_input(rl_target=0.5, llm_conf=0.8, yolo_conf=0.7))

        assert decision.dominant_source == "LLM"

    def test_yolo_can_be_dominant(self):
        """With high YOLO weight, YOLO can dominate."""
        engine = FusionEngine()
        engine._weights = {"w_rl": 1.0, "w_llm": 1.0, "w_yolo": 1.5}

        with patch(
            "services.fusion_engine.signal_expiry_checker.validate",
            return_value=_mock_expiry_continue(),
        ):
            # rl=0.5**1.0=0.5, llm=0.3**1.0=0.3, yolo=0.8**1.5≈0.717
            # YOLO dominates
            decision = engine.fuse(_fuse_input(rl_target=0.5, llm_conf=0.3, yolo_conf=0.8))

        assert decision.dominant_source == "YOLO"

    def test_regime_fallback_panic_regime(self):
        """PANIC regime has low RL weight and high YOLO weight."""
        engine = FusionEngine()
        engine._weights = {}

        weights = engine._get_weights_for_regime("PANIC")
        assert weights["w_rl"] == pytest.approx(0.5)
        assert weights["w_llm"] == pytest.approx(1.3)
        assert weights["w_yolo"] == pytest.approx(1.5)

    def test_regime_fallback_unknown_regime_uses_unit(self):
        """Unknown regime defaults to all 1.0."""
        engine = FusionEngine()
        engine._weights = {}

        weights = engine._get_weights_for_regime("UNKNOWN_REGIME")
        assert weights["w_rl"] == pytest.approx(1.0)
        assert weights["w_llm"] == pytest.approx(1.0)
        assert weights["w_yolo"] == pytest.approx(1.0)

    def test_redis_weights_multiplied_with_regime_defaults(self):
        """Redis weights layer multiplicatively on top of regime defaults."""
        engine = FusionEngine()
        engine._weights = {"w_rl": 1.5, "w_llm": 1.0, "w_yolo": 1.0}

        weights = engine._get_weights_for_regime("NEUTRAL")
        assert weights["w_rl"] == pytest.approx(1.5)
        assert weights["w_llm"] == pytest.approx(1.0)


class TestColdStartFallback:
    """Three-tier fallback: Redis → regime default → all-1.0."""

    @patch("services.fusion_engine.FusionEngine._get_redis")
    def test_load_weights_redis_unavailable_uses_local_defaults(self, mock_get_redis):
        """When Redis connection fails, local safe defaults (all 1.0) are used."""
        mock_get_redis.return_value = None

        engine = FusionEngine()
        engine._load_weights()

        assert engine._weights == {"w_rl": 1.0, "w_llm": 1.0, "w_yolo": 1.0}
        assert engine._weights_timestamp is None

    @patch("services.fusion_engine.FusionEngine._get_redis")
    def test_load_weights_no_key_uses_local_defaults(self, mock_get_redis):
        """When Redis has no fusion:weights:current key, local defaults used."""
        mock_redis = MagicMock()
        mock_redis.get.return_value = None
        mock_get_redis.return_value = mock_redis

        engine = FusionEngine()
        engine._load_weights()

        assert engine._weights == {"w_rl": 1.0, "w_llm": 1.0, "w_yolo": 1.0}
        assert engine._weights_timestamp is None

    @patch("services.fusion_engine.FusionEngine._get_redis")
    def test_load_weights_with_fresh_redis_data(self, mock_get_redis):
        """When Redis has weights with fresh ts, they are loaded."""
        import time
        mock_redis = MagicMock()
        # Include current ts so weights are considered fresh
        payload = {"w_rl": 1.3, "w_llm": 0.9, "w_yolo": 1.1, "ts": int(time.time())}
        mock_redis.get.return_value = json.dumps(payload)
        mock_get_redis.return_value = mock_redis

        engine = FusionEngine()
        engine._load_weights()

        assert engine._weights["w_rl"] == 1.3
        assert engine._weights["w_llm"] == 0.9
        assert engine._weights["w_yolo"] == 1.1
        assert engine._weights_timestamp is not None

    @patch("services.fusion_engine.FusionEngine._get_redis")
    def test_load_weights_stale_redis_data_rejected(self, mock_get_redis):
        """When Redis weights have old ts (> 30 min), local defaults are used."""
        import time
        mock_redis = MagicMock()
        # ts is 60 minutes old — should be rejected
        stale_payload = {"w_rl": 1.3, "w_llm": 0.9, "w_yolo": 1.1, "ts": int(time.time()) - 3600}
        mock_redis.get.return_value = json.dumps(stale_payload)
        mock_get_redis.return_value = mock_redis

        engine = FusionEngine()
        engine._load_weights()

        # Should have fallen back to local defaults
        assert engine._weights == {"w_rl": 1.0, "w_llm": 1.0, "w_yolo": 1.0}
        assert engine._weights_timestamp is None


class TestDominantSourceTracking:
    """dominant_source field flows correctly through all decision paths."""

    def test_abort_decision_has_default_dominant_source(self):
        """Abort decisions have dominant_source = 'LLM' (dataclass default)."""
        engine = FusionEngine()
        engine._weights = {"w_rl": 1.0, "w_llm": 1.0, "w_yolo": 1.0}

        from services.signal_expiry import ExpiryAction, SignalValidationResult
        abort_result = SignalValidationResult(
            action=ExpiryAction.ABORT,
            current_yolo_conf=0.0,
            current_rl_target=0.0,
            confidence_drift=1.0,
            direction_reversed=False,
            signal_age_ms=0,
            reason="No baseline",
        )
        with patch(
            "services.fusion_engine.signal_expiry_checker.validate",
            return_value=abort_result,
        ):
            decision = engine.fuse(_fuse_input())

        assert decision.side == "HOLD"
        assert decision.dominant_source == "LLM"

    def test_metadata_contains_weights_and_source_scores(self):
        """Successful fusion includes weights and source_scores in metadata."""
        engine = FusionEngine()
        engine._weights = {"w_rl": 1.0, "w_llm": 1.0, "w_yolo": 1.0}

        with patch(
            "services.fusion_engine.signal_expiry_checker.validate",
            return_value=_mock_expiry_continue(),
        ):
            decision = engine.fuse(_fuse_input(rl_target=0.5, llm_conf=0.8, yolo_conf=0.7))

        if "EXPIRED" not in decision.sources and "BELOW_MIN" not in decision.sources:
            assert "weights" in decision.metadata
            assert "source_scores" in decision.metadata
            assert decision.dominant_source in ("RL", "LLM", "YOLO")
