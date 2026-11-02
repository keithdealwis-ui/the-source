"""GitWatch MCP adapter: ChatGPT's connector onto the waiting room (KEI-932, docs/WAITING-ROOM.md).

    python -m source_pipeline.gitwatch_mcp serve --state DIR [--port 8853]

One remote MCP endpoint, /gitwatch/mcp, with two tools, submitRepository and getSubmission, that
forward to the waiting room on 127.0.0.1:8852 as the `gitwatch` producer (contract
source-submission/1). The producer credential is read once from a systemd credential and never
appears in a tool argument, a response or a log line.

Access is OAuth 2.1 for one person, Keith, in the shape ChatGPT already uses for OpenSEO:
authorization code with PKCE (S256), refresh-token rotation, RFC 8414 server metadata, RFC 9728
protected-resource metadata and RFC 7591 registration limited to ChatGPT's redirect origins.
Keith authorises a connection by entering a passphrase whose scrypt hash is the only copy held
here. Tokens are opaque, stored as SHA-256 hashes and bound to this resource: a token from any
other issuer (OpenSEO's included) is unknown here, and one whose audience is not
RESOURCE is refused. This is not an identity platform: there are no accounts, only the one
subject `keith`.

No request logging. The audit log records events (client registered, authorised, token refused),
never tokens, codes, passphrases, notes or tool arguments.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SUBJECT = "keith"
SCOPE = "gitwatch.submit"
SCOPES = (SCOPE, "offline_access")
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
PENDING_TTL = 900
LOGIN_FAILURES = 5                 # wrong passphrases per address per LOGIN_WINDOW before it is locked out
LOGIN_WINDOW = 900
LOGIN_GLOBAL = 100                 # wrong passphrases from all addresses per hour that raise an audit alert
FAMILY_MAX_AGE = 90 * 86400        # a connection re-authorises at least this often
MAX_CLIENTS = 50                   # registered (RFC 7591) clients; idle ones are pruned first
CLIENT_IDLE = 3600
MAX_PENDING = 1000                 # consent pages open at once; the oldest is dropped beyond this
PENDING_PER_ADDRESS = 10           # per browser address; its own oldest is dropped beyond this
MAX_STATE = 512
MAX_BODY = 64 * 1024
MAX_INFLIGHT = 16
INTAKE_TIMEOUT = 20
CHATGPT_REDIRECT_HOSTS = ("chatgpt.com", "chat.openai.com", "platform.openai.com")
# ChatGPT's OAuth callbacks: connectors, and GPT Actions (/aip/<gpt id>/oauth/callback). Nothing else on those hosts.
CHATGPT_REDIRECT_PATH = re.compile(r"^/(connector_platform_oauth_redirect|aip/[A-Za-z0-9_-]{1,128}/oauth/callback)$")
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUBMISSION_ID = re.compile(r"^sub-[0-9a-f]{16}$")
SAAS_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
PROVIDER = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
KEY = re.compile(r"^[A-Za-z0-9._-]{6,80}$")
MCP_PATH = "/gitwatch/mcp"
OAUTH = "/gitwatch/oauth"


def _now() -> int:
    return int(time.time())


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def hash_passphrase(passphrase: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(passphrase.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${_b64url(salt)}${_b64url(dk)}"


def check_passphrase(passphrase: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt, want = stored.split("$")
    salt_b = base64.urlsafe_b64decode(salt + "=" * (-len(salt) % 4))
    return hmac.compare_digest(hash_passphrase(passphrase, salt_b).split("$")[2], want)


class Store:
    """Clients, pending authorisations, codes and tokens. Secrets are kept only as hashes."""

    def __init__(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        self.audit_path = path / "audit.jsonl"
        self.db = sqlite3.connect(path / "gitwatch-mcp.sqlite", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, secret_hash TEXT, auth_method TEXT NOT NULL,
                redirect_uris TEXT NOT NULL, host_pattern INTEGER NOT NULL, name TEXT, created_at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS authz_pending (id TEXT PRIMARY KEY, client_id TEXT, redirect_uri TEXT,
                state TEXT, code_challenge TEXT, scope TEXT, created_at INTEGER, ip TEXT);
            CREATE TABLE IF NOT EXISTS codes (code_hash TEXT PRIMARY KEY, client_id TEXT, redirect_uri TEXT,
                code_challenge TEXT, scope TEXT, family TEXT, expires_at INTEGER, used INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS tokens (token_hash TEXT PRIMARY KEY, kind TEXT, client_id TEXT, scope TEXT,
                resource TEXT, subject TEXT, family TEXT, expires_at INTEGER, revoked INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS login_attempts (at INTEGER, ip TEXT);
            CREATE TABLE IF NOT EXISTS families (family TEXT PRIMARY KEY, created_at INTEGER);
        """)

    def audit(self, event: str, **fields) -> None:
        line = json.dumps({"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": event, **fields},
                          sort_keys=True)
        with self.lock, open(self.audit_path, "a") as f:
            f.write(line + "\n")

    def setting(self, key: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    # clients
    def add_client(self, name: str, redirect_uris: list[str], auth_method: str, host_pattern: bool = False):
        # Registration comes from ChatGPT's servers, not Keith's browser, so it is not limited per address
        # (that would let other ChatGPT users exhaust a shared allowance). The cap is kept by evicting the
        # oldest registration that never connected and has no consent page open.
        with self.lock:
            if not host_pattern:
                now = _now()
                self.db.execute(
                    "DELETE FROM clients WHERE host_pattern=0 AND client_id NOT IN (SELECT client_id FROM codes"
                    " UNION SELECT client_id FROM tokens UNION SELECT client_id FROM authz_pending WHERE created_at >= ?)"
                    " AND created_at < ?", (now - PENDING_TTL, now - CLIENT_IDLE))
                if self.db.execute("SELECT COUNT(*) FROM clients WHERE host_pattern=0").fetchone()[0] >= MAX_CLIENTS:
                    # make room by evicting the oldest registration that never connected
                    old = self.db.execute(
                        "SELECT client_id FROM clients WHERE host_pattern=0 AND client_id NOT IN (SELECT client_id"
                        " FROM codes UNION SELECT client_id FROM tokens UNION SELECT client_id FROM authz_pending"
                        " WHERE created_at >= ?) ORDER BY created_at LIMIT 1", (now - PENDING_TTL,)).fetchone()
                    if old is None:
                        raise ValueError("too_many_clients")
                    self.db.execute("DELETE FROM clients WHERE client_id=?", (old["client_id"],))
            cid = "gw_" + secrets.token_urlsafe(16)
            secret = None if auth_method == "none" else "gws_" + secrets.token_urlsafe(32)
            self.db.execute("INSERT INTO clients VALUES (?,?,?,?,?,?,?)",
                            (cid, _sha(secret) if secret else None, auth_method, json.dumps(redirect_uris),
                             int(host_pattern), name, _now()))

        self.audit("client_registered", client_id=cid, auth_method=auth_method, host_pattern=host_pattern)
        return cid, secret

    def client(self, cid: str):
        with self.lock:
            return self.db.execute("SELECT * FROM clients WHERE client_id=?", (cid or "",)).fetchone()

    # tokens
    def issue(self, client_id: str, scope: str, resource: str, family: str) -> dict:
        access, refresh = "gwa_" + secrets.token_urlsafe(32), "gwr_" + secrets.token_urlsafe(32)
        now = _now()
        with self.lock:
            self.db.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?,0)",
                            (_sha(access), "access", client_id, scope, resource, SUBJECT, family, now + ACCESS_TTL))
            self.db.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?,0)",
                            (_sha(refresh), "refresh", client_id, scope, resource, SUBJECT, family, now + REFRESH_TTL))
            self.db.execute("DELETE FROM tokens WHERE expires_at < ?", (now - 86400,))
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                "refresh_token": refresh, "scope": scope}

    def revoke_family(self, family: str) -> None:
        with self.lock:
            self.db.execute("UPDATE tokens SET revoked=1 WHERE family=?", (family,))
        self.audit("token_family_revoked", family=family[:8])

    def access(self, token: str, resource: str):
        """The access token's row if it is live, issued here, for this resource and for Keith."""
        with self.lock:
            row = self.db.execute("SELECT * FROM tokens WHERE token_hash=? AND kind='access'", (_sha(token),)).fetchone()
        if (row is None or row["revoked"] or row["expires_at"] < _now() or row["resource"] != resource
                or row["subject"] != SUBJECT):
            return None
        return row

    def begin_attempt(self, ip: str):
        """Record a passphrase attempt before checking it (so parallel guesses all count); None when this
        address is locked out. There is deliberately no global lock: it would let anyone with many addresses
        lock Keith out, and the passphrase (about 144 bits) makes distributed guessing pointless. A high
        global failure rate is audited instead."""
        now = _now()
        with self.lock:
            self.db.execute("DELETE FROM login_attempts WHERE at < ?", (now - 3600,))
            mine = self.db.execute("SELECT COUNT(*) FROM login_attempts WHERE ip=? AND at >= ?",
                                   (ip, now - LOGIN_WINDOW)).fetchone()[0]
            if mine >= LOGIN_FAILURES:
                return None
            if self.db.execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0] == LOGIN_GLOBAL:
                self.audit("passphrase_failures_high", per_hour=LOGIN_GLOBAL)
            return self.db.execute("INSERT INTO login_attempts VALUES (?,?)", (now, ip)).lastrowid

    def attempt_succeeded(self, rowid: int) -> None:
        with self.lock:
            self.db.execute("DELETE FROM login_attempts WHERE rowid=?", (rowid,))


