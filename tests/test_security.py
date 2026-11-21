"""Offline security tests: do not contact Binance or submit orders."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

import safe_orders
from secure_storage import load_token, save_token


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patcher = patch.dict(os.environ, {
            "BINANCE_EVENT_STATE_DIR": self.temp.name + "/private",
            "BINANCE_EVENT_LIVE_TRADING": safe_orders.LIVE_OPT_IN,
            "BINANCE_EVENT_MAX_ORDER_USDT": "5",
        })
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.uid_patcher = patch.object(safe_orders.os, "geteuid", return_value=1001)
        self.uid_patcher.start()
        self.addCleanup(self.uid_patcher.stop)
        self.params = dict(
            csrftoken="test-csrf", p20t="test-cookie",
            orderAmount="5", timeIncrements="TEN_MINUTE", symbolName="BTCUSDT",
            payoutRatio="0.80", direction="LONG", intent_id="signal-abc-001",
        )

    def test_token_private_atomic_round_trip(self):
        secret = {"csrftoken": "secret-csrf", "p20t": "secret-cookie", "expirationTimestamp": 111}
        save_token(secret)
        path = Path(os.environ["BINANCE_EVENT_STATE_DIR"]) / "token.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(load_token(), secret)
        save_token({**secret, "expirationTimestamp": 222})
        self.assertEqual(load_token()["expirationTimestamp"], 222)

    def test_reject_insecure_token_file(self):
        save_token({"csrftoken": "a", "p20t": "b"})
        path = Path(os.environ["BINANCE_EVENT_STATE_DIR"]) / "token.json"
        path.chmod(0o644)
        with self.assertRaises(PermissionError):
            load_token()

    def test_reject_token_symlink(self):
        state = Path(os.environ["BINANCE_EVENT_STATE_DIR"])
        state.mkdir(mode=0o700)
        outside = Path(self.temp.name) / "outside.json"
        outside.write_text("unchanged", encoding="utf-8")
        (state / "token.json").symlink_to(outside)
        with self.assertRaises(RuntimeError):
            save_token({"csrftoken": "a", "p20t": "b"})
        self.assertEqual(outside.read_text(encoding="utf-8"), "unchanged")

    def test_live_orders_disabled_by_default(self):
        with patch.dict(os.environ, {"BINANCE_EVENT_LIVE_TRADING": ""}), \
             patch.object(safe_orders.requests, "post") as post:
            with self.assertRaises(PermissionError):
                safe_orders.place_order_web(**self.params)
            post.assert_not_called()

    def test_nonroot_required(self):
        with patch.object(safe_orders.os, "geteuid", return_value=0), \
             patch.object(safe_orders.requests, "post") as post:
            with self.assertRaises(PermissionError):
                safe_orders.place_order_web(**self.params)
            post.assert_not_called()

    def test_cap_and_invalid_values_block_before_network(self):
        for change in [
            {"orderAmount": "5.01"}, {"orderAmount": "-1"},
            {"orderAmount": "NaN"}, {"direction": "SELL"},
            {"symbolName": "../../etc"}, {"payoutRatio": "inf"},
            {"intent_id": "../bad"}, {"timeIncrements": "x"},
        ]:
            with self.subTest(change=change):
                with patch.object(safe_orders.requests, "post") as post:
                    with self.assertRaises(ValueError):
                        safe_orders.place_order_web(**{**self.params, **change})
                    post.assert_not_called()

    def test_success_once_and_duplicate_blocked(self):
        response = Mock()
        response.json.return_value = {"success": True, "data": {"orderId": "order-123"}}
        with patch.object(safe_orders.requests, "post", return_value=response) as post:
            result = safe_orders.place_order_web(**self.params)
            self.assertEqual(result["data"]["orderId"], "order-123")
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.kwargs["timeout"], (5, 12))
            with self.assertRaises(safe_orders.DuplicateIntent):
                safe_orders.place_order_web(**self.params)
            self.assertEqual(post.call_count, 1)
        intent_dir = Path(os.environ["BINANCE_EVENT_STATE_DIR"]) / "intents"
        self.assertEqual(len(list(intent_dir.glob("*.json"))), 1)
        self.assertEqual(len(list(intent_dir.glob("*.confirmed"))), 1)
        for entry in intent_dir.iterdir():
            self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)
            self.assertNotIn("test-cookie", entry.read_text(encoding="utf-8"))

    def test_timeout_must_never_retry_or_log_token(self):
        with patch.object(safe_orders.requests, "post", side_effect=requests.Timeout("private")) as post:
            with self.assertRaises(safe_orders.OrderNotConfirmed) as ctx:
                safe_orders.place_order_web(**self.params)
            self.assertNotIn("private", str(ctx.exception))
            self.assertNotIn("test-cookie", str(ctx.exception))
            self.assertEqual(post.call_count, 1)
            with self.assertRaises(safe_orders.DuplicateIntent):
                safe_orders.place_order_web(**self.params)
            self.assertEqual(post.call_count, 1)

    def test_success_without_order_id_is_unknown(self):
        response = Mock()
        response.json.return_value = {"success": True, "data": {}}
        with patch.object(safe_orders.requests, "post", return_value=response):
            with self.assertRaises(safe_orders.OrderNotConfirmed):
                safe_orders.place_order_web(**self.params)

    def test_failed_response_is_unknown(self):
        response = Mock()
        response.json.return_value = {"success": False, "code": "ERROR"}
        with patch.object(safe_orders.requests, "post", return_value=response):
            with self.assertRaises(safe_orders.OrderNotConfirmed):
                safe_orders.place_order_web(**self.params)


if __name__ == "__main__":
    unittest.main()
