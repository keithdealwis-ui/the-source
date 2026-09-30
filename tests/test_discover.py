"""Tests for the Discover lane (KEI-811). Each class names the criterion it guards."""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from source_pipeline import common, discover, discover_build
from source_pipeline.activity import iso
from source_pipeline.discover_universe import (all_routes, depth1_routes, extract_github_repos, order_for_cap,
                                               parse_seed_file, rank_topics, recursion_frontier, slug_key)
from source_pipeline.enrich import policy_fingerprint

AS_OF = datetime(2026, 9, 29, tzinfo=timezone.utc)
WINDOW = iso(AS_OF - timedelta(days=365))
CFG = discover.load_cfg()
POLICY = common.load_policy()


def ago(days):
    return iso(AS_OF - timedelta(days=days))


class TestSeedIngestion:
    """AC3: Keith's pasted corpus is the input; no per-project ingestion."""

    def test_table_rows_and_bare_urls(self):
        text = ("| Project | Repository / source | Category |\n| -- | -- | -- |\n"
                "| LibreChat | https://github.com/danny-avila/LibreChat | AI Workspace |\n"
                "| Meta advertising skills | Canonical upstream to verify | Paid Media |\n"
                "also https://github.com/tobi/qmd\n")
        rows = parse_seed_file(text)
        assert [r["slug"] for r in rows] == ["danny-avila/librechat", None, "tobi/qmd"]
        assert rows[1]["unresolved_reason"] == "no_repository_url_in_seed_row"
        assert rows[0]["category"] == "AI Workspace" and rows[0]["line"] == 3

    def test_real_seed_file_parses_every_row(self):
        rows = parse_seed_file((common.ROOT / CFG["seeds"]["keith_seed_file"]).read_text())
        assert len(rows) == 73
        assert sum(1 for r in rows if r["slug"]) == 72
        assert len({r["slug"] for r in rows if r["slug"]}) == 72


class TestLinkExtraction:
    """AC4/AC7: links normalise to one address per repository."""

    def test_normalises_and_deduplicates(self):
        text = ("see https://github.com/Foo/Bar.git and github.com/foo/bar/tree/main\n"
                "[x](https://github.com/sponsors/foo) https://github.com/topics/ai https://github.com/a/b.\n")
        assert extract_github_repos(text) == [("a/b", 2), ("foo/bar", 1)]

    def test_non_repository_paths(self):
        assert slug_key("orgs", "x") is None
        assert slug_key("foo", "followers") is None
        assert slug_key("github", "github-mcp-server") == "github/github-mcp-server"


def frontier_fixture():
    return {
        "as_of": iso(AS_OF),
        "config_sha256": discover.cfg_fingerprint(CFG),
        "seed_file_sha256": "0" * 64,
        "seeds": {
            "keith": [{"line": 1, "name": "A", "source_text": "", "category": None, "slug": "o1/a"},
                      {"line": 2, "name": "Meta", "source_text": "", "category": None, "slug": None,
                       "unresolved_reason": "no_repository_url_in_seed_row"}],
            "accounts": {"k": {"starred": ["o1/a"], "watching": [], "starred_status": "ok", "watching_status": "ok"}},
            "replace": ["r/replace"],
        },
        "seed_meta": {},
        "strategies": {
            "owner_neighbourhood": {"o1": {"status": "ok", "repos": ["o1/a", "o1/b", "o1/fork"], "via": ["o1/a"]}},
            "readme_links": {"o1/a": {"status": "ok", "links": [["x/linked", 4], ["o1/b", 5], ["o1/a", 9]]}},
            "topic_search": {"ranked_topics": [["mcp", 1]], "results": {"mcp": {"query": "q", "status": "ok",
                             "total_count": 3, "repos": ["o1/b", "x/stale", "x/renamed-old", "r/replace"]}}},
            "curated_lists": {"l/list": {"decision": "allow", "commit": "c" * 40, "links": [["x/archived", 3],
                                                                                         ["x/renamed-new", 7]]},
                              "l/refused": {"decision": "refuse", "commit": "d" * 40, "links": [["x/never", 1]]}},
            "trending": {"t": {"query": "q", "status": "ok", "repos": ["x/linked"]}},
            "recursion": {"frontier": ["o1/b"], "owner_neighbourhood": {},
                          "readme_links": {"o1/b": {"status": "ok", "links": [["y/deep", 2]]}}},
        },
    }


def meta(slug, dbid, **kw):
    base = {"found": True, "name_with_owner": slug, "canonical_slug": slug, "database_id": dbid,
            "archived": False, "disabled": False, "empty": False, "is_fork": False, "is_mirror": False,
            "private": False, "parent": None, "stars": 100, "forks": 1, "pushed_at": ago(3),
            "created_at": ago(900), "description": "d", "homepage": None, "licence_spdx": "MIT",
            "licence_name": "MIT License", "primary_language": "Go", "languages": ["Go"], "topics": ["mcp"],
            "owner": {"login": slug.split("/")[0], "type": "User"}, "latest_release": None}
    base.update(kw)
    return base


