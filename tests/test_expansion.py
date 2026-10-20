"""KEI-923 controlled corpus expansion: the frontier, the quotas, the historical tier and the noise gate."""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timezone

import pytest

from source_pipeline import corpus_admit as A
from source_pipeline import expansion as E
from source_pipeline import noise as N

AS_OF = datetime(2026, 10, 20, tzinfo=timezone.utc)


class FakeSearchHttp:
    """GitHub search over a fixed population: (full_name, stars). Honours stars bands and the 1,000 cap."""

    def __init__(self, population):
        self.population = population
        self.calls = 0
        self.failures = []
        self.queries = []

    def call(self, url, auth=False, **_):
        self.calls += 1
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        query, page = q["q"][0], int(q["page"][0])
        self.queries.append(query)
        band = next(t for t in query.split() if t.startswith("stars:"))[6:]
        if band.startswith(">="):
            lo, hi = int(band[2:]), None
        else:
            lo, hi = map(int, band.split(".."))
        hits = sorted([r for r in self.population if r[1] >= lo and (hi is None or r[1] <= hi)], key=lambda r: -r[1])
        items = [{"full_name": n, "id": i, "stargazers_count": s, "pushed_at": "2026-10-01T00:00:00Z",
                  "archived": False} for i, (n, s) in enumerate(hits)][:1000][(page - 1) * 100: page * 100]
        return 200, {"total_count": len(hits), "items": items}


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(E, "SEARCH_PAUSE_S", 0)
    monkeypatch.setattr(E, "AUX_PAUSE_S", 0)


def test_star_bands_split_until_every_result_is_reachable():
    pop = [(f"o/r{i}", 30 + (i % 4000)) for i in range(2600)]
    http = FakeSearchHttp(pop)
    items, queries = E._Search(http).all("topic:crm", 30, None)
    assert len({r["full_name"] for r in items}) == 2600
    assert len(queries) > 1 and all("stars:" in q for q in queries)


def test_a_band_that_cannot_split_keeps_its_first_thousand():
    pop = [(f"o/r{i}", 50) for i in range(1500)]
    items, _ = E._Search(FakeSearchHttp(pop)).all("topic:crm", 50, 50)
    assert len(items) == 1000


@pytest.mark.parametrize("url,key", [
    ("https://github.com/Owner/Repo", "github.com/owner/repo"),
    ("https://github.com/owner/repo.git", "github.com/owner/repo"),
    ("https://github.com/bitnami/charts/tree/main/bitnami/wordpress", None),
    ("https://gitlab.com/group/sub/proj", "gitlab.com/group/sub/proj"),
    ("https://gitlab.com/group/proj/-/tree/main", None),
    ("https://codeberg.org/forgejo/forgejo", "codeberg.org/forgejo/forgejo"),
    ("https://bitnami.com", None),
    ("", None),
])
def test_only_repository_roots_are_kept(url, key):
    assert E._repo_root(url) == key


def test_merge_keeps_every_route_and_historical_only_when_no_active_route():
    rows = [{"key": "github.com/a/b", "source": "github_topics:crm", "stars": 10, "historical_candidate": False},
            {"key": "github.com/a/b", "source": "awesome_lists:x", "stars": None, "historical_candidate": False},
            {"key": "github.com/c/d", "source": "github_topics:crm", "stars": 5000, "historical_candidate": True},
            {"key": "github.com/e/f", "source": "github_topics:erp", "stars": 2000, "historical_candidate": True},
            {"key": "github.com/e/f", "source": "github_topics:cms", "stars": 2100, "historical_candidate": False}]
    m = {r["key"]: r for r in E.merge(rows)}
    assert m["github.com/a/b"]["sources"] == ["awesome_lists:x", "github_topics:crm"]
    assert m["github.com/c/d"]["historical_candidate"] is True
    assert m["github.com/e/f"]["historical_candidate"] is False and m["github.com/e/f"]["stars"] == 2100


