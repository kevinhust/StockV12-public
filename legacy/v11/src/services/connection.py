from typing import Dict, Any, List, Optional
import os
import time
from loguru import logger
from ib_insync import IB, Stock, MarketOrder, LimitOrder, Contract

class Connector:
    """
    Base Connector class.
    """
    def __init__(self, provider: str = "Base"):
        self.provider = provider
        self.connected = False
        
    def connect(self, **kwargs):
        raise NotImplementedError
        
    def disconnect(self):
        raise NotImplementedError

    def place_order(self, symbol: str, action: str, qty: float, price: float = 0.0) -> Any:
        raise NotImplementedError
        
    def cancel_order(self, order_id: str):
        raise NotImplementedError
        
    def get_positions(self) -> List[Dict[str, Any]]:
        raise NotImplementedError

class MockConnector(Connector):
    """
    Phase 0: Simulation / Logging only.
    """
    def __init__(self):
        super().__init__("MOCK")
        
    def connect(self, **kwargs):
        logger.info(f"Connecting to MOCK provider...")
        time.sleep(1)
        self.connected = True
        logger.info(f"Connected to MOCK.")
        
    def disconnect(self):
        self.connected = False
        logger.info("Disconnected from MOCK.")
        
    def place_order(self, symbol: str, action: str, qty: float, price: float = 0.0) -> str:
        if not self.connected:
            raise ConnectionError("Not connected")
        order_id = f"ORD_{int(time.time()*1000)}"
        logger.info(f"[MOCK] PLACING ORDER: {action} {qty} {symbol} @ {price} | ID: {order_id}")
        return order_id
        
    def cancel_order(self, order_id: str):
        logger.info(f"[MOCK] CANCEL ORDER: {order_id}")
        
    def get_positions(self) -> List[Dict[str, Any]]:
        return [
            {"symbol": "NVDA", "quantity": 10, "avg_cost": 120.0, "market_price": 130.0}
        ]

class IBConnector(Connector):
    """
    IBKR Connector using ib_insync.
    """
    def __init__(self, host: str = None, port: int = None, client_id: int = None):
        super().__init__("IBKR")
        self.host = host or os.getenv("IB_HOST", "127.0.0.1")
        self.port = port or int(os.getenv("IB_PORT", "4002"))
        self.client_id = client_id or int(os.getenv("IB_CLIENT_ID", "1"))
        self.ib = IB()
        
    def connect(self, **kwargs):
        host = kwargs.get('host', self.host)
        port = kwargs.get('port', self.port)
        client_id = kwargs.get('client_id', self.client_id)
        
        logger.info(f"Connecting to IBKR at {host}:{port} (ClientId: {client_id})...")
        try:
            self.ib.connect(host, port, clientId=client_id)
            self.connected = True
            logger.info("✅ Connected to IBKR Gateway/TWS.")
        except Exception as e:
            logger.error(f"❌ Failed to connect to IBKR: {e}")
            self.connected = False
            raise

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()
            self.connected = False
            logger.info("Disconnected from IBKR.")

    def place_order(self, symbol: str, action: str, qty: float, price: float = 0.0, order_type: str = "MKT") -> Any:
        if not self.ib.isConnected():
            raise ConnectionError("Not connected to IBKR")
            
        contract = Stock(symbol, 'SMART', 'USD')
        
        if order_type.upper() == "MKT":
            order = MarketOrder(action.upper(), qty)
        elif order_type.upper() == "LMT":
            order = LimitOrder(action.upper(), qty, price)
        else:
            raise ValueError(f"Unsupported order type: {order_type}")
            
        trade = self.ib.placeOrder(contract, order)
        logger.info(f"[IBKR] Order Placed: {trade.order}")
        return trade
        
    def cancel_order(self, order_id: str):
        # Note: ib_insync uses Order objects, checking if we can find by ID isn't direct
        # For now, simplistic implementation assuming we have the trade object or similar logic
        logger.warning("[IBKR] Cancel order by ID not fully implemented in this snippet")
        
    def get_positions(self) -> List[Dict[str, Any]]:
        if not self.ib.isConnected():
            return []
            
        positions = self.ib.positions()
        results = []
        for p in positions:
            results.append({
                "symbol": p.contract.symbol,
                "quantity": p.position,
                "avg_cost": p.avgCost,
                "account": p.account
            })
        return results

