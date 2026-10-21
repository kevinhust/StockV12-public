"""EX-1 and basic bracket contract tests (Phase 2)."""


from stockapp_v12.core.execution.bracket_order import BracketOrderService
from stockapp_v12.core.limits import V12RiskLimits


class FakeBroker:
    def __init__(self):
        self.calls = []

    def submit_bracket_order(self, *, symbol, side, qty, entry_price, stop_price, mode="paper_ibkr"):
        self.calls.append(locals())
        parent = f"parent_{symbol}_{qty}"
        sl = f"sl_{symbol}_{qty}"
        return parent, sl


def test_bracket_submit_paper():
    broker = FakeBroker()
    svc = BracketOrderService(broker, limits=V12RiskLimits())

    res = svc.submit_entry_bracket(
        symbol="NVDA",
        qty=42,
        entry_price=135.20,
        atr=8.50,
        mode="paper_ibkr",
        tier="high_vol",
    )

    assert res.parent_order_id.startswith("parent_NVDA")
    assert res.sl_order_id.startswith("sl_NVDA")
    assert res.sl_price < 135.20
    assert res.submitted_qty == 42
    assert "atr_used" in res.meta
    assert len(broker.calls) == 1


def test_sl_price_calc_high_vol():
    broker = FakeBroker()
    svc = BracketOrderService(broker)
    sl = svc.calculate_sl_price(100.0, 5.0, tier="high_vol")
    # 1.2 * 5 = 6 → 94.00
    assert sl == 94.0
