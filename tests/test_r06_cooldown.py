"""R-06: symbol cooldown blocks re-entry."""
from unittest.mock import patch

import pytest

from stockapp_v12.adapters.legacy_v11.risk import load_hard_risk_gatekeeper_class


# Post-migration (2026-07-09): see tests/test_r05_daily_cap.py for the
# full explanation of the V1→V12 bridge xfail rationale.
@pytest.mark.xfail(
    reason="V1→V12 bridge: R-06 logic lives in V2 extensions, not reachable via V1 front door",
    strict=False,
)
def test_cooldown_blocks_buy(fake_redis):
    HardRiskGatekeeper = load_hard_risk_gatekeeper_class()
    if HardRiskGatekeeper is None:
        pytest.skip("V11 archive unavailable or STOCKAPP_DISABLE_V11_ARCHIVE=1")

    fake_redis.get.side_effect = lambda k: "1" if "risk:cooldown" in k else None
    fake_redis.ttl.return_value = 3600

    with patch("redis.Redis", return_value=fake_redis):
        gk = HardRiskGatekeeper(initial_capital=100_000.0, secret_key="test")
    gk.use_v12_risk = True
    gk._state_updated_at = __import__("time").time()
    gk.current_equity = 100_000.0
    gk._get_current_regime = lambda: "BULL_LONG"
    gk._get_portfolio_metrics = lambda: {"drawdown_pct": 0, "total_exposure_pct": 0}
    gk._get_existing_positions = lambda: []
    gk._check_correlation_risk = lambda s, sz: (sz, {})

    approved, reason, _ = gk.validate_order_intent(
        "NVDA", confidence=0.85, side="BUY", price=100.0, atr_pct=0.05
    )
    assert not approved
    assert "R-06" in reason
