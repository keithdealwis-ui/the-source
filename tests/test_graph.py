"""Tests for the knowledge graph (KEI-844). Each class names the criterion it guards.

Most tests build a small synthetic corpus in memory, so they need no network and no built
data. TestBuiltGraph runs only when data/graph/ has been built.
"""
import copy

import pytest

from source_pipeline import common, graph_build, graph_export
from source_pipeline.graph_build import Builder, build, category_key, scoring_inputs
from source_pipeline.graph_model import RELATIONS, edge_id
from source_pipeline.graph_retrieval import Executor, Graph, RetrievalIndex

AS_OF = "2026-09-29T10:00:00Z"
ONTOLOGY = common.load_yaml(common.CONFIG / "graph_ontology.yaml")


def project(pid, canonical, *, desc="", topics=(), langs=(), owner=("acme", "Organization"), lanes=None,
            replace=None, aliases=None, lists=(), spdx="MIT"):
    login, otype = owner
    return {
        "id": pid, "canonical": canonical, "url": f"https://{canonical}", "aliases": aliases or [canonical],
        "descriptive": {"description": desc, "homepage": None},
        "discovery": {"depth": 1, "distinct_routes": 1, "strategies": ["topic_search"]},
        "entities": {"languages": [f"language:{x}" for x in langs],
                     "owner": {"id": f"github-owner:{login.lower()}", "login": login, "type": otype},
                     "topics": [f"github-topic:{t}" for t in topics]},
        "freshness": {"history": [], "observed_as_of": AS_OF},
        "lanes": lanes or {"discover": True, "replace": False},
        "live": {"canonical_name": canonical.split("/", 1)[1], "provider": canonical.split(".")[0],
                 "stars": 10, "forks": 1, "fetched_at": AS_OF, "created_at": "2024-01-01T00:00:00Z",
                 "pushed_at": AS_OF},
        "outcome": "recommendation_eligible", "recommendation_eligible": True,
        "provenance": [{"source": f"{x}@abc", "locator": "line 1", "strategy": "curated_list", "depth": 1}
                       for x in lists],
        "quality": {"licence": {"recognised": bool(spdx), "spdx": spdx, "status": "recognised" if spdx else "unrecognised",
                                "detection": {"method": "host_api"}},
                    "maintenance": {"status": "active", "days_since_meaningful_activity": 3}},
        "replace": replace,
    }


def kei805_edge(oss_id, saas_id, status="validated", sources=1, review="left standing"):
    return {"edge_id": f"{saas_id}--{oss_id}", "oss_id": oss_id, "saas_id": saas_id, "lane": "recommendable",
            "recommendation_eligible": status == "validated", "corroborated": sources > 1, "source_count": sources,
            "provenance": [{"source_id": f"s{i}", "source_commit": "c", "locator": "L1"} for i in range(sources)],
            "validation": {"status": status, "validated_as_of": AS_OF, "plausibility_review": {"outcome": review}}}


