"""shop-api: a tiny HTTP service with built-in fault switches, used as the victim in chaos demos.

Fault switches (trigger from inside the pod with `python app.py fault <name>`):
  unready  the readiness probe starts failing forever: a "stuck" pod Kubernetes never restarts
  leak     leaks memory steadily until the container would be OOM-killed

Behaviour knobs (env): APP_VERSION, ERROR_RATE (0..1 of requests answered with 500),
WORK_ITERATIONS (CPU work per request), LEAK_MIB_PER_TICK, LEAK_INTERVAL_SECONDS.
"""

import hashlib
import os
import random
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = os.getenv("APP_VERSION", "v1")
ERROR_RATE = float(os.getenv("ERROR_RATE", "0"))
WORK_ITERATIONS = int(os.getenv("WORK_ITERATIONS", "10000"))
LEAK_MIB_PER_TICK = int(os.getenv("LEAK_MIB_PER_TICK", "1"))
LEAK_INTERVAL_SECONDS = float(os.getenv("LEAK_INTERVAL_SECONDS", "3"))
PORT = 8080
ROUTE = "/api/products"
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


class Metrics:
    """Hand-rolled Prometheus exposition so the image needs nothing beyond the stdlib."""

    def __init__(self):
        self._lock = threading.Lock()
        self._requests = {}
        self._bucket_counts = [0] * len(BUCKETS)
        self._count = 0
        self._sum = 0.0

    def observe(self, code, seconds):
        with self._lock:
            self._requests[code] = self._requests.get(code, 0) + 1
            self._count += 1
            self._sum += seconds
            for i, bound in enumerate(BUCKETS):
                if seconds <= bound:
                    self._bucket_counts[i] += 1

    def render(self):
        labels = f'path="{ROUTE}"'
        with self._lock:
            lines = [
                "# HELP app_info Static information about the running build.",
                "# TYPE app_info gauge",
                f'app_info{{version="{VERSION}"}} 1',
                "# HELP http_requests_total Requests served, by status code.",
                "# TYPE http_requests_total counter",
            ]
            lines += [f'http_requests_total{{{labels},code="{code}"}} {n}' for code, n in sorted(self._requests.items())]
            lines += [
                "# HELP http_request_duration_seconds Request latency.",
                "# TYPE http_request_duration_seconds histogram",
            ]
            lines += [
                f'http_request_duration_seconds_bucket{{{labels},le="{bound}"}} {count}'
                for bound, count in zip(BUCKETS, self._bucket_counts)
            ]
            lines += [
                f'http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {self._count}',
                f"http_request_duration_seconds_sum{{{labels}}} {self._sum}",
                f"http_request_duration_seconds_count{{{labels}}} {self._count}",
            ]
        return ("\n".join(lines) + "\n").encode()


class Faults:
    def __init__(self):
        self.ready = True
        self._leaked = []
        self._leaking = False
        self._lock = threading.Lock()

    def start_leak(self):
        with self._lock:
            if self._leaking:
                return
            self._leaking = True
        threading.Thread(target=self._leak_forever, daemon=True).start()

    def _leak_forever(self):
        chunk = LEAK_MIB_PER_TICK * 1024 * 1024
        while True:
            # Filled (not zeroed) bytes, so the pages are really touched and show up in the working set.
            self._leaked.append(b"\x01" * chunk)
            time.sleep(LEAK_INTERVAL_SECONDS)


METRICS = Metrics()
FAULTS = Faults()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == ROUTE:
            self._serve_products()
        elif path == "/healthz":
            self._reply(200, b"ok")
        elif path == "/readyz":
            self._reply(200, b"ready") if FAULTS.ready else self._reply(503, b"not ready")
        elif path == "/metrics":
            self._reply(200, METRICS.render(), "text/plain; version=0.0.4")
        elif path == "/admin/unready":
            FAULTS.ready = False
            self._reply(200, b"readiness probe will now fail forever")
        elif path == "/admin/leak":
            FAULTS.start_leak()
            self._reply(200, f"leaking {LEAK_MIB_PER_TICK} MiB every {LEAK_INTERVAL_SECONDS}s".encode())
        else:
            self._reply(404, b"not found")

    do_POST = do_GET

    def _serve_products(self):
        start = time.perf_counter()
        digest = b"shop"
        for _ in range(WORK_ITERATIONS):
            digest = hashlib.sha256(digest).digest()
        if random.random() < ERROR_RATE:
            code, body = 500, b'{"error":"internal error"}'
        else:
            code, body = 200, ('{"version":"%s","products":["tea","coffee","cocoa"]}' % VERSION).encode()
        self._reply(code, body, "application/json")
        METRICS.observe(str(code), time.perf_counter() - start)

    def _reply(self, code, body, content_type="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # access logs would drown the interesting output; metrics tell the story


def main():
    if sys.argv[1:2] == ["fault"]:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/admin/{sys.argv[2]}", timeout=5) as resp:
            print(resp.read().decode())
        return
    print(f"shop-api {VERSION} listening on :{PORT} (error_rate={ERROR_RATE})", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
