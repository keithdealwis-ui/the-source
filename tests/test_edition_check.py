"""Automated edition check for open-core projects (source_pipeline/edition_check.py): pass, fail and ambiguous,
the generated licence exceptions, corpus validation, and the open-core disclosure in MCP output. No network."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from source_pipeline import build, common, corpus as C, edition_check as E
from source_pipeline import mcp_server as M

ROOT = Path(__file__).resolve().parent.parent
COMMIT = "a" * 40
SLUG = "acme/chat"


def _row(paths=("ee",), markers=(), core="AGPL-3.0", commit=COMMIT):
    cl = {"class": "open_core", "reason": "core under AGPL-3.0", "method": "the-source.licence-classify/1",
          "core_spdx": core, "excluded": {"paths": list(paths), "markers": list(markers)},
          "evidence": {"excluded_paths": [{"path": p, "why": "directory carries its own non-open licence file"}
                                          for p in paths]},
          "provenance": {"source": {"api": "github", "files": ["LICENSE"]}, "commit": commit,
                         "checked_at": "2026-10-17T06:31:30Z"}}
    return {"id": f"github.com/{SLUG}", "full_name": SLUG, "licence": {
        "spdx": "NOASSERTION", "status": "issue", "issue": {"category": "open_core", "reason": "open core"},
        "classification": cl,
        "eligibility": {"status": "conditional", "condition": "core only; edition check required before recommendation",
                        "follow_up": "edition_check_required_before_recommendation", "self_hostable": True,
                        "self_hosting_evidence": [], "recommendation_gate": "unchanged"}}}


def _rec(files: dict, extra_paths=(), commit=COMMIT, found=True):
    paths = set()
    for p in list(files) + list(extra_paths):
        parts = p.split("/")
        for i in range(1, len(parts)):
            paths.add(("/".join(parts[:i]), "tree"))
        paths.add((p, "blob"))
    return {"slug": SLUG, "found": found, "commit": commit if found else None, "error": None if found else "not_found",
            "fetched_at": "2026-10-17T10:00:00Z", "paths": sorted(paths), "texts": dict(files), "list_depth": 3}


CORE_COMPOSE = """
services:
  app:
    image: acme/chat:latest
    environment:
      ENTERPRISE_ENABLED: "false"
  db:
    image: postgres:16
