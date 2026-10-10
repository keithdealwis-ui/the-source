"""The Source MCP server over Streamable HTTP (KEI-851, docs/MCP.md).

    python -m source_pipeline.mcp_http [--root DIR] [--host 127.0.0.1] [--port 8851]
                                       [--telemetry FILE] [--allow-origin ORIGIN ...]
    python -m source_pipeline mcp-http ...

The transport for the engine in mcp_server.py, which stays standard library only, read-only
and network-free. This module adds the socket and nothing else:

* POST /mcp takes one JSON-RPC message and answers application/json (the Streamable HTTP
  transport, MCP 2025-03-26 and 2025-06-18). Notifications get 202 with no body. Batches,
  GET streams and DELETE are not offered (405). The server is stateless: it issues no
  Mcp-Session-Id, and each request is answered under the protocol version named in its
  MCP-Protocol-Version header (2025-03-26 when absent, as the specification says).
* GET /healthz answers the dataset version being served.
* Bounded: request bodies over MAX_BODY are refused (413), at most MAX_INFLIGHT requests
  are processed at once (503 beyond that), and each client address gets RATE_PER_MIN
  requests a minute (429). Addresses are held in memory for the rate window only; they are
  never written or logged.
* An Origin header, when a browser sends one, must be in --allow-origin (DNS-rebinding
  protection, as the transport specification requires); non-browser clients send none.
* No request logging. --telemetry writes the engine's aggregate counters (mcp_server.Telemetry:
  tool, outcome, latency bucket per UTC day; no arguments, results, identities or addresses).

Intended to run behind a TLS reverse proxy on 127.0.0.1 (deploy/the-source-mcp/).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import mcp_server as M

MAX_BODY = 64 * 1024
MAX_INFLIGHT = 8
RATE_PER_MIN = 120
TELEMETRY_EVERY_S = 60
DEFAULT_PROTOCOL = "2025-03-26"


class RateLimiter:
    """A fixed one-minute window per client address, in memory only."""

    def __init__(self, per_min: int):
        self.per_min, self.window, self.counts = per_min, None, {}
        self.lock = threading.Lock()

    def allow(self, client: str) -> bool:
        minute = int(time.time() // 60)
        with self.lock:
            if minute != self.window:
                self.window, self.counts = minute, {}
            n = self.counts.get(client, 0) + 1
            self.counts[client] = n
            return n <= self.per_min


class App:
    def __init__(self, server: M.Server, allow_origins=(), telemetry_path=None,
                 rate_per_min=RATE_PER_MIN, max_inflight=MAX_INFLIGHT):
        self.server = server
        self.allow_origins = set(allow_origins)
        self.telemetry_path = Path(telemetry_path) if telemetry_path else None
        self.rate = RateLimiter(rate_per_min)
        self.slots = threading.BoundedSemaphore(max_inflight)
        self.engine = threading.Lock()  # the engine holds per-request protocol state; one call at a time
        self.last_flush = 0.0
        self.dirty = False

    def answer(self, msg, protocol):
        """One JSON-RPC message under one protocol version -> response object or None."""
        with self.engine:
            s = self.server
            if isinstance(msg, dict) and msg.get("method") == "initialize":
                s.protocol = None
            else:
                s.protocol = protocol if protocol in M.PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
            try:
                return s.handle(msg)
            except Exception as exc:  # never die on one bad request
                print(f"the-source-mcp: internal error: {type(exc).__name__}", file=sys.stderr)
                return M._err(msg.get("id") if isinstance(msg, dict) else None, -32603, "Internal error")
            finally:
                s.protocol = None
                self.dirty = True

    def flusher(self):
        """Write the counters every TELEMETRY_EVERY_S while anything changed, so the last window is never lost."""
        def run():
            while True:
                time.sleep(TELEMETRY_EVERY_S)
                if self.dirty:
                    self.maybe_flush(force=True)
        threading.Thread(target=run, daemon=True, name="telemetry-flush").start()

    def maybe_flush(self, force=False):
        if not self.telemetry_path or (not force and time.monotonic() - self.last_flush < TELEMETRY_EVERY_S):
            return
        self.last_flush = time.monotonic()
        with self.engine:
            self.dirty = False
            data = json.dumps(self.server.telemetry.snapshot(), sort_keys=True, indent=1)
        d = self.telemetry_path.parent
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".telemetry-")
        with os.fdopen(fd, "w") as fh:
            fh.write(data)
        os.replace(tmp, self.telemetry_path)


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "the-source-mcp"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # no request logging
            pass

        def _send(self, code, body=b"", ctype="application/json", extra=None):
            self.send_response(code)
            if body:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(self, code, obj, extra=None):
            self._send(code, json.dumps(obj, sort_keys=True, ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8"), extra=extra)

        def _client(self):
            # Behind the proxy the peer is 127.0.0.1; the proxy names the real client in X-Real-IP.
            peer = self.client_address[0]
            return (self.headers.get("X-Real-IP") or peer) if peer in ("127.0.0.1", "::1") else peer

        def _path(self):
            return self.path.split("?", 1)[0]

        def do_GET(self):
            if self._path() == "/healthz":
                return self._json(200, {"status": "ok", "server": M.SERVER_NAME, "version": M.SERVER_VERSION,
                                        "serving_dataset_version": app.server.layer.dataset_version})
            if self._path() == "/mcp":
                return self._send(405, extra={"Allow": "POST"})
            return self._send(404)

        def do_DELETE(self):
            return self._send(405, extra={"Allow": "POST"}) if self._path() == "/mcp" else self._send(404)

        def do_POST(self):
            if self._path() != "/mcp":
                return self._send(404)
            origin = self.headers.get("Origin")
            if origin and origin not in app.allow_origins:
                return self._json(403, M._err(None, -32600, "Origin not allowed"))
            if not app.rate.allow(self._client()):
                return self._json(429, M._err(None, -32000, "Rate limit exceeded; retry in a minute"),
                                  extra={"Retry-After": "60"})
            try:
                length = int(self.headers.get("Content-Length") or "-1")
            except ValueError:
                length = -1
            if length < 0:
                return self._json(411, M._err(None, -32600, "Content-Length required"))
            if length > MAX_BODY:
                return self._json(413, M._err(None, -32600, f"Request body over {MAX_BODY} bytes"))
            if not app.slots.acquire(blocking=False):
                return self._json(503, M._err(None, -32000, "Server busy; retry shortly"),
                                  extra={"Retry-After": "1"})
            try:
                raw = self.rfile.read(length)
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    return self._json(400, M._err(None, -32700, "Parse error"))
                if isinstance(msg, list):
                    return self._json(400, M._err(None, -32600, "Batches are not supported"))
                out = app.answer(msg, self.headers.get("MCP-Protocol-Version"))
                if out is None:
                    return self._send(202)
                return self._json(200, out)
            finally:
                app.slots.release()

    return Handler


def serve(app: App, host: str, port: int):
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    return httpd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="the-source-mcp-http", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8851)
    ap.add_argument("--telemetry", help="write aggregate usage counters to this JSON file")
    ap.add_argument("--allow-origin", action="append", default=[], help="browser Origin allowed to call /mcp")
    ap.add_argument("--rate-per-min", type=int, default=RATE_PER_MIN)
    a = ap.parse_args(argv)
    try:
        server = M.Server(Path(a.root))
    except Exception as exc:  # a layer that cannot be loaded and verified is never served
        print(f"the-source-mcp: refusing to serve: {exc}", file=sys.stderr)
        return 2
    app = App(server, a.allow_origin, a.telemetry, a.rate_per_min)
    httpd = serve(app, a.host, a.port)
    app.flusher()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    print(f"the-source-mcp: serving dataset {server.layer.dataset_version} on http://{a.host}:{a.port}/mcp",
          file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.maybe_flush(force=True)
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
