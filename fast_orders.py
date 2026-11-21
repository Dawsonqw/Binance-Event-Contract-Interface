"""Nonblocking event-contract order dispatcher.

A short HTTP request only journals a UNIQUE intent into SQLite WAL and queues it
in memory. Exactly one local worker sends network requests. There is NO retry
when the response is unknown. Signal expiry before network dispatch is a
definite local drop; a timeout AFTER dispatch is NOT proof of exchange failure.

This is not exchange-level idempotency. All credentials stay outside SQLite.
"""
import fcntl
import hashlib
import json
import os
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

import safe_orders
from secure_storage import load_token, state_dir
from session_lifecycle import require_fresh_session

DEFAULT_MAX_SIGNAL_AGE_MS = 1000
MAX_SIGNAL_AGE_MS = 5000
DEFAULT_QUEUE_SIZE = 128
CONNECT_TIMEOUT_SECONDS = 0.7
READ_TIMEOUT_SECONDS = 1.3
_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,100}\Z")

ACTIVE_STATES = ("QUEUED", "DISPATCHING")
FINAL_STATES = (
    "DROPPED_STALE_BEFORE_SEND",
    "REJECTED_AUTH_BEFORE_SEND",
    "REJECTED_DISABLED_BEFORE_SEND",
    "REJECTED_OVERLOAD_BEFORE_SEND",
    "REJECTED_INTERNAL_BEFORE_SEND",
    "ACKNOWLEDGED_NOT_SETTLEMENT_VERIFIED",
    "OUTCOME_UNKNOWN",
)
_SELECT = """SELECT intent_id, status, created_ms, deadline_ms, updated_ms,
                    exchange_order_id, reason, request_json
             FROM orders"""


class StaleSignal(ValueError):
    """Signal was already stale on arrival and was never accepted."""


class DispatcherUnavailable(RuntimeError):
    """Queue/SQLite unavailable; no request is transmitted by this attempt."""


class ConflictingIntent(ValueError):
    """The same intent ID was previously used with different order parameters."""


def _validated_timing(signal_time_ms, max_age_ms):
    if isinstance(max_age_ms, bool) or not isinstance(max_age_ms, int):
        raise ValueError("max_age_ms must be an integer")
    if not (50 <= max_age_ms <= MAX_SIGNAL_AGE_MS):
        raise ValueError("max_age_ms must be between 50 and 5000")
    now = time.time_ns() // 1_000_000
    signal_time_ms = now if signal_time_ms is None else signal_time_ms
    if isinstance(signal_time_ms, bool) or not isinstance(signal_time_ms, int):
        raise ValueError("signal_time_ms must be an epoch-millisecond integer")
    if signal_time_ms > now + 500:
        raise ValueError("signal_time_ms is in the future (clock skew)")
    deadline = signal_time_ms + max_age_ms
    if deadline <= now:
        raise StaleSignal("Signal expired before enqueue; definitely not transmitted")
    return now, deadline


def _safe_intent_id(intent_id):
    if not isinstance(intent_id, str) or _ID_RE.fullmatch(intent_id) is None:
        raise ValueError("Invalid intent_id")
    return intent_id


def _public_row(row):
    if row is None:
        return None
    intent_id, status, created, deadline, updated, exchange_order_id, reason, request_json = row
    order = json.loads(request_json)
    return {
        "intent_id": intent_id,
        "status": status,
        "created_ms": created,
        "deadline_ms": deadline,
        "updated_ms": updated,
        "order_id": exchange_order_id,
        "reason": reason,
        "order": order,
        "source": "local_dispatch_journal",
        "exchange_verified": False,
        "final_settlement_verified": False,
        "safe_to_auto_retry": False,
    }


