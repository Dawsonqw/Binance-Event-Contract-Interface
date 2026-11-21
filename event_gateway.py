"""Stable Python integration surface for automation frameworks.

Imports are side-effect free. No browser is launched at import time.
Trading is opt-in and delegates all risk guards to safe_orders.
"""
from dataclasses import dataclass

from order_queries import (
    get_local_order,
    list_local_orders,
    query_exchange_order,
    query_market_quote,
    query_settlement,
)
from safe_orders import place_order_web
from secure_storage import load_token
from session_lifecycle import get_session_status


@dataclass(frozen=True)
class OrderRequest:
    """Public, stable parameter naming for existing event-contract orders."""
    intent_id: str
    orderAmount: str
    timeIncrements: str
    symbolName: str
    payoutRatio: str
    direction: str

    @classmethod
    def from_dict(cls, payload):
        if not isinstance(payload, dict):
            raise ValueError("Order request must be a JSON object")
        required = set(cls.__dataclass_fields__)
        if set(payload) != required:
            raise ValueError(
                "Order request requires exactly: " + ", ".join(sorted(required))
            )
        if not all(isinstance(value, str) for value in payload.values()):
            raise ValueError("All order fields must be strings")
        return cls(**payload)


class TradingInterface:
    """Trading adapter. Never retrieves login credentials from API clients."""

    def submit_order(self, order: OrderRequest):
        if not isinstance(order, OrderRequest):
            raise TypeError("submit_order expects OrderRequest")
        # Existing safe_orders enforces the live-trading opt-in, UID, cap,
        # session freshness, durable intent and network timeouts.
        from os import environ, geteuid
        from safe_orders import LIVE_OPT_IN
        if geteuid() == 0:
            raise PermissionError("Do not trade as root")
        if environ.get("BINANCE_EVENT_LIVE_TRADING") != LIVE_OPT_IN:
            raise PermissionError("Trading disabled: explicit live opt-in required")
        token = load_token()
        place_order_web(
            token["csrftoken"],
            token["p20t"],
            orderAmount=order.orderAmount,
            timeIncrements=order.timeIncrements,
            symbolName=order.symbolName,
            payoutRatio=order.payoutRatio,
            direction=order.direction,
            intent_id=order.intent_id,
        )
        # Only return whitelisted local audit fields. Raw Binance responses
        # may contain undocumented data and are deliberately not propagated.
        record = get_local_order(order.intent_id)
        if record is None:
            raise RuntimeError("Order sent but local audit record not found: reconcile manually")
        return record


class QueryInterface:
    """Explicit separation of LOCAL state and unavailable EXCHANGE queries."""

    def capabilities(self):
        return {
            "trading": "opt_in_unverified_unofficial",
            "local_order_query": True,
            "local_session_metadata": True,
            "exchange_order_query": False,
            "live_market_quote": False,
            "exchange_settlement_query": False,
            "local_records_are_not_exchange_confirmation": True,
        }

    def get_session(self):
        return get_session_status()

    def get_order(self, intent_id):
        return get_local_order(intent_id)

    def list_orders(self, limit=50):
        return list_local_orders(limit)

    def get_exchange_order(self, order_id):
        return query_exchange_order(order_id)

    def get_quote(self, symbol, time_increments):
        return query_market_quote(symbol, time_increments)

    def get_settlement(self, order_id):
        return query_settlement(order_id)


class EventContractGateway:
    """Convenience composition; trading and queries are independent interfaces."""

    def __init__(self):
        self.trading = TradingInterface()
        self.queries = QueryInterface()