def deep(slug, commit_days=3, paths=("src/main.go",), spdx="MIT"):
    return {"provider": "github", "fetched_at": iso(AS_OF), "found": True, "canonical_name": slug,
            "canonical_id": "github.com/" + slug, "host_repo_id": 1, "archived": False, "disabled": False,
            "empty": False, "is_fork": False, "is_mirror": False, "licence_spdx": spdx, "licence_name": spdx,
            "stars": 100, "forks": 1, "pushed_at": ago(3), "created_at": ago(900), "default_branch": "main",
            "observations": {"commits": [{"ref": "c" * 40, "at": ago(commit_days), "author": "alice",
                                          "paths": list(paths)}],
                             "merged_prs": [], "releases": [], "tags": [], "commits_in_window": 5,
                             "issues_closed_12m": 2}}


def live_fixture():
    m = {
        "o1/a": meta("o1/a", 1),
        "o1/b": meta("o1/b", 2),
        "o1/fork": meta("o1/fork", 3, is_fork=True, parent={"slug": "o1/a", "database_id": 1}),
        "x/linked": meta("x/linked", 4),
        "x/stale": meta("x/stale", 5, pushed_at=ago(500)),
        "x/archived": meta("x/archived", 6, archived=True),
        "x/renamed-old": meta("x/renamed-new", 7),
        "x/renamed-new": meta("x/renamed-new", 7),
        "y/deep": meta("y/deep", 8),
        "r/replace": meta("r/replace", 99),
    }
    return {
        "complete": True, "as_of": iso(AS_OF), "window_start": WINDOW, "maintenance_policy_sha256": policy_fingerprint(POLICY),
        "frontier_as_of": iso(AS_OF), "universe_evaluated": len(m), "http_calls": 0, "lookup_failures": [],
        "meta": m,
        "deep": {"o1/a": deep("o1/a"), "o1/b": deep("o1/b", paths=("README.md",)),
                 "x/linked": deep("x/linked", spdx=None), "x/renamed-new": deep("x/renamed-new"),
                 "y/deep": deep("y/deep")},
    }


def replace_record():
    return {"oss_id": "github.com/r/replace", "live": {"host_repo_id": 99, "canonical_name": "r/replace",
            "provider": "github", "found": True, "archived": False},
            "derived": {"lane": "recommendable", "evaluated_as_of": "2026-09-27T05:08:30Z",
                        "eligibility": {"recommended": True, "exclusion_reasons": []},
                        "maintenance": {"status": "active", "evidence": [{"kind": "code_commit"}]},
                        "licence": {"spdx": "MIT", "recognised": True, "status": "recognised", "usable": True}}}


@pytest.fixture
def built(tmp_path, monkeypatch):
    d = tmp_path / "discover"
    dataset = tmp_path / "dataset"
    monkeypatch.setattr(discover_build, "FRONTIER", d / "frontier.json")
    monkeypatch.setattr(discover_build, "LIVE", d / "live.json")
    monkeypatch.setattr(discover_build, "HISTORY", d / "history" / "observations.jsonl")
    monkeypatch.setattr(discover_build, "CORPUS", d / "corpus")
    monkeypatch.setattr(common, "DATASET", dataset)
    common.write_json(d / "frontier.json", frontier_fixture())
    live = live_fixture()
    common.write_json(d / "live.json", live)
    common.write_jsonl(d / "history" / "observations.jsonl",
                       [discover.history_row(live["meta"]["o1/a"], live["deep"]["o1/a"], "2026-09-01T00:00:00Z"),
                        discover.history_row(live["meta"]["o1/a"], live["deep"]["o1/a"], iso(AS_OF))])
    common.write_jsonl(dataset / "oss_projects.jsonl", [replace_record()])
    common.write_jsonl(dataset / "relationships.jsonl", [{"oss_id": "github.com/r/replace", "saas_id": "slack",
                       "edge_id": "slack--github.com/r/replace", "lane": "recommendable",
                       "recommendation_eligible": True}])
    common.write_json(dataset / "MANIFEST.json", {})
    summary = discover_build.run()
    corpus = d / "corpus"
    return {"summary": summary, "corpus": corpus,
            "projects": {p["canonical"]: p for p in common.read_jsonl(corpus / "projects.jsonl")},
            "rejections": common.read_jsonl(corpus / "rejections.jsonl"),
            "edges": common.read_jsonl(corpus / "relationship_candidates.jsonl"),
            "coverage": common.read_json(corpus / "seed_coverage.json")}