"""


def test_pass_a_core_only_compose_makes_the_row_eligible_with_its_disclosure():
    row = _row()
    rec = _rec({"docker-compose.yml": CORE_COMPOSE, "ee/README.md": "# Enterprise\nSSO and audit logs"},
               extra_paths=["ee/sso/a.go", "ee/audit/b.go", "src/main.go"])
    res = E.check(row, rec, "2026-10-17T10:00:00Z")
    assert (res["result"], res["reason_code"], res["core_deployment"]) == ("pass", "core_only_deployment",
                                                                            "docker-compose.yml")
    assert res["disclosure"] == "open core: recommendation covers the AGPL-3.0 core; enterprise features excluded (ee/): audit, sso"
    scope = res["excluded_scope"][0]
    assert scope["path"] == "ee" and scope["contains"] == ["README.md", "audit", "sso"]
    assert scope["readme_summary"] == "Enterprise"
    assert set(res["provenance"]) == {"source", "commit", "checked_at"} and res["provenance"]["commit"] == COMMIT
    E.apply(row, res, {row["id"]: ["slack"]})
    e = row["licence"]["eligibility"]
    assert e["status"] == "eligible" and e["condition"] == res["disclosure"] and e["follow_up"] is None
    assert row["licence"]["listed_alternative_to"] == ["slack"]
    entry = E.generated_entry(row, res)
    assert entry["approved_by"] == "edition-check" and entry["edition_check"]["commit"] == COMMIT
    assert build._generated_exception(entry) is entry


@pytest.mark.parametrize("compose,code", [
    ("services:\n  app:\n    image: acme/chat-enterprise:1\n", "deployment_requires_excluded_parts"),
    ("services:\n  app:\n    build:\n      context: ./ee/server\n", "deployment_requires_excluded_parts"),
    ("services:\n  app:\n    image: acme/chat:1\n    volumes: ['./ee/plugins:/plugins']\n",
     "deployment_requires_excluded_parts"),
    ("services:\n  app:\n    image: acme/chat:1\n    environment:\n      CHAT_ENTERPRISE: 'true'\n",
     "deployment_requires_excluded_parts"),
])
def test_fail_every_deployment_needs_an_excluded_part(compose, code):
    row = _row()
    res = E.check(row, _rec({"docker-compose.yml": compose}, extra_paths=["ee/x.go"]), "t")
    assert (res["result"], res["reason_code"]) == ("fail", code)
    E.apply(row, res, {})
    e = row["licence"]["eligibility"]
    assert e["status"] == "conditional" and e["reason_code"] == code
    assert e["follow_up"] == "edition_check_required_before_recommendation"


def test_fail_a_compose_named_for_the_enterprise_edition():
    res = E.check(_row(), _rec({"docker/compose.enterprise.yml": CORE_COMPOSE}), "t")
    assert res["result"] == "fail"


def test_fail_a_dockerfile_that_copies_an_excluded_path():
    compose = "services:\n  app:\n    build: .\n"
    res = E.check(_row(), _rec({"docker-compose.yml": compose, "Dockerfile": "FROM node\nCOPY ee/ /app/ee\n"}), "t")
    assert res["result"] == "fail" and "Dockerfile copies ee/" in res["reason"]


@pytest.mark.parametrize("rec,code", [
    (None, "repository_unreadable"),
    (_rec({}, found=False), "repository_unreadable"),
    (_rec({"docker-compose.yml": CORE_COMPOSE}, commit="b" * 40), "repository_unreadable"),  # another commit
    (_rec({"src/main.go": "package main"}), "no_deployment_files_in_repository"),
    (_rec({"docker-compose.yml": "services:\n  db:\n    image: postgres:16\n"}), "no_deployment_runs_the_project"),
    (_rec({"docker-compose.dev.yml": CORE_COMPOSE}), "only_development_deployments"),
    (_rec({"server/docker-compose.yml": CORE_COMPOSE}), "only_development_deployments"),  # inside the source tree
    (_rec({"docker-compose.yml": "services: [unclosed"}), "deployment_file_unreadable"),
])
def test_ambiguous_fails_closed(rec, code):
    row = _row()
    res = E.check(row, rec, "t")
    assert (res["result"], res["reason_code"]) == ("ambiguous", code)
    E.apply(row, res, {})
    assert row["licence"]["eligibility"]["status"] == "conditional"


def test_ambiguous_when_the_classification_names_no_excluded_part():
    row = _row(paths=())
    assert E.check(row, _rec({"docker-compose.yml": CORE_COMPOSE}), "t")["reason_code"] == "not_classified_open_core"


def test_an_unresolved_image_variable_is_not_the_project():
    compose = "services:\n  app:\n    image: ${APP_IMAGE:?set it}\n"
    assert E.check(_row(), _rec({"docker-compose.yml": compose}), "t")["reason_code"] == "no_deployment_runs_the_project"
    assert E.image_name("ghcr.io/${ORG}/chat:${TAG}") == "chat"
    assert E.image_name("${IMG:-acme/chat-enterprise:1}") == "chat-enterprise"


def test_helm_chart_passes_and_an_enterprise_image_in_values_fails():
    files = {"charts/chat/Chart.yaml": "name: chat\n", "charts/chat/values.yaml": "image:\n  repository: acme/chat\n"}
    assert E.check(_row(), _rec(files), "t")["result"] == "pass"
    files["charts/chat/values.yaml"] = "image:\n  repository: acme/chat-ee\nenterprise:\n  enabled: true\n"
    assert E.check(_row(), _rec(files), "t")["result"] == "fail"


# ---------------------------------------------------------------- generated exceptions and validation


def _entry():
    row = _row()
    res = E.check(row, _rec({"docker-compose.yml": CORE_COMPOSE}, extra_paths=["ee/x.go"]), "t")
    return E.generated_entry(row, res)


@pytest.mark.parametrize("drop", ["disclosure", "core_spdx", "approval_ref"])
def test_a_generated_exception_without_its_provenance_or_disclosure_is_refused(drop):
    e = _entry()
    e.pop(drop)
    with pytest.raises(ValueError):
        build._generated_exception(e)
    e = _entry()
    e["edition_check"]["commit"] = "HEAD"
    with pytest.raises(ValueError):
        build._generated_exception(e)


def test_generated_exceptions_feed_the_build_only_when_switched_on(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "licence_exceptions.yaml").write_text("exceptions: []\n")
    E.write_generated([_entry()], cfg / "licence_exceptions.generated.yaml")
    lane = yaml.safe_load((ROOT / "config" / "licence_lane.yaml").read_text())
    (cfg / "licence_lane.yaml").write_text(yaml.safe_dump(lane))
    monkeypatch.setattr(common, "CONFIG", cfg)
    assert build.load_exceptions() == {}
    lane["edition_check"]["generated_exceptions_feed_the_build"] = True
    (cfg / "licence_lane.yaml").write_text(yaml.safe_dump(lane))
    exc = build.load_exceptions()
    rec = build.exception_record(exc[f"github.com/{SLUG}"])
    assert rec["kind"] == "edition_check" and rec["disclosure"].startswith("open core: recommendation covers")
    # the switch never lets a hand-written entry claim the edition-check approver
    (cfg / "licence_exceptions.yaml").write_text(yaml.safe_dump({"exceptions": [
        {"oss_id": "github.com/x/y", "approved_by": "edition-check", "approval_ref": "x"}]}))
    with pytest.raises(ValueError):
        build.load_exceptions()


def test_the_committed_generated_file_is_valid_and_matches_the_corpus():
    doc = yaml.safe_load((ROOT / "config" / "licence_exceptions.generated.yaml").read_text())
    assert doc["schema"] == build.GENERATED_SCHEMA
    rows = {p["id"]: p for p in common.read_jsonl(ROOT / "data" / "corpus" / "projects.jsonl")}
    for e in doc["exceptions"]:
        build._generated_exception(e)
        ec = rows[e["oss_id"]]["licence"]["edition_check"]
        assert ec["result"] == "pass" and ec["provenance"]["commit"] == e["edition_check"]["commit"]
    passed = {pid for pid, p in rows.items() if (p["licence"].get("edition_check") or {}).get("result") == "pass"}
    assert passed == {e["oss_id"] for e in doc["exceptions"]}


def test_corpus_validation_refuses_a_pass_without_its_disclosure():
    rows = common.read_jsonl(ROOT / "data" / "corpus" / "projects.jsonl")
    p = copy.deepcopy(next(r for r in rows if r["id"] == "github.com/twentyhq/twenty"))
    failed = []
    C._check_classification(p, lambda ok, what: failed.append(what) if not ok else None)
    assert failed == []
    p["licence"]["edition_check"]["disclosure"] = None
    C._check_classification(p, lambda ok, what: failed.append(what) if not ok else None)
    assert any("disclosure" in f for f in failed)
    q = copy.deepcopy(next(r for r in rows if r["id"] == "github.com/mattermost/mattermost"))
    q["licence"]["eligibility"]["status"] = "eligible"  # an ambiguous check can never make a row eligible
    failed = []
    C._check_classification(q, lambda ok, what: failed.append(what) if not ok else None)
    assert failed


# ---------------------------------------------------------------- MCP


@pytest.fixture(scope="module")
def srv():
    s = M.Server(ROOT, M.datetime(2026, 10, 17, 12, tzinfo=M.timezone.utc))
    s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    return s


def _call(s, name, **args):
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return r["result"]["structuredContent"]["result"]


def test_slack_discovery_lists_mattermost_and_rocket_chat_as_open_core(srv):
    r = _call(srv, "source_get_alternatives", saas="slack", include_discovery=True, limit=10)
    disc = {c["id"]: c for c in r["discovery_candidates"]["candidates"]}
    for pid in ("github.com/mattermost/mattermost", "github.com/rocketchat/rocket.chat"):
        assert disc[pid]["licence_label"] == "open_core" and disc[pid]["tier"] == "discovery"
        assert disc[pid]["open_core"]["disclosure"].startswith("open core: recommendation covers")
        assert "slack" in disc[pid]["listed_alternative_to"]
        assert pid not in {x["oss_id"] for x in r["recommendations"]}
    # conditional rows are listed, never counted as licence issues
    assert all(M.corpus_licence_eligibility(c) != "not_eligible" for c in map(srv.layer.corpus.get, disc))


def test_no_open_core_project_is_recommended_without_the_disclosure(srv):
    layer = srv.layer
    assert layer.undisclosed == []
    open_core = set(layer.disclosures)
    seen = 0
    for sid in sorted(layer.saas):
        for row in _call(srv, "source_get_alternatives", saas=sid, limit=50)["recommendations"]:
            if row["oss_id"] in open_core:
                seen += 1
                assert row["licence_disclosure"] == layer.disclosures[row["oss_id"]]
    assert seen >= 1  # Databend: Apache-2.0 core, ee directories excluded


def test_an_open_core_project_that_cannot_be_disclosed_is_not_recommended(srv):
    layer = srv.layer
    pid, rels = next((p, rs) for p, rs in layer.rel_by_project.items() if any(r["recommended"] for r in rs))
    saved = (copy.deepcopy(layer.projects[pid]["licence"]), layer.canonical_corpus.get(pid),
             [r["recommended"] for r in rels], dict(layer.disclosures), list(layer.undisclosed))
    try:
        layer.projects[pid]["licence"]["open_core"] = {"core_spdx": "MIT", "disclosure": None, "edition_check": {}}
        layer.canonical_corpus.pop(pid, None)
        layer._apply_disclosures()
        assert all(not r["recommended"] for r in rels) and layer.undisclosed
    finally:
        layer.projects[pid]["licence"] = saved[0]
        if saved[1] is not None:
            layer.canonical_corpus[pid] = saved[1]
        for r, was in zip(rels, saved[2]):
            r["recommended"] = was
        layer.disclosures, layer.undisclosed = saved[3], saved[4]


# ---------------------------------------------------------------- launch-policy amendment (proposal)


def test_an_excluded_runtime_component_is_ambiguous():
    row = _row(paths=("packages/backend",))
    res = E.check(row, _rec({"docker-compose.yml": CORE_COMPOSE}, extra_paths=["packages/backend/x.ts"]), "t")
    assert (res["result"], res["reason_code"]) == ("ambiguous", "excluded_part_is_a_runtime_component")


def test_the_disclosure_names_the_excluded_features():
    row = _row()
    rec = _rec({"docker-compose.yml": CORE_COMPOSE}, extra_paths=["ee/src/data_mask/a.rs", "ee/src/stream.rs",
                                                                  "ee/src/lib.rs", "ee/src/test_kits/t.rs"])
    res = E.check(row, rec, "t")
    assert res["excluded_scope"][0]["features"] == ["data mask", "stream"]
    assert res["disclosure"].endswith("enterprise features excluded (ee/): data mask, stream")


JOB = {"core": [["session recording", "heatmap"], ["record", "replay"]], "features": ["funnel", "survey"]}


def test_pair_rights_fail_closed_when_the_job_is_enterprise_only():
    scope = [{"path": "ee", "contains": ["session_recordings"], "features": ["session recording", "survey"]}]
    pr = E.pair_rights(scope, JOB)
    assert pr["status"] == "enterprise_job" and pr["core_hits"] == ["session recording"]
    assert E.pair_rights([{"path": "ee", "features": ["survey", "billing"]}], JOB) == {
        "status": "covered", "reason": "the excluded parts name none of the product's defining job; "
                                      "enterprise-only features: survey", "core_hits": [], "feature_hits": ["survey"]}
    assert E.pair_rights([{"marker": "@license Enterprise", "files_listed": False}], JOB)["status"] == "unknown"
    assert E.pair_rights(None, JOB)["status"] == "unknown"
    assert E.pair_rights(scope, None)["status"] == "unknown"


def _launch_fixture():
    saas = {s: {"source_ids": ["a"] * n, "name": s} for s, n in (("s1", 3), ("s2", 2), ("s3", 1))}
    projects = {f"p{i}": {"product_group": f"g{i}", "derived": {"maintenance": {
        "last_meaningful_activity_at": "2026-10-01T00:00:00Z"}}} for i in range(1, 6)}

    def edge(sid, pid):
        return {"edge_id": f"{sid}--{pid}", "saas_id": sid, "oss_id": pid, "source_count": 1,
                "validation": {"status": "validated", "reasons": []}}
    edges = [edge("s1", "p1"), edge("s1", "p4"), edge("s1", "p5"), edge("s2", "p2"), edge("s3", "p3")]
    policy = {"launch": {"saas_target": 2, "saas_target_is": "floor", "oss_minimum": 2, "edges_min": 1,
                         "edges_max": 4}}
    return saas, edges, projects, policy


def test_preserve_served_keeps_every_served_product_and_relationship():
    saas, edges, projects, policy = _launch_fixture()
    plain = build.select_launch(saas, edges, projects, policy)
    assert plain["saas_ranked"] == ["s1", "s2"]  # the floor already reaches oss_minimum: s3 is dropped
    served = {("s1", "p1"), ("s3", "p3"), ("s2", "p2")}
    kept = build.select_launch(saas, edges, projects, policy, served)
    assert kept["saas_ranked"] == ["s1", "s2", "s3"]
    assert served <= {tuple(k.split("--")) for k in kept["edge_picks"]}
    assert kept["preserve_served"] == {"served_before": 3, "kept": 3, "not_kept": []}
    # a served relationship that stops validating is the only thing that can leave, and says why
    edges[4]["validation"] = {"status": "rejected", "reasons": ["archived"]}
    out = build.select_launch(saas, edges, projects, policy, served)
    assert out["preserve_served"]["not_kept"] == [{"saas_id": "s3", "oss_id": "p3",
                                                   "reason": "no longer validated: archived"}]
