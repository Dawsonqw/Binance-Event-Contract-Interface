"""Offline tests for verified Binance event-contract read-only list integration.

The Binance network is NEVER contacted; all responses and credentials are
test fixtures, and POSTs are asserted against the two allowlisted LIST paths.
"""
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import http_api
from binance_event_queries import (
    BinanceEventQueryProvider, CLOSED_PATH, ONGOING_PATH, ORIGIN,
    ExchangeQueryError,
)
from event_gateway import EventContractGateway
from order_queries import RemoteQueryUnavailable
from secure_storage import save_token
from session_lifecycle import AuthenticationRequired


API_TOKEN = "a" * 48
CLOSED_ROW = {
    "id": 1234567890123456,
    "symbolName": "BTCUSDT",
    "direction": "LONG",
    "orderAmount": "5",
    "openPrice": "98000.12",
    "closePrice": "98010.34",
    "payoutRatio": "0.80",
    "openTime": 1795000000000,
    "expiredTimestamp": 1795000600000,
    "result": "WIN",
    "settlementAmount": "9",
    "timeIncrements": "TEN_MINUTE",
    "email": "SHOULD_NOT_LEAK@example.com",
    "userId": 987654,
    "x-token": "SHOULD_NOT_LEAK",
}


def fake_response(payload, status=200, headers=None):
    response = Mock()
    response.status_code = status
    response.headers = headers or {}
    response.json.return_value = payload
    return response


def accepted(data):
    return {
        "success": True, "code": "000000", "message": None,
        "data": data,
    }


class ExchangeListTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "BINANCE_EVENT_STATE_DIR": self.tmp.name + "/private",
            "BINANCE_EVENT_LIVE_TRADING": "",
        })
        env.start()
        self.addCleanup(env.stop)
        self.transport = Mock()
        self.provider = BinanceEventQueryProvider(transport=self.transport)
        save_token({
            "csrftoken": "private-csrf",
            "p20t": "private-cookie",
            "expirationTimestamp": int(time.time()) + 3600,
        })

    def test_closed_orders_parse_realistic_nested_shape_and_redact(self):
        self.transport.post.return_value = fake_response(
            accepted({"rows": [CLOSED_ROW], "total": 326})
        )
        result = self.provider.list_closed_orders()
        self.assertEqual(result["kind"], "closed")
        self.assertEqual(result["total"], 326)
        self.assertEqual(result["returned_count"], 1)
        self.assertTrue(result["has_more"])
        self.assertFalse(result["pagination_supported"])
        self.assertTrue(result["reported_closed_by_exchange"])
        self.assertFalse(result["independently_settlement_verified"])
        row = result["items"][0]
        self.assertEqual(row["id"], "1234567890123456")
        self.assertEqual(row["closePrice"], "98010.34")
        self.assertNotIn("email", row)
        self.assertNotIn("userId", row)
        self.assertNotIn("x-token", row)
        args, kwargs = self.transport.post.call_args
        self.assertEqual(args[0], ORIGIN + CLOSED_PATH)
        self.assertEqual(kwargs["json"], {})
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["headers"]["csrftoken"], "private-csrf")
        self.assertEqual(kwargs["headers"]["Cookie"], "p20t=private-cookie")

    def test_ongoing_none_rows_means_empty_only_when_total_zero(self):
        self.transport.post.return_value = fake_response(
            accepted({"result": {"rows": None, "total": 0}, "serverTime": 1})
        )
        result = self.provider.list_ongoing_orders()
        self.assertEqual(result["items"], [])
        self.assertEqual(result["total"], 0)
        self.assertEqual(self.transport.post.call_args.args[0], ORIGIN + ONGOING_PATH)

    def test_ongoing_nonempty_records_are_supported(self):
        self.transport.post.return_value = fake_response(
            accepted({"result": {"rows": [CLOSED_ROW], "total": 1}})
        )
        result = self.provider.list_ongoing_orders()
        self.assertEqual(len(result["items"]), 1)
        self.assertFalse(result["reported_closed_by_exchange"])

    def test_never_claim_empty_when_rows_missing_but_total_nonzero(self):
        self.transport.post.return_value = fake_response(
            accepted({"result": {"rows": None, "total": 1}})
        )
        with self.assertRaises(ExchangeQueryError):
            self.provider.list_ongoing_orders()

    def test_error_response_does_not_expose_server_data(self):
        self.transport.post.return_value = fake_response({
            "success": False, "code": "000002",
            "message": "SENSITIVE account data",
            "data": None,
        }, status=400)
        with self.assertRaises(ExchangeQueryError) as ctx:
            self.provider.list_closed_orders()
        self.assertNotIn("SENSITIVE", str(ctx.exception))
        self.assertNotIn("private-", str(ctx.exception))

    def test_waf_challenge_rejected(self):
        self.transport.post.return_value = fake_response(
            accepted({"rows": [], "total": 0}),
            status=202, headers={"x-amzn-waf-action": "challenge"},
        )
        with self.assertRaises(ExchangeQueryError):
            self.provider.list_closed_orders()

    def test_pagination_unverified_is_explicitly_unavailable(self):
        with self.assertRaises(RemoteQueryUnavailable):
            self.provider.list_closed_orders(page=2)
        with self.assertRaises(ValueError):
            self.provider.list_ongoing_orders(page=0)
        self.transport.post.assert_not_called()

    def test_expired_session_blocks_all_network(self):
        save_token({
            "csrftoken": "private-csrf", "p20t": "private-cookie",
            "expirationTimestamp": int(time.time()) - 1,
        })
        with self.assertRaises(AuthenticationRequired):
            self.provider.list_closed_orders()
        self.transport.post.assert_not_called()

    def test_direct_order_lookup_is_not_guessed(self):
        with self.assertRaises(RemoteQueryUnavailable):
            self.provider.get_order("12345")
        with self.assertRaises(RemoteQueryUnavailable):
            self.provider.get_quote("BTCUSDT", "TEN_MINUTE")
        self.transport.post.assert_not_called()

    def test_malformed_json_cannot_return_success(self):
        self.transport.post.return_value = fake_response(
            accepted({"rows": "not a list", "total": 99})
        )
        with self.assertRaises(ExchangeQueryError):
            self.provider.list_closed_orders()

    def test_capabilities_describe_only_verified_operations(self):
        gw = EventContractGateway(query_provider=self.provider)
        caps = gw.queries.capabilities()
        self.assertTrue(caps["exchange_ongoing_orders"])
        self.assertTrue(caps["exchange_closed_orders"])
        self.assertFalse(caps["exchange_order_query"])
        self.assertFalse(caps["exchange_settlement_query"])
        self.assertFalse(caps["live_market_quote"])
        self.assertFalse(caps["exchange_history_pagination_verified"])


