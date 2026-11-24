"""MCP answer quality: typo tolerance, 1-day momentum noise, release readings, emerging query strength, public
descriptions, supported-SaaS paging and the full-mode edition_check summary."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from source_pipeline import momentum
from source_pipeline import mcp_server as M

ROOT = Path(__file__).resolve().parent.parent
HAVE_LAYER = (ROOT / "data" / "canonical" / "MANIFEST.json").is_file() and (ROOT / "api" / "v1" / "index.json").is_file()
pytestmark = pytest.mark.skipif(not HAVE_LAYER, reason="needs the committed read layer")
INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}


@pytest.fixture(scope="module")
def server():
    s = M.Server(ROOT, M.datetime(2026, 11, 24, 12, tzinfo=M.timezone.utc))
    assert "result" in s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": INIT})
    return s


def _call(s, name, **args):
    return s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": name, "arguments": args}})["result"]


def _ok(s, name, **args):
    res = _call(s, name, **args)
    assert res["isError"] is False, res
    return res["structuredContent"]["result"]


def _err(s, name, **args):
    res = _call(s, name, **args)
    assert res["isError"] is True, res
    return res["content"][0]["text"]


# -- 1. typo tolerance

def test_fuzzy_saas_resolves_a_confident_typo(server):
    out = _ok(server, "source_get_alternatives", saas="Airtabel")
    assert out["saas"]["saas_id"] == "airtable" and out["resolved_by"] == "fuzzy"
    assert _ok(server, "source_get_alternatives", saas="Airtable")["resolved_by"] == "name"


def test_fuzzy_unrelated_name_still_errors(server):
    assert "not a product" in _err(server, "source_get_alternatives", saas="zzqqxxyy")


def test_fuzzy_names_ambiguous_lists_suggestions():
    names = {"gitlab": {"gitlab"}, "github": {"github"}}
    pid, scored = M.fuzzy_names("gitlub", names)
    assert pid is None and {k for k, _, _ in scored} == {"gitlab", "github"}
    assert "did you mean" in M._suggestion_text(scored)
    assert M.fuzzy_names("gitlabb", {"gitlab": {"x"}})[0] == "x"


def test_fuzzy_project(server):
    out = _ok(server, "source_get_project", project="Rocket.Chatt")
    assert out["project"]["oss_id"] == "github.com/rocketchat/rocket.chat" and out["resolved_by"] == "fuzzy"
    assert _ok(server, "source_get_project", project="github.com/rocketchat/rocket.chat")["resolved_by"] == "id"


# -- 2. momentum noise

def test_one_day_label_suppressed_below_threshold():
    thr = M.MOMENTUM_SERVING["one_day_min_abs_delta"]
    win = {"status": "measured", "momentum": "declining", "stars_delta": -(thr - 1), "acceleration": {}}
    v = M._window_view(win, "1")
    assert v["momentum"] is None and v["momentum_suppressed"]["label"] == "declining"
    assert M._window_view({**win, "stars_delta": -thr}, "1")["momentum"] == "declining"
    assert M._window_view(win, "7")["momentum"] == "declining"  # only the 1-day window is gated


def test_headline_is_the_seven_day_window(server):
    out = _ok(server, "source_get_momentum", project="github.com/rocketchat/rocket.chat")
    rm = out["repository_momentum"]
    assert rm["headline"]["window_days"] == 7
    assert rm["headline"]["momentum"] == rm["windows"]["7"]["momentum"]


def test_serving_config_is_outside_the_fingerprint():
    cfg = momentum.load_config()
    assert cfg["serving"]["headline_window"] == "7"
    assert momentum.check_version(cfg) == []
    assert momentum.parameters_sha256({**cfg, "serving": {"one_day_min_abs_delta": 1}}) == \
        momentum.parameters_sha256(cfg)


# -- 3. release readings

def test_release_readings_label_both_when_they_disagree(server):
    layer = server.layer
    pid = next(iter(layer.projects))
    row = {"on": "2026-11-24", "windows": {"7": {"activity": {"latest_release_tag": "99.0.0"}}}}
    saved = layer.momentum.get(M._repo_key(pid))
    layer.momentum[M._repo_key(pid)] = row
    try:
        readings, note = M.release_readings(layer, pid, {"tag": "1.0.0", "published_at": "x"}, "2026-11-23T00:00:00Z")
        assert [r["tag"] for r in readings] == ["1.0.0", "99.0.0"] and all(r["as_of"] and r["basis"] for r in readings)
        assert "disagree" in note
        assert M.release_readings(layer, pid, {"tag": "99.0.0"}, "t") is None
    finally:
        if saved is None:
            layer.momentum.pop(M._repo_key(pid))
        else:
            layer.momentum[M._repo_key(pid)] = saved


# -- 4. emerging query strength

def test_emerging_query_drops_weak_matches(server):
    out = _ok(server, "source_emerging_projects", query="AI agents", limit=50)
    keys = {r["key"] for r in out["repository_momentum"]}
    assert "github.com/open-net-worth-ai/open-net-worth" not in keys
    assert "github.com/bex-co/beancount-io" not in keys


# -- 5. public text

def test_no_internal_ticket_ids_in_client_text(server):
    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    init = server.handle({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": INIT})
    text = json.dumps(listed) + json.dumps(init) + M.QUICK_NOTE
    assert not re.search(r"KEI-\d+", text)
    assert "9,4" not in text
    desc = {t["name"]: t["description"] for t in listed["result"]["tools"]}
    assert "replace product X" in desc["source_get_alternatives"]
    assert "free-text need" in desc["source_recommend"]


# -- 6. supported SaaS filters and paging

def test_list_supported_paging_and_filters(server):
    everything = _ok(server, "source_list_supported_saas")
    assert everything["total"] == everything["count"] and everything["next_offset"] is None
    page = _ok(server, "source_list_supported_saas", limit=10, offset=5)
    assert page["count"] == 10 and page["next_offset"] == 15
    assert [r["saas_id"] for r in page["supported_saas"]] == \
        [r["saas_id"] for r in everything["supported_saas"][5:15]]
    chat = _ok(server, "source_list_supported_saas", category="chat")
    assert chat["total"] >= 1 and all("chat" in r["category"].lower() for r in chat["supported_saas"])
    ok = _ok(server, "source_list_supported_saas", status="ok")
    assert all(r["status"] == "ok" for r in ok["supported_saas"])
    assert _call(server, "source_list_supported_saas", status="nope")["isError"] is True


# -- 7. edition_check deployment summary

def test_deployment_summary():
    dep = [{"kind": "docker_compose", "path": "a/docker-compose.yml", "files": ["a/Dockerfile"], "runs_project": True,
            "development": True, "services": [{"name": "x"}, {"name": "y"}]},
           {"kind": "dockerfile", "path": "Dockerfile", "files": [], "runs_project": True, "services": []}]
    s = M.deployment_summary(dep)
    assert s["artefacts"] == 2 and s["services"] == 2 and s["kinds"] == {"docker_compose": 1, "dockerfile": 1}
    assert s["files_sample"] == ["a/docker-compose.yml", "a/Dockerfile", "Dockerfile"]


def test_full_search_has_no_service_lists(server):
    res = _call(server, "source_search", query="mattermost", detail="full")
    assert res["isError"] is False
    text = json.dumps(res["structuredContent"])
    assert '"services": [' not in text and '"services":[' not in text
