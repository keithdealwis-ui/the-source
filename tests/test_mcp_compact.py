"""KEI-961: compact MCP responses. A compact answer (the default) leaves out state, explanatory and evidence blocks
unless asked, and never loses a field that carries a trust obligation: tier, recommended flags, licence and its
disclosure, freshness beside live figures, attribution, and any reason, cap or suppression."""
import json
import re
from pathlib import Path

import pytest

from source_pipeline import mcp_server as M

ROOT = Path(__file__).resolve().parent.parent
HAVE = all((ROOT / p).exists() for p in ("data/canonical/MANIFEST.json", "api/v1/index.json",
                                          "data/momentum/MANIFEST.json", "data/radar/MANIFEST.json"))
pytestmark = pytest.mark.skipif(not HAVE, reason="needs the committed read and signal layers")
NOW = M.datetime(2026, 10, 10, 12, tzinfo=M.timezone.utc)

# One representative call per tool, plus the discovery-bearing and licence-exception variants.
CALLS = [
    ("source_dataset_info", {}),
    ("source_list_supported_saas", {}),
    ("source_get_alternatives", {"saas": "Notion"}),
    ("source_get_alternatives", {"saas": "Slack", "include_discovery": True, "limit": 10}),
    ("source_detect_saas", {"url": "https://www.notion.so/x"}),
    ("source_search", {"query": "password manager"}),
    ("source_search", {"query": "data integration elt", "limit": 20}),
    ("source_get_project", {"project": "Docmost"}),
    ("source_get_project", {"project": "github.com/airbytehq/airbyte"}),
    ("source_get_relationship", {"saas": "Notion", "project": "Docmost"}),
    ("source_compare_projects", {"projects": ["Docmost", "AppFlowy"]}),
    ("source_compare_projects", {"projects": ["github.com/airbytehq/airbyte", "github.com/activepieces/activepieces"]}),
    ("source_recommend", {"need": "team wiki and notes like Notion", "include_discovery": True}),
    ("source_emerging_projects", {}),
    ("source_get_momentum", {"project": "Docmost"}),
]
# The fields INSTRUCTIONS obliges every answer to keep, plus any reason / cap / suppression.
CORE_TRUST = re.compile(r"^(tier|recommended|recommendation|licence_disclosure|licence_label|licence_status|"
                        r"live_checked_at|observed_as_of|warning|caveat|attribution|licence|label|disclosure|"
                        r"shortfall|degraded|uncertainty|verdict)$|_reason$|^reason$|suppress|capped|(^|_)cap(_|$)")
_WHOLE_KEYS = {k for b in M._WHOLE for k in M.COMPACT_BLOCKS[b]}


def _server(**kw):
    s = M.Server(ROOT, NOW, **kw)
    s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    return s


S = _server() if HAVE else None


def _res(s, name, **args):
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return r["result"]


def _sc(s, name, **args):
    r = _res(s, name, **args)
    assert r["isError"] is False, r
    assert json.loads(r["content"][0]["text"]) == r["structuredContent"]
    return r["structuredContent"]


