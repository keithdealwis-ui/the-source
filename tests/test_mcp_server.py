"""KEI-851: The Source MCP retrieval server over the canonical read layer. No network."""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

from source_pipeline import mcp_server as M

ROOT = Path(__file__).resolve().parent.parent
HAVE_LAYER = (ROOT / "data" / "canonical" / "MANIFEST.json").is_file() and (ROOT / "api" / "v1" / "index.json").is_file()
pytestmark = pytest.mark.skipif(not HAVE_LAYER, reason="needs the committed read layer")
NOW = "2026-10-10T12:00:00Z"
INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}


def _layer_copy(tmp_path, signals=True) -> Path:
    """Only the read layer: data/canonical/ and api/v1/, plus the published signal layers data/momentum/ and
    data/radar/ and the discovery corpus data/corpus/ unless signals is False. Nothing else from the repository."""
    root = tmp_path / "layer"
    shutil.copytree(ROOT / "data" / "canonical", root / "data" / "canonical")
    shutil.copytree(ROOT / "api" / "v1", root / "api" / "v1")
    if signals:
        for name in ("momentum", "radar", "corpus"):
            shutil.copytree(ROOT / "data" / name, root / "data" / name)
    return root


def _server(root=ROOT, protocol="2025-06-18"):
    s = M.Server(root, M.datetime(2026, 10, 10, 12, tzinfo=M.timezone.utc))
    r = s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {**INIT, "protocolVersion": protocol}})
    assert "result" in r
    s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return s


def _call(s, name, **args):
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return r["result"]


def _ok(s, name, **args):
    res = _call(s, name, **args)
    assert res["isError"] is False, res
    assert json.loads(res["content"][0]["text"]) == res["structuredContent"]
    return res["structuredContent"]["result"]


def _api(*parts):
    return json.loads((ROOT / "api" / "v1" / Path(*parts)).read_text())


def _jsonl(name):
    return [json.loads(l) for l in (ROOT / "data" / "canonical" / name).read_text().splitlines() if l.strip()]


# -- protocol


@pytest.mark.parametrize("asked,got", [("2025-06-18", "2025-06-18"), ("2025-03-26", "2025-03-26"),
                                       ("2024-11-05", "2024-11-05"), ("2099-01-01", "2025-06-18")])
def test_initialize_negotiates_protocol(asked, got):
    s = M.Server(ROOT)
    r = s.handle({"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {**INIT, "protocolVersion": asked}})
    assert r["id"] == 7 and r["result"]["protocolVersion"] == got
    assert r["result"]["serverInfo"]["name"] == "the-source"
    assert set(r["result"]["capabilities"]) == {"tools", "resources"}
    assert "CC BY 4.0" in r["result"]["instructions"]


def test_requests_before_initialize_are_refused_but_ping_works():
    s = M.Server(ROOT)
    assert s.handle({"jsonrpc": "2.0", "id": 1, "method": "ping"}) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    r = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert r["error"]["code"] == -32600


def test_tools_list_is_read_only_and_strict():
    s = _server()
    tools = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["source_dataset_info", "source_list_supported_saas",
                                         "source_get_alternatives", "source_detect_saas", "source_search",
                                         "source_get_project", "source_get_relationship", "source_compare_projects",
                                         "source_recommend", "source_emerging_projects", "source_get_momentum"]
    for t in tools:
        assert t["annotations"]["readOnlyHint"] is True and t["annotations"]["openWorldHint"] is False
        assert t["annotations"]["destructiveHint"] is False
        assert t["inputSchema"]["type"] == "object" and t["inputSchema"]["additionalProperties"] is False


def test_protocol_errors():
    s = _server()
    assert s.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})["error"]["code"] == -32601
    assert s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "rm_rf", "arguments": {}}})["error"]["code"] == -32602
    assert s.handle({"id": 1, "method": "ping"})["error"]["code"] == -32600
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}) is None
    out = io.StringIO()
    s.serve(io.StringIO('not json\n[{"jsonrpc":"2.0","id":1,"method":"ping"}]\n\n'), out)
    lines = [json.loads(l) for l in out.getvalue().splitlines()]
    assert [l["error"]["code"] for l in lines] == [-32700, -32600]


