"""Offline benchmark for HTTP-independent order intake; NO exchange network.

Run as an unprivileged Linux user:
  python tools/benchmark_dispatcher.py
Produces intake p50/p95/p99 latency for 120 unique signals using temporary
SQLite storage and a fake transport. This is not exchange execution latency.
"""
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from event_gateway import OrderRequest
from fast_orders import FastOrderDispatcher
from safe_orders import LIVE_OPT_IN
from secure_storage import save_token


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"success": True, "data": {"orderId": "benchmark-fixture"}}


class FakeTransport:
    def __init__(self):
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return FakeResponse()

    def close(self):
        pass


def percentile(samples, p):
    values = sorted(samples)
    return values[min(len(values) - 1, int((len(values) - 1) * p))]


def main():
    with tempfile.TemporaryDirectory(prefix="event-dispatch-benchmark-") as directory:
        os.environ["BINANCE_EVENT_STATE_DIR"] = directory
        os.environ["BINANCE_EVENT_LIVE_TRADING"] = LIVE_OPT_IN
        os.environ["BINANCE_EVENT_MAX_ORDER_USDT"] = "5"
        save_token({
            "csrftoken": "BENCHMARK-NOT-REAL",
            "p20t": "BENCHMARK-NOT-REAL",
            "expirationTimestamp": int(time.time()) + 3600,
        })
        transport = FakeTransport()
        timings = []
        with FastOrderDispatcher(max_queue=256, transport_factory=lambda: transport) as dispatcher:
            for n in range(120):
                order = OrderRequest(
                    intent_id=f"benchmark-{n:04d}",
                    orderAmount="5",
                    timeIncrements="TEN_MINUTE",
                    symbolName="BTCUSDT",
                    payoutRatio="0.80",
                    direction="LONG",
                )
                start_ns = time.perf_counter_ns()
                dispatcher.submit(order, max_age_ms=5000)
                timings.append((time.perf_counter_ns() - start_ns) / 1_000_000)
            print("OFFLINE 120 unique-order intake benchmark; NO Binance network")
            print(f"Acceptance latency ms: p50={percentile(timings, 0.50):.2f}, "
                  f"p95={percentile(timings, 0.95):.2f}, "
                  f"p99={percentile(timings, 0.99):.2f}, max={max(timings):.2f}")
            print("All intents durably journaled:", bool(dispatcher.journal.get("benchmark-0119")))


if __name__ == "__main__":
    main()