class SQLiteJournal:
    """Durable intent unique key and constant-time status lookup."""

    def __init__(self, db_path=None):
        path = Path(db_path) if db_path is not None else state_dir() / "async-orders.sqlite3"
        if path.is_symlink():
            raise RuntimeError("Refusing symlinked order database")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = path
        self._initialize()

    def _connect(self):
        if self.path.is_symlink():
            raise RuntimeError("Refusing symlinked order database")
        connection = sqlite3.connect(str(self.path), timeout=0.15, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=150")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self):
        con = self._connect()
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=FULL")
            con.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    intent_id TEXT PRIMARY KEY NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_ms INTEGER NOT NULL,
                    deadline_ms INTEGER NOT NULL,
                    updated_ms INTEGER NOT NULL,
                    exchange_order_id TEXT,
                    reason TEXT
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS orders_created_idx ON orders(created_ms DESC)")
            os.chmod(self.path, 0o600)
        finally:
            con.close()

    def recover_without_replaying(self):
        """Fail closed on restart: queued jobs are dropped; inflight are UNKNOWN."""
        now = time.time_ns() // 1_000_000
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE orders SET status=?,reason=?,updated_ms=? WHERE status=?",
                ("DROPPED_STALE_BEFORE_SEND", "restart_discarded_queue", now, "QUEUED"),
            )
            con.execute(
                "UPDATE orders SET status=?,reason=?,updated_ms=? WHERE status=?",
                ("OUTCOME_UNKNOWN", "restart_inflight_may_have_transmitted", now, "DISPATCHING"),
            )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def put_once(self, intent_id, order_fields, created_ms, deadline_ms):
        _safe_intent_id(intent_id)
        serial = json.dumps(order_fields, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        digest = hashlib.sha256(serial.encode("utf-8")).hexdigest()
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(_SELECT + " WHERE intent_id=?", (intent_id,)).fetchone()
            if row is not None:
                old_digest = con.execute(
                    "SELECT payload_sha256 FROM orders WHERE intent_id=?", (intent_id,)
                ).fetchone()[0]
                if old_digest != digest:
                    raise ConflictingIntent("An existing intent has different order parameters")
                con.execute("COMMIT")
                result = _public_row(row)
                result["already_exists"] = True
                return result
            con.execute(
                """INSERT INTO orders(
                    intent_id,payload_sha256,request_json,status,created_ms,deadline_ms,updated_ms
                ) VALUES (?,?,?,?,?,?,?)""",
                (intent_id, digest, serial, "QUEUED", created_ms, deadline_ms, created_ms),
            )
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
        result = self.get(intent_id)
        result["already_exists"] = False
        return result

    def transition(self, intent_id, expected, status, reason=None, order_id=None):
        now = time.time_ns() // 1_000_000
        con = self._connect()
        try:
            changed = con.execute(
                """UPDATE orders SET status=?, updated_ms=?, reason=?,
                      exchange_order_id=COALESCE(?,exchange_order_id)
                      WHERE intent_id=? AND status=?""",
                (status, now, reason, order_id, intent_id, expected),
            ).rowcount
            return changed == 1
        finally:
            con.close()

    def get(self, intent_id):
        _safe_intent_id(intent_id)
        con = self._connect()
        try:
            return _public_row(con.execute(_SELECT + " WHERE intent_id=?", (intent_id,)).fetchone())
        finally:
            con.close()

    def list(self, limit=50):
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 100):
            raise ValueError("limit must be between 1 and 100")
        con = self._connect()
        try:
            rows = con.execute(_SELECT + " ORDER BY created_ms DESC LIMIT ?", (limit,)).fetchall()
            return [_public_row(row) for row in rows]
        finally:
            con.close()


