"""KEI-851: the Streamable HTTP transport (source_pipeline/mcp_http.py). Loopback only."""
from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from source_pipeline import mcp_http as H
from source_pipeline import mcp_server as M

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not (ROOT / "data" / "canonical" / "MANIFEST.json").is_file(),
                                reason="needs the committed read layer")


@pytest.fixture()
def endpoint(tmp_path):
    app = H.App(M.Server(ROOT), allow_origins=["https://allowed.example"], telemetry_path=tmp_path / "t.json",
                rate_per_min=40)
    httpd = H.serve(app, "127.0.0.1", 0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1], app, tmp_path / "t.json"
    httpd.shutdown()
    httpd.server_close()


def _req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})}
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    c.close()
    return r.status, dict(r.getheaders()), (json.loads(raw) if raw else None)


def test_initialize_then_tools_without_a_session(endpoint):
    port, _, _ = endpoint
    st, hd, body = _req(port, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                               "params": {"protocolVersion": "2025-06-18"}})
    assert st == 200 and body["result"]["protocolVersion"] == "2025-06-18"
    assert "Mcp-Session-Id" not in hd and hd["Content-Type"] == "application/json"
    st, _, _ = _req(port, "POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert st == 202
    st, _, body = _req(port, "POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                              "params": {"name": "source_recommend", "arguments": {"saas": "Notion"}}},
                       {"MCP-Protocol-Version": "2025-06-18"})
    assert st == 200 and body["result"]["isError"] is False
    assert body["result"]["structuredContent"]["meta"]["licence"] == "CC BY 4.0"
    st, _, body = _req(port, "POST", "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert st == 200 and len(body["result"]["tools"]) == len(M.TOOLS)  # no header: 2025-03-26, still answered


def test_health_and_methods(endpoint):
    port, app, _ = endpoint
    st, _, body = _req(port, "GET", "/healthz")
    assert st == 200 and body["serving_dataset_version"] == app.server.layer.dataset_version
    assert _req(port, "GET", "/mcp")[0] == 405
    assert _req(port, "DELETE", "/mcp")[0] == 405
    assert _req(port, "POST", "/other", {"jsonrpc": "2.0", "id": 1, "method": "ping"})[0] == 404


def test_bounds_and_refusals(endpoint):
    port, _, _ = endpoint
    assert _req(port, "POST", "/mcp", b"{not json")[0] == 400
    assert _req(port, "POST", "/mcp", [{"jsonrpc": "2.0", "id": 1, "method": "ping"}])[0] == 400
    assert _req(port, "POST", "/mcp", b"x" * (H.MAX_BODY + 1))[0] == 413
    st, _, _ = _req(port, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    {"Origin": "https://evil.example"})
    assert st == 403
    st, _, _ = _req(port, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    {"Origin": "https://allowed.example"})
    assert st == 200


def test_rate_limit_per_client(endpoint):
    port, _, _ = endpoint
    codes = [_req(port, "POST", "/mcp", {"jsonrpc": "2.0", "id": i, "method": "ping"},
                  {"X-Real-IP": "203.0.113.9"})[0] for i in range(45)]
    assert codes.count(429) >= 1 and codes[0] == 200
    assert _req(port, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"X-Real-IP": "203.0.113.10"})[0] == 200


def test_telemetry_file_holds_counts_only(endpoint):
    port, app, path = endpoint
    _req(port, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "source_search", "arguments": {"query": "zz-private-query-77"}}},
         {"MCP-Protocol-Version": "2025-06-18", "X-Real-IP": "198.51.100.7"})
    app.maybe_flush(force=True)
    text = path.read_text()
    assert "zz-private-query-77" not in text and "198.51.100.7" not in text
    assert json.loads(text)["schema"] == "the-source.mcp-telemetry/1"


def test_background_flush_writes_the_last_window_without_another_request(endpoint, monkeypatch):
    import time
    port, app, path = endpoint
    monkeypatch.setattr(H, "TELEMETRY_EVERY_S", 0.2)
    app.flusher()
    _req(port, "POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "source_dataset_info", "arguments": {}}},
         {"MCP-Protocol-Version": "2025-06-18"})
    for _ in range(50):
        if path.exists() and "source_dataset_info" in path.read_text():
            break
        time.sleep(0.1)
    assert "source_dataset_info" in path.read_text()