@pytest.fixture()
def inputs():
    projects = [
        project("github:1", "github.com/acme/notes", desc="A self-hosted Notion alternative with a REST API",
                topics=["self-hosted", "notion", "knowledge-base"], langs=["TypeScript", "Dockerfile"],
                lanes={"discover": True, "replace": True},
                replace={"kei805_oss_id": "github.com/acme/notes", "saas_edge_ids": []},
                aliases=["github.com/acme/notes", "github.com/acme/old-notes"]),
        project("github:2", "github.com/bob/wiki", desc="Personal wiki and notes app", topics=["wiki", "golang"],
                langs=["Go"], owner=("bob", "User"), lanes={"discover": False, "replace": True},
                replace={"kei805_oss_id": "github.com/bob/wiki", "saas_edge_ids": []}),
        project("github:3", "github.com/acme/agent", desc="MCP server for AI agents to read Notion pages",
                topics=["mcp-server", "notion", "ai-agents"], langs=["Python"],
                lists=["list:punkpeye/awesome-mcp-servers"]),
        project("github:4", "github.com/zed/mirror", desc="Mirror", lanes={"discover": False, "replace": True},
                replace={"kei805_oss_id": "gitlab.com/zed/mirror", "saas_edge_ids": []}, spdx=None),
        project("kei805:gitlab.com/zed/mirror", "gitlab.com/zed/mirror", lanes={"discover": False, "replace": True},
                replace={"kei805_oss_id": "gitlab.com/zed/mirror2", "saas_edge_ids": []}),
    ]
    return {
        "manifest": {"as_of": AS_OF},
        "projects": projects,
        "nodes": [],
        "candidates": [{"from": "github:3", "relation": "readme_links_to", "to": "github:1",
                        "evidence": {"locator": "line 9"}},
                       {"from": "github:3", "relation": "readme_links_to", "to": "github:999",
                        "evidence": {"locator": "line 10"}}],
        "history": [{"id": "github:1", "as_of": "2026-08-01T00:00:00Z", "stars": 5},
                    {"id": "github:1", "as_of": AS_OF, "stars": 10}],
        "kei805_manifest": {"sha256": {}},
        "saas": [{"saas_id": "notion", "name": "Notion", "domain": "notion.so", "category": "Note taking",
                  "aliases": ["Notion.so"], "imported": {"spellings_seen": ["Notion"]}},
                 {"saas_id": "airtable", "name": "Airtable", "domain": "airtable.com", "category": "No-code database",
                  "aliases": [], "imported": {}}],
        "kei805_edges": [kei805_edge("github.com/acme/notes", "notion", sources=3),
                         kei805_edge("github.com/bob/wiki", "notion", review="not reviewed"),
                         kei805_edge("github.com/acme/agent", "notion", status="rejected"),
                         kei805_edge("gitlab.com/zed/mirror", "airtable")],
        "kei805_oss": [{"oss_id": "github.com/bob/wiki",
                        "imported": {"categories": [{"source_id": "altstack", "value": "Notes Taking"},
                                                    {"source_id": "btwso", "value": "note-taking"}]}}],
        "kei805_live": {"as_of": "2026-09-27T00:00:00Z", "projects": {
            "github.com/bob/wiki": {"found": True, "fetched_at": "2026-09-27T00:00:00Z", "stars": 7, "observed": {}}}},
        "ontology": copy.deepcopy(ONTOLOGY),
    }


def edges_of(out, relation):
    return [e for e in out["edges"] if e["relation"] == relation]


class TestDeterministicIngestion:
    """AC1: the corpus ingests deterministically."""

    def test_same_inputs_same_records(self, inputs):
        a, b = build(copy.deepcopy(inputs)), build(copy.deepcopy(inputs))
        for k in ("entities", "edges", "observations", "identity", "excluded"):
            assert common.dumps(a[k]) == common.dumps(b[k])

    def test_every_project_is_an_entity_or_an_alias(self, inputs):
        out = build(inputs)
        ids = {e["id"] for e in out["entities"]}
        aliases = {i["alias"]: i["entity"] for i in out["identity"] if i["type"] == "Project"}
        for p in inputs["projects"]:
            assert p["id"] in ids or aliases.get(p["id"].lower()) in ids


class TestIdentity:
    """AC2: renames, mirrors, owners, categories, technologies and SaaS resolve to one entity."""

    def test_renamed_address_resolves(self, inputs):
        out = build(inputs)
        alias = {i["alias"]: i["entity"] for i in out["identity"] if i["type"] == "Project"}
        assert alias["github.com/acme/old-notes"] == "github:1"

    def test_cross_host_mirror_is_one_project(self, inputs):
        out = build(inputs)
        ids = {e["id"] for e in out["entities"]}
        assert "kei805:gitlab.com/zed/mirror" not in ids and "github:4" in ids
        rep = edges_of(out, "REPLACES")
        assert any(e["from"] == "github:4" and e["to"] == "saas:airtable" for e in rep)

    def test_category_labels_merge_across_schemes(self, inputs):
        out = build(inputs)
        cats = [e for e in out["entities"] if e["type"] == "Category"]
        note = [c for c in cats if c["id"] == "category:note-taking"]
        assert len(note) == 1
        assert {x["scheme"] for x in note[0]["attrs"]["labels"]} == {"catalogue", "altstack", "btwso"}
        assert note[0]["name"] == "Note taking"

    def test_category_key(self):
        assert category_key("Notes Taking") == category_key("note-taking") == category_key("Note taking")
        assert category_key("Identity & Access") == category_key("identity and access")
        assert category_key("Business") != category_key("Busines Intelligence")

    def test_technology_spellings(self, inputs):
        out = build(inputs)
        alias = {(i["type"], i["alias"]): i["entity"] for i in out["identity"]}
        assert alias[("Technology", "topic:golang")] == alias[("Technology", "language:go")] == "technology:go"

    def test_alias_conflict_is_refused(self, inputs):
        b = Builder(inputs)
        b.entity("saas:a", "A")
        b.entity("saas:b", "B")
        b.alias("SaaSProduct", "x", "saas:a", "t")
        with pytest.raises(ValueError, match="identity conflict"):
            b.alias("SaaSProduct", "X", "saas:b", "t")