@pytest.mark.parametrize("name,args,needle", [
    ("source_get_alternatives", {}, "missing argument"),
    ("source_get_alternatives", {"saas": "notion", "x": 1}, "unknown argument"),
    ("source_get_alternatives", {"saas": "notion", "limit": 0}, "limit"),
    ("source_get_alternatives", {"saas": "notion", "limit": 51}, "limit"),
    ("source_get_alternatives", {"saas": "notion", "limit": True}, "limit"),
    ("source_get_alternatives", {"saas": "no-such-product"}, "not a product"),
    ("source_get_alternatives", {"saas": "../../etc/passwd"}, "not a product"),
    ("source_get_alternatives", {"saas": 3}, "must be a string"),
    ("source_search", {"query": "   "}, "query"),
    ("source_search", {"query": "x" * 400}, "query"),
    ("source_search", {"query": "crm", "kind": "saas", "licence": "MIT"}, "filter projects"),
    ("source_search", {"query": "crm", "maintenance_status": "stale"}, "maintenance_status"),
    ("source_search", {"query": "crm", "min_health": 101}, "min_health"),
    ("source_get_project", {"project": "github.com/nobody/nothing"}, "not a project"),
    ("source_get_relationship", {"saas": "notion", "project": "github.com/dani-garcia/vaultwarden"}, "no relationship"),
    ("source_detect_saas", {"url": ""}, "url"),
])
def test_bad_arguments_are_tool_errors_not_crashes(name, args, needle):
    res = _call(_server(), name, **args)
    assert res["isError"] is True and needle in res["content"][0]["text"]
    assert "structuredContent" not in res


def test_older_protocol_gets_text_only():
    res = _call(_server(protocol="2024-11-05"), "source_dataset_info")
    assert "structuredContent" not in res and json.loads(res["content"][0]["text"])["result"]["dataset_version"]


# -- the read layer and its gates


def test_every_answer_carries_version_licence_and_attribution():
    s = _server()
    manifest = json.loads((ROOT / "data" / "canonical" / "MANIFEST.json").read_text())
    res = _call(s, "source_list_supported_saas")["structuredContent"]
    assert res["meta"]["dataset_version"] == manifest["dataset_version"]
    assert res["meta"]["licence"] == "CC BY 4.0" and "Keith de Alwis" in res["meta"]["attribution"]
    assert res["meta"]["notice"] == "data/canonical/NOTICE.md"


def test_alternatives_for_supported_products_equal_the_published_page():
    s = _server()
    index = _api("index.json")
    assert len(index["supported_saas"]) >= 1
    for row in index["supported_saas"]:
        page = _api("saas", f"{row['saas_id']}.json")
        got = _ok(s, "source_get_alternatives", saas=row["saas_id"], limit=5)
        assert got["surface"] == f"api/v1/saas/{row['saas_id']}.json"
        assert got["recommendations"] == page["recommendations"]
        assert got["status"] == page["status"] and got["more_available"] == page["more_available"]
        assert got["provenance"] == page["provenance"] and got["last_checked"] == page["last_checked"]
        assert _ok(s, "source_get_alternatives", saas=row["name"])["recommendations"] == page["recommendations"]
        # the page is the top of the canonical ranking, so rows past it continue the same order
        more = _ok(s, "source_get_alternatives", saas=row["saas_id"], limit=50)
        assert more["recommendations"][:len(page["recommendations"])] == page["recommendations"]
        extra = more["recommendations"][len(page["recommendations"]):]
        assert len(extra) == page["more_available"] and all(r["beyond_published_page"] for r in extra)
        keys = [M._rank_key(r) for r in more["recommendations"]]
        assert keys == sorted(keys)
        assert [r["rank"] for r in more["recommendations"]] == list(range(1, len(keys) + 1))


def test_limit_trims_and_counts_the_rest():
    s = _server()
    page = _api("saas", "notion.json")
    got = _ok(s, "source_get_alternatives", saas="notion", limit=2)
    assert got["recommendations"] == page["recommendations"][:2]
    assert got["more_available"] == page["more_available"] + len(page["recommendations"]) - 2


def test_only_recommended_relationships_are_ever_recommendations():
    s = _server()
    rels = _jsonl("relationships.jsonl")
    recommended = {(r["saas_id"], r["oss_id"]) for r in rels if r["recommended"]}
    not_recommended = {(r["saas_id"], r["oss_id"]) for r in rels if not r["recommended"]}
    assert not_recommended, "the layer should hold some non-recommended relationships to test the gate"
    projects = {p["oss_id"]: p for p in _jsonl("oss_projects.jsonl")}
    served = set()
    for sp in _jsonl("saas_products.jsonl"):
        got = _ok(s, "source_get_alternatives", saas=sp["saas_id"], limit=50)
        for rec in got["recommendations"]:
            pair = (sp["saas_id"], rec["oss_id"])
            assert pair in recommended and pair not in not_recommended
            assert projects[rec["oss_id"]]["maintenance"]["status"] in ("active", "maintained")
            assert projects[rec["oss_id"]]["live"]["archived"] is False
            served.add(pair)
        assert got["not_recommended"]["count"] == sum(1 for p in not_recommended if p[0] == sp["saas_id"])
    assert served == recommended