class TestUniverse:
    """AC4: bounded, reproducible recursion over recorded strategies."""

    def test_routes_and_refused_list(self):
        routes = all_routes(frontier_fixture(), CFG)
        assert "x/never" not in routes                    # refused list contributes nothing
        assert {r["strategy"] for r in routes["o1/a"]} >= {"keith_seed", "github_starred", "owner_neighbourhood"}
        assert min(r["depth"] for r in routes["y/deep"]) == 2
        assert all(r["source"] != "readme:o1/a" for r in routes["o1/a"])  # no self-links

    def test_recursion_frontier_needs_two_routes(self):
        routes = depth1_routes(frontier_fixture(), CFG)
        assert recursion_frontier(routes, CFG) == ["o1/b", "x/linked"]   # 3 routes, then 2

    def test_cap_order_is_deterministic(self):
        routes = all_routes(frontier_fixture(), CFG)
        shuffled = dict(reversed(list(routes.items())))
        assert order_for_cap(routes) == order_for_cap(shuffled)
        assert order_for_cap(routes)[0] == "o1/a"

    def test_topic_ranking(self):
        cfg = copy.deepcopy(CFG)
        seed_meta = {"a": {"topics": ["mcp", "seo", "python"]}, "b": {"topics": ["mcp"]}}
        assert rank_topics(seed_meta, cfg) == [["mcp", 2], ["seo", 1]]   # python is a stop topic


class TestPrefilter:
    """AC5: metadata rejections are sufficient reasons under the KEI-805 policy, not new rules."""

    def test_reasons(self):
        assert discover.prefilter(meta("a/a", 1), WINDOW, CFG) == []
        assert discover.prefilter(meta("a/a", 1, archived=True), WINDOW, CFG) == ["archived"]
        assert discover.prefilter(meta("a/a", 1, pushed_at=ago(400)), WINDOW, CFG) == \
            ["stale_no_meaningful_activity_12_months"]
        assert discover.prefilter(meta("a/a", 1, is_fork=True), WINDOW, CFG) == ["fork_alias"]
        assert discover.prefilter({"found": False, "error": "not_found"}, WINDOW, CFG) == ["repository_not_found"]
        assert discover.prefilter({"found": False, "error": "graphql_http_502"}, WINDOW, CFG) == \
            ["live_verification_failed"]


class TestBuild:
    def test_lanes(self, built):
        p = built["projects"]
        assert p["github.com/r/replace"]["lanes"] == {"replace": True, "discover": True}      # AC1 + topic route
        assert p["github.com/r/replace"]["quality"]["basis"] == "kei805"
        assert p["github.com/o1/a"]["lanes"] == {"replace": False, "discover": True}           # AC2
        assert p["github.com/o1/a"]["replace"] is None

    def test_quality_bar(self, built):
        p, rej = built["projects"], built["rejections"]
        assert p["github.com/o1/a"]["recommendation_eligible"] is True
        assert p["github.com/x/linked"]["outcome"] == "retained"                              # licence lane
        assert p["github.com/x/linked"]["quality"]["quality_lane"] == "licence_exception"
        reasons = {r["candidate"]: r["reasons"] for r in rej}
        assert reasons["o1/b"] == ["stale_no_meaningful_activity_12_months"]                  # docs-only churn
        assert reasons["x/stale"] == ["stale_no_meaningful_activity_12_months"]
        assert reasons["x/archived"] == ["archived"]
        assert reasons["o1/fork"] == ["fork_alias"]
        fork = next(r for r in rej if r["candidate"] == "o1/fork")
        assert fork["evidence"]["parent_id"] == "github:1"                                     # AC7

    def test_renamed_repository_is_one_record(self, built):
        p = built["projects"]["github.com/x/renamed-new"]                                      # AC7
        assert p["aliases"] == ["github.com/x/renamed-new", "github.com/x/renamed-old"]
        assert p["discovery"]["strategies"] == ["curated_list", "topic_search"]

    def test_provenance_and_endorsement(self, built):
        for p in built["projects"].values():                                                  # AC6, AC12
            assert p["provenance"] and p["entry_reason"]
            assert p["endorsed"] is False and p["publication_status"] == "internal_not_published"
        assert built["projects"]["github.com/y/deep"]["discovery"]["depth"] == 2

    def test_seed_coverage(self, built):
        cov = built["coverage"]                                                                # AC3, AC8
        assert [c["outcome"] for c in cov] == ["recommendation_eligible", "rejected"]
        assert cov[1]["reasons"] == ["seed_unresolved"]

    def test_graph_edges(self, built):
        rels = {(e["from"], e["relation"], e["to"]) for e in built["edges"]}                  # AC9
        assert ("github:1", "readme_links_to", "github:4") in rels
        assert ("github:1", "owned_by", "github-owner:o1") in rels
        assert ("github:99", "saas_replacement_candidate", "saas:slack") in rels
        assert ("github:7", "listed_in", "list:l/list@" + "c" * 40) in rels

    def test_history_is_appended_not_overwritten(self, built):
        hist = built["projects"]["github.com/o1/a"]["freshness"]["history"]                   # AC10
        assert [h["as_of"] for h in hist] == ["2026-09-01T00:00:00Z", iso(AS_OF)]

    def test_counts_add_up(self, built):
        c = built["summary"]["counts"]                                                         # AC8
        assert c["retained"] + c["rejected_repositories"] + 0 == c["resolved_unique_repositories"]
        assert c["recommendation_eligible"] == 4   # o1/a, x/renamed-new, y/deep, r/replace (KEI-805 verdict)

    def test_deterministic(self, built):
        first = {p.name: p.read_bytes() for p in built["corpus"].iterdir() if p.is_file()}   # AC11
        discover_build.run()
        second = {p.name: p.read_bytes() for p in built["corpus"].iterdir() if p.is_file()}
        assert first == second