class TestRelationships:
    """AC3/AC4: typed, directed, evidence-backed, with confidence where inferred."""

    def test_registry_is_complete(self):
        for spec in RELATIONS.values():
            for f in ("from", "to", "reads", "semantics", "assertion", "evidence", "confidence"):
                assert spec.get(f), f

    def test_wrong_direction_and_missing_evidence_refused(self, inputs):
        b = Builder(inputs)
        b.entity("saas:notion", "Notion")
        b.entity("github:1", "p")
        with pytest.raises(ValueError, match="not allowed"):
            b.edge("REPLACES", "saas:notion", "github:1", "explicit", 1.0, [{"ref": "x:y#z"}])
        with pytest.raises(ValueError, match="needs evidence"):
            b.edge("REPLACES", "github:1", "saas:notion", "explicit", 1.0, [])

    def test_symmetric_edge_stored_once(self):
        assert edge_id("SIMILAR_TO", "github:9", "github:1") == edge_id("SIMILAR_TO", "github:1", "github:9")

    def test_every_edge_has_evidence_and_inferred_below_one(self, inputs):
        out = build(inputs)
        for e in out["edges"]:
            assert e["evidence"] and all(x["ref"] for x in e["evidence"])
            if e["assertion"] == "inferred":
                assert e["confidence"] < 1

    def test_replaces_confidence_rule(self):
        assert Builder.replaces_confidence(kei805_edge("a", "b", sources=1)) == 0.7
        assert Builder.replaces_confidence(kei805_edge("a", "b", sources=2)) == 0.8
        assert Builder.replaces_confidence(kei805_edge("a", "b", sources=3, review="not reviewed")) == 0.8

    def test_several_routes_keep_all_evidence(self, inputs):
        out = build(inputs)
        e = next(e for e in out["edges"] if e["id"] == "github:1|HAS_CAPABILITY|capability:self-hosting")
        assert e["confidence"] == 0.9 and len(e["evidence"]) >= 3   # topic + keyword, each with its ontology ref

    def test_unknown_stays_unknown(self, inputs):
        out = build(inputs)
        assert not edges_of(out, "DEPENDS_ON")
        assert not any(e["from"] == "github:4" for e in edges_of(out, "LICENSED_UNDER"))
        maint = {e["from"] for e in edges_of(out, "MAINTAINED_BY")}
        assert maint == {"github:2"}                   # the only user-owned project

    def test_integration_is_not_inferred_for_an_alternative(self, inputs):
        out = build(inputs)
        integ = {(e["from"], e["to"]) for e in edges_of(out, "INTEGRATES_WITH")}
        assert ("github:3", "saas:notion") in integ
        assert ("github:1", "saas:notion") not in integ    # it replaces Notion; its topic is not an integration

    def test_links_only_between_retained_projects(self, inputs):
        out = build(inputs)
        assert [e["to"] for e in edges_of(out, "LINKS_TO")] == ["github:1"]
        assert any(x["claim"] == "LINKS_TO" and x["reason"] == "endpoint_not_retained" for x in out["excluded"])


class TestLanes:
    """AC5: Replace and Discover stay distinguishable; nothing forces a SaaS edge."""

    def test_rejected_claim_never_becomes_replaces(self, inputs):
        out = build(inputs)
        assert not any(e["from"] == "github:3" for e in edges_of(out, "REPLACES"))
        assert any(x["reason"] == "kei805_status_rejected" for x in out["excluded"])

    def test_discover_only_projects_have_no_replaces(self, inputs):
        out = build(inputs)
        ents = {e["id"]: e for e in out["entities"]}
        for e in edges_of(out, "REPLACES"):
            assert ents[e["from"]]["attrs"]["lanes"]["replace"]


class TestLongitudinal:
    """AC7: observations accumulate; trends need enough history."""

    def test_observations_from_both_sources(self, inputs):
        out = build(inputs)
        ids = [o["id"] for o in out["observations"]]
        assert len(ids) == len(set(ids))
        assert {o["source"] for o in out["observations"]} == {"kei811-history", "kei805-live"}

    def test_trend_measured_only_with_span(self, inputs):
        out = build(inputs)
        ents = {e["id"]: e for e in out["entities"]}
        t1 = ents["github:1"]["attrs"]["trend"]
        assert t1["status"] == "measured" and t1["stars_delta"] == 5
        assert ents["github:3"]["attrs"]["trend"]["status"] == "insufficient_history"


