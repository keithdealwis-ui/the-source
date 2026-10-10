"""KEI-912: the public discovery corpus (data/corpus/) and the discovery tier in the MCP server. No network."""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from source_pipeline import common, corpus as C
from source_pipeline import mcp_server as M

ROOT = Path(__file__).resolve().parent.parent
HAVE_CORPUS = (ROOT / "data" / "corpus" / "MANIFEST.json").is_file()
NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}
LIST = "list:punkpeye/awesome-mcp-servers@3c30195615095be4f22ac120a7cee82f215cf547"


# --------------------------------------------------------------------------- export / refresh / validate


def _rec(slug, outcome="recommendation_eligible", routes=None, licence=None, desc="A tool", stars=10):
    lic = licence or {"spdx": "MIT", "status": "recognised", "issue": None}
    return {
        "canonical": f"github.com/{slug}", "aliases": [f"github.com/{slug}"], "url": f"https://github.com/{slug}",
        "descriptive": {"description": desc, "homepage": None},
        "entities": {"languages": ["language:Python"], "topics": ["github-topic:mcp", "github-topic:cli"],
                     "owner": {"login": slug.split("/")[0], "type": "User"}},
        "freshness": {"observed_as_of": "2026-09-29T10:34:38Z",
                      "history": [{"latest_release": {"tagName": "v1", "publishedAt": "2026-09-01T00:00:00Z"}}]},
        "lanes": {"discover": True, "replace": False}, "outcome": outcome,
        "live": {"canonical_name": slug, "host_repo_id": abs(hash(slug)) % 10 ** 9, "found": True, "stars": stars,
                 "forks": 1, "pushed_at": "2026-09-20T00:00:00Z", "archived": False, "licence_spdx": lic["spdx"]},
        "provenance": routes or [{"depth": 1, "locator": "line 7", "source": LIST, "strategy": "curated_list",
                                  "via": None}],
        "quality": {"licence": lic, "exclusion_reasons": [] if lic["status"] == "recognised"
                    else ["no_recognised_open_source_licence"],
                    "maintenance": {"status": "active", "last_meaningful_activity_at": "2026-09-20T00:00:00Z"},
                    "evaluated_as_of": "2026-09-29T10:34:38Z"},
    }


@pytest.fixture
def cfg_root(tmp_path, monkeypatch):
    """A scratch repository root holding config/corpus.yaml and a three-project Discover corpus."""
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config" / "corpus.yaml", tmp_path / "config" / "corpus.yaml")
    monkeypatch.setattr(common, "CONFIG", tmp_path / "config")
    src = tmp_path / "data" / "discover" / "corpus"
    src.mkdir(parents=True)
    recs = [
        _rec("alice/scraper"),
        _rec("bob/seeded", routes=[{"depth": 0, "locator": "line 84", "source": "config/discover_seeds.md",
                                     "strategy": "keith_seed", "via": None},
                                    {"depth": 1, "locator": None, "source": "github-topic:mcp",
                                     "strategy": "topic_search", "via": None}]),
        _rec("carol/nolicence", outcome="retained",
             licence={"spdx": "NOASSERTION", "status": "issue",
                      "issue": {"category": "restricted", "reason": "licence text adds terms", "detected": {}}}),
        _rec("dave/rejected", outcome="rejected"),
    ]
    common.write_jsonl(src / "projects.jsonl", recs)
    common.write_json(src / "MANIFEST.json", {"as_of": "2026-09-29T10:34:38Z", "schema": "the-source.discover/1",
                                              "sha256": {"projects.jsonl": common.sha256_file(src / "projects.jsonl")}})
    return tmp_path


def test_export_publishes_eligible_and_retained_only_and_validates(cfg_root):
    out = cfg_root / "data" / "corpus"
    m = C.export(cfg_root / "data" / "discover" / "corpus", out)
    assert m["counts"]["projects"] == 3 and m["counts"]["licence_issue"] == 1
    assert m["membership"]["skipped"] == {"rejected": 1}
    assert C.validate(out)["failed_checks"] == []
    rows = {p["id"]: p for p in common.read_jsonl(out / "projects.jsonl")}
    assert set(rows) == {"github.com/alice/scraper", "github.com/bob/seeded", "github.com/carol/nolicence"}
    route = rows["github.com/alice/scraper"]["provenance"][0]
    assert route["source"] == "list:punkpeye/awesome-mcp-servers" and route["commit"].startswith("3c3019")
    assert route["locator"] == "line 7"
    assert rows["github.com/alice/scraper"]["topics"] == ["mcp", "cli"]