def test_unsupported_product_is_served_from_canonical_with_a_caveat():
    s = _server()
    unsupported = [r["saas_id"] for r in _jsonl("saas_products.jsonl") if not r["supported"]]
    rels = _jsonl("relationships.jsonl")
    sid = next(x for x in unsupported if sum(1 for r in rels if r["saas_id"] == x and r["recommended"]) >= 2)
    got = _ok(s, "source_get_alternatives", saas=sid)
    assert got["surface"] == "data/canonical/relationships.jsonl" and "no published api/v1 page" in got["caveat"]
    keys = [M._rank_key(r) for r in got["recommendations"]]
    assert keys == sorted(keys) and [r["rank"] for r in got["recommendations"]] == list(range(1, len(keys) + 1))
    assert all("note" not in r and "self_hosting" not in r for r in got["recommendations"])


def test_resolution_by_alias_domain_and_url():
    s = _server()
    assert _ok(s, "source_get_alternatives", saas="AWS S3")["saas"]["saas_id"] == "amazon-s3"
    assert _ok(s, "source_get_alternatives", saas="notion.so")["saas"]["saas_id"] == "notion"
    assert _ok(s, "source_get_alternatives", saas="https://www.notion.so/acme/page")["resolved_by"] == "url"
    res = _call(s, "source_get_alternatives", saas="aws.amazon.com")
    assert res["isError"] and "more than one product" in res["content"][0]["text"]


def _reference_match(domains, url):
    """tests/test_canonical.py's reference matcher, restricted to http(s) like the extension."""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        return None
    host = (u.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    hits = [r for r in domains["rules"]
            if (r.get("host") == host or (r.get("host_suffix") and host.endswith(r["host_suffix"])))
            and (not r.get("path_prefix") or u.path.startswith(r["path_prefix"]))]
    hits.sort(key=lambda r: 0 if r.get("path_prefix") else 1)
    return hits[0]["saas_id"] if hits else None


def test_detect_agrees_with_the_reference_matcher():
    s = _server()
    domains = _api("domains.json")
    assert _ok(s, "source_detect_saas", url="https://acme.atlassian.net/jira/software")["saas_id"] == "jira"
    assert _ok(s, "source_detect_saas", url="https://acme.atlassian.net/wiki/spaces")["saas_id"] is None
    assert _ok(s, "source_detect_saas", url="https://aws.amazon.com/s3/pricing/")["saas_id"] == "amazon-s3"
    assert _ok(s, "source_detect_saas", url="shop.myshopify.com")["saas_id"] == "shopify"
    assert _ok(s, "source_detect_saas", url="ftp://notion.so/")["saas_id"] is None
    urls = ["https://example.com/"]
    for r in domains["rules"]:
        hosts = [r["host"], "www." + r["host"], "x." + r["host"]] if "host" in r else \
            ["acme" + r["host_suffix"], r["host_suffix"][1:]]
        for h in hosts:
            for p in ["/", r.get("path_prefix", "/app"), r.get("path_prefix", "") + "/deep", "/other"]:
                urls += [f"https://{h}{p}", f"http://{h.upper()}{p}?q=1#f"]
    assert len(urls) > 300
    for u in urls:
        assert _ok(s, "source_detect_saas", url=u)["saas_id"] == _reference_match(domains, u), u


def test_search_ranks_exact_names_first_and_filters_projects():
    s = _server()
    top = _ok(s, "source_search", query="Notion")["results"][0]
    assert top["kind"] == "saas" and top["id"] == "notion" and top["exact_name_match"]
    page = _api("saas", "notion.json")
    first = page["recommendations"][0]
    hit = _ok(s, "source_search", query=first["name"], kind="project")["results"][0]
    assert hit["id"] == first["oss_id"] and "notion" in hit["recommended_for"]
    projects = {p["oss_id"]: p for p in _jsonl("oss_projects.jsonl")}
    got = _ok(s, "source_search", query="project management analytics crm", kind="project",
              licence="agpl-3.0", maintenance_status="active", min_health=60, limit=50)
    assert got["results"]
    for r in got["results"]:
        p = projects[r["id"]]
        assert p["licence"]["spdx"] == "AGPL-3.0" and p["maintenance"]["status"] == "active"
        assert p["project_health"]["score"] >= 60
    scores = [(-r["score"], r["kind"], r["id"]) for r in got["results"]]
    assert scores == sorted(scores)
    sup = _ok(s, "source_search", query="management", kind="saas", supported_only=True, limit=50)["results"]
    assert sup and all(r["saas"]["supported"] for r in sup)


def test_project_and_relationship_evidence():
    s = _server()
    rels = _jsonl("relationships.jsonl")
    bad = next(r for r in rels if not r["recommended"])
    got = _ok(s, "source_get_relationship", saas=bad["saas_id"], project=bad["oss_id"])
    assert got["relationship"] == bad and got["verdict"].startswith("not recommended")
    good = next(r for r in rels if r["recommended"])
    got = _ok(s, "source_get_relationship", saas=good["saas_id"], project="https://" + good["oss_id"] + ".git/")
    assert got["relationship"]["provenance"] == good["provenance"] and got["verdict"].startswith("recommended")
    proj = _ok(s, "source_get_project", project=good["oss_id"])
    assert {r["edge_id"] for r in proj["relationships"]} == {r["edge_id"] for r in rels if r["oss_id"] == good["oss_id"]}


def test_resources():
    s = _server()
    res = s.handle({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})["result"]["resources"]
    uris = {r["uri"] for r in res}
    assert "source://data/canonical/NOTICE.md" in uris and "source://api/v1/saas/notion" in uris
    r = s.handle({"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": "source://api/v1/saas/notion"}})
    assert json.loads(r["result"]["contents"][0]["text"]) == _api("saas", "notion.json")
    for bad in ("source://api/v1/saas/../../../etc/passwd", "file:///etc/passwd", "source://data/live/snapshot.json"):
        r = s.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": bad}})
        assert r["error"]["code"] == -32002


