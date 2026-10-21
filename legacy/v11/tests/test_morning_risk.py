"""
Tests for morning risk override loading pipeline.
Covers: guardrails, override loading, review queue, CLI.
"""
import json
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────
#  Guardrails
# ─────────────────────────────────────────────

class TestGuardrails:
    """Three-layer guardrail validation for risk overrides."""

    def _make_gatekeeper(self):
        """Create a HardRiskGatekeeper with mocked Redis."""
        with patch("src.core.risk_controller.redis.Redis") as mock_redis_cls:
            mock_r = MagicMock()
            mock_r.get.return_value = None  # No existing blacklist/overrides
            mock_redis_cls.return_value = mock_r
            from src.core.risk_controller import HardRiskGatekeeper
            gk = HardRiskGatekeeper()
            return gk

    def test_kelly_range_valid(self):
        gk = self._make_gatekeeper()
        errors = gk._validate_overrides({"kelly_fraction": 0.5}, "2026-03-30")
        assert not any("Kelly" in e for e in errors)

    def test_kelly_range_invalid_low(self):
        gk = self._make_gatekeeper()
        errors = gk._validate_overrides({"kelly_fraction": 0.05}, "2026-03-30")
        assert any("Kelly" in e for e in errors)

    def test_kelly_range_invalid_high(self):
        gk = self._make_gatekeeper()
        errors = gk._validate_overrides({"kelly_fraction": 1.5}, "2026-03-30")
        assert any("Kelly" in e for e in errors)

    def test_min_confidence_range_valid(self):
        gk = self._make_gatekeeper()
        errors = gk._validate_overrides({"min_confidence": 0.5}, "2026-03-30")
        assert not any("Min confidence" in e for e in errors)

    def test_min_confidence_range_invalid(self):
        gk = self._make_gatekeeper()
        errors = gk._validate_overrides({"min_confidence": 0.9}, "2026-03-30")
        assert any("Min confidence" in e for e in errors)

    def test_risk_scaling_range_valid(self):
        gk = self._make_gatekeeper()
        errors = gk._validate_overrides({"risk_scaling": 1.0}, "2026-03-30")
        assert not any("Risk scaling" in e for e in errors)

    def test_risk_scaling_range_invalid(self):
        gk = self._make_gatekeeper()
        errors = gk._validate_overrides({"risk_scaling": 0.1}, "2026-03-30")
        assert any("Risk scaling" in e for e in errors)

    def test_consistency_check_pass(self):
        gk = self._make_gatekeeper()
        errors = gk._validate_overrides(
            {"kelly_fraction": 0.4, "risk_scaling": 0.8}, "2026-03-30"
        )
        assert not any("Inconsistent" in e for e in errors)

    def test_consistency_check_fail(self):
        gk = self._make_gatekeeper()
        errors = gk._validate_overrides(
            {"kelly_fraction": 0.9, "risk_scaling": 0.4}, "2026-03-30"
        )
        assert any("Inconsistent" in e for e in errors)

    def test_recency_check_fresh(self):
        gk = self._make_gatekeeper()
        recent = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
        errors = gk._validate_overrides({"kelly_fraction": 0.5}, "2026-03-30", recent)
        assert not any("stale" in e.lower() for e in errors)

    def test_recency_check_stale(self):
        gk = self._make_gatekeeper()
        stale = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
        errors = gk._validate_overrides({"kelly_fraction": 0.5}, "2026-03-30", stale)
        assert any("stale" in e.lower() for e in errors)

    def test_no_overrides_no_errors(self):
        gk = self._make_gatekeeper()
        errors = gk._validate_overrides({}, "2026-03-30")
        assert errors == []


# ─────────────────────────────────────────────
#  Override Loading
# ─────────────────────────────────────────────

