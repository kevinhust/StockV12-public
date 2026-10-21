"""BrokerAdapter port — canonical import path for hexagonal consumers.

Short-term re-export of the Protocol defined in
``stockapp_v12.core.execution.bracket_order`` so object identity is preserved
(``contracts.BrokerAdapter is core...BrokerAdapter``).

Note: this creates a ``contracts → core`` dependency. Acceptable for Phase 0/1;
Phase 2 may move the Protocol body into contracts and reverse the re-export.
"""
from stockapp_v12.core.execution.bracket_order import BrokerAdapter

__all__ = ["BrokerAdapter"]
