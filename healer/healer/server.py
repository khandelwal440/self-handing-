"""HTTP entrypoint: Alertmanager webhook on /webhook, Prometheus metrics on /metrics."""

import json
import logging
import os
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import Config
from .engine import Engine
from .kube import Kube

log = logging.getLogger("healer")


def make_handler(engine):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/healthz":
                self._send(200, b"ok", "text/plain")
            elif self.path == "/metrics":
                self._send(200, generate_latest(), CONTENT_TYPE_LATEST)
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/webhook":
                self._send(404, b"not found", "text/plain")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length))
            except ValueError:
                self._send(400, b"invalid JSON", "text/plain")
                return
            if not isinstance(payload, dict):
                self._send(400, b"expected an Alertmanager webhook object", "text/plain")
                return
            outcomes = engine.handle(payload)
            self._send(200, json.dumps([asdict(o) for o in outcomes]).encode(), "application/json")

        def _send(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

    return Handler


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = Config.from_env()
    engine = Engine(Kube.connect(), config)
    port = int(os.getenv("PORT", "8080"))
    log.info(
        "healer listening on :%d (dry_run=%s, namespaces=%s)",
        port,
        config.dry_run,
        ",".join(sorted(config.allowed_namespaces)),
    )
    ThreadingHTTPServer(("", port), make_handler(engine)).serve_forever()


if __name__ == "__main__":
    main()