def redirect_allowed(uri: str, hosts) -> bool:
    # urlsplit silently drops CR, LF and tab, so anything at or below space is refused before parsing:
    # the stored string is later written into a Location header.
    if not isinstance(uri, str) or not uri.isascii() or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in uri):
        return False
    try:
        u = urllib.parse.urlsplit(uri)
        port = u.port
    except ValueError:
        return False
    if u.fragment or u.username or u.password or "\\" in uri:
        return False
    if u.scheme == "https" and u.hostname in hosts and port is None:
        return bool(CHATGPT_REDIRECT_PATH.match(u.path))
    return u.scheme == "http" and u.hostname == "127.0.0.1" and "127.0.0.1" in hosts  # tests only


TOOLS = [
    {"name": "submitRepository",
     "title": "Submit a repository to The Source",
     "description": ("Send one open-source repository (github.com, gitlab.com or codeberg.org) to The Source's "
                     "waiting room as Keith's GitWatch. This is a request for assessment, never a recommendation: "
                     "The Source's own intake decides, normally overnight. The answer shows the recorded "
                     "submission, its canonical repository, how many producers have seen it, and the corpus "
                     "record if The Source already holds it. Resubmitting the same repository with the same "
                     "details on the same day is safe (it returns the recorded submission)."),
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["repo_url"], "properties": {
         "repo_url": {"type": "string", "maxLength": 2048, "description": "The repository URL."},
         "saas_hint": {"type": "string", "maxLength": 80,
                       "description": "Lowercase id of the SaaS product it may replace, e.g. jira, notion, calendly."},
         "name": {"type": "string", "maxLength": 300, "description": "Project name."},
         "description": {"type": "string", "maxLength": 300, "description": "One-line description."},
         "evidence_url": {"type": "string", "maxLength": 2048,
                          "description": "The post, video or page where it was seen. Kept private."},
         "source_provider": {"type": "string", "maxLength": 40,
                             "description": "Where it was seen: chatgpt (default), youtube, x, bluesky, reddit, ..."},
         "note": {"type": "string", "maxLength": 500, "description": "Private note for Keith. Never published."},
         "idempotency_key": {"type": "string", "maxLength": 80,
                             "description": "Optional. 6-80 of A-Z a-z 0-9 . _ -; derived from the details if omitted."}}},
     "annotations": {"title": "Submit a repository to The Source", "readOnlyHint": False,
                     "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}},
    {"name": "getSubmission",
     "title": "Check a GitWatch submission",
     "description": ("Read the current standing of one of GitWatch's submissions: received or queued (not "
                     "processed yet), accepted, canonical (now in The Source), already_known, held (needs "
                     "evidence that it replaces a SaaS product), rejected (with reasons) or dead_letter."),
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["submission_id"], "properties": {
         "submission_id": {"type": "string", "pattern": "^sub-[0-9a-f]{16}$"}}},
     "annotations": {"title": "Check a GitWatch submission", "readOnlyHint": True, "destructiveHint": False,
                     "idempotentHint": True, "openWorldHint": False}},
]


