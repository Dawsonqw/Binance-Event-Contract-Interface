"""One-shot *read-only* HTTP gateway smoke test on localhost.

Invokes GET /v1/exchange/orders/{ongoing,closed} with an in-process ephemeral
gateway key and prints ONLY status and list counts; never outputs order data,
account identity, login cookies or API key.

DO NOT run this as root. No trade submission route is called.
"""
import http.client
import json
import os
import secrets
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from http_api import make_server


def main():
    # Ensure this diagnostic cannot turn on trading even when invoked in a
    # process inheriting the live opt-in environment.
    os.environ.pop("BINANCE_EVENT_LIVE_TRADING", None)
    secret = secrets.token_urlsafe(48)
    server = make_server(port=0, token=secret)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for kind in ("ongoing", "closed"):
            client = http.client.HTTPConnection(
                "127.0.0.1", server.server_address[1], timeout=8
            )
            try:
                client.request(
                    "GET", "/v1/exchange/orders/" + kind,
                    headers={"Authorization": "Bearer " + secret},
                )
                response = client.getresponse()
                data = json.loads(response.read())
                output = {"kind": kind, "http": response.status}
                if response.status == 200:
                    output["total"] = data.get("total")
                    output["returned_count"] = data.get("returned_count")
                    output["has_more"] = data.get("has_more")
                    output["source"] = data.get("source")
                else:
                    output["error_code"] = data.get("error", {}).get("code")
                print(json.dumps(output, ensure_ascii=False))
                if response.status != 200:
                    return 1
            finally:
                client.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
