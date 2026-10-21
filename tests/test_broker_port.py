"""BrokerAdapter port — re-export identity under contracts (Task 5).

Prefer re-export of the core Protocol so isinstance/type identity is stable.
"""
from stockapp_v12.contracts.broker import BrokerAdapter as FromContracts
from stockapp_v12.core.execution.bracket_order import BrokerAdapter as FromCore
from stockapp_v12.contracts import BrokerAdapter as FromContractsPackage
from stockapp_v12 import BrokerAdapter as FromRoot


def test_broker_adapter_reexport_identity():
    assert FromContracts is FromCore


def test_broker_adapter_package_exports_identity():
    """contracts package and root package must re-export the same Protocol."""
    assert FromContractsPackage is FromCore
    assert FromRoot is FromCore
