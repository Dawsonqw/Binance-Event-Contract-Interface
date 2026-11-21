"""Offline integration tests for the Python and HTTP gateway.

All exchange HTTP requests are mocked. Only a temporary loopback HTTP
listener is used; no login or real trade is performed.
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
import safe_orders
from event_gateway import EventContractGateway, OrderRequest
from fast_orders import FastOrderDispatcher, FINAL_STATES
from order_queries import RemoteQueryUnavailable, get_local_order, list_local_orders
from secure_storage import save_token


API_SECRET = "t" * 48
ORDER = {
    "intent_id": "strategy-demo-001",
    "orderAmount": "5",
    "timeIncrements": "TEN_MINUTE",
    "symbolName": "BTCUSDT",
    "payoutRatio": "0.80",
    "direction": "LONG",
}


class GatewayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environ = patch.dict(os.environ, {
            "BINANCE_EVENT_STATE_DIR": directory.name + "/state",
            "BINANCE_EVENT_API_TOKEN": API_SECRET,
            "BINANCE_EVENT_LIVE_TRADING": "",
            "BINANCE_EVENT_MAX_ORDER_USDT": "5",
        })
        environ.start()
        self.addCleanup(environ.stop)
        fake_user = patch.object(os, "geteuid", return_value=1001)
        fake_user.start()
        self.addCleanup(fake_user.stop)
        self.gateway = EventContractGateway()

    def test_no_local_records(self):
        self.assertIsNone(self.gateway.queries.get_order("missing-001"))
        self.assertEqual(self.gateway.queries.list_orders(), [])
        self.assertEqual(self.gateway.queries.get_session()["state"], "missing")

    def test_capabilities_are_explicit(self):
        caps = self.gateway.queries.capabilities()
        self.assertTrue(caps["local_order_query"])
        self.assertFalse(caps["exchange_order_query"])
        self.assertFalse(caps["live_market_quote"])
        self.assertFalse(caps["exchange_settlement_query"])
        with self.assertRaises(RemoteQueryUnavailable):
            self.gateway.queries.get_quote("BTCUSDT", "TEN_MINUTE")
        with self.assertRaises(RemoteQueryUnavailable):
            self.gateway.queries.get_exchange_order("123")

    def test_pluggable_remote_query_provider(self):
        class DemoProvider:
            def get_order(self, order_id):
                return {"provider": "fixture", "order_id": order_id}
            def get_quote(self, symbol, time_increments):
                return {"provider": "fixture", "symbol": symbol}
            def get_settlement(self, order_id):
                return {"provider": "fixture", "settled": False}

        configured = EventContractGateway(query_provider=DemoProvider())
        self.assertTrue(configured.queries.capabilities()["exchange_order_query"])
        self.assertEqual(configured.queries.get_exchange_order("id-1")["order_id"], "id-1")
        self.assertEqual(configured.queries.get_quote("BTCUSDT", "TEN_MINUTE")["symbol"], "BTCUSDT")
        self.assertFalse(configured.queries.get_settlement("id-1")["settled"])

    def test_request_schema(self):
        req = OrderRequest.from_dict(ORDER)
        self.assertEqual(req.intent_id, ORDER["intent_id"])
        with self.assertRaises(ValueError):
            OrderRequest.from_dict({**ORDER, "csrftoken": "secret"})
        with self.assertRaises(ValueError):
            OrderRequest.from_dict({**ORDER, "orderAmount": 5})

    def test_local_ledger_retains_unknown_outcome(self):
        safe_orders._record_intent("strategy-demo-001", {
            "orderAmount": "5", "symbolName": "BTCUSDT", "direction": "LONG",
            "payoutRatio": "0.80", "timeIncrements": "TEN_MINUTE",
        })
        record = get_local_order("strategy-demo-001")
        self.assertEqual(record["status"], "submission_attempted_outcome_unknown")
        self.assertIsNone(record["order_id"])
        self.assertFalse(record["exchange_verified"])
        self.assertEqual(len(list_local_orders()), 1)

    def test_invalid_id_is_rejected(self):
        with self.assertRaises(ValueError):
            get_local_order("../x")
        with self.assertRaises(ValueError):
            list_local_orders(limit=101)


class HTTPGatewayTests(GatewayTests):
    def setUp(self):
        super().setUp()
        self.transport = Mock()
        self.dispatcher = FastOrderDispatcher(transport_factory=lambda: self.transport)
        self.server = http_api.make_server(port=0, gateway=self.gateway, dispatcher=self.dispatcher)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method, path, body=None, *, token=API_SECRET, content_type="application/json"):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        headers = {"Authorization": "Bearer " + token}
        if body is not None:
            headers["Content-Type"] = content_type
            body = json.dumps(body)
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = json.loads(res.read().decode("utf-8"))
        headers_out = dict(res.getheaders())
        conn.close()
        return res.status, data, headers_out

    def wait_final(self, intent_id, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.dispatcher.journal.get(intent_id)
            if state and state["status"] in FINAL_STATES:
                return state
            time.sleep(0.005)
        self.fail("Background worker did not finish intent in time")

    def test_unauthorized_and_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        status, body, _ = self.request("GET", "/v1/health", token="bad")
        self.assertEqual(status, 401)
        self.assertNotIn(API_SECRET, str(body))

    def test_health_capabilities_and_unavailable_queries(self):
        s, body, headers = self.request("GET", "/v1/health")
        self.assertEqual(s, 200)
        self.assertFalse(body["exchange_connected"])
        self.assertFalse(body["live_trading_enabled"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        s, body, _ = self.request("GET", "/v1/capabilities")
        self.assertEqual(s, 200)
        self.assertFalse(body["exchange_order_query"])
        for uri in (
            "/v1/exchange/orders/1",
            "/v1/quotes?symbolName=BTCUSDT&timeIncrements=TEN_MINUTE",
            "/v1/settlements/1",
        ):
            s, body, _ = self.request("GET", uri)
            self.assertEqual(s, 501, uri)
            self.assertEqual(body["error"]["code"], "exchange_query_unavailable")

    def test_read_only_queries(self):
        s, body, _ = self.request("GET", "/v1/session")
        self.assertEqual((s, body["state"]), (200, "missing"))
        s, body, _ = self.request("GET", "/v1/orders")
        self.assertEqual((s, body["items"]), (200, []))
        s, body, _ = self.request("GET", "/v1/orders/strategy-demo-001")
        self.assertEqual(s, 404)

    def test_live_disabled_default(self):
        s, body, _ = self.request("POST", "/v1/orders", ORDER)
        self.assertEqual(s, 403)
        self.assertEqual(body["error"]["code"], "trading_disabled")

    def test_live_order_mock_and_same_intent_blocked(self):
        os.environ["BINANCE_EVENT_LIVE_TRADING"] = safe_orders.LIVE_OPT_IN
        save_token({
            "csrftoken": "csrf-test", "p20t": "cookie-test",
            "expirationTimestamp": int(time.time()) + 7200,
        })
        exchange_response = Mock()
        exchange_response.status_code = 200
        exchange_response.json.return_value = {
            "success": True, "data": {"orderId": "EX-123"},
        }
        self.transport.post.return_value = exchange_response
        s, body, _ = self.request("POST", "/v1/orders", ORDER)
        self.assertEqual((s, body["status"]), (202, "QUEUED"))
        self.assertFalse(body["already_exists"])
        final = self.wait_final(ORDER["intent_id"])
        self.assertEqual(final["status"], "ACKNOWLEDGED_NOT_SETTLEMENT_VERIFIED")
        self.assertEqual(final["order_id"], "EX-123")
        self.assertFalse(final["exchange_verified"])
        self.assertEqual(self.transport.post.call_count, 1)
        s, body, _ = self.request("POST", "/v1/orders", ORDER)
        self.assertEqual(s, 200)
        self.assertTrue(body["already_exists"])
        self.assertEqual(self.transport.post.call_count, 1)
        s, body, _ = self.request("GET", "/v1/orders/strategy-demo-001")
        self.assertEqual((s, body["order_id"]), (200, "EX-123"))
        s, body, _ = self.request("GET", "/v1/orders?limit=1")
        self.assertEqual(len(body["items"]), 1)

    def test_timeout_marked_unknown_not_failed_or_retried(self):
        os.environ["BINANCE_EVENT_LIVE_TRADING"] = safe_orders.LIVE_OPT_IN
        save_token({
            "csrftoken": "csrf-test", "p20t": "cookie-test",
            "expirationTimestamp": int(time.time()) + 7200,
        })
        self.transport.post.side_effect = safe_orders.requests.Timeout("no response")
        s, body, _ = self.request("POST", "/v1/orders", ORDER)
        self.assertEqual(s, 202)
        self.assertEqual(body["status"], "QUEUED")
        final = self.wait_final(ORDER["intent_id"])
        self.assertEqual(final["status"], "OUTCOME_UNKNOWN")
        self.assertFalse(final["safe_to_auto_retry"])
        self.assertEqual(self.transport.post.call_count, 1)
        s, body, _ = self.request("POST", "/v1/orders", ORDER)
        self.assertEqual(s, 200)
        self.assertTrue(body["already_exists"])
        self.assertEqual(self.transport.post.call_count, 1)

    def test_submission_is_fast_even_while_exchange_request_blocks(self):
        os.environ["BINANCE_EVENT_LIVE_TRADING"] = safe_orders.LIVE_OPT_IN
        save_token({
            "csrftoken": "csrf-test", "p20t": "cookie-test",
            "expirationTimestamp": int(time.time()) + 7200,
        })
        started = threading.Event()
        release = threading.Event()
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"success": True, "data": {"orderId": "EX-555"}}

        def delayed_network(*args, **kwargs):
            started.set()
            release.wait(timeout=3)
            return response

        self.transport.post.side_effect = delayed_network
        try:
            begin = time.monotonic()
            s, body, _ = self.request("POST", "/v1/orders", ORDER)
            elapsed = time.monotonic() - begin
            self.assertEqual(s, 202)
            self.assertLess(elapsed, 0.5)
            self.assertTrue(started.wait(1))
            begin = time.monotonic()
            s, duplicate, _ = self.request("POST", "/v1/orders", ORDER)
            self.assertEqual(s, 200)
            self.assertTrue(duplicate["already_exists"])
            self.assertLess(time.monotonic() - begin, 0.5)
        finally:
            release.set()
        self.wait_final(ORDER["intent_id"])
        self.assertEqual(self.transport.post.call_count, 1)

    def test_schema_validation(self):
        s, body, _ = self.request("GET", "/v1/orders?limit=9999")
        self.assertEqual(s, 400)
        s, body, _ = self.request("POST", "/v1/orders", {**ORDER, "x-token": "bad"})
        self.assertEqual(s, 400)


if __name__ == "__main__":
    unittest.main()