class ToolError(Exception):
    pass


def build_submission(args: dict, today: datetime | None = None) -> dict:
    """source-submission/1 from the tool arguments. Defaults make a same-day retry an exact replay."""
    if not isinstance(args, dict) or set(args) - set(TOOLS[0]["inputSchema"]["properties"]):
        raise ToolError("unknown argument")
    url = args.get("repo_url")
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise ToolError("repo_url is required")
    for k, limit in (("saas_hint", 80), ("name", 300), ("description", 300), ("evidence_url", 2048),
                     ("source_provider", 40), ("note", 500), ("idempotency_key", 80)):
        v = args.get(k)
        if v is not None and (not isinstance(v, str) or len(v) > limit):
            raise ToolError(f"{k} must be text of at most {limit} characters")
    if args.get("saas_hint") and not SAAS_ID.match(args["saas_hint"]):
        raise ToolError("saas_hint must be a lowercase id such as jira or notion")
    provider = args.get("source_provider") or "chatgpt"
    if not PROVIDER.match(provider):
        raise ToolError("source_provider must be lowercase letters, digits or '-'")
    day = (today or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    source = {"provider": provider, "observed_at": day + "T00:00:00Z"}
    if args.get("evidence_url"):
        source["evidence_url"] = args["evidence_url"]
    provenance = {"on_behalf_of": SUBJECT}
    if args.get("note"):
        provenance["note"] = args["note"]
    candidate = {k: args[k] for k in ("saas_hint", "name", "description") if args.get(k)}
    body = {"contract": "source-submission/1", "repo_url": url.strip(), "source": source, "provenance": provenance}
    if candidate:
        body["candidate"] = candidate
    key = args.get("idempotency_key")
    if key is None:
        key = f"gitwatch-{day}-" + _sha(json.dumps(body, sort_keys=True))[:12]
    elif not KEY.match(key):
        raise ToolError("idempotency_key must be 6-80 of A-Z a-z 0-9 . _ -")
    body["idempotency_key"] = key
    return body


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # never carry the credential to another address
        return None


class Intake:
    """The waiting room, reached on loopback with the gitwatch producer credential."""

    def __init__(self, base: str, credential: str):
        self.base, self._credential = base.rstrip("/"), credential
        self._open = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + self._credential,
                                              "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=INTAKE_TIMEOUT) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {"error": "intake_error"}
        except (urllib.error.URLError, TimeoutError, OSError):
            return 503, {"error": "intake_unavailable"}


def call_tool(intake: Intake, name: str, args: dict) -> dict:
    try:
        if name == "submitRepository":
            status, body = intake.call("POST", "/intake/v1/submissions", build_submission(args or {}))
        elif name == "getSubmission":
            sid = (args or {}).get("submission_id")
            if not isinstance(sid, str) or not SUBMISSION_ID.match(sid) or set(args) - {"submission_id"}:
                raise ToolError("submission_id must look like sub-0123456789abcdef")
            status, body = intake.call("GET", f"/intake/v1/submissions/{sid}")
        else:
            raise ToolError(f"unknown tool {name}")
    except ToolError as e:
        return {"content": [{"type": "text", "text": str(e)}], "isError": True}
    result = {"http_status": status, **(body if isinstance(body, dict) else {"body": body})}
    # 422 is a recorded, reason-coded answer, not a failure of the call.
    return {"content": [{"type": "text", "text": json.dumps(result, indent=1, sort_keys=True)}],
            "structuredContent": result, "isError": status >= 400 and status != 422}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, store: Store, intake: Intake, public_base: str, redirect_hosts):
        super().__init__(addr, Handler)
        self.store, self.intake = store, intake
        self.public = public_base.rstrip("/")
        self.issuer = self.public + "/gitwatch"  # path-scoped: nothing is advertised for the rest of the host
        self.resource = self.public + MCP_PATH
        self.redirect_hosts = tuple(redirect_hosts)
        self.slots = threading.BoundedSemaphore(MAX_INFLIGHT)

    def as_metadata(self) -> dict:
        p = self.public
        return {"issuer": self.issuer, "authorization_endpoint": p + OAUTH + "/authorize",
                "token_endpoint": p + OAUTH + "/token", "registration_endpoint": p + OAUTH + "/register",
                "scopes_supported": list(SCOPES), "response_types_supported": ["code"],
                "response_modes_supported": ["query"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
                "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True,
                "client_id_metadata_document_supported": False}

    def pr_metadata(self) -> dict:
        return {"resource": self.resource, "authorization_servers": [self.issuer], "scopes_supported": [SCOPE],
                "bearer_methods_supported": ["header"], "resource_name": "The Source — GitWatch"}


CONSENT = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Connect GitWatch</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:28rem;margin:3rem auto;padding:0 1rem;color:#111;background:#fff}}
input{{font:inherit;width:100%;padding:.5rem;margin:.25rem 0 1rem;box-sizing:border-box}}
button{{font:inherit;padding:.5rem 1rem;margin-right:.5rem}}.err{{color:#b00020}}.muted{{color:#555;font-size:.9rem}}</style>
</head><body><h1>Connect GitWatch to The Source</h1>
<p><strong>{client}</strong> is asking to submit repositories to The Source's waiting room and read their
results, as Keith. It will be sent back to <strong>{host}</strong>.</p>{error}
<form method="post" action="{action}"><input type="hidden" name="pending" value="{pending}">
<label for="p">Passphrase</label><input id="p" name="passphrase" type="password" autocomplete="current-password" autofocus>
<button name="decision" value="approve" type="submit">Allow</button>
<button name="decision" value="deny" type="submit">Deny</button></form>
<p class="muted">Only Keith can approve this. Submissions are requests for assessment, never recommendations.</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "the-source-gitwatch"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 15

    def log_message(self, *a):  # no request logging
        pass

    # plumbing
    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json", headers: dict | None = None):
        if status >= 400:
            self.close_connection = True  # an unread or refused body is never parsed as a next request
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            self.send_header("Connection", "close")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj, headers: dict | None = None):
        self._send(status, json.dumps(obj).encode(), headers=headers)

    def _redirect(self, url: str):
        if "\r" in url or "\n" in url:
            return self._send(400, b"Bad redirect address.", "text/plain; charset=utf-8")
        self._send(302, headers={"Location": url})

    def _client_ip(self) -> str:
        """The caller's address (from nginx), with IPv6 grouped by /64 so one host cannot rotate through it."""
        peer = self.client_address[0]
        raw = (self.headers.get("X-Real-IP") or peer)[:64] if peer == "127.0.0.1" else peer
        try:
            a = ipaddress.ip_address(raw)
        except ValueError:
            return raw
        return str(ipaddress.ip_network(f"{a}/64", strict=False)) if a.version == 6 else str(a)

    def _body(self) -> bytes | None:
        length = self.headers.get("Content-Length") or "0"
        if self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9]{1,7}", length):
            self._json(400, {"error": "bad_request"})
            return None
        n = int(length)
        if n > MAX_BODY:
            self._json(413, {"error": "too_large"})
            return None
        return self.rfile.read(n) if n else b""

    def _form(self, raw: bytes) -> dict:
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()
                if len(v) == 1}

    def _dispatch(self, fn):
        if not self.server.slots.acquire(blocking=False):
            return self._json(503, {"error": "busy"})
        try:
            fn()
        except Exception as e:  # never leak details; note type and path only
            sys.stderr.write(f"gitwatch-mcp error {type(e).__name__} on {self.path.split('?')[0]}\n")
            try:
                self._json(500, {"error": "server_error"})
            except Exception:
                pass
        finally:
            self.server.slots.release()

    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

    def do_DELETE(self):
        self._dispatch(lambda: self._json(405, {"error": "method_not_allowed"}, {"Allow": "POST"}))

    # routing
    def _get(self):
        path = urllib.parse.urlsplit(self.path).path
        s = self.server
        # Metadata only at the path-specific locations (RFC 8414 section 3.1, RFC 9728 section 3.1), so the
        # anonymous /mcp on the same host is never presented as needing authorisation.
        if path in ("/.well-known/oauth-authorization-server/gitwatch", "/gitwatch/.well-known/oauth-authorization-server"):
            return self._json(200, s.as_metadata())
        if path == "/.well-known/oauth-protected-resource" + MCP_PATH:
            return self._json(200, s.pr_metadata())
        if path == "/gitwatch/healthz":
            return self._json(200, {"status": "ok", "ready": bool(s.store.setting("passphrase"))})
        if path == OAUTH + "/authorize":
            return self._authorize_get()
        if path == MCP_PATH:
            return self._json(405, {"error": "method_not_allowed"}, {"Allow": "POST"})
        self._json(404, {"error": "not_found"})

    def _post(self):
        path = urllib.parse.urlsplit(self.path).path
        raw = self._body()
        if raw is None:
            return
        if path == OAUTH + "/authorize":
            return self._authorize_post(raw)
        if path == OAUTH + "/token":
            return self._token(raw)
        if path == OAUTH + "/register":
            return self._register(raw)
        if path == MCP_PATH:
            return self._mcp(raw)
        self._json(404, {"error": "not_found"})

    # OAuth
    def _redirect_error(self, uri: str, state: str | None, error: str):
        q = {"error": error, "iss": self.server.issuer}
        if state is not None:
            q["state"] = state
        self._redirect(uri + ("&" if "?" in uri else "?") + urllib.parse.urlencode(q))

    def _client_redirect_ok(self, client, uri: str) -> bool:
        if not uri or not redirect_allowed(uri, self.server.redirect_hosts):
            return False
        return bool(client["host_pattern"]) or uri in json.loads(client["redirect_uris"])

    def _consent(self, pending: str, client, uri: str, error: str = "", status: int = 200):
        hosts = " ".join("http://127.0.0.1:*" if h == "127.0.0.1" else "https://" + h for h in self.server.redirect_hosts)
        page = CONSENT.format(client=html.escape(f'"{client["name"] or "ChatGPT"}" (self-declared name; client '
                                                 f'{client["client_id"]}, registered '
                                                 f'{datetime.fromtimestamp(client["created_at"], timezone.utc):%Y-%m-%d %H:%M} UTC)'),
                              host=html.escape(urllib.parse.urlsplit(uri).hostname or ""),
                              error=f'<p class="err">{html.escape(error)}</p>' if error else "",
                              action=OAUTH + "/authorize", pending=html.escape(pending))
        self._send(status, page.encode(), "text/html; charset=utf-8", {
            "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; form-action 'self' {hosts}; "
                                       "frame-ancestors 'none'; base-uri 'none'",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})

    def _authorize_get(self):
        s, q = self.server, {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
        client = s.store.client(q.get("client_id"))
        uri = q.get("redirect_uri", "")
        if client is None or not self._client_redirect_ok(client, uri):
            return self._send(400, b"Unknown client or redirect address.", "text/plain; charset=utf-8")
        state = q.get("state")
        if state is not None and len(state) > MAX_STATE:
            return self._redirect_error(uri, None, "invalid_request")
        if q.get("response_type") != "code":
            return self._redirect_error(uri, state, "unsupported_response_type")
        if q.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", q.get("code_challenge", "")):
            return self._redirect_error(uri, state, "invalid_request")
        if q.get("resource") not in (None, s.resource):
            return self._redirect_error(uri, state, "invalid_target")
        scopes = (q.get("scope") or SCOPE).split()
        if set(scopes) - set(SCOPES):
            return self._redirect_error(uri, state, "invalid_scope")
        scope = " ".join(sorted(set(scopes) | {SCOPE}))
        if not s.store.setting("passphrase"):
            return self._send(503, b"GitWatch is not ready: no passphrase is set.", "text/plain; charset=utf-8")
        pid = secrets.token_urlsafe(24)
        ip = self._client_ip()
        with s.store.lock:  # bounded by eviction, never by refusal, so open consents cannot lock Keith out
            db = s.store.db
            db.execute("DELETE FROM authz_pending WHERE created_at < ?", (_now() - PENDING_TTL,))
            db.execute("DELETE FROM authz_pending WHERE id IN (SELECT id FROM authz_pending WHERE ip=? "
                       "ORDER BY created_at DESC LIMIT -1 OFFSET ?)", (ip, PENDING_PER_ADDRESS - 1))
            db.execute("DELETE FROM authz_pending WHERE id IN (SELECT id FROM authz_pending "
                       "ORDER BY created_at DESC LIMIT -1 OFFSET ?)", (MAX_PENDING - 1,))
            db.execute("INSERT INTO authz_pending VALUES (?,?,?,?,?,?,?,?)",
                       (pid, client["client_id"], uri, state, q["code_challenge"], scope, _now(), ip))
        self._consent(pid, client, uri)

    def _authorize_post(self, raw: bytes):
        s, f = self.server, self._form(raw)
        with s.store.lock:
            p = s.store.db.execute("SELECT * FROM authz_pending WHERE id=? AND created_at >= ?",
                                   (f.get("pending", ""), _now() - PENDING_TTL)).fetchone()
        if p is None:
            return self._send(400, b"This request has expired. Start the connection again from ChatGPT.",
                              "text/plain; charset=utf-8")
        client = s.store.client(p["client_id"])
        if client is None:
            return self._send(400, b"Unknown client.", "text/plain; charset=utf-8")
        if f.get("decision") != "approve":
            with s.store.lock:
                s.store.db.execute("DELETE FROM authz_pending WHERE id=?", (p["id"],))
            s.store.audit("authorization_denied", client_id=p["client_id"])
            return self._redirect_error(p["redirect_uri"], p["state"], "access_denied")
        attempt = s.store.begin_attempt(self._client_ip())
        if attempt is None:
            s.store.audit("passphrase_locked")
            return self._consent(p["id"], client, p["redirect_uri"], "Too many attempts. Try again in 15 minutes.", 429)
        if not check_passphrase(f.get("passphrase", ""), s.store.setting("passphrase")):
            s.store.audit("passphrase_refused", client_id=p["client_id"])
            return self._consent(p["id"], client, p["redirect_uri"], "That passphrase is not right.", 401)
        s.store.attempt_succeeded(attempt)
        code, family = secrets.token_urlsafe(32), secrets.token_hex(8)
        with s.store.lock:
            s.store.db.execute("DELETE FROM authz_pending WHERE id=?", (p["id"],))
            s.store.db.execute("INSERT INTO families VALUES (?,?)", (family, _now()))
            s.store.db.execute("INSERT INTO codes VALUES (?,?,?,?,?,?,?,0)",
                               (_sha(code), p["client_id"], p["redirect_uri"], p["code_challenge"], p["scope"],
                                family, _now() + CODE_TTL))
        s.store.audit("authorized", client_id=p["client_id"])
        q = {"code": code, "iss": s.issuer}
        if p["state"] is not None:
            q["state"] = p["state"]
        self._redirect(p["redirect_uri"] + ("&" if "?" in p["redirect_uri"] else "?") + urllib.parse.urlencode(q))

    def _authenticate_client(self, f: dict):
        cid, secret = f.get("client_id"), f.get("client_secret")
        basic = self.headers.get("Authorization", "")
        if basic.startswith("Basic "):
            try:
                cid, secret = (urllib.parse.unquote(x) for x in
                               base64.b64decode(basic[6:]).decode().split(":", 1))
            except Exception:
                return None
        client = self.server.store.client(cid)
        if client is None:
            return None
        if client["auth_method"] == "none":
            return client if not secret else None
        if not secret or not hmac.compare_digest(_sha(secret), client["secret_hash"] or ""):
            return None
        return client

    def _token(self, raw: bytes):
        s, f = self.server, self._form(raw)
        oauth_error = lambda code, status=400: self._json(status, {"error": code})  # noqa: E731
        client = self._authenticate_client(f)
        if client is None:
            return oauth_error("invalid_client", 401)
        if f.get("resource") not in (None, s.resource):
            return oauth_error("invalid_target")
        grant = f.get("grant_type")
        if grant == "authorization_code":
            with s.store.lock:
                row = s.store.db.execute("SELECT * FROM codes WHERE code_hash=?", (_sha(f.get("code", "")),)).fetchone()
                if row is None or row["client_id"] != client["client_id"]:
                    return oauth_error("invalid_grant")
                if row["used"]:  # a replayed code revokes everything it produced
                    s.store.revoke_family(row["family"])
                    return oauth_error("invalid_grant")
                s.store.db.execute("UPDATE codes SET used=1 WHERE code_hash=?", (row["code_hash"],))
            verifier = f.get("code_verifier", "")
            if (row["expires_at"] < _now() or row["redirect_uri"] != f.get("redirect_uri")
                    or not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
                    or not hmac.compare_digest(_b64url(hashlib.sha256(verifier.encode()).digest()), row["code_challenge"])):
                return oauth_error("invalid_grant")
            s.store.audit("token_issued", client_id=client["client_id"], grant="authorization_code")
            return self._json(200, s.store.issue(client["client_id"], row["scope"], s.resource, row["family"]),
                              {"Pragma": "no-cache"})
        if grant == "refresh_token":
            with s.store.lock:
                row = s.store.db.execute("SELECT * FROM tokens WHERE token_hash=? AND kind='refresh'",
                                         (_sha(f.get("refresh_token", "")),)).fetchone()
                if row is None or row["client_id"] != client["client_id"]:
                    return oauth_error("invalid_grant")
                if row["revoked"]:  # reuse of a rotated refresh token: the family is compromised
                    s.store.revoke_family(row["family"])
                    return oauth_error("invalid_grant")
                born = s.store.db.execute("SELECT created_at FROM families WHERE family=?", (row["family"],)).fetchone()
                if (row["expires_at"] < _now() or row["resource"] != s.resource or born is None
                        or born["created_at"] + FAMILY_MAX_AGE < _now()):
                    return oauth_error("invalid_grant")
                s.store.db.execute("UPDATE tokens SET revoked=1 WHERE family=? AND kind IN ('access','refresh')",
                                   (row["family"],))
                out = s.store.issue(client["client_id"], row["scope"], s.resource, row["family"])
            s.store.audit("token_issued", client_id=client["client_id"], grant="refresh_token")
            return self._json(200, out, {"Pragma": "no-cache"})
        return oauth_error("unsupported_grant_type")

    def _register(self, raw: bytes):
        s = self.server
        try:
            meta = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"error": "invalid_client_metadata"})
        uris = meta.get("redirect_uris") if isinstance(meta, dict) else None
        if (not isinstance(uris, list) or not uris or len(uris) > 5
                or not all(isinstance(u, str) and len(u) <= 512 and redirect_allowed(u, s.redirect_hosts) for u in uris)):
            return self._json(400, {"error": "invalid_redirect_uri"})
        method = meta.get("token_endpoint_auth_method", "client_secret_basic")
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            return self._json(400, {"error": "invalid_client_metadata"})
        if set(meta.get("grant_types") or ["authorization_code"]) - {"authorization_code", "refresh_token"}:
            return self._json(400, {"error": "invalid_client_metadata"})
        name = str(meta.get("client_name") or "ChatGPT")[:100]
        try:
            cid, secret = s.store.add_client(name, uris, method)
        except ValueError:
            return self._json(429, {"error": "invalid_client_metadata", "error_description": "too many registrations"})
        out = {"client_id": cid, "client_id_issued_at": _now(), "client_name": name, "redirect_uris": uris,
               "token_endpoint_auth_method": method, "grant_types": ["authorization_code", "refresh_token"],
               "response_types": ["code"], "scope": " ".join(SCOPES)}
        if secret:
            out.update(client_secret=secret, client_secret_expires_at=0)
        self._json(201, out)

    # MCP (streamable HTTP, JSON responses, stateless)
    def _unauthorized(self, error: str | None = None, status: int = 401):
        meta = self.server.public + "/.well-known/oauth-protected-resource" + MCP_PATH
        challenge = f'Bearer resource_metadata="{meta}", scope="{SCOPE}"'
        if error:
            challenge += f', error="{error}"'
        self._json(status, {"error": error or "unauthorized"}, {"WWW-Authenticate": challenge})

    def _mcp(self, raw: bytes):
        s = self.server
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).hostname not in s.redirect_hosts:
            return self._json(403, {"error": "origin_not_allowed"})
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return self._unauthorized()
        row = s.store.access(auth[7:].strip(), s.resource)
        if row is None:
            s.store.audit("token_refused")
            return self._unauthorized("invalid_token")
        if SCOPE not in row["scope"].split():
            return self._unauthorized("insufficient_scope", 403)
        try:
            msg = json.loads(raw)
        except ValueError:
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
        if "id" not in msg:  # a notification
            return self._send(202)
        rid, method, params = msg["id"], msg["method"], msg.get("params") or {}
        if not isinstance(params, dict) or not isinstance(params.get("arguments", {}), dict):
            return self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "invalid params"}})
        ok = lambda result: self._json(200, {"jsonrpc": "2.0", "id": rid, "result": result})  # noqa: E731
        if method == "initialize":
            asked = params.get("protocolVersion")
            return ok({"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "the-source-gitwatch", "title": "The Source — GitWatch",
                                      "version": "1.0.0"},
                       "instructions": ("Submit open-source repositories to The Source for assessment and check "
                                        "their results. A submission is never a recommendation; The Source's "
                                        "intake decides, normally overnight.")})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name")
            result = call_tool(s.intake, name, params.get("arguments") or {})
            s.store.audit("tool_called", tool=str(name)[:40], is_error=result["isError"],
                          http_status=(result.get("structuredContent") or {}).get("http_status"))
            return ok(result)
        self._json(200, {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})


