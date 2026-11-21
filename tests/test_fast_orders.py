"""Offline dispatcher tests: no Binance network access, real SQLite WAL."""
import concurrent.futures
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from event_gateway import EventContractGateway, OrderRequest
from fast_orders import (
    ConflictingIntent, DEFAULT_MAX_SIGNAL_AGE_MS, DispatcherUnavailable,
    FastOrderDispatcher, SQLiteJournal, StaleSignal, FINAL_STATES,
)
from safe_orders import LIVE_OPT_IN
from secure_storage import save_token

PARAMS = {
    "intent_id": "unique-001", "orderAmount": "5",
    "timeIncrements": "TEN_MINUTE", "symbolName": "BTCUSDT",
    "payoutRatio": "0.8", "direction": "LONG",
}


class FastOrdersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {
            "BINANCE_EVENT_STATE_DIR": self.temp.name + "/secret-state",
            "BINANCE_EVENT_LIVE_TRADING": LIVE_OPT_IN,
            "BINANCE_EVENT_MAX_ORDER_USDT": "5",
        })
        env.start()
        self.addCleanup(env.stop)
        identity = patch.object(os, "geteuid", return_value=1001)
        identity.start()
        self.addCleanup(identity.stop)
        save_token({
            "csrftoken": "test-csrf", "p20t": "test-cookie",
            "expirationTimestamp": int(time.time()) + 7200,
        })
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"success": True, "data": {"orderId": "ack-test"}}
        self.transport = Mock()
        self.transport.post.return_value = response

    def dispatcher(self, **kwargs):
        return FastOrderDispatcher(transport_factory=lambda: self.transport, **kwargs)

    @staticmethod
    def wait_final(dispatcher, intent_id, timeout=4.0):
        stop = time.monotonic() + timeout
        while time.monotonic() < stop:
            entry = dispatcher.journal.get(intent_id)
            if entry is not None and entry["status"] in FINAL_STATES:
                return entry
            time.sleep(0.005)
        raise AssertionError("Order did not reach a terminal local status")

    def test_idempotent_duplicate_and_payload_conflict(self):
        order = OrderRequest.from_dict(PARAMS)
        with self.dispatcher() as dispatch:
            gateway = EventContractGateway(dispatcher=dispatch)
            first = gateway.trading.submit_order_async(order)
            self.assertFalse(first["already_exists"])
            self.assertEqual(first["status"], "QUEUED")
            settled = self.wait_final(dispatch, order.intent_id)
            self.assertEqual(settled["status"], "ACKNOWLEDGED_NOT_SETTLEMENT_VERIFIED")
            self.assertFalse(settled["exchange_verified"])
            self.assertEqual(self.transport.post.call_count, 1)
            second = gateway.trading.submit_order_async(order, signal_time_ms=1)
            self.assertTrue(second["already_exists"])
            self.assertEqual(self.transport.post.call_count, 1)
            with self.assertRaises(ConflictingIntent):
                gateway.trading.submit_order_async(OrderRequest.from_dict({
                    **PARAMS, "direction": "SHORT",
                }))
            self.assertEqual(gateway.queries.get_order(order.intent_id)["order_id"], "ack-test")

    def test_stale_signal_before_enqueue_definitely_not_sent(self):
        old = time.time_ns() // 1_000_000 - 5000
        with self.dispatcher() as dispatch:
            with self.assertRaises(StaleSignal):
                dispatch.submit(OrderRequest.from_dict(PARAMS),
                                signal_time_ms=old, max_age_ms=1000)
            self.assertIsNone(dispatch.journal.get(PARAMS["intent_id"]))
            self.transport.post.assert_not_called()

    def test_expiring_while_queued_is_dropped_not_submitted(self):
        started = threading.Event()
        release = threading.Event()

        def blocked(*args, **kwargs):
            started.set()
            release.wait(timeout=2)
            return self.transport.post.return_value

        response = self.transport.post.return_value
        def mock_post(*args, **kwargs):
            started.set()
            release.wait(timeout=2)
            return response

        self.transport.post.side_effect = mock_post
        with self.dispatcher(max_queue=8) as dispatch:
            first = OrderRequest.from_dict(PARAMS)
            second = OrderRequest.from_dict({**PARAMS, "intent_id": "queued-short-lived"})
            dispatch.submit(first)
            self.assertTrue(started.wait(1))
            dispatch.submit(second, max_age_ms=60)
            time.sleep(0.15)
            release.set()
            self.wait_final(dispatch, first.intent_id)
            final = self.wait_final(dispatch, second.intent_id)
            self.assertEqual(final["status"], "DROPPED_STALE_BEFORE_SEND")
            self.assertEqual(self.transport.post.call_count, 1)

    def test_unknown_network_result_has_no_retry(self):
        from requests import Timeout
        self.transport.post.side_effect = Timeout("network timeout")
        with self.dispatcher() as dispatch:
            dispatch.submit(OrderRequest.from_dict(PARAMS))
            entry = self.wait_final(dispatch, PARAMS["intent_id"])
            self.assertEqual(entry["status"], "OUTCOME_UNKNOWN")
            self.assertFalse(entry["safe_to_auto_retry"])
            self.assertEqual(self.transport.post.call_count, 1)

    def test_recovery_does_not_auto_replay(self):
        journal = SQLiteJournal()
        now = time.time_ns() // 1_000_000
        journal.put_once("staged-queued", {"orderAmount": "5"}, now, now + 10000)
        journal.put_once("staged-dispatch", {"orderAmount": "5"}, now, now + 10000)
        journal.transition("staged-dispatch", "QUEUED", "DISPATCHING")
        with self.dispatcher() as dispatch:
            self.assertEqual(
                dispatch.journal.get("staged-queued")["status"], "DROPPED_STALE_BEFORE_SEND"
            )
            self.assertEqual(
                dispatch.journal.get("staged-dispatch")["status"], "OUTCOME_UNKNOWN"
            )
            self.transport.post.assert_not_called()

    def test_parallel_duplicate_only_one_network_send(self):
        order = OrderRequest.from_dict(PARAMS)
        with self.dispatcher() as dispatch:
            def call(_):
                try:
                    return dispatch.submit(order)
                except DispatcherUnavailable:
                    return None
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                responses = list(pool.map(call, range(16)))
            valid = [r for r in responses if r is not None]
            self.assertGreater(len(valid), 0)
            self.assertEqual(sum(not r["already_exists"] for r in valid), 1)
            self.wait_final(dispatch, order.intent_id)
            self.assertEqual(self.transport.post.call_count, 1)

    def test_120_distinct_daily_signals_are_durable(self):
        with self.dispatcher(max_queue=128) as dispatch:
            seen = []
            for i in range(120):
                order = OrderRequest.from_dict({
                    **PARAMS, "intent_id": f"daily-{i:04d}",
                })
                receipt = dispatch.submit(order, max_age_ms=5000)
                self.assertFalse(receipt["already_exists"])
                seen.append(receipt["intent_id"])
            self.assertEqual(len(set(seen)), 120)
            self.assertEqual(len(dispatch.journal.list(limit=100)), 100)
            self.assertIsNotNone(dispatch.journal.get("daily-0119"))

    def test_disabled_trade_rejected_without_journal(self):
        with self.dispatcher() as dispatch:
            os.environ["BINANCE_EVENT_LIVE_TRADING"] = ""
            with self.assertRaises(PermissionError):
                dispatch.submit(OrderRequest.from_dict(PARAMS))
            self.assertIsNone(dispatch.journal.get(PARAMS["intent_id"]))
            self.transport.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
