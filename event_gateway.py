"""Stable Python integration surface for automation frameworks.

Imports are side-effect free. No browser is launched at import time.
Trading is opt-in and delegates all risk guards to safe_orders.
"""
from dataclasses import dataclass
from typing import Protocol, Optional

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

    def __init__(self, dispatcher=None):
        self.dispatcher = dispatcher

    def submit_order_async(self, order: OrderRequest, *, signal_time_ms=None, max_age_ms=1000):
        """Durably enqueue without waiting for an exchange response."""
        if not isinstance(order, OrderRequest):
            raise TypeError("submit_order_async expects OrderRequest")
        if self.dispatcher is None:
            raise RuntimeError("Use EventContractGateway(dispatcher=FastOrderDispatcher()) or HTTP API")
        return self.dispatcher.submit(
            order, signal_time_ms=signal_time_ms, max_age_ms=max_age_ms,
        )

    def submit_order(self, order: OrderRequest, *, signal_time_ms=None, max_age_ms=1000):
        """Default SDK submission is nonblocking; 202/QUEUED is not execution."""
        return self.submit_order_async(
            order, signal_time_ms=signal_time_ms, max_age_ms=max_age_ms,
        )

    def submit_order_sync_legacy(self, order: OrderRequest):
        """Blocking compatibility path. Do not use in latency-sensitive strategies."""
        if not isinstance(order, OrderRequest):
            raise TypeError("submit_order_sync_legacy expects OrderRequest")
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


class ExchangeQueryProvider(Protocol):
    """Optional pluggable backend for independently verified query endpoints.

    Never install a guessed URL here. Implementations must define the true
    authentication and data provenance semantics before being enabled.
    """
    def get_order(self, order_id: str): ...
    def get_quote(self, symbol: str, time_increments: str): ...
    def get_settlement(self, order_id: str): ...


class QueryInterface:
    """Explicit separation of LOCAL audit state and EXCHANGE-backed queries."""

    def __init__(self, remote_provider: Optional[ExchangeQueryProvider] = None,
                 dispatcher=None):
        self.remote_provider = remote_provider
        self.dispatcher = dispatcher

    def capabilities(self):
        supported = self.remote_provider is not None
        return {
            "trading": "opt_in_unverified_unofficial",
            "local_order_query": True,
            "local_session_metadata": True,
            "exchange_order_query": supported,
            "live_market_quote": supported,
            "exchange_settlement_query": supported,
            "local_records_are_not_exchange_confirmation": True,
        }

    def get_session(self):
        return get_session_status()

    def get_order(self, intent_id):
        if self.dispatcher is not None:
            record = self.dispatcher.journal.get(intent_id)
            if record is not None:
                return record
        return get_local_order(intent_id)

    def list_orders(self, limit=50):
        if self.dispatcher is not None:
            return self.dispatcher.journal.list(limit)
        return list_local_orders(limit)

    def get_exchange_order(self, order_id):
        return self.remote_provider.get_order(order_id) if self.remote_provider is not None else query_exchange_order(order_id)

    def get_quote(self, symbol, time_increments):
        return self.remote_provider.get_quote(symbol, time_increments) if self.remote_provider is not None else query_market_quote(symbol, time_increments)

    def get_settlement(self, order_id):
        return self.remote_provider.get_settlement(order_id) if self.remote_provider is not None else query_settlement(order_id)


class EventContractGateway:
    """Convenience composition; trading and queries are independent interfaces."""

    def __init__(self, query_provider: Optional[ExchangeQueryProvider] = None,
                 dispatcher=None):
        self.trading = TradingInterface(dispatcher=dispatcher)
        self.queries = QueryInterface(remote_provider=query_provider, dispatcher=dispatcher)
