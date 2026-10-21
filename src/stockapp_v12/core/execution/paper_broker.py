"""
PaperBrokerAdapter — simple in-memory broker for paper trading and tests.

Implements BrokerAdapter protocol. Generates synthetic order ids.
No real execution, just records submissions for verification.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional, Tuple

from stockapp_v12.core.execution.bracket_order import BrokerAdapter


class PaperBroker(BrokerAdapter):
    """
    Ultra-simple paper broker.
    - Always "fills" parent immediately.
    - Creates synthetic SL order id.
    - Stores submissions for inspection in tests / shadow runs.
    """

    def __init__(self):
        self.submissions: list[Dict[str, Any]] = []
        self.orders: Dict[str, Dict[str, Any]] = {}  # order_id -> status

    def _new_id(self, prefix: str, symbol: str) -> str:
        return f"{prefix}_{symbol}_{uuid.uuid4().hex[:8]}"

    def submit_bracket_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: int,
        entry_price: float,
        stop_price: float,
        mode: str = "paper_ibkr",
    ) -> Tuple[str, Optional[str]]:
        parent_id = self._new_id("paper_parent", symbol)
        sl_id = self._new_id("paper_sl", symbol)

        rec = {
            "symbol": symbol.upper(),
            "side": side,
            "qty": qty,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "mode": mode,
            "parent_order_id": parent_id,
            "sl_order_id": sl_id,
            "status": "filled",  # paper assumes immediate fill for parent
        }
        self.submissions.append(rec)
        self.orders[parent_id] = rec
        self.orders[sl_id] = {**rec, "status": "open", "order_type": "stop"}

        return parent_id, sl_id

    def get_order_status(self, order_id: str) -> Dict[str, Any]:
        return self.orders.get(order_id, {"status": "unknown", "order_id": order_id})

    def cancel_order(self, order_id: str) -> bool:
        if order_id in self.orders:
            self.orders[order_id]["status"] = "cancelled"
            return True
        return False

    def reset(self):
        self.submissions.clear()
        self.orders.clear()
