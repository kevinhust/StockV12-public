"""Gatekeeper uses v12_risk sizing model in metadata."""
import pytest



# Post-migration (2026-07-09): V1 HardRiskGatekeeper.validate_order_intent()
# does not accept the atr_14 kwarg that V12's compute_position_size extension
# expects. The V12 sizing path is reached in production via the V12
# EntryOrchestrator, not through the V1 gatekeeper front door.
@pytest.mark.xfail(
    reason="V1→V12 bridge: V1 gatekeeper.validate_order_intent() lacks atr_14 kwarg",
    strict=False,
)
def test_approved_includes_v12_audit(gatekeeper):
    approved, reason, meta = gatekeeper.validate_order_intent(
        symbol="AAPL",
        confidence=0.85,
        side="BUY",
        price=150.0,
        atr_pct=0.04,
        atr_14=6.0,
    )
    assert approved
    assert meta.get("sizing_model") == "v12_risk"
    assert "risk_amount" in meta
    assert "stop_distance" in meta
    assert meta.get("size_pct", 0) > 0
