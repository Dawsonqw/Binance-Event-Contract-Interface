"""Minimal loopback-only HTTP interface for other automation frameworks.

Run as the unprivileged binanceevt user with BINANCE_EVENT_API_TOKEN set to a
random 32+ character secret. The HTTP listener is ALWAYS 127.0.0.1; use SSH
tunneling for a remote framework. No automatic live-trading opt-in.
"""
import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from event_gateway import EventContractGateway, OrderRequest
from order_queries import LocalRecordError, RemoteQueryUnavailable
from safe_orders import DuplicateIntent, OrderNotConfirmed, LIVE_OPT_IN
from session_lifecycle import AuthenticationRequired

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18765
MAX_BODY_BYTES = 4096


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    gateway = None
    api_token = None
    server_version = "BinanceEventGateway/1"
    sys_version = ""

    def log_message(self, fmt, *args):
        # No URL/query/body/Authorization headers in access logs.
        pass

    def _json(self, status, value):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _error(self, status, code, message):
        self._json(status, {"error": {"code": code, "message": message}})

    def _authorize(self):
        auth = self.headers.get("Authorization", "")
        expected = "Bearer " + self.api_token
        if not hmac.compare_digest(auth.encode("utf-8", errors="replace"), expected.encode("utf-8")):
            self._error(401, "unauthorized", "A valid Bearer token is required")
            return False
        return True

    def _dispatch(self, method):
        if not self._authorize():
            return
        route = urlsplit(self.path)
        path = route.path
        try:
            if method == "GET":
                if path == "/v1/health":
                    return self._json(200, {
                        "service": "running",
                        "exchange_connected": False,
                        "live_trading_enabled": os.environ.get("BINANCE_EVENT_LIVE_TRADING") == LIVE_OPT_IN,
                    })
                if path == "/v1/capabilities":
                    return self._json(200, self.gateway.queries.capabilities())
                if path == "/v1/session":
                    return self._json(200, self.gateway.queries.get_session())
                if path == "/v1/orders":
                    params = parse_qs(route.query, keep_blank_values=True)
                    raw = params.get("limit", ["50"])
                    if len(raw) != 1 or not raw[0].isdecimal():
                        raise ValueError("limit must be a decimal integer")
                    return self._json(200, {"items": self.gateway.queries.list_orders(int(raw[0]))})
                if path.startswith("/v1/orders/"):
                    intent_id = unquote(path[len("/v1/orders/"):])
                    record = self.gateway.queries.get_order(intent_id)
                    if record is None:
                        return self._error(404, "not_found", "No local record for that intent_id")
                    return self._json(200, record)
                if path.startswith("/v1/exchange/orders/"):
                    self.gateway.queries.get_exchange_order(unquote(path.split("/")[-1]))
                if path == "/v1/quotes":
                    params = parse_qs(route.query, keep_blank_values=True)
                    self.gateway.queries.get_quote(
                        params.get("symbolName", [""])[0],
                        params.get("timeIncrements", [""])[0],
                    )
                if path.startswith("/v1/settlements/"):
                    self.gateway.queries.get_settlement(unquote(path.split("/")[-1]))
                return self._error(404, "not_found", "Endpoint not found")

            if method == "POST" and path == "/v1/orders":
                if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    return self._error(415, "unsupported_media_type", "application/json is required")
                size = self.headers.get("Content-Length")
                if size is None or not size.isdecimal() or not (0 < int(size) <= MAX_BODY_BYTES):
                    return self._error(413, "invalid_body_size", "JSON body must be 1-4096 bytes")
                try:
                    payload = json.loads(self.rfile.read(int(size)))
                except (ValueError, UnicodeDecodeError):
                    return self._error(400, "invalid_json", "Invalid JSON request")
                order = OrderRequest.from_dict(payload)
                try:
                    result = self.gateway.trading.submit_order(order)
                except OrderNotConfirmed:
                    # A timeout could mean an executed order. Never imply failure.
                    return self._json(202, {
                        "intent_id": order.intent_id,
                        "status": "submission_attempted_outcome_unknown",
                        "source": "local_audit_only",
                        "exchange_verified": False,
                        "action": "DO_NOT_RETRY_VERIFY_IN_BINANCE_APP",
                    })
                return self._json(200, result)

            return self._error(404, "not_found", "Endpoint not found")

        except AuthenticationRequired:
            return self._error(409, "authentication_required", "Local Binance session is unavailable or expiring")
        except FileNotFoundError:
            return self._error(409, "authentication_required", "No local Binance credentials; interactive login required")
        except PermissionError:
            return self._error(403, "trading_disabled", "Live trading is disabled or credentials are not accessible")
        except DuplicateIntent:
            return self._error(409, "duplicate_intent", "Intent has already been attempted; query local state, do not retry")
        except RemoteQueryUnavailable:
            return self._error(501, "exchange_query_unavailable", "No verified event-contract exchange query endpoint")
        except LocalRecordError:
            return self._error(503, "local_record_invalid", "Local audit record unavailable or corrupt")
        except (TypeError, ValueError):
            return self._error(400, "invalid_request", "Invalid request parameters")
        except Exception:
            # Avoid printing raw exception text: it may contain credentials or
            # undocumented payload. For order POST, outcome must be treated as
            # unknown and reconciled manually.
            if method == "POST" and path == "/v1/orders":
                return self._json(202, {
                    "status": "submission_outcome_unknown",
                    "source": "local_audit_only",
                    "exchange_verified": False,
                    "action": "DO_NOT_RETRY_VERIFY_IN_BINANCE_APP",
                })
            return self._error(500, "internal_error", "Internal error (details suppressed)")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def make_server(port=DEFAULT_PORT, gateway=None, token=None):
    if os.geteuid() == 0:
        raise PermissionError("Never start the HTTP gateway as root")
    secret = token if token is not None else os.environ.get("BINANCE_EVENT_API_TOKEN", "")
    if not isinstance(secret, str) or len(secret) < 32 or not secret.isascii():
        raise ValueError("Set BINANCE_EVENT_API_TOKEN to a random ASCII secret (32+ characters)")
    if not isinstance(port, int) or not (0 <= port <= 65535):
        raise ValueError("port must be between 0 and 65535")
    # Never bind to a public interface; use SSH port forwarding.
    handler = type("BoundEventHandler", (_Handler,), {
        "gateway": gateway or EventContractGateway(),
        "api_token": secret,
    })
    return ThreadingHTTPServer((DEFAULT_HOST, port), handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Loopback-only event-contract trading/query API")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Loopback port (default 18765)")
    args = parser.parse_args(argv)
    with make_server(args.port) as server:
        print(f"Listening on http://{DEFAULT_HOST}:{server.server_address[1]} (loopback only)")
        server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