def test_private_seed_routes_lose_file_and_line(cfg_root):
    out = cfg_root / "data" / "corpus"
    C.export(cfg_root / "data" / "discover" / "corpus", out)
    text = (out / "projects.jsonl").read_text()
    assert "discover_seeds" not in text and "keith_seed" not in text and "line 84" not in text
    seeded = next(p for p in common.read_jsonl(out / "projects.jsonl") if p["id"] == "github.com/bob/seeded")
    assert seeded["provenance"][0] == {"strategy": "maintainer_seed", "source": None, "commit": None,
                                       "locator": None, "depth": 0, "via": None}


def test_licence_issue_is_named_compactly(cfg_root):
    out = cfg_root / "data" / "corpus"
    C.export(cfg_root / "data" / "discover" / "corpus", out)
    p = next(p for p in common.read_jsonl(out / "projects.jsonl") if p["id"] == "github.com/carol/nolicence")
    assert p["licence"] == {"spdx": "NOASSERTION", "status": "issue",
                            "issue": {"category": "restricted", "reason": "licence text adds terms"}}


def test_validate_refuses_a_tampered_or_leaky_layer(cfg_root):
    out = cfg_root / "data" / "corpus"
    C.export(cfg_root / "data" / "discover" / "corpus", out)
    path = out / "projects.jsonl"
    rows = common.read_jsonl(path)
    rows[0]["provenance"][0]["source"] = "config/discover_seeds.md"
    common.write_jsonl(path, rows)
    assert any("sha256" in f for f in C.validate(out)["failed_checks"])
    m = common.read_json(out / "MANIFEST.json")
    m["files"]["projects.jsonl"] = common.sha256_file(path)
    common.write_json(out / "MANIFEST.json", m)
    failed = C.validate(out)["failed_checks"]
    assert any("provenance carries" in f for f in failed) and any("corpus_version" in f for f in failed)


class FakeApi:
    def __init__(self, answers):
        self.answers = answers

        class H:
            calls, failures = 3, []
        self.http = H()

    def meta_batch(self, slugs, size=50):
        return {s: self.answers.get(s, {"found": False, "error": "graphql_http_502"}) for s in slugs}


def _meta(nwo, stars, **kw):
    return {"found": True, "name_with_owner": nwo, "canonical_slug": nwo.lower(), "stars": stars, "forks": 2,
            "pushed_at": "2026-10-09T00:00:00Z", "archived": False, "licence_spdx": "MIT",
            "latest_release": {"tagName": "v2", "publishedAt": "2026-10-01T00:00:00Z"},
            "description": "A tool", "homepage": None, "topics": ["cli", "mcp"], "languages": ["Python"], **kw}


def test_refresh_updates_live_facts_follows_renames_and_records_deletions(cfg_root):
    out = cfg_root / "data" / "corpus"
    C.export(cfg_root / "data" / "discover" / "corpus", out)
    api = FakeApi({"alice/scraper": _meta("Alice/Scraper", 99),
                   "bob/seeded": _meta("newbob/Seeded", 5),
                   "carol/nolicence": {"found": False, "error": "not_found"}})
    res = C.refresh(NOW, out, api)["refresh"]
    assert (res["resolved"], res["renamed"], res["not_found"], res["unresolved"]) == (2, 1, 1, 0)
    live = {r["id"]: r for r in common.read_jsonl(out / "live.jsonl")}
    assert live["github.com/alice/scraper"]["stars"] == 99
    assert live["github.com/alice/scraper"]["observed_as_of"] == "2026-10-10T12:00:00Z"
    assert live["github.com/carol/nolicence"]["found"] is False
    bob = next(p for p in common.read_jsonl(out / "projects.jsonl") if p["id"] == "github.com/bob/seeded")
    assert "github.com/newbob/seeded" in bob["aliases"] and bob["full_name"] == "newbob/Seeded"
    assert C.validate(out)["failed_checks"] == []
    m = common.read_json(out / "MANIFEST.json")
    assert m["refresh"]["last"]["at"] == "2026-10-10T12:00:00Z" and m["counts"]["host_not_found"] == 1


