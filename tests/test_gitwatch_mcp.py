"""GitWatch MCP adapter (KEI-932): OAuth for Keith only, audience-bound tokens, and forwarding to the
waiting room with the producer credential held server-side."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import secrets
import socket
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from source_pipeline import gitwatch_mcp as G

CRED = "swr_" + "c" * 43
PASS = "correct horse battery staple"
CB = "http://127.0.0.1:9/callback"


def _loopback_ok() -> bool:
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0)); s.listen(1)
            socket.create_connection(s.getsockname(), timeout=1).close()
        return True
    except OSError:
        return False


needs_loopback = pytest.mark.skipif(not _loopback_ok(), reason="loopback connect unavailable in this sandbox")


class FakeIntake(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def _reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeIntake.seen.append((self.headers.get("Authorization"), body))
        if self.headers.get("Authorization") != "Bearer " + CRED:
            return self._reply(401, {"error": "unauthenticated"})
        if "example.com" in body["repo_url"]:
            return self._reply(422, {"submission_state": "invalid", "reasons": ["host_not_supported"]})
        self._reply(202, {"submission_id": "sub-0123456789abcdef", "submission_state": "recorded", "producer": "gitwatch"})

    def do_GET(self):
        FakeIntake.seen.append((self.headers.get("Authorization"), self.path))
        self._reply(200, {"submission_id": self.path.rsplit("/", 1)[1], "repository": {"state": "queued"}})


@pytest.fixture
def env(tmp_path):
    if not _loopback_ok():
        pytest.skip("loopback connect unavailable in this sandbox")
    FakeIntake.seen = []
    intake = ThreadingHTTPServer(("127.0.0.1", 0), FakeIntake)
    store = G.Store(tmp_path / "state")
    store.set_setting("passphrase", G.hash_passphrase(PASS))
    srv = G.Server(("127.0.0.1", 0), store, G.Intake(f"http://127.0.0.1:{intake.server_port}", CRED),
                   "https://source-mcp.example", ("127.0.0.1",))
    for s in (intake, srv):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown(); intake.shutdown()


def req(srv, method, path, body=None, headers=None, form=False):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=10)
    h = dict(headers or {})
    if body is not None:
        if form:
            body = urllib.parse.urlencode(body); h["Content-Type"] = "application/x-www-form-urlencoded"
        elif not isinstance(body, (str, bytes)):
            body = json.dumps(body); h["Content-Type"] = "application/json"
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    raw = r.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = raw.decode()
    return r.status, dict(r.getheaders()), data


def pkce():
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def register(srv, method="none", ip=None):
    ip = ip or f"192.0.2.{secrets.randbelow(250) + 1}"
    st, _, out = req(srv, "POST", "/gitwatch/oauth/register",
                     {"redirect_uris": [CB], "client_name": "ChatGPT", "token_endpoint_auth_method": method},
                     {"X-Real-IP": ip})
    assert st == 201, out
    return out


def authorize(srv, client, passphrase=PASS, ip=None, **extra):
    verifier, challenge = pkce()
    q = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CB, "state": "st8",
         "code_challenge": challenge, "code_challenge_method": "S256", "scope": "gitwatch.submit offline_access",
         "resource": srv.resource, **extra}
    st, h, page = req(srv, "GET", "/gitwatch/oauth/authorize?" + urllib.parse.urlencode(q))
    if st != 200:
        return st, h, None, verifier
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
    pending = re.search(r'name="pending" value="([^"]+)"', page).group(1)
    st, h, _ = req(srv, "POST", "/gitwatch/oauth/authorize",
                   {"pending": pending, "passphrase": passphrase, "decision": "approve"},
                   {"X-Real-IP": ip} if ip else None, form=True)
    return st, h, pending, verifier


def code_of(location):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)
    assert q["state"] == ["st8"] and q["iss"] == ["https://source-mcp.example/gitwatch"]
    return q["code"][0]


def tokens(srv):
    client = register(srv)
    st, h, _, verifier = authorize(srv, client)
    assert st == 302
    st, _, tok = req(srv, "POST", "/gitwatch/oauth/token",
                     {"grant_type": "authorization_code", "code": code_of(h["Location"]), "redirect_uri": CB,
                      "code_verifier": verifier, "client_id": client["client_id"], "resource": srv.resource}, form=True)
    assert st == 200, tok
    return client, tok


def mcp(srv, token, method, params=None, rid=1, headers=None):
    return req(srv, "POST", "/gitwatch/mcp", {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}},
               {"Authorization": "Bearer " + token, **(headers or {})})


def test_build_submission_is_a_same_day_replay():
    day = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
    a = G.build_submission({"repo_url": "https://github.com/o/r", "saas_hint": "jira"}, day)
    b = G.build_submission({"repo_url": "https://github.com/o/r", "saas_hint": "jira"}, day)
    assert a == b and a["idempotency_key"].startswith("gitwatch-2026-11-02-")
    assert a["source"] == {"provider": "chatgpt", "observed_at": "2026-11-02T00:00:00Z"}
    assert a["provenance"] == {"on_behalf_of": "keith"} and a["contract"] == "source-submission/1"
    with pytest.raises(G.ToolError):
        G.build_submission({"repo_url": "https://github.com/o/r", "credential": "x"})
    with pytest.raises(G.ToolError):
        G.build_submission({"repo_url": "https://github.com/o/r", "saas_hint": "Not An Id"})


def test_passphrase_hash_round_trip():
    h = G.hash_passphrase(PASS)
    assert PASS not in h and G.check_passphrase(PASS, h) and not G.check_passphrase(PASS + "x", h)


@needs_loopback
def test_metadata_and_challenge(env):
    assert req(env, "GET", "/.well-known/oauth-authorization-server")[0] == 404
    assert req(env, "GET", "/.well-known/oauth-protected-resource")[0] == 404
    st, _, asm = req(env, "GET", "/.well-known/oauth-authorization-server/gitwatch")
    assert st == 200 and asm["issuer"] == "https://source-mcp.example/gitwatch" and asm["code_challenge_methods_supported"] == ["S256"]
    st, _, prm = req(env, "GET", "/.well-known/oauth-protected-resource/gitwatch/mcp")
    assert prm["resource"] == "https://source-mcp.example/gitwatch/mcp" and prm["authorization_servers"] == [asm["issuer"]]
    st, h, _ = req(env, "POST", "/gitwatch/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert st == 401 and 'resource_metadata="https://source-mcp.example/.well-known/oauth-protected-resource/gitwatch/mcp"' \
        in h["WWW-Authenticate"]


@needs_loopback
def test_registration_only_for_allowed_redirects(env):
    st, _, out = req(env, "POST", "/gitwatch/oauth/register", {"redirect_uris": ["https://evil.example/cb"]})
    assert st == 400 and out["error"] == "invalid_redirect_uri"
    st, _, out = req(env, "POST", "/gitwatch/oauth/register", {"redirect_uris": ["http://127.0.0.1/cb#frag"]})
    assert st == 400


@needs_loopback
def test_full_flow_tools_and_credential_stays_server_side(env):
    _, tok = tokens(env)
    st, _, init = mcp(env, tok["access_token"], "initialize", {"protocolVersion": "2025-06-18"})
    assert st == 200 and init["result"]["protocolVersion"] == "2025-06-18"
    st, _, tl = mcp(env, tok["access_token"], "tools/list")
    names = {t["name"] for t in tl["result"]["tools"]}
    assert names == {"submitRepository", "getSubmission"}
    assert all("credential" not in json.dumps(t["inputSchema"]) for t in tl["result"]["tools"])
    st, _, res = mcp(env, tok["access_token"], "tools/call",
                     {"name": "submitRepository", "arguments": {"repo_url": "https://github.com/o/r", "saas_hint": "jira"}})
    r = res["result"]
    assert not r["isError"] and r["structuredContent"]["http_status"] == 202
    auth, body = FakeIntake.seen[-1]
    assert auth == "Bearer " + CRED and body["contract"] == "source-submission/1"
    assert CRED not in json.dumps(res)
    st, _, res = mcp(env, tok["access_token"], "tools/call",
                     {"name": "submitRepository", "arguments": {"repo_url": "https://example.com/x"}})
    assert res["result"]["structuredContent"]["http_status"] == 422 and not res["result"]["isError"]
    st, _, res = mcp(env, tok["access_token"], "tools/call",
                     {"name": "getSubmission", "arguments": {"submission_id": "sub-0123456789abcdef"}})
    assert res["result"]["structuredContent"]["repository"]["state"] == "queued"
    st, _, res = mcp(env, tok["access_token"], "tools/call",
                     {"name": "getSubmission", "arguments": {"submission_id": "../../etc"}})
    assert res["result"]["isError"]
    st, _, _ = req(env, "POST", "/gitwatch/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"},
                   {"Authorization": "Bearer " + tok["access_token"]})
    assert st == 202
    audit = (env.store.audit_path).read_text()
    assert CRED not in audit and tok["access_token"] not in audit and PASS not in audit


@needs_loopback
def test_foreign_and_wrong_audience_tokens_refused(env):
    st, h, out = mcp(env, "openseo-or-any-other-issuer-token", "tools/list")
    assert st == 401 and 'error="invalid_token"' in h["WWW-Authenticate"]
    other = "gwa_" + secrets.token_urlsafe(32)
    env.store.db.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?,0)",
                         (G._sha(other), "access", "x", "gitwatch.submit", "https://seo.de-alwis.com/mcp", "keith", "f",
                          G._now() + 600))
    assert mcp(env, other, "tools/list")[0] == 401
    st, _, tok = req(env, "POST", "/gitwatch/oauth/token", {"grant_type": "authorization_code", "code": "x",
                                                            "client_id": "nope"}, form=True)
    assert st == 401 and tok["error"] == "invalid_client"


@needs_loopback
def test_resource_must_be_this_endpoint(env):
    client = register(env)
    st, h, _, _ = authorize(env, client, resource="https://seo.de-alwis.com/mcp")
    assert st == 302 and "error=invalid_target" in h["Location"]


@needs_loopback
def test_pkce_and_code_replay(env):
    client = register(env)
    st, h, _, verifier = authorize(env, client)
    code = code_of(h["Location"])
    bad = {"grant_type": "authorization_code", "code": code, "redirect_uri": CB, "code_verifier": "x" * 50,
           "client_id": client["client_id"]}
    assert req(env, "POST", "/gitwatch/oauth/token", bad, form=True)[2]["error"] == "invalid_grant"
    # the code was burned by the failed attempt
    good = {**bad, "code_verifier": verifier}
    assert req(env, "POST", "/gitwatch/oauth/token", good, form=True)[2]["error"] == "invalid_grant"
    client, tok = tokens(env)


@needs_loopback
def test_code_replay_revokes_issued_tokens(env):
    client = register(env)
    st, h, _, verifier = authorize(env, client)
    form = {"grant_type": "authorization_code", "code": code_of(h["Location"]), "redirect_uri": CB,
            "code_verifier": verifier, "client_id": client["client_id"]}
    tok = req(env, "POST", "/gitwatch/oauth/token", form, form=True)[2]
    assert mcp(env, tok["access_token"], "ping")[0] == 200
    assert req(env, "POST", "/gitwatch/oauth/token", form, form=True)[2]["error"] == "invalid_grant"
    assert mcp(env, tok["access_token"], "ping")[0] == 401


@needs_loopback
def test_refresh_rotation_and_reuse_detection(env):
    client, tok = tokens(env)
    form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client["client_id"]}
    st, _, new = req(env, "POST", "/gitwatch/oauth/token", form, form=True)
    assert st == 200 and new["access_token"] != tok["access_token"]
    assert mcp(env, tok["access_token"], "ping")[0] == 401 and mcp(env, new["access_token"], "ping")[0] == 200
    assert req(env, "POST", "/gitwatch/oauth/token", form, form=True)[2]["error"] == "invalid_grant"
    assert mcp(env, new["access_token"], "ping")[0] == 401  # reuse revoked the family


@needs_loopback
def test_confidential_client_needs_its_secret(env):
    client = register(env, "client_secret_basic")
    st, h, _, verifier = authorize(env, client)
    form = {"grant_type": "authorization_code", "code": code_of(h["Location"]), "redirect_uri": CB,
            "code_verifier": verifier}
    basic = base64.b64encode(f"{client['client_id']}:wrong".encode()).decode()
    assert req(env, "POST", "/gitwatch/oauth/token", form, {"Authorization": "Basic " + basic}, form=True)[0] == 401
    basic = base64.b64encode(f"{client['client_id']}:{client['client_secret']}".encode()).decode()
    assert req(env, "POST", "/gitwatch/oauth/token", form, {"Authorization": "Basic " + basic}, form=True)[0] == 200


@needs_loopback
def test_wrong_passphrase_and_lockout(env):
    client = register(env)
    for _ in range(G.LOGIN_FAILURES):
        st, _, _, _ = authorize(env, client, passphrase="wrong")
        assert st == 401
    st, _, _, _ = authorize(env, client)
    assert st == 429


@needs_loopback
def test_unknown_client_or_redirect_never_redirects(env):
    st, h, _ = req(env, "GET", "/gitwatch/oauth/authorize?client_id=nope&redirect_uri=" + urllib.parse.quote(CB))
    assert st == 400 and "Location" not in h
    client = register(env)
    st, h, _ = req(env, "GET", "/gitwatch/oauth/authorize?" + urllib.parse.urlencode(
        {"client_id": client["client_id"], "redirect_uri": "http://127.0.0.1:9/other", "response_type": "code"}))
    assert st == 400 and "Location" not in h


@needs_loopback
def test_foreign_origin_refused(env):
    _, tok = tokens(env)
    assert mcp(env, tok["access_token"], "ping", headers={"Origin": "https://evil.example"})[0] == 403


def test_redirects_are_pinned_and_control_characters_refused():
    hosts = G.CHATGPT_REDIRECT_HOSTS
    assert G.redirect_allowed("https://chatgpt.com/connector_platform_oauth_redirect", hosts)
    assert G.redirect_allowed("https://chat.openai.com/aip/g-abc123/oauth/callback", hosts)
    for bad in ("https://chatgpt.com/connector_platform_oauth_redirect\r\nSet-Cookie: pwn=1",
                "https://chatgpt.com/connector_platform_oauth_redirect\tx",
                "https://chatgpt.com/other/path", "https://chatgpt.com:8443/connector_platform_oauth_redirect",
                "https://evil.example/connector_platform_oauth_redirect",
                "https://chatgpt.com@evil.example/connector_platform_oauth_redirect",
                "https://chatgpt.com/connector_platform_oauth_redirect#x", "https://chatgpt.cöm/x",
                "http://chatgpt.com/connector_platform_oauth_redirect"):
        assert not G.redirect_allowed(bad, hosts), bad


@needs_loopback
def test_crlf_redirect_cannot_be_registered(env):
    st, _, out = req(env, "POST", "/gitwatch/oauth/register", {"redirect_uris": [CB + "\r\nX-Injected: 1"]})
    assert st == 400


@needs_loopback
def test_lockout_is_per_address(env):
    client = register(env)
    for _ in range(G.LOGIN_FAILURES):
        assert authorize(env, client, passphrase="wrong", ip="203.0.113.9")[0] == 401
    assert authorize(env, client, ip="203.0.113.9")[0] == 429
    assert authorize(env, client, ip="198.51.100.7")[0] == 302  # Keith, elsewhere, is not locked out


@needs_loopback
def test_registration_flood_does_not_close_registration(env):
    connected, _ = tokens(env)
    keith = register(env)  # registered, consent page open, no code yet
    _, _, page = req(env, "GET", "/gitwatch/oauth/authorize?" + urllib.parse.urlencode(
        {"response_type": "code", "client_id": keith["client_id"], "redirect_uri": CB, "state": "st8",
         "code_challenge": pkce()[1], "code_challenge_method": "S256"}))
    assert 'name="pending"' in page
    for i in range(G.MAX_CLIENTS + 10):  # a flood fills the cap; the oldest idle registrations are evicted
        register(env, ip="203.0.113.50")
    assert env.store.client(connected["client_id"]) is not None  # a client with tokens is never evicted
    assert env.store.client(keith["client_id"]) is not None      # nor one with a consent page open
    cid, _ = env.store.add_client("cli", [], "none", host_pattern=True)
    assert cid.startswith("gw_")


@needs_loopback
def test_open_consents_are_bounded_by_eviction_not_refusal(env):
    client = register(env)
    for _ in range(G.PENDING_PER_ADDRESS + 5):
        assert req(env, "GET", "/gitwatch/oauth/authorize?" + urllib.parse.urlencode(
            {"response_type": "code", "client_id": client["client_id"], "redirect_uri": CB,
             "code_challenge": pkce()[1], "code_challenge_method": "S256"}), headers={"X-Real-IP": "203.0.113.77"})[0] == 200
    assert env.store.db.execute("SELECT COUNT(*) FROM authz_pending WHERE ip='203.0.113.77'").fetchone()[0] == G.PENDING_PER_ADDRESS
    assert authorize(env, client, ip="198.51.100.20")[0] == 302


@needs_loopback
def test_many_addresses_cannot_lock_keith_out(env):
    client = register(env)
    for i in range(G.LOGIN_GLOBAL + 5):
        assert authorize(env, client, passphrase="wrong", ip=f"198.51.{i // 200}.{i % 200 + 1}")[0] == 401
    assert authorize(env, client, ip="192.0.2.200")[0] == 302


@needs_loopback
def test_ipv6_is_grouped_by_64(env):
    client = register(env)
    for i in range(G.LOGIN_FAILURES):
        assert authorize(env, client, passphrase="wrong", ip=f"2001:db8:1:2::{i + 1}")[0] == 401
    assert authorize(env, client, ip="2001:db8:1:2::ffff")[0] == 429


@needs_loopback
def test_malformed_bodies_and_params(env):
    c = http.client.HTTPConnection("127.0.0.1", env.server_port, timeout=10)
    c.putrequest("POST", "/gitwatch/oauth/token"); c.putheader("Content-Length", "-1"); c.endheaders()
    assert c.getresponse().status == 400
    _, tok = tokens(env)
    st, _, out = mcp(env, tok["access_token"], "tools/call", ["not", "an", "object"])
    assert out["error"]["code"] == -32602


@needs_loopback
def test_refresh_family_has_a_maximum_age(env):
    client, tok = tokens(env)
    env.store.db.execute("UPDATE families SET created_at = created_at - ?", (G.FAMILY_MAX_AGE + 10,))
    form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client["client_id"]}
    assert req(env, "POST", "/gitwatch/oauth/token", form, form=True)[2]["error"] == "invalid_grant"