class FastOrderDispatcher:
    """Single-worker, bounded queue. Never resubmit automatically after restart."""

    def __init__(self, *, max_queue=DEFAULT_QUEUE_SIZE, transport_factory=None):
        if os.geteuid() == 0:
            raise PermissionError("Fast order gateway must run as a non-root user")
        self._lock_fd = None
        self._closed = False
        self.queue = queue.Queue(maxsize=max_queue)
        self.journal = SQLiteJournal()
        # OS-level lock prevents two local server processes from replaying the same DB.
        lock_path = state_dir() / "fast-dispatch.lock"
        if lock_path.is_symlink():
            raise RuntimeError("Refusing symlinked dispatcher lock")
        self._lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._lock_fd)
            self._lock_fd = None
            raise DispatcherUnavailable("Another order dispatcher is active") from None
        try:
            self.journal.recover_without_replaying()
        except Exception:
            self.close()
            raise
        self._stop = threading.Event()
        self._transport_factory = transport_factory or self._new_transport
        self._worker = threading.Thread(target=self._run, name="event-order-worker", daemon=True)
        self._worker.start()

    @staticmethod
    def _new_transport():
        session = requests.Session()
        session.trust_env = False  # never send Binance cookies to a proxy from environment
        session.mount("https://", HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0))
        return session

    def submit(self, order, *, signal_time_ms=None, max_age_ms=DEFAULT_MAX_SIGNAL_AGE_MS):
        """Journal and enqueue only; never wait for Binance network response."""
        if self._closed or not self._worker.is_alive():
            raise DispatcherUnavailable("Dispatcher is not running")
        if os.environ.get("BINANCE_EVENT_LIVE_TRADING") != safe_orders.LIVE_OPT_IN:
            raise PermissionError("Live trading is disabled")
        fields = safe_orders._validate(
            order.orderAmount, order.timeIncrements, order.symbolName,
            order.payoutRatio, order.direction, order.intent_id,
        )
        # A duplicate lookup must work even after the original signal expired
        # or while the queue is full; it NEVER initiates another HTTP request.
        existing = self.journal.get(order.intent_id)
        if existing is not None:
            if existing["order"] != fields:
                raise ConflictingIntent("Intent ID reused with different order parameters")
            existing["already_exists"] = True
            return existing
        created_ms, deadline_ms = _validated_timing(signal_time_ms, max_age_ms)
        if self.queue.full():
            raise DispatcherUnavailable("Bounded order queue is full; signal not accepted")
        try:
            result = self.journal.put_once(order.intent_id, fields, created_ms, deadline_ms)
        except sqlite3.OperationalError:
            raise DispatcherUnavailable("Order journal busy; signal not accepted") from None
        if not result["already_exists"]:
            try:
                self.queue.put_nowait((order.intent_id, fields, deadline_ms))
            except queue.Full:
                self.journal.transition(
                    order.intent_id, "QUEUED", "REJECTED_OVERLOAD_BEFORE_SEND",
                    "queue_full_after_journal",
                )
                result = self.journal.get(order.intent_id)
                result["already_exists"] = False
        return result

    def _run(self):
        transport = self._transport_factory()
        try:
            while not self._stop.is_set():
                try:
                    intent_id, fields, deadline_ms = self.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    self._execute_one(intent_id, fields, deadline_ms, transport)
                except Exception:
                    # Keep the worker alive, but never resubmit a possibly
                    # transmitted intent. Status transitions remain fail-closed.
                    try:
                        state = self.journal.get(intent_id)
                        if state is not None and state["status"] == "DISPATCHING":
                            self.journal.transition(
                                intent_id, "DISPATCHING", "OUTCOME_UNKNOWN",
                                "worker_internal_error_after_dispatch_started",
                            )
                        elif state is not None and state["status"] == "QUEUED":
                            self.journal.transition(
                                intent_id, "QUEUED", "REJECTED_INTERNAL_BEFORE_SEND",
                                "worker_internal_error_before_dispatch",
                            )
                    except Exception:
                        # The transaction log is still durable; no auto replay.
                        pass
                finally:
                    self.queue.task_done()
        finally:
            close = getattr(transport, "close", None)
            if callable(close):
                close()

    def _execute_one(self, intent_id, fields, deadline_ms, transport):
        now = time.time_ns() // 1_000_000
        if now >= deadline_ms:
            self.journal.transition(intent_id, "QUEUED", "DROPPED_STALE_BEFORE_SEND", "queue_expired")
            return
        if os.environ.get("BINANCE_EVENT_LIVE_TRADING") != safe_orders.LIVE_OPT_IN:
            self.journal.transition(intent_id, "QUEUED", "REJECTED_DISABLED_BEFORE_SEND", "live_trading_disabled")
            return
        try:
            token = load_token()
            require_fresh_session(token["csrftoken"], token["p20t"])
        except Exception:
            self.journal.transition(intent_id, "QUEUED", "REJECTED_AUTH_BEFORE_SEND", "local_session_invalid")
            return

        if time.time_ns() // 1_000_000 >= deadline_ms:
            self.journal.transition(intent_id, "QUEUED", "DROPPED_STALE_BEFORE_SEND", "preflight_expired")
            return

        if not self.journal.transition(intent_id, "QUEUED", "DISPATCHING", "started_network_attempt"):
            return
        try:
            payload = safe_orders.place_order_web(
                token["csrftoken"], token["p20t"],
                fields["orderAmount"], fields["timeIncrements"],
                fields["symbolName"], fields["payoutRatio"], fields["direction"],
                intent_id=intent_id,
                transport=transport,
                timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                deadline_ms=deadline_ms,
            )
            exchange_id = safe_orders._confirmed_order_id(payload)
            self.journal.transition(intent_id, "DISPATCHING", "ACKNOWLEDGED_NOT_SETTLEMENT_VERIFIED",
                                    "api_response_acknowledged", exchange_id)
        except safe_orders.SignalExpiredBeforeSend:
            self.journal.transition(intent_id, "DISPATCHING", "DROPPED_STALE_BEFORE_SEND", "expired_at_network_boundary")
        except safe_orders.OrderNotConfirmed:
            self.journal.transition(intent_id, "DISPATCHING", "OUTCOME_UNKNOWN", "timeout_or_unverified_response")
        except Exception:
            # Anything after DISPATCHING is conservatively UNKNOWN; no retry.
            self.journal.transition(intent_id, "DISPATCHING", "OUTCOME_UNKNOWN", "unhandled_dispatch_exception")

    def health(self):
        return {
            "worker_alive": self._worker.is_alive(),
            "queue_depth": self.queue.qsize(),
            "queue_max": self.queue.maxsize,
            "network_timeout_sec": CONNECT_TIMEOUT_SECONDS + READ_TIMEOUT_SECONDS,
            "default_signal_ttl_ms": DEFAULT_MAX_SIGNAL_AGE_MS,
        }

    def close(self):
        if self._closed:
            return
        self._closed = True
        stop = getattr(self, "_stop", None)
        worker = getattr(self, "_worker", None)
        if stop is not None:
            stop.set()
        if worker is not None:
            worker.join(timeout=3)
        # No queued orders are replayed after shutdown. Inflight responses
        # are conservatively unknown if the worker did not finish.
        if getattr(self, "journal", None) is not None:
            try:
                self.journal.recover_without_replaying()
            except Exception:
                pass
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