def test_refresh_that_cannot_read_the_corpus_keeps_the_previous_layer(cfg_root):
    out = cfg_root / "data" / "corpus"
    C.export(cfg_root / "data" / "discover" / "corpus", out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    with pytest.raises(C.CorpusError, match="unresolved"):
        C.refresh(NOW, out, FakeApi({}))
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before


def test_due_follows_the_last_refresh(cfg_root):
    out = cfg_root / "data" / "corpus"
    C.export(cfg_root / "data" / "discover" / "corpus", out)
    assert C.due(datetime(2026, 10, 1, tzinfo=timezone.utc), out)["due"] is False
    assert C.due(datetime(2026, 10, 9, tzinfo=timezone.utc), out)["due"] is True


# --------------------------------------------------------------------------- MCP discovery tier (committed layer)


needs_corpus = pytest.mark.skipif(not HAVE_CORPUS, reason="needs the committed data/corpus layer")


def _server(root=ROOT):
    s = M.Server(root, NOW)
    s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": INIT})
    return s


def _ok(s, name, **args):
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    r = r["result"]
    assert r["isError"] is False, r
    return r["structuredContent"]["result"]


@pytest.fixture(scope="module")
def srv():
    return _server()


@needs_corpus
def test_the_committed_corpus_validates():
    assert C.validate(ROOT / "data" / "corpus")["failed_checks"] == []


@needs_corpus
def test_every_published_project_is_served_and_the_count_matches_the_manifest(srv):
    layer = srv.layer
    m = common.read_json(ROOT / "data" / "corpus" / "MANIFEST.json")
    assert layer.corpus_state["status"] == "ok"
    assert len(layer.corpus) + len(layer.canonical_corpus) == m["counts"]["projects"]
    for p in common.read_jsonl(ROOT / "data" / "corpus" / "projects.jsonl"):
        key, tier = layer.resolve_any(p["id"])
        assert tier in M.TIERS
        assert layer.resolve_any(p["url"])[0] == key


@needs_corpus
def test_no_discovery_or_scored_project_is_ever_recommended(srv):
    for sid in sorted(srv.layer.saas):
        alt = _ok(srv, "source_get_alternatives", saas=sid, limit=50, include_discovery=True)
        rec_ids = {r["oss_id"] for r in alt["recommendations"]}
        assert all(srv.layer.tier_of(i) == "recommended" for i in rec_ids), sid
        cands = alt["discovery_candidates"]["candidates"]
        assert all(c["tier"] == "discovery" and c["id"] not in rec_ids for c in cands), sid
        rec = _ok(srv, "source_recommend", saas=sid, limit=50)
        assert all(srv.layer.tier_of(r["oss_id"]) == "recommended" for r in rec["recommendations"]), sid


def _answers(s):
    out = []
    for sid in sorted(s.layer.saas):
        out.append(_ok(s, "source_get_alternatives", saas=sid, limit=50))
        out.append(_ok(s, "source_recommend", saas=sid, limit=50))
    for need in ("self-hosted analytics", "password manager", "team wiki like Notion", "MCP server for web scraping"):
        out.append(_ok(s, "source_recommend", need=need, limit=50))
    return json.dumps(out, sort_keys=True)


@needs_corpus
def test_the_corpus_cannot_change_a_recommendation(tmp_path, srv):
    root = tmp_path / "layer"
    shutil.copytree(ROOT / "data" / "canonical", root / "data" / "canonical")
    shutil.copytree(ROOT / "api" / "v1", root / "api" / "v1")
    for name in ("momentum", "radar"):
        shutil.copytree(ROOT / "data" / name, root / "data" / name)
    without = _server(root)
    assert without.layer.corpus_state["status"] == "unavailable"
    assert _answers(without) == _answers(srv)


@needs_corpus
def test_every_search_result_carries_a_tier_and_the_filter_holds(srv):
    out = _ok(srv, "source_search", query="mcp server web scraping", limit=50)
    assert out["results"] and all(r["tier"] in M.TIERS + ("catalogue",) for r in out["results"])
    assert any(r["tier"] == "discovery" for r in out["results"])
    for tier in M.TIERS:
        rows = _ok(srv, "source_search", query="analytics", kind="project", tier=tier, limit=50)["results"]
        assert all(r["tier"] == tier for r in rows)
    disc = _ok(srv, "source_search", query="analytics", tier="discovery", limit=5)["results"]
    assert all(r["project"]["label"] == M.DISCOVERY_LABEL for r in disc)


@needs_corpus
def test_hybrid_search_over_the_full_corpus_shows_its_components(srv):
    out = _ok(srv, "source_search", query="MCP server for web scraping", mode="hybrid", limit=10)
    assert out["searched"].startswith("full corpus")
    assert out["results"] and all(set(r["score_components"]) == {"lexical", "semantic", "graph"}
                                  for r in out["results"])


@needs_corpus
def test_a_discovery_project_carries_provenance_freshness_and_its_licence_flag(srv):
    layer = srv.layer
    listed = next(c for c in layer.corpus.values() if any(r["strategy"] == "curated_list" for r in c["provenance"]))
    p = _ok(srv, "source_get_project", project=listed["url"])
    assert p["tier"] == "discovery" and p["relationships"] == []
    assert p["project"]["observed_as_of"] and p["project"]["label"] == M.DISCOVERY_LABEL
    route = next(r for r in p["project"]["provenance"] if r["strategy"] == "curated_list")
    assert route["source"].startswith("list:") and len(route["commit"]) == 40 and route["locator"]
    flagged = next(c for c in layer.corpus.values() if c["licence"]["status"] == "issue")
    q = _ok(srv, "source_get_project", project=flagged["id"])["project"]
    assert q["licence"]["status"] == "issue" and q["licence"]["issue"]["reason"] and "licence issue" in q["warning"]


@needs_corpus
def test_canonical_projects_keep_their_answer_and_gain_a_tier(srv):
    pid = next(iter(sorted(srv.layer.canonical_corpus)))
    p = _ok(srv, "source_get_project", project=pid)
    assert p["tier"] in ("recommended", "scored") and p["project"]["oss_id"] == pid
    assert p["discovery_record"]["provenance"]


@needs_corpus
def test_compare_mixes_tiers_and_says_what_discovery_lacks(srv):
    canon = next(pid for pid in sorted(srv.layer.projects) if srv.layer.tier_of(pid) == "recommended")
    disc = next(iter(sorted(srv.layer.corpus)))
    out = _ok(srv, "source_compare_projects", projects=[canon, disc])
    tiers = {r["oss_id"]: r["tier"] for r in out["projects"]}
    assert tiers == {canon: "recommended", disc: "discovery"}
    assert any("discovery tier, not scored" in u for u in out["uncertainty"])


@needs_corpus
def test_dataset_info_reports_the_corpus_and_tiers(srv):
    info = _ok(srv, "source_dataset_info")
    assert info["corpus"]["status"] in ("ok", "stale") and info["corpus"]["projects_served"] > 9000
    assert set(info["tiers"]["counts"]) == set(M.TIERS)
    assert "data/corpus/projects.jsonl" in info["serves"]


@needs_corpus
def test_a_tampered_corpus_is_not_served_and_canonical_keeps_serving(tmp_path):
    root = tmp_path / "layer"
    for d in ("data/canonical", "api/v1", "data/momentum", "data/radar", "data/corpus"):
        shutil.copytree(ROOT / d, root / d)
    with open(root / "data" / "corpus" / "live.jsonl", "a") as fh:
        fh.write("\n")
    s = _server(root)
    assert s.layer.corpus_state["status"] == "unavailable" and "sha256" in s.layer.corpus_state["reason"]
    out = _ok(s, "source_search", query="analytics")
    assert out["searched"].startswith("canonical layer only") and all(r["tier"] != "discovery" for r in out["results"])
    assert _ok(s, "source_get_alternatives", saas="Notion")["recommendations"]


@needs_corpus
def test_a_corpus_past_its_refresh_window_says_it_is_stale():
    s = M.Server(ROOT, datetime(2027, 1, 1, tzinfo=timezone.utc))
    state = M._corpus_state(s.layer, s)
    assert state["status"] == "stale" and "refresh" in state["reason"]
