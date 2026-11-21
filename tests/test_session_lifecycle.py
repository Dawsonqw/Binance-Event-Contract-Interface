"""Offline session lifecycle tests."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from secure_storage import save_token
from session_lifecycle import (
    AuthenticationRequired,
    get_session_status,
    main,
    require_fresh_session,
)


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.dict(os.environ, {"BINANCE_EVENT_STATE_DIR": self.temp.name + "/private"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 2000000000

    def _save(self, expiry):
        save_token({"csrftoken": "a-secret", "p20t": "b-secret", "expirationTimestamp": expiry})

    def test_missing(self):
        self.assertEqual(get_session_status(now=self.now)["state"], "missing")
        with self.assertRaises(AuthenticationRequired):
            require_fresh_session("a-secret", "b-secret", now=self.now)

    def test_corrupt_token_fails_closed_without_dumping_content(self):
        directory = Path(os.environ["BINANCE_EVENT_STATE_DIR"])
        directory.mkdir(mode=0o700)
        (directory / "token.json").write_text("malformed credentials", encoding="utf-8")
        (directory / "token.json").chmod(0o600)
        self.assertEqual(get_session_status(now=self.now)["state"], "invalid_local_state")
        with self.assertRaises(AuthenticationRequired) as ctx:
            require_fresh_session("a-secret", "b-secret", now=self.now)
        self.assertNotIn("malformed credentials", str(ctx.exception))

    def test_known_expiry(self):
        self._save(self.now + 100000)
        status = get_session_status(now=self.now)
        self.assertEqual(status["state"], "locally_valid")
        self.assertEqual(status["remaining_seconds"], 100000)
        self.assertTrue(require_fresh_session("a-secret", "b-secret", now=self.now))

    def test_expiring_at_24_hours(self):
        self._save(self.now + 24 * 3600)
        self.assertEqual(get_session_status(now=self.now)["state"], "expiring")
        self.assertTrue(require_fresh_session("a-secret", "b-secret", now=self.now))

    def test_expiry_buffer_blocks_trading(self):
        self._save(self.now + 15 * 60)
        with self.assertRaises(AuthenticationRequired):
            require_fresh_session("a-secret", "b-secret", now=self.now)

    def test_expired(self):
        self._save(self.now - 1)
        self.assertEqual(get_session_status(now=self.now)["state"], "expired")
        with self.assertRaises(AuthenticationRequired):
            require_fresh_session("a-secret", "b-secret", now=self.now)

    def test_unknown_expiry(self):
        for invalid in [-1, None, True, "invalid", 0, 253402300800]:
            with self.subTest(invalid=invalid):
                self._save(invalid)
                self.assertEqual(get_session_status(now=self.now)["state"], "unknown_expiry")
                with self.assertRaises(AuthenticationRequired):
                    require_fresh_session("a-secret", "b-secret", now=self.now)

    def test_mismatch_is_rejected(self):
        self._save(self.now + 100000)
        with self.assertRaises(AuthenticationRequired):
            require_fresh_session("incorrect", "b-secret", now=self.now)

    def test_no_token_in_status_output(self):
        self._save(self.now + 100000)
        from io import StringIO
        with patch("sys.stdout", new_callable=StringIO) as output:
            with patch("session_lifecycle.time.time", return_value=self.now):
                self.assertEqual(main(["status"]), 0)
            displayed = output.getvalue()
            self.assertNotIn("a-secret", displayed)
            self.assertNotIn("b-secret", displayed)
            self.assertIn("locally_valid", displayed)


if __name__ == "__main__":
    unittest.main()