def _trust_paths(node, path=()):
    """(path, value) of every trust field in an answer, outside explanatory text blocks."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _WHOLE_KEYS:
                continue
            if CORE_TRUST.search(k):
                yield path + (k,), v
            yield from _trust_paths(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _trust_paths(v, path + (i,))


def _get(node, path):
    for p in path:
        node = node[p]
    return node


def test_every_tool_takes_detail_and_include_and_says_so():
    tools = {t["name"]: t for t in S.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert set(tools) == set(M.TOOL_BY_NAME) and len(tools) == 11
    for t in tools.values():
        props = t["inputSchema"]["properties"]
        assert props["detail"]["enum"] == ["compact", "full"]
        assert set(props["include"]["items"]["enum"]) == set(M.COMPACT_BLOCKS)
        assert "compact by default" in t["description"] and "detail \"full\"" in t["description"]
    assert "detail" not in tools["source_search"]["inputSchema"]["required"]


@pytest.mark.parametrize("name,args", CALLS, ids=[f"{n}-{i}" for i, (n, _) in enumerate(CALLS)])
def test_compact_keeps_every_trust_field_and_is_smaller(name, args):
    full = _sc(S, name, **args, detail="full")
    compact = _sc(S, name, **args)
    assert compact == _sc(S, name, **args, detail="compact")  # compact is the default
    paths = list(_trust_paths(full["result"]))
    for path, value in paths:
        assert _get(compact["result"], path) == value, (name, path)
    # attribution, licence and version on every answer
    for k in ("dataset_version", "data_as_of", "licence", "attribution", "notice"):
        assert compact["meta"][k] == full["meta"][k]
    assert compact["meta"]["attribution"] == M.ATTRIBUTION and compact["meta"]["licence"] == "CC BY 4.0"
    assert compact["response"]["detail"] == "compact" and "meta" in compact["response"]["left_out"]
    assert set(compact["response"]["left_out"]) <= set(M.COMPACT_BLOCKS)
    assert "response" not in full
    assert len(_res(S, name, **args)["content"][0]["text"]) < len(_res(S, name, **args, detail="full")["content"][0]["text"])


def test_full_is_exactly_the_old_answer():
    old = _server(default_detail="full")
    for name, args in CALLS:
        assert _res(old, name, **args) == _res(S, name, **args, detail="full")
        text = _res(old, name, **args)["content"][0]["text"]
        assert text == json.dumps(_res(old, name, **args)["structuredContent"], sort_keys=True,
                                  ensure_ascii=False, indent=1)
    with pytest.raises(ValueError):
        M.Server(ROOT, NOW, default_detail="tiny")


def test_licence_disclosure_survives_compact_wherever_the_project_appears():
    pid = "github.com/airbytehq/airbyte"
    disclosure = S.layer.disclosures[pid]
    p = _sc(S, "source_get_project", project=pid)["result"]
    assert p["project"]["licence_disclosure"] == disclosure and p["tier"] in M.TIERS
    c = _sc(S, "source_compare_projects", projects=[pid, "Docmost"])["result"]
    assert c["projects"][0]["licence_disclosure"] == disclosure
    for sid in {r["saas_id"] for r in S.layer.rel_by_project[pid] if r["recommended"]}:
        recs = _sc(S, "source_get_alternatives", saas=sid, limit=50)["result"]["recommendations"]
        assert all(r["licence_disclosure"] == disclosure for r in recs if r["oss_id"] == pid)


def test_discovery_rows_keep_tier_label_and_freshness():
    r = _sc(S, "source_search", query="password manager", tier="discovery", limit=10)["result"]
    assert r["results"]
    for row in r["results"]:
        assert row["tier"] == "discovery" and row["project"]["label"] == M.DISCOVERY_LABEL
        assert row["project"]["observed_as_of"]
        qs = row["project"].get("quick_scores")
        if qs:
            assert qs["recommendation"] is False and qs["label"] == "quick"
            assert "note" not in qs  # the long quick-score note is explanatory
        assert "topics" not in row["project"] and "matched" not in row
    assert "corpus" in r and r["corpus"]["status"] and "as_of" in r["corpus"]  # cut down, not dropped
    assert "tiers" not in r and "ranking" not in r


def test_signal_state_is_cut_to_freshness_not_dropped():
    full = _sc(S, "source_compare_projects", projects=["Docmost", "AppFlowy"], detail="full")["result"]
    compact = _sc(S, "source_compare_projects", projects=["Docmost", "AppFlowy"])["result"]
    for n in M.SIGNALS:
        assert set(compact["signals"][n]) <= {"status", "reason", "as_of", "age_hours", "stale", "stale_after_hours",
                                              "source_status"}
        for k in ("status", "as_of", "stale"):
            assert compact["signals"][n][k] == full["signals"][n][k]
    assert "measures" not in compact and "note" not in compact
    assert compact["projects"] and all("tier" in p for p in compact["projects"])


def test_include_brings_named_blocks_back():
    full = _sc(S, "source_compare_projects", projects=["Docmost", "AppFlowy"], detail="full")
    got = _sc(S, "source_compare_projects", projects=["Docmost", "AppFlowy"], include=["signals", "notes", "meta"])
    assert got["result"]["signals"] == full["result"]["signals"]
    assert got["result"]["measures"] == full["result"]["measures"] and got["meta"] == full["meta"]
    assert not {"signals", "notes", "meta"} & set(got["response"]["left_out"])
    alt = _sc(S, "source_get_alternatives", saas="Notion", include=["evidence"])["result"]
    page = json.loads((ROOT / "api" / "v1" / "saas" / "notion.json").read_text())
    assert alt["recommendations"][0]["evidence"] == page["recommendations"][0]["evidence"]


def test_hybrid_search_keeps_its_score_components_in_compact():
    r = _sc(S, "source_search", query="open source password manager", mode="hybrid", limit=5)
    assert all("score_components" in x for x in r["result"]["results"])
    assert "retrieval" not in r["response"]["left_out"]


def test_bad_detail_or_include_is_a_tool_error():
    for args in ({"detail": "tiny"}, {"include": ["everything"]}, {"include": "signals"}):
        r = _res(S, "source_dataset_info", **args)
        assert r["isError"] is True, args


def test_new_reason_cap_and_suppression_fields_survive_generically():
    """Fields other work adds later (a corroboration cap with its reason, a momentum suppression, a pass rate)
    survive compact output without this module knowing their names."""
    payload = {"meta": {"dataset_version": "x", "licence": "CC BY 4.0", "attribution": M.ATTRIBUTION,
                        "api_version": "v1", "snapshot_current": False},
               "result": {
                   "recommendation_pass_rate": {"passed": 9, "of": 10},  # a new top-level field
                   "signals": {"momentum": {"status": "ok", "as_of": "t", "manifest_sha256": "abc",
                                            "suppressed": True, "suppression_reason": "bot burst"}},
                   "recommendations": [{"oss_id": "a", "tier": "recommended",
                                        "replacement_fit": {"score": 70, "capped": True, "cap_reason": "uncorroborated"},
                                        "evidence": {"replacement_fit": {"dimensions": {"x": 1},
                                                                         "corroboration_cap": 70,
                                                                         "score_cap_reason": "one source"}},
                                        "explanation": ["long"], "note": "boilerplate", "topics": ["a"]}],
                   "ranking": "how it ranks"}}
    out = M.compact_payload("source_get_alternatives", payload)
    r = out["result"]
    assert r["recommendation_pass_rate"] == {"passed": 9, "of": 10}
    assert r["signals"] == {"momentum": {"status": "ok", "as_of": "t", "suppressed": True,
                                         "suppression_reason": "bot burst"}}
    row = r["recommendations"][0]
    assert row["replacement_fit"] == {"score": 70, "capped": True, "cap_reason": "uncorroborated"}
    assert row["evidence"] == {"replacement_fit": {"corroboration_cap": 70, "score_cap_reason": "one source"}}
    assert not {"explanation", "note", "topics"} & set(row) and "ranking" not in r
    assert out["meta"] == {"dataset_version": "x", "licence": "CC BY 4.0", "attribution": M.ATTRIBUTION,
                           "snapshot_current": False}
    assert set(out["response"]["left_out"]) == {"meta", "signals", "evidence", "explanations", "notes", "topics"}


def test_a_rows_own_quick_scores_and_the_search_scope_are_kept():
    pid = next(p for p in sorted(S.layer.corpus) if p in S.layer.quick_health)
    full = _sc(S, "source_get_project", project=pid, detail="full")["result"]
    compact = _sc(S, "source_get_project", project=pid)["result"]
    want = {k: v for k, v in full["project"]["quick_scores"]["quick_health"].items() if k != "confidence_meaning"}
    assert compact["project"]["quick_scores"]["quick_health"] == want  # its scores and components, less the gloss
    r = _sc(S, "source_search", query="analytics")["result"]
    assert r["searched"].startswith("full corpus")  # says when the discovery corpus is not searched
    info = _sc(S, "source_dataset_info")["result"]
    assert info["corpus"]["projects_served"] > 9000 and set(info["tiers"]["counts"]) == set(M.TIERS)