def test_pool_carries_expansion_provenance(tmp_path):
    (tmp_path / "frontier.jsonl").write_text(json.dumps(
        {"key": "github.com/a/b", "host": "github.com", "sources": ["github_topics:crm"], "host_repo_id": 7,
         "stars": 40, "pushed_at": None, "archived": False, "historical_candidate": False}) + "\n")
    p = E.pool(tmp_path)["github.com/a/b"]
    assert p["providers"] == [{"provider": "corpus_expansion", "query": "github_topics:crm"}]
    assert A.strategy_of(p["providers"][0]) == "corpus_expansion:github_topics:crm"
    assert A._routes(p)[0]["strategy"] == "corpus_expansion"


def test_quotas_cap_each_query_and_each_family():
    pick, charge, counts = A._quota({"max_per_query": 2, "max_source_share": 0.5, "max_admit_per_run": 6})
    got = []
    for _ in range(5):
        r = pick(["corpus_expansion:github_topics:crm", "corpus_expansion:github_topics:erp"])
        if r:
            charge(r)
        got.append(r)
    # family cap is 3 (0.5 x 6); query cap 2: crm, erp, crm, then nothing
    assert got[:3] == ["corpus_expansion:github_topics:crm", "corpus_expansion:github_topics:erp",
                       "corpus_expansion:github_topics:crm"]
    assert got[3:] == [None, None]
    assert counts["per_family"] == {"github_topics": 3}


@pytest.mark.parametrize("args,codes", [
    (("awesome-selfhosted", "o/awesome-selfhosted", "A list of Free Software network services", []), ["curated_list"]),
    (("dotfiles", "o/dotfiles", "my dotfiles", []), ["personal_configuration"]),
    (("home-ops", "o/home-ops", "My homelab Kubernetes cluster managed by Flux", ["homelab"]), ["personal_configuration"]),
    (("nextjs-starter", "o/nextjs-starter", "A Next.js starter kit", []), ["template_or_example"]),
    (("react-course", "o/react-course", "Code for the React course", []), ["learning_material"]),
    (("twenty", "twentyhq/twenty", "Building a modern alternative to Salesforce", ["crm"]), []),
    (("kickstarter-clone", "o/kickstarter-clone", "A crowdfunding platform", []), []),
    (("bookstack", "o/bookstack", "A platform to create documentation/wiki content", []), []),
    (("mysql-gui", "o/mysql-gui", "My SQL client", []), []),
])
def test_intake_reasons(args, codes):
    assert N.intake_reasons(*args) == codes


def test_audit_is_independent_and_spares_api_tooling_clients():
    row = {"full_name": "o/insomnia", "description": "The open-source API client for GraphQL and REST", "topics": ["api"]}
    assert N.audit(row, "API tooling") == []
    assert "integration" in N.audit({"full_name": "o/acme-mcp", "description": "MCP server for Acme", "topics": []})
    assert N.audit({"full_name": "o/x", "description": "", "topics": []}) == ["undescribed"]


def test_noise_check_uses_the_wider_of_threshold_and_baseline():
    cfg = {"thresholds": {"top10_discovery_irrelevant": 0.10, "top5_supported_irrelevant": 0.05,
                          "intake_irrelevant": 0.10}, "tolerance": 0.02}

    def res(top10, recs=()):
        return {"measures": {"top10_discovery_irrelevant": {"share": top10},
                             "top5_supported_irrelevant": {"share": 0.01}, "intake_irrelevant": {"share": 0.05},
                             "integration_false_positives_in_recommendations": {"rows": list(recs)}}}
    base = res(0.34, [{"saas": "s", "id": "a"}])
    assert N.check(res(0.30, [{"saas": "s", "id": "a"}]), base, cfg) == []
    assert N.check(res(0.37), base, cfg)                                   # worse than baseline + tolerance
    assert N.check(res(0.09), None, cfg) == []
    assert N.check(res(0.12), None, cfg)                                   # no baseline: the threshold
    assert N.check(res(0.30, [{"saas": "s", "id": "b"}]), base, cfg)       # a newly flagged recommendation