def read_credential(path: str | None) -> str:
    p = Path(path) if path else Path(os.environ.get("CREDENTIALS_DIRECTORY", "/nonexistent")) / "gitwatch_token"
    token = p.read_text().strip()
    if not token.startswith("swr_"):
        raise SystemExit("the gitwatch producer credential is missing or malformed")
    return token


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="gitwatch-mcp", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("serve")
    p.add_argument("--state", required=True, type=Path)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8853)
    p.add_argument("--intake", default="http://127.0.0.1:8852")
    p.add_argument("--public-base", default="https://source-mcp.de-alwis.com")
    p.add_argument("--credential-file", help="default: $CREDENTIALS_DIRECTORY/gitwatch_token")
    p = sub.add_parser("set-passphrase", help="read Keith's passphrase from stdin and keep only its hash")
    p.add_argument("--state", required=True, type=Path)
    p = sub.add_parser("client-add", help="pre-register a client for ChatGPT's user-defined OAuth client")
    p.add_argument("--state", required=True, type=Path)
    p.add_argument("--name", default="ChatGPT")
    p = sub.add_parser("clients")
    p.add_argument("--state", required=True, type=Path)
    p = sub.add_parser("revoke-all", help="revoke every token (connections must re-authorise)")
    p.add_argument("--state", required=True, type=Path)
    a = ap.parse_args(argv)
    if os.geteuid() == 0:
        raise SystemExit("refusing to run as root: use the service user")
    store = Store(a.state)
    if a.cmd == "serve":
        srv = Server((a.host, a.port), store, Intake(a.intake, read_credential(a.credential_file)), a.public_base,
                     CHATGPT_REDIRECT_HOSTS)
        srv.serve_forever()
    elif a.cmd == "set-passphrase":
        phrase = sys.stdin.readline().rstrip("\n")
        if len(phrase) < 16:
            raise SystemExit("the passphrase must be at least 16 characters")
        store.set_setting("passphrase", hash_passphrase(phrase))
        store.audit("passphrase_set")
        print("passphrase set")
    elif a.cmd == "client-add":
        cid, _ = store.add_client(a.name, [], "none", host_pattern=True)
        print(json.dumps({"client_id": cid, "token_endpoint_auth_method": "none"}))
    elif a.cmd == "clients":
        rows = store.db.execute("SELECT client_id, auth_method, name, host_pattern, created_at FROM clients").fetchall()
        print(json.dumps([dict(r) for r in rows], indent=1))
    elif a.cmd == "revoke-all":
        with store.lock:
            n = store.db.execute("UPDATE tokens SET revoked=1 WHERE revoked=0").rowcount
        store.audit("tokens_revoked_all", count=n)
        print(json.dumps({"revoked": n}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
