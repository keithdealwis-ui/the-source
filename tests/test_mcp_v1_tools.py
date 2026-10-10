"""KEI-851 V1 capability groups: compare, recommend against explicit requirements, emerging projects,
Repository/Community Momentum, hybrid (KEI-844 semantic + graph) retrieval, degraded signals, telemetry."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from source_pipeline import mcp_server as M

ROOT = Path(__file__).resolve().parent.parent
HAVE = all((ROOT / p).is_file() for p in ("data/canonical/MANIFEST.json", "api/v1/index.json",
                                          "data/momentum/MANIFEST.json", "data/radar/MANIFEST.json"))
pytestmark = pytest.mark.skipif(not HAVE, reason="needs the committed read and signal layers")
NOW = M.datetime(2026, 10, 10, 12, tzinfo=M.timezone.utc)


def _server(root=ROOT, now=NOW):
    s = M.Server(root, now)
    s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    return s


def _call(s, name, **args):
    return s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": name, "arguments": args}})["result"]


def _ok(s, name, **args):
    r = _call(s, name, **args)
    assert r["isError"] is False, r
    return r["structuredContent"]["result"]


def _copy(tmp_path, signals=("momentum", "radar")):
    root = tmp_path / "layer"
    for rel in ["data/canonical", "api/v1"] + [f"data/{n}" for n in signals]:
        shutil.copytree(ROOT / rel, root / rel)
    return root


S = None


def setup_module():
    global S
    S = _server()


# -- the six capability groups are all exposed with strict schemas

def test_six_capability_groups_have_tools():
    tools = {t["name"]: t for t in S.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    groups = {"discovery": "source_search", "alternatives": "source_get_alternatives",
              "project_intelligence": "source_get_project", "comparison": "source_compare_projects",
              "recommendation": "source_recommend", "emerging": "source_emerging_projects"}
    for tool in groups.values():
        t = tools[tool]
        assert t["inputSchema"]["additionalProperties"] is False
        assert t["annotations"]["readOnlyHint"] is True and t["annotations"]["destructiveHint"] is False
    assert "source_get_momentum" in tools


# -- comparison

def test_compare_keeps_the_four_measures_separate():
    r = _ok(S, "source_compare_projects", projects=["Docmost", "github.com/appflowy-io/appflowy"])
    assert [p["oss_id"] for p in r["projects"]] == ["github.com/docmost/docmost", "github.com/appflowy-io/appflowy"]
    assert set(r["measures"]) == {"project_health", "replacement_fit", "repository_momentum", "community_momentum"}
    for p in r["projects"]:
        assert {"project_health", "replaces", "repository_momentum", "community_momentum"} <= set(p)
    notion = [x for x in r["shared_saas"] if x["saas_id"] == "notion"]
    assert notion and set(notion[0]["by_project"]) == {p["oss_id"] for p in r["projects"]}


@pytest.mark.parametrize("projects,needle", [(["Docmost"], "2 to 5"), (["Docmost", "docmost"], "same project"),
                                             (["Docmost", "no-such-project-xyz"], "not a project")])
def test_compare_refuses_bad_input(projects, needle):
    r = _call(S, "source_compare_projects", projects=projects)
    assert r["isError"] and needle in r["content"][0]["text"]


# -- recommendation against explicit requirements

def test_recommend_applies_every_constraint_exactly():
    args = dict(saas="Notion", licence_family="copyleft", self_hostable=True, maintenance_status=["active"],
                min_health=50)
    r = _ok(S, "source_recommend", **args)
    assert r["status"] == "ok" and r["recommendations"]
    for x in r["recommendations"]:
        assert M.LICENCE_FAMILIES[x["licence_spdx"]] == "copyleft"
        assert x["self_hosting"]["self_hostable"] is True
        assert x["maintenance_status"] == "active"
        assert x["project_health"]["score"] >= 50
        assert x["for_saas"] == "notion"
    assert r["candidates_considered"] == len(r["recommendations"]) + r["more_available"] + \
        sum(r["excluded_by_constraint"].values())
    assert "API quality or coverage" in r["not_evaluated"]["requirements"]


def test_recommend_from_a_need_in_words_uses_named_product_and_hybrid_retrieval():
    r = _ok(S, "source_recommend", need="self-hosted Notion replacement for a team wiki")
    assert r["matched_products"][0] == {"saas_id": "notion", "name": "Notion", "matched_by": "named_in_need",
                                        "strength": 1.0}
    assert all(x["via"] == "replaces notion" for x in r["recommendations"])
    r = _ok(S, "source_recommend", need="customer support helpdesk")
    assert r["matched_products"] and all(m["matched_by"] == "hybrid" for m in r["matched_products"])
    assert all(set(m["components"]) == {"lexical", "semantic", "graph"} for m in r["matched_products"])
    assert r["recommendations"]


def test_recommend_says_when_nothing_is_recommended():
    r = _ok(S, "source_recommend", saas="Notion", licence=["WTFPL"])
    assert r["status"] == "no_candidate_meets_constraints" and r["recommendations"] == []
    assert _call(S, "source_recommend")["isError"]


# -- emerging / accelerating

def test_emerging_reports_measured_rows_and_unmeasurable_windows():
    r = _ok(S, "source_emerging_projects", window="7", max_stars=10000, limit=50)
    for x in r["repository_momentum"]:
        assert x["momentum"] in ("surging", "rising") and x["stars"] <= 10000
    growth = [x["growth_pct_per_30d"] for x in r["repository_momentum"]]
    assert growth == sorted(growth, reverse=True)
    cov = r["window_coverage"]
    if cov.get("acceleration_status") != "measured":
        assert any("acceleration" in d for d in r["degraded"])
    r1 = _ok(S, "source_emerging_projects", window="1", accelerating_only=True)
    assert all(x["acceleration"]["label"] == "accelerating" for x in r1["repository_momentum"])
    assert r1["community_momentum"] and r1["community_momentum"][0]["rank"] == 1


def test_momentum_for_canonical_and_discovery_repositories():
    r = _ok(S, "source_get_momentum", project="Docmost")
    assert r["in_canonical_layer"] and r["repository_momentum"]["status"] == "tracked"
    assert set(r["repository_momentum"]["windows"]) == {"1", "7", "30", "90"}
    top = S.layer.radar_ranked[0]["key"]
    r = _ok(S, "source_get_momentum", project=top)
    assert r["community_momentum"]["rank"] == 1 and r["community_momentum"]["mentions"]
    assert all(m["permalink"].startswith("https://") for m in r["community_momentum"]["mentions"])


# -- KEI-844 semantic model and graph

def test_semantic_tokeniser_is_kei844s():
    gr = pytest.importorskip("source_pipeline.graph_retrieval")
    assert M.SEMANTIC_MODEL == gr.MODEL and M._SEM_STOP == gr.STOP and M._SEM_WORD.pattern == gr.WORD.pattern
    for text in ("Self-hosted Notion replacement, C++ & C# tools v2.1", "open source password manager"):
        assert M._sem_tokens(text) == gr.tokens(text)


def test_hybrid_search_returns_components_and_graph_paths():
    r = _ok(S, "source_search", query="open source password manager", mode="hybrid", limit=5)
    assert r["mode"] == "hybrid" and r["results"]
    for x in r["results"]:
        c = x["score_components"]
        expect = round(sum(M.HYBRID_WEIGHTS[k] * c[k] for k in c), 4)
        assert abs(x["score"] - expect) < 1e-3
    assert any(x["graph_path"] and x["graph_path"]["relation"] == "REPLACES" for x in r["results"])
    lexical = _ok(S, "source_search", query="password manager")
    assert lexical["mode"] == "lexical" and "score_components" not in lexical["results"][0]


# -- degraded and stale signal layers

def test_missing_signals_degrade_explicitly_and_canonical_tools_keep_serving(tmp_path):
    s = _server(_copy(tmp_path, signals=()))
    info = _ok(s, "source_dataset_info")
    assert info["signals"]["momentum"]["status"] == "unavailable"
    assert info["signals"]["radar"]["status"] == "unavailable"
    assert _ok(s, "source_get_alternatives", saas="Notion")["recommendations"]
    r = _ok(s, "source_emerging_projects")
    assert r["repository_momentum"] == [] and any("unavailable" in d for d in r["degraded"])


def test_tampered_signal_is_not_served(tmp_path):
    root = _copy(tmp_path)
    p = root / "data" / "momentum" / "projects.jsonl"
    p.write_bytes(p.read_bytes().replace(b"rising", b"surging", 1))
    s = _server(root)
    st = _ok(s, "source_dataset_info")["signals"]["momentum"]
    assert st["status"] == "unavailable" and "MANIFEST sha256" in st["reason"]
    assert _ok(s, "source_get_momentum", project="Docmost")["repository_momentum"]["status"] == "not_tracked"


def test_old_signal_is_reported_stale():
    later = M.datetime(2026, 10, 20, 12, tzinfo=M.timezone.utc)
    st = _ok(_server(now=later), "source_compare_projects", projects=["Docmost", "AppFlowy"])["signals"]
    assert st["momentum"]["status"] == "stale" and st["momentum"]["stale"] is True


# -- privacy-safe telemetry

def test_telemetry_is_aggregate_only():
    s = _server()
    _ok(s, "source_recommend", need="secret-project-name-1234 wiki")
    _call(s, "source_compare_projects", projects=["only-one"])
    snap = s.telemetry.snapshot()
    text = json.dumps(snap)
    assert "secret-project-name-1234" not in text and "only-one" not in text
    day = snap["days"]["2026-10-10"]
    assert day["source_recommend"]["ok"]["count"] == 1
    assert day["source_compare_projects"]["tool_error"]["count"] == 1
    assert set(day["source_recommend"]["ok"]) == {"count", "latency"}
