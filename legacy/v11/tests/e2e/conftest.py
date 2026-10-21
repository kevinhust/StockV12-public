"""
conftest.py for e2e tests -- shared fixtures.
"""

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))


@pytest.fixture()
def fake_redis():
    """Isolated fakeredis instance per test."""
    import fakeredis
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def gatekeeper(fake_redis):
    """HardRiskGatekeeper wired to fakeredis (no network calls)."""
    from src.core.risk_controller import HardRiskGatekeeper

    gk = HardRiskGatekeeper.__new__(HardRiskGatekeeper)
    gk.r = fake_redis
    gk.initial_capital = 100000.0
    gk._lock = threading.RLock()
    gk.blacklist_symbols = set()
    gk.blacklist_reasons = {}
    gk.blacklist_metadata = {}
    gk.blacklist_expires = {}
    gk.kelly_fraction_override = None
    gk.min_confidence_override = None
    gk.risk_scaling_override = None
    gk.market_regime = "NEUTRAL"
    gk.rejection_cooldowns = {}
    gk.current_equity = 100000.0
    gk.current_pnl = 0.0
    gk._state_updated_at = 0.0
    gk.secret_key = "test-secret-key-for-e2e"
    gk.token_version = "v1"
    return gk


@pytest.fixture()
def sample_pattern_event():
    """A typical pattern event as it would arrive from Kafka events.pattern."""
    return {
        "symbol": "TSLA",
        "pattern": "BREAKOUT",
        "type": "BREAKOUT",
        "source": "yolo",
        "price": 250.50,
        "trace_id": "tr_001_e2e",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "confidence": 0.82,
            "pattern_score": 0.75,
        },
    }


@pytest.fixture()
def sample_trade_event():
    """A typical trade execution event for TradeRecorder."""
    return {
        "event_type": "TRADE_EXECUTION",
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 10,
        "price": 195.50,
        "fill_price": 195.50,
        "status": "EXECUTED",
        "mode": "PAPER",
        "confidence": 0.85,
        "pattern_type": "BREAKOUT",
        "matching_method": "FIFO",
        "pnl_status": "PENDING",
        "trace_id": "tr_aapl_001",
        "intent_id": "intent_aapl_001",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "risk_metadata": {
            "intent_context": {
                "trace_id": "tr_aapl_001",
                "intent_id": "intent_aapl_001",
                "regime": "BULL_LONG",
            },
        },
    }