class TestHistory:
    def test_append_is_idempotent(self, tmp_path, monkeypatch):                               # AC10
        monkeypatch.setattr(discover, "HISTORY", tmp_path / "h.jsonl")
        snap = {"as_of": iso(AS_OF), "meta": {"a/a": meta("a/a", 1)}, "deep": {}}
        assert discover.append_history(snap) == 1
        assert discover.append_history(snap) == 0
        snap2 = dict(snap, as_of=iso(AS_OF + timedelta(days=7)))
        assert discover.append_history(snap2) == 1
        assert len(common.read_jsonl(tmp_path / "h.jsonl")) == 2


class TestEarlyStop:
    """AC5: the early stop decides nothing the full inspection would decide differently."""

    class FakeHttp:
        def __init__(self, pr_days):
            self.rest = []
            self.pr_days = pr_days

        def call(self, url, data=None, auth=False, tries=5):
            if url.endswith("/graphql"):
                return 200, {"data": {"repository": {
                    "nameWithOwner": "o/r", "databaseId": 1, "isArchived": False, "isDisabled": False,
                    "isEmpty": False, "isFork": False, "isMirror": False, "isPrivate": False,
                    "stargazerCount": 1, "forkCount": 0, "pushedAt": ago(1), "createdAt": ago(900),
                    "licenseInfo": {"spdxId": "MIT", "name": "MIT", "key": "mit"},
                    "defaultBranchRef": {"name": "main", "target": {"history": {"totalCount": 1, "nodes": [
                        {"oid": "e" * 40, "committedDate": ago(1), "author": {"user": {"login": "bob"}}}]}}},
                    "releases": {"nodes": [{"tagName": "v1.0.0", "publishedAt": ago(2), "isDraft": False,
                                            "isPrerelease": False, "tagCommit": {"oid": "f" * 40}}]},
                    "refs": {"nodes": []},
                    "pullRequests": {"nodes": [{"number": 7, "mergedAt": ago(self.pr_days),
                                                "author": {"login": "alice"},
                                                "files": {"nodes": [{"path": "src/x.go"}]}}]},
                    "closedIssues": {"totalCount": 0}}}}
            self.rest.append(url)
            return 200, {"files": [{"filename": "src/y.go"}]}

    def _run(self, pr_days, stop):
        from source_pipeline import enrich
        from source_pipeline.activity import Rules, evaluate
        http = self.FakeHttp(pr_days)
        enrich.CACHE = None
        live = enrich.github(http, "github.com/o/r", AS_OF - timedelta(days=365), Rules(POLICY), POLICY,
                             iso(AS_OF), stop_when_active=stop)
        return http, evaluate(live, Rules(POLICY), AS_OF), live

    def test_stops_when_pr_decides_active(self):
        http, m, live = self._run(pr_days=10, stop=True)
        assert http.rest == [] and m["status"] == "active"
        assert "inspection_stopped" in live["observations"]

    def test_same_class_as_full_inspection(self):
        for days in (10, 60, 200, 300):
            _, full, _ = self._run(days, stop=False)
            _, fast, _ = self._run(days, stop=True)
            assert full["status"] == fast["status"] == "active"
            if days > 90:  # not decided early: the newest evidence is found exactly
                assert full["last_meaningful_activity_at"] == fast["last_meaningful_activity_at"]

    def test_does_not_stop_outside_active_window(self):
        http, m, live = self._run(pr_days=200, stop=True)
        assert http.rest and "inspection_stopped" not in live["observations"]

    def test_off_by_default(self):
        http, _, _ = self._run(pr_days=10, stop=False)
        assert http.rest   # KEI-805 behaviour unchanged
