"""HTTP surface of the waiting room (KEI-932, docs/WAITING-ROOM.md).

    python -m source_pipeline waiting-room serve --state DIR --corpus-root DIR [--port 8852]

* POST /intake/v1/submissions          bearer credential, scope `submit`; one source-submission/1
* GET  /intake/v1/submissions/{id}     bearer credential; a producer reads only its own
* GET  /intake/v1/batches/latest       public: the signed daily batch (canonical URLs, producer
                                       names and SaaS hints only; never notes or evidence)
* GET  /intake/v1/healthz              public
* GET  /intake/v1/openapi.json         public: the contract, usable as a ChatGPT Action schema

Bounded like the MCP transport: bodies over MAX_BODY are refused (413), at most MAX_INFLIGHT
requests run at once (503), each client address gets AUTH_FAILURES_PER_MIN failed
authentications a minute before 429, and every producer has its own hourly and daily quota.
No request logging; the store's audit log records events, never tokens or payloads. Intended to
run on 127.0.0.1 behind the TLS proxy (deploy/the-source-intake/).
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import waiting_room as W

MAX_BODY = 8 * 1024
MAX_INFLIGHT = 8
AUTH_FAILURES_PER_MIN = 10
OPENAPI = Path(__file__).resolve().parent.parent / "api" / "intake" / "openapi.json"
SUBMISSION_PATH = re.compile(r"^/intake/v1/submissions/(sub-[0-9a-f]{16})$")


class AuthFailures:
    def __init__(self, per_min=AUTH_FAILURES_PER_MIN):
        self.per_min, self.hits, self.lock = per_min, defaultdict(deque), threading.Lock()

    MAX_CLIENTS = 10000

    def blocked(self, client: str) -> bool:
        with self.lock:
            q = self.hits.get(client)
            if q is None:
                return False
            while q and q[0] < time.monotonic() - 60:
                q.popleft()
            if not q:
                del self.hits[client]
                return False
            return len(q) >= self.per_min

    def fail(self, client: str):
        with self.lock:
            if client not in self.hits and len(self.hits) >= self.MAX_CLIENTS:
                cutoff = time.monotonic() - 60
                for k in [k for k, q in self.hits.items() if not q or q[-1] < cutoff]:
                    del self.hits[k]
                if len(self.hits) >= self.MAX_CLIENTS:
                    self.hits.pop(next(iter(self.hits)))
            self.hits[client].append(time.monotonic())


def make_handler(store: W.Store, corpus: W.Corpus):
    gate = threading.BoundedSemaphore(MAX_INFLIGHT)
    failures = AuthFailures()
    openapi = OPENAPI.read_bytes() if OPENAPI.exists() else b"{}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "the-source-waiting-room"
        sys_version = ""
        timeout = 15  # a slow or short body cannot hold a slot

        def log_message(self, fmt, *args):  # no request logging
            pass

        def _send(self, code, obj):
            body = obj if isinstance(obj, bytes) else (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _client(self):
            # The proxy sets X-Real-IP; it is trusted only from loopback.
            peer = self.client_address[0]
            return (self.headers.get("X-Real-IP") or peer) if peer in ("127.0.0.1", "::1") else peer

        def _producer(self, scope):
            # A valid credential is never throttled by other clients' failures behind a shared address
            # (ChatGPT Actions egress); only failures are counted and limited.
            client = self._client()
            auth = self.headers.get("Authorization") or ""
            token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
            producer = store.authenticate(token)
            if producer is not None and scope not in producer["scopes"].split(","):
                self._send(403, {"error": "scope_missing"})
                return None
            if producer is None and failures.blocked(client):
                self._send(429, {"error": "too_many_failed_authentications"})
                return None
            if producer is None:
                failures.fail(client)
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Bearer realm="the-source-waiting-room"')
                body = b'{"error": "unauthenticated"}\n'
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return producer

        def _guarded(self, fn):
            if not gate.acquire(blocking=False):
                return self._send(503, {"error": "busy"})
            try:
                return fn()
            except Exception as exc:  # never leak internals; the journal gets the type and path only
                print(f"waiting room: {type(exc).__name__} on {self.command} {self.path.split('?', 1)[0][:80]}",
                      file=sys.stderr, flush=True)
                return self._send(500, {"error": "internal_error"})
            finally:
                gate.release()

        def do_GET(self):
            path = self.path.split("?", 1)[0]

            def run():
                if path == "/intake/v1/healthz":
                    return self._send(200, {"status": "ok", "contract": W.CONTRACT})
                if path == "/intake/v1/openapi.json":
                    return self._send(200, openapi)
                if path == "/intake/v1/batches/latest":
                    b = store.latest_batch()
                    return self._send(200, b) if b else self._send(404, {"error": "no_batch_sealed_yet"})
                m = SUBMISSION_PATH.match(path)
                if m:
                    producer = self._producer("read_own")
                    if producer is None:
                        return None
                    body = store.readback(m.group(1), producer["name"])
                    return self._send(200, body) if body else self._send(404, {"error": "not_found"})
                return self._send(404, {"error": "not_found"})
            return self._guarded(run)

        def do_POST(self):
            path = self.path.split("?", 1)[0]

            def run():
                if path != "/intake/v1/submissions":
                    return self._send(404, {"error": "not_found"})
                producer = self._producer("submit")
                if producer is None:
                    return None
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    return self._send(415, {"error": "content_type_must_be_application_json"})
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                except ValueError:
                    return self._send(400, {"error": "bad_content_length"})
                if length <= 0 or length > MAX_BODY:
                    return self._send(413, {"error": f"body must be 1-{MAX_BODY} bytes"})
                try:
                    payload = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError):
                    return self._send(400, {"error": "body_not_json"})
                code, body = store.submit(producer, payload, corpus)
                return self._send(code, body)
            return self._guarded(run)

        def do_PUT(self):
            self._send(405, {"error": "method_not_allowed"})

        do_DELETE = do_PATCH = do_PUT

    return Handler


def serve(store: W.Store, corpus: W.Corpus, host: str, port: int) -> int:
    httpd = ThreadingHTTPServer((host, port), make_handler(store, corpus))
    httpd.daemon_threads = True
    print(f"waiting room listening on {host}:{port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