def test_harvest_keeps_each_source_and_resume_reuses_it(tmp_path, monkeypatch):
    calls = []

    def fake_awesome(http, src, family):
        calls.append(family)
        return [{"key": "github.com/a/b", "source": f"{family}:x", "host_repo_id": None, "stars": None,
                 "historical_candidate": False}], ["q"], []

    monkeypatch.setattr(E, "_awesome", fake_awesome)

    class NoHttp:
        calls = 0
        failures = []

    E.harvest(AS_OF, NoHttp(), tmp_path, only=["awesome_lists"])
    assert (tmp_path / "sources" / "awesome_lists.jsonl").exists() and len(calls) == 1
    m = E.harvest(AS_OF, NoHttp(), tmp_path, only=["awesome_lists"], resume=True)
    assert len(calls) == 1 and m["sources"]["awesome_lists"]["resumed"] is True
    assert m["counts"]["addresses"] == 1


@pytest.mark.parametrize("args,codes", [
    (("portfolio", "o/portfolio", "A developer portfolio built with Next.js", []), ["personal_site"]),
    (("o.github.io", "o/o.github.io", "Personal website", []), ["personal_site"]),
    (("patch-netvlad", "o/patch-netvlad", 'Code for the CVPR2021 paper "Patch-NetVLAD"', []), ["research_code"]),
    (("docs", "greenbone/docs", "Documentation for the Greenbone Community Edition", []), ["documentation"]),
    (("lenny-skills", "o/lenny-skills", "86 product management skills from Lenny's Podcast for Claude Code", []),
     ["prompt_pack"]),
    (("sb-admin", "o/sb-admin", "An admin dashboard template with dark mode", []), ["template_or_example"]),
    (("sec", "o/sec", "CompTIA Security+ study guide with exam objectives", []), ["learning_material"]),
    # products the desk-review rules must not catch
    (("skillhub", "iflytek/skillhub", "Self-hosted, open-source agent skill registry for enterprises", []), []),
    (("homepage", "gethomepage/homepage", "A highly customizable homepage (or startpage / application dashboard)", []),
     []),
    (("docs", "sismics/docs", "Lightweight document management system", []), []),
    (("wiki", "requarks/wiki", "Wiki.js | Next Generation Open Source Wiki", []), []),
    (("PDFMathTranslate", "o/PDFMathTranslate", "[EMNLP 2025 Demo] PDF scientific paper translation", []), []),
    (("capnweb", "cloudflare/capnweb", "JavaScript-native, low-boilerplate, object-capability RPC system", []), []),
    (("ghostfolio", "o/ghostfolio", "Open source wealth management software for portfolio tracking", []), []),
    # the second desk review (2026-10-21)
    (("x", "o/x-survey", "A Survey on Multimodal Retrieval-Augmented Generation", []), ["research_code"]),
    (("x", "o/x", "UrbanLoco: A Full Sensor Suite Dataset for Mapping and Localization", []), ["research_code"]),
    (("docs", "knative/docs", "User documentation for Knative components.", []), ["documentation"]),
    (("x", "o/x", "Specification for a Fediverse Directory Server", []), ["documentation"]),
    (("chat", "Azure-Samples/chat", "Serverless AI chat with RAG", []), ["template_or_example"]),
    (("t", "skills/test-with-actions", "Create CI workflows", ["skills-course"]), ["learning_material"]),
    (("docmd", "docmd-io/docmd", "Documentation compiler for humans and machines", []), []),
    (("communitynotes", "twitter/communitynotes", "Documentation and source code powering Community Notes", []), []),
])
def test_desk_review_intake_classes(args, codes):
    assert N.intake_reasons(*args) == codes