class HTTPReadOnlyTests(ExchangeListTests):
    def setUp(self):
        super().setUp()
        # Real listener is restricted to loopback and uses a fake read-only
        # query transport, plus a safe dispatcher whose live trading is OFF.
        user = patch.object(os, "geteuid", return_value=1001)
        user.start()
        self.addCleanup(user.stop)
        gateway = EventContractGateway(query_provider=self.provider)
        self.server = http_api.make_server(port=0, gateway=gateway, token=API_TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close_server)

    def _close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def _get(self, path, token=API_TOKEN):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request("GET", path, headers={"Authorization": "Bearer " + token})
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        status = response.status
        connection.close()
        return status, payload

    def test_http_closed_and_ongoing(self):
        self.transport.post.side_effect = [
            fake_response(accepted({"rows": [CLOSED_ROW], "total": 326})),
            fake_response(accepted({"result": {"rows": None, "total": 0}})),
        ]
        status, body = self._get("/v1/exchange/orders/closed?page=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 326)
        self.assertEqual(body["items"][0]["id"], "1234567890123456")
        self.assertNotIn("SHOULD_NOT_LEAK", json.dumps(body))
        status, body = self._get("/v1/exchange/orders/ongoing")
        self.assertEqual(status, 200)
        self.assertEqual(body["items"], [])

    def test_http_requires_gateway_token(self):
        self.transport.post.return_value = fake_response(
            accepted({"rows": [], "total": 0})
        )
        status, body = self._get("/v1/exchange/orders/closed", token="wrong")
        self.assertEqual(status, 401)
        self.transport.post.assert_not_called()

    def test_http_unverified_page_returns_501(self):
        status, body = self._get("/v1/exchange/orders/closed?page=2")
        self.assertEqual(status, 501)
        self.transport.post.assert_not_called()

    def test_http_sanitized_upstream_error(self):
        self.transport.post.return_value = fake_response(
            {"success": False, "code": "000002", "message": "private server detail"},
            status=400,
        )
        status, body = self._get("/v1/exchange/orders/closed")
        self.assertEqual(status, 502)
        self.assertNotIn("private server detail", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