class TestOverrideLoading:
    """Test _load_approved_overrides from Redis."""

    def _make_gatekeeper_with_overrides(self, override_data=None):
        """Create gatekeeper with mocked Redis returning specific override data."""
        with patch("src.core.risk_controller.redis.Redis") as mock_redis_cls:
            mock_r = MagicMock()
            # First call: _load_blacklist_from_redis; second: _load_approved_overrides
            if override_data is not None:
                mock_r.get.side_effect = [None, json.dumps(override_data)]
            else:
                mock_r.get.side_effect = [None, None]
            mock_redis_cls.return_value = mock_r
            from src.core.risk_controller import HardRiskGatekeeper
            gk = HardRiskGatekeeper()
            return gk, mock_r

    def test_load_from_redis_approved(self):
        overrides = {
            "kelly_fraction": 0.4,
            "min_confidence": 0.6,
            "risk_scaling": 0.8,
            "approved_by": "human",
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
        gk, mock_r = self._make_gatekeeper_with_overrides(overrides)
        assert gk.kelly_fraction_override == 0.4
        assert gk.min_confidence_override == 0.6
        assert gk.risk_scaling_override == 0.8

    def test_no_approved_key_noop(self):
        gk, mock_r = self._make_gatekeeper_with_overrides(None)
        assert gk.kelly_fraction_override is None
        assert gk.min_confidence_override is None

    def test_guardrail_failure_uses_defaults(self):
        overrides = {
            "kelly_fraction": 5.0,  # Invalid
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
        gk, mock_r = self._make_gatekeeper_with_overrides(overrides)
        assert gk.kelly_fraction_override is None

    def test_blacklist_additions_applied(self):
        overrides = {
            "kelly_fraction": 0.5,
            "blacklist_additions": ["TSLA", "NVDA"],
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
        gk, mock_r = self._make_gatekeeper_with_overrides(overrides)
        assert "TSLA" in gk.blacklist_symbols
        assert "NVDA" in gk.blacklist_symbols

    def test_kelly_stored_in_redis(self):
        overrides = {
            "kelly_fraction": 0.45,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
        gk, mock_r = self._make_gatekeeper_with_overrides(overrides)
        # Check that r.set was called with kelly_fraction:current
        set_calls = {call[0][0]: call[0][1] for call in mock_r.set.call_args_list}
        assert "kelly_fraction:current" in set_calls
        assert float(set_calls["kelly_fraction:current"]) == 0.45

    def test_min_confidence_stored_in_redis(self):
        overrides = {
            "min_confidence": 0.55,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
        gk, mock_r = self._make_gatekeeper_with_overrides(overrides)
        set_calls = {call[0][0]: call[0][1] for call in mock_r.set.call_args_list}
        assert "risk:min_confidence:current" in set_calls
        assert float(set_calls["risk:min_confidence:current"]) == 0.55


# ─────────────────────────────────────────────
#  Review Queue
# ─────────────────────────────────────────────

class TestReviewQueue:
    """Test post-mortem review queue (Phase 1)."""

    def test_only_high_severity_with_P0_P1_queued(self):
        """Verify _queue_for_review only queues P0/P1 suggestions for high severity."""
        with patch("src.core.risk_controller.redis.Redis") as mock_redis_cls:
            mock_r = MagicMock()
            mock_r.get.return_value = None
            mock_redis_cls.return_value = mock_r
            from src.core.risk_controller import HardRiskGatekeeper

            with patch("src.services.post_mortem_service.KnowledgeStore"):
                with patch("src.services.post_mortem_service.ReinforcementEngine"):
                    from src.services.post_mortem_service import PostMortemService
                    from src.services.post_mortem_models import (
                        LessonEnvelope, SystemSuggestion, TradeReviewModel,
                    )

                    svc = PostMortemService.__new__(PostMortemService)
                    svc.knowledge_store = MagicMock()

                    review = TradeReviewModel(
                        overview="test",
                        execution_score=5,
                        signal_score=3,
                        pnl_attribution=[],
                        example_category="E",
                        failure_reason="false_breakout",
                        key_lesson=["test lesson"],
                        system_suggestions=[
                            SystemSuggestion(priority="P0", suggestion="Lower kelly"),
                            SystemSuggestion(priority="P2", suggestion="Minor tweak"),
                            SystemSuggestion(priority="P1", suggestion="Blacklist TSLA"),
                        ],
                        severity=8,
                        confidence=0.8,
                    )

                    envelope = LessonEnvelope(
                        doc_id="lesson_test123",
                        trace_id="test123",
                        symbol="TSLA",
                        regime="BEAR_LONG",
                        pattern="FALSE_BREAKOUT",
                        failure_reason="false_breakout",
                        lesson_text="test lesson text",
                        severity=8,
                        confidence=0.8,
                        pnl_pct=-3.5,
                        review=review,
                    )

                    report = {"lessons_injected": 1}

                    with patch("redis.Redis") as mock_redis_cls:
                        mock_redis_inst = MagicMock()
                        mock_redis_cls.return_value = mock_redis_inst

                        svc._queue_for_review(envelope, report)

                        # Should have called hset (P0 + P1 only, not P2)
                        mock_redis_inst.hset.assert_called_once()
                        call_args = mock_redis_inst.hset.call_args
                        payload = json.loads(call_args[0][2])
                        assert len(payload["suggestions"]) == 2
                        assert all(s["priority"] in ("P0", "P1") for s in payload["suggestions"])
                        assert report["queued_for_review"] == 1

    def test_low_severity_not_queued(self):
        """Below SEVERITY_RAG_THRESHOLD should not be queued."""
        with patch("src.core.risk_controller.redis.Redis") as mock_redis_cls:
            mock_r = MagicMock()
            mock_r.get.return_value = None
            mock_redis_cls.return_value = mock_r

            with patch("src.services.post_mortem_service.KnowledgeStore"):
                with patch("src.services.post_mortem_service.ReinforcementEngine"):
                    from src.services.post_mortem_service import PostMortemService
                    from src.services.post_mortem_models import LessonEnvelope

                    svc = PostMortemService.__new__(PostMortemService)

                    envelope = LessonEnvelope(
                        doc_id="lesson_low",
                        trace_id="low123",
                        symbol="AAPL",
                        regime="NEUTRAL",
                        pattern="UNKNOWN",
                        failure_reason="minor",
                        lesson_text="minor lesson",
                        severity=5,  # Below threshold of 7
                        confidence=0.5,
                        pnl_pct=-0.8,
                    )

                    report = {"lessons_injected": 1}
                    # _queue_for_review shouldn't even be called because of the
                    # severity check in run_daily_postmortem, but if called
                    # with no review, it should return early
                    svc._queue_for_review(envelope, report)
                    assert "queued_for_review" not in report


# ─────────────────────────────────────────────
#  CLI Guardrails
# ─────────────────────────────────────────────

class TestCLI:
    """Test audit_review.py guardrail logic."""

    def test_cli_validate_kelly_range(self):
        # Import the validation function from the CLI script
        import importlib.util
        import os
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "audit_review.py",
        )
        spec = importlib.util.spec_from_file_location("audit_review", script_path)
        mod = importlib.util.module_from_spec(spec)

        # Mock redis to avoid connection
        with patch("redis.from_url"):
            spec.loader.exec_module(mod)

        # Valid
        assert mod.validate_overrides({"kelly_fraction": 0.5}) == []
        # Invalid
        errors = mod.validate_overrides({"kelly_fraction": 0.05})
        assert any("Kelly" in e for e in errors)

    def test_cli_approve_writes_overrides(self):
        """Verify approve writes merged overrides to approved key."""
        import importlib.util
        import os
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "audit_review.py",
        )
        spec = importlib.util.spec_from_file_location("audit_review", script_path)
        mod = importlib.util.module_from_spec(spec)

        mock_r = MagicMock()
        mock_r.hget.return_value = json.dumps({
            "doc_id": "lesson_test",
            "symbol": "TSLA",
            "suggestions": [{"priority": "P0", "suggestion": "test"}],
        })
        mock_r.get.return_value = None  # No existing approved

        with patch("redis.from_url", return_value=mock_r):
            spec.loader.exec_module(mod)
            mod.approve_lesson(
                "lesson_test", "2026-03-30",
                {"kelly_fraction": 0.4, "min_confidence": 0.6},
            )

        # Should have called r.set with the approved key
        mock_r.set.assert_called_once()
        call_args = mock_r.set.call_args
        assert "rag:review:approved:2026-03-30" == call_args[0][0]
        data = json.loads(call_args[0][1])
        assert data["kelly_fraction"] == 0.4
        assert data["min_confidence"] == 0.6

    def test_reject_adds_to_rejected_set(self):
        """Verify reject adds doc_id to rejected set and removes from pending."""
        import importlib.util
        import os
        script_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "audit_review.py",
        )
        spec = importlib.util.spec_from_file_location("audit_review", script_path)
        mod = importlib.util.module_from_spec(spec)

        mock_r = MagicMock()
        mock_r.hget.return_value = json.dumps({"doc_id": "lesson_test"})

        with patch("redis.from_url", return_value=mock_r):
            spec.loader.exec_module(mod)
            mod.reject_lesson("lesson_test", "2026-03-30", "bad lesson")

        mock_r.sadd.assert_called_once_with("rag:review:rejected:2026-03-30", "lesson_test")
        mock_r.hdel.assert_called_once_with("rag:review:pending:2026-03-30", "lesson_test")