def test_dataset_info_freshness_uses_the_pinned_clock():
    got = _ok(_server(), "source_dataset_info")
    assert got["freshness"]["now"] == NOW and got["snapshot"]["current"] is True
    assert got["dataset_version"] == json.loads((ROOT / "data" / "canonical" / "MANIFEST.json").read_text())["dataset_version"]


# -- integrity, reload, isolation


def _reversion(root: Path, extra: str):
    """A valid new snapshot: change NOTICE.md and re-derive every hash and dataset_version."""
    canon, api = root / "data" / "canonical", root / "api" / "v1"
    with open(canon / "NOTICE.md", "a") as fh:
        fh.write(extra)
    m = json.loads((canon / "MANIFEST.json").read_text())
    m["files"]["NOTICE.md"] = hashlib.sha256((canon / "NOTICE.md").read_bytes()).hexdigest()
    m["dataset_version"] = hashlib.sha256(M._dumps(m["files"]).encode()).hexdigest()[:16]
    (canon / "MANIFEST.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
    for f in [api / "index.json", api / "domains.json", *sorted((api / "saas").glob("*.json"))]:
        d = json.loads(f.read_text())
        d["dataset_version"] = m["dataset_version"]
        f.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
    return m["dataset_version"]


@pytest.mark.parametrize("tamper", ["canonical_byte", "api_version_mismatch", "missing_page", "manifest_count"])
def test_a_layer_that_does_not_verify_is_never_served(tmp_path, tamper):
    root = _layer_copy(tmp_path)
    canon, api = root / "data" / "canonical", root / "api" / "v1"
    if tamper == "canonical_byte":
        b = bytearray((canon / "oss_projects.jsonl").read_bytes())
        b[10] ^= 1
        (canon / "oss_projects.jsonl").write_bytes(bytes(b))
    elif tamper == "api_version_mismatch":
        d = json.loads((api / "saas" / "notion.json").read_text())
        d["dataset_version"] = "0" * 16
        (api / "saas" / "notion.json").write_text(json.dumps(d))
    elif tamper == "missing_page":
        (api / "saas" / "notion.json").unlink()
    else:
        m = json.loads((canon / "MANIFEST.json").read_text())
        m["counts"]["relationships"] += 1
        (canon / "MANIFEST.json").write_text(json.dumps(m))
    with pytest.raises(M.LayerError):
        M.Server(root)
    p = subprocess.run([sys.executable, "-m", "source_pipeline.mcp_server", "--root", str(root)],
                       input="", capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert p.returncode == 2 and p.stdout == "" and "refusing to serve" in p.stderr


def test_reload_switches_to_a_verified_snapshot_and_keeps_the_old_one_otherwise(tmp_path):
    root = _layer_copy(tmp_path)
    s = _server(root)
    v0 = _ok(s, "source_dataset_info")["dataset_version"]
    v1 = _reversion(root, "\n<!-- next cycle -->\n")
    assert v1 != v0
    assert _ok(s, "source_dataset_info")["dataset_version"] == v1
    # a half-written cycle: MANIFEST changed, files not
    m = json.loads((root / "data" / "canonical" / "MANIFEST.json").read_text())
    m["files"]["NOTICE.md"] = "0" * 64
    (root / "data" / "canonical" / "MANIFEST.json").write_text(json.dumps(m))
    res = _call(s, "source_get_alternatives", saas="notion")["structuredContent"]
    assert res["meta"]["dataset_version"] == v1 and res["meta"]["snapshot_current"] is False
    info = _ok(s, "source_dataset_info")["snapshot"]
    assert info["current"] is False and "did not verify" in info["reload_error"]
    # a malformed record in an otherwise consistent update is refused the same way
    root2 = _layer_copy(tmp_path / "b")
    s2 = _server(root2)
    v2 = _ok(s2, "source_dataset_info")["dataset_version"]
    canon = root2 / "data" / "canonical"
    (canon / "saas_products.jsonl").write_text('{"saas_id": 1}\n')
    m = json.loads((canon / "MANIFEST.json").read_text())
    m["files"]["saas_products.jsonl"] = hashlib.sha256((canon / "saas_products.jsonl").read_bytes()).hexdigest()
    (canon / "MANIFEST.json").write_text(json.dumps(m))
    res = _call(s2, "source_get_alternatives", saas="notion")["structuredContent"]
    assert res["meta"]["dataset_version"] == v2 and res["meta"]["snapshot_current"] is False


AUDIT = r"""
import json, sys
opened, net = set(), []
def hook(event, args):
    if event == "open" and isinstance(args[0], str):
        opened.add(args[0])
    if event.startswith("socket.") or event in ("urllib.Request", "http.client.connect"):
        net.append(event)
sys.addaudithook(hook)
from source_pipeline import mcp_server
import io
srv_in = io.StringIO(sys.stdin.read())
out = io.StringIO()
server = mcp_server.Server(mcp_server.Path(sys.argv[1]))
server.serve(srv_in, out)
json.dump({"opened": sorted(opened), "net": net, "out": out.getvalue()}, sys.stderr)
"""


def _transcript_requests():
    rows = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/list"}]
    calls = [("source_dataset_info", {}), ("source_list_supported_saas", {}),
             ("source_get_alternatives", {"saas": "Notion"}), ("source_get_alternatives", {"saas": "1password"}),
             ("source_detect_saas", {"url": "https://acme.atlassian.net/jira/"}),
             ("source_search", {"query": "password manager"}),
             ("source_get_project", {"project": "github.com/docmost/docmost"})]
    for i, (n, a) in enumerate(calls, 4):
        rows.append({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": n, "arguments": a}})
    return "".join(json.dumps(r) + "\n" for r in rows)


def test_reads_only_the_read_layer_and_never_the_network(tmp_path):
    root = _layer_copy(tmp_path)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    p = subprocess.run([sys.executable, "-c", AUDIT, str(root)], input=_transcript_requests(),
                       capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    report = json.loads(p.stderr[p.stderr.index("{"):])
    data_opens = [o for o in report["opened"] if o.startswith(str(root))]
    assert data_opens and report["net"] == []
    allowed = ("/data/canonical/", "/api/v1/", "/data/momentum/", "/data/radar/", "/data/corpus/")
    assert all(any(a in o[len(str(root)):] for a in allowed) for o in data_opens), data_opens
    assert len([l for l in report["out"].splitlines() if l]) == 10
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_same_layer_same_clock_same_bytes():
    def run():
        return subprocess.run([sys.executable, "-m", "source_pipeline", "mcp-serve", "--now", NOW],
                              input=_transcript_requests(), capture_output=True, text=True, cwd=ROOT, timeout=120)
    a, b = run(), run()
    assert a.returncode == 0 and a.stdout and a.stdout == b.stdout
    for line in a.stdout.splitlines():
        msg = json.loads(line)
        assert "error" not in msg and not msg["result"].get("isError")


def test_module_is_standard_library_only():
    import ast

    src = (ROOT / "source_pipeline" / "mcp_server.py").read_text()
    imports = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imports |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module.split(".")[0])
    assert imports <= {"__future__", "argparse", "hashlib", "json", "math", "re", "sys", "time", "unicodedata",
                       "datetime", "pathlib", "urllib"}
    assert "urlopen" not in src and "socket" not in src and "subprocess" not in src