class TestRetrievalAndQueries:
    """AC6/AC8/AC9/AC10: hybrid retrieval, multi-hop traversal, evidence on every result."""

    def executor(self, inputs):
        out = build(inputs)
        return Executor(Graph(out["entities"], out["edges"]), out["index"])

    def test_documents_are_hybrid(self, inputs):
        out = build(inputs)
        d = out["index"].docs["github:1"]
        assert d["vector"]["model"] and d["facets"]["lanes"]["replace"] and "REPLACES" in d["graph"]

    def test_multi_hop_require_returns_the_chain(self, inputs):
        ex = self.executor(inputs)
        res = ex.run({"start": {"type": "Project"}, "steps": [
            {"op": "require", "path": [{"relation": "REPLACES"},
                                       {"relation": "BELONGS_TO_CATEGORY", "to": "category:note-taking"}]}]})
        assert {r["id"] for r in res["results"]} == {"github:1", "github:2"}
        for r in res["results"]:
            assert [e["relation"] for e in r["evidence"]["edges"]] == ["REPLACES", "BELONGS_TO_CATEGORY"]

    def test_traverse_group_and_exclude(self, inputs):
        ex = self.executor(inputs)
        res = ex.run({"start": {"ids": ["saas:notion"]}, "steps": [
            {"op": "traverse", "relation": "REPLACES", "direction": "in"},
            {"op": "exclude", "path": [{"relation": "OWNED_BY", "to": "org:github:acme"}]}]})
        assert [r["id"] for r in res["results"]] == ["github:2"]
        grp = ex.run({"start": {"type": "Project"}, "steps": [{"op": "group", "relation": "OWNED_BY", "min_count": 2}]})
        assert grp["results"][0]["id"] == "org:github:acme" and grp["results"][0]["member_count"] >= 2

    def test_text_step_scores_and_explains(self, inputs):
        ex = self.executor(inputs)
        res = ex.run({"start": {"type": "Project"}, "steps": [{"op": "text", "query": "mcp server notion", "min_score": 0.05},
                                                               {"op": "rank", "by": "score"}]})
        assert res["results"][0]["id"] == "github:3"
        assert res["results"][0]["evidence"]["semantic"][0]["matched_terms"]


class TestStorageIndependence:
    """AC11: the canonical records round-trip through a different storage shape."""

    def test_property_graph_round_trip(self, inputs, tmp_path):
        out = build(inputs)
        graph_export.property_graph_csv(out["entities"], out["edges"], tmp_path)
        ents, edges = graph_export.read_property_graph_csv(tmp_path)
        strip = [{k: v for k, v in e.items() if k not in ("observed_as_of", "first_observed")} for e in out["edges"]]
        assert common.dumps(ents) == common.dumps(out["entities"])
        assert common.dumps(edges) == common.dumps(strip)


class TestScoringView:
    """AC12: scoring consumes entities, edges and confidence directly."""

    def test_replacement_fit_only_with_a_saas_edge(self, inputs):
        out = build(inputs)
        rows = {r["project"]: r for r in scoring_inputs(out["entities"], out["edges"], out["observations"])}
        assert rows["github:1"]["replacement_fit_applicable"]
        assert rows["github:1"]["replaces"][0]["confidence"] == 0.9
        assert not rows["github:3"]["replacement_fit_applicable"]
        assert not any(r["endorsed"] for r in rows.values())


BUILT = (graph_build.GRAPH / "reports" / "validation_report.json").exists()


@pytest.mark.skipif(not BUILT, reason="data/graph/ not built")
class TestBuiltGraph:
    """The real graph: the acceptance report passed and its query suite held."""

    def test_validation_report_passes(self):
        report = common.read_json(graph_build.GRAPH / "reports" / "validation_report.json")
        assert report["ok"], [c["id"] for c in report["checks"] if not c["ok"]]

    def test_query_suite(self):
        q = common.read_json(graph_build.GRAPH / "reports" / "query_results.json")
        assert q["queries"] >= 20 and q["ok"] and q["multi_hop"] >= 5

    def test_retrieval_index_loads(self):
        ix = RetrievalIndex.load(graph_build.GRAPH / "retrieval")
        assert ix.text_scores("self-hosted notion alternative")
