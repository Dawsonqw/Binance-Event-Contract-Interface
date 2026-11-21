"""Read-only Binance event-contract order-list provider.

The *paths and empty JSON POST shape* were verified with an authenticated
account on 2026-11-21. They are undocumented; Binance may change or revoke
them. Never send credentials to a host other than www.binance.com.

Only first-page queries with {} are verified. Returning an untested second
page, or silently treating an ignored pagination parameter as valid, could
cause false negative order reconciliation: fail closed instead.
"""
import re
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter

from order_queries import RemoteQueryUnavailable
from secure_storage import load_token
from session_lifecycle import require_fresh_session

ORIGIN = "https://www.binance.com"
ONGOING_PATH = "/bapi/futures/v2/private/future/event-contract/ongoing-order-list"
CLOSED_PATH = "/bapi/futures/v1/private/future/event-contract/closed-order-list"
TIMEOUT_SECONDS = (2, 4)

# Only data fields seen during the authenticated read-only test are exposed.
# Responses may contain additional account/personally-identifying metadata.
ORDER_FIELDS = (
    "id", "symbolName", "direction", "orderAmount", "openPrice",
    "closePrice", "payoutRatio", "openTime", "expiredTimestamp",
    "result", "settlementAmount", "timeIncrements",
)


class ExchangeQueryError(RuntimeError):
    """Sanitized query failure; no token, URL headers or raw response body."""


class BinanceEventQueryProvider:
    """One optional requests Session, restricted to two verified list paths.

    The gateway does not make any HTTP request until a query method is called.
    The provider NEVER calls the event-contract place-order path.
    """

    def __init__(self, transport=None):
        self._owned_session = transport is None
        self._session = transport if transport is not None else self._new_session()

    @staticmethod
    def _new_session():
        session = requests.Session()
        session.trust_env = False
        session.mount(
            ORIGIN, HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0)
        )
        return session

    @staticmethod
    def capabilities():
        return {
            "exchange_ongoing_orders": True,
            "exchange_closed_orders": True,
            "exchange_order_query": False,  # no verified arbitrary-ID lookup
            "exchange_settlement_query": False,  # not a full settlement API
            "live_market_quote": False,
            "exchange_history_pagination_verified": False,
        }

    @staticmethod
    def _first_page(page):
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError("page must be a positive integer")
        if page != 1:
            raise RemoteQueryUnavailable(
                "Only the first page is verified; Binance pagination parameters remain unverified"
            )

    @staticmethod
    def _safe_order(raw):
        if not isinstance(raw, dict):
            raise ExchangeQueryError("Binance returned a malformed order item")
        order = {}
        for field in ORDER_FIELDS:
            value = raw.get(field)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ExchangeQueryError("Binance order field has an unexpected type")
            # Keep decimal strings exact; stringify numeric IDs to avoid JS precision loss.
            if field == "id":
                identifier = str(value)
                if not identifier or len(identifier) > 100:
                    raise ExchangeQueryError("Binance order ID is invalid")
                order[field] = identifier
            else:
                order[field] = value
        return order

    def _query(self, kind):
        if kind not in ("ongoing", "closed"):
            raise ValueError("Unsupported order list")
        token = load_token()
        require_fresh_session(token["csrftoken"], token["p20t"])
        path = ONGOING_PATH if kind == "ongoing" else CLOSED_PATH
        # The only trusted target is this constant HTTPS origin. No redirects.
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "clienttype": "web",
            "csrftoken": token["csrftoken"],
            "Cookie": "p20t=" + token["p20t"],
        }
        try:
            response = self._session.post(
                ORIGIN + path,
                headers=headers,
                json={},  # verified read-only query shape; NOT an order submission
                timeout=TIMEOUT_SECONDS,
                allow_redirects=False,
            )
            if response.status_code != 200:
                raise ExchangeQueryError(
                    "Binance order query returned an unexpected HTTP status"
                )
            if response.headers.get("x-amzn-waf-action"):
                raise ExchangeQueryError("Binance order query encountered a security challenge")
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            # Do not propagate exception text; it can contain URL or secret data.
            if isinstance(exc, ExchangeQueryError):
                raise
            raise ExchangeQueryError("Binance order query failed or returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise ExchangeQueryError("Binance order query returned an invalid envelope")
        if payload.get("success") is not True or str(payload.get("code")) != "000000":
            raise ExchangeQueryError("Binance order query was not accepted")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ExchangeQueryError("Binance order query returned invalid data")
        result = data.get("result") if kind == "ongoing" else data
        if not isinstance(result, dict):
            raise ExchangeQueryError("Binance order query returned an unexpected list shape")
        rows = result.get("rows")
        total = result.get("total")
        if isinstance(total, bool) or not isinstance(total, (int, str)):
            raise ExchangeQueryError("Binance order query total is missing or invalid")
        try:
            total = int(total)
        except (ValueError, TypeError, OverflowError):
            raise ExchangeQueryError("Binance order query total is invalid") from None
        if not (0 <= total <= 10_000_000):
            raise ExchangeQueryError("Binance order query total is out of range")
        if rows is None and total == 0:
            rows = []
        if not isinstance(rows, list):
            raise ExchangeQueryError("Binance order rows are missing or malformed")
        if total < len(rows):
            raise ExchangeQueryError("Binance list count exceeds reported total")
        orders = [self._safe_order(row) for row in rows]
        return {
            "source": "binance_web_private_undocumented",
            "kind": kind,
            "exchange_response_verified": True,  # verified read response only
            "order_settlement_verified": kind == "closed",  # status as reported by Binance
            "page": 1,
            "returned_count": len(orders),
            "total": total,
            "has_more": total > len(orders),
            "pagination_supported": False,
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "items": orders,
        }

    def list_ongoing_orders(self, page=1):
        self._first_page(page)
        return self._query("ongoing")

    def list_closed_orders(self, page=1):
        self._first_page(page)
        return self._query("closed")

    def get_order(self, order_id):
        # A successful list query cannot prove a missing ID when more pages
        # exist. Avoid reporting false "not found" for old closed orders.
        raise RemoteQueryUnavailable(
            "Arbitrary order-id lookup is unavailable until verified pagination or detail API exists"
        )

    def get_quote(self, symbol, time_increments):
        raise RemoteQueryUnavailable("Live event-contract payout quotes are not verified")

    def get_settlement(self, order_id):
        raise RemoteQueryUnavailable("Arbitrary settlement lookup is not verified")

    def close(self):
        if self._owned_session:
            self._session.close()
