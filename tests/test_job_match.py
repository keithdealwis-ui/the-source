"""KEI-930: job-matched candidates — reproducible ledger, never an integration, never a listed pair, labelled, and
counted toward a status only through the full evaluation."""
import json
import re

import pytest

from source_pipeline import build, common, job_match

HAVE_LEDGER = job_match.LEDGER.exists()
needs_ledger = pytest.mark.skipif(not HAVE_LEDGER, reason="no job-match ledger committed")


@pytest.fixture(scope="module")
def ledger():
    return common.read_json(job_match.LEDGER)


@pytest.fixture(scope="module")
def relationships():
    return common.read_jsonl(common.DATASET / "relationships.jsonl")


# ---------------------------------------------------------------- the ledger

@needs_ledger
def test_ledger_is_reproducible_from_committed_inputs(ledger):
    if ledger.get("inputs") != job_match.inputs_digest():
        pytest.skip("ledger is older than its inputs (quick Health rescored); `job-match` regenerates it")
    assert job_match.select() == ledger


@needs_ledger
def test_every_candidate_states_the_job_in_its_own_description(ledger):
    from source_pipeline.score_build import load_jobs

    jobs = load_jobs()
    projects = {p["id"]: p for p in common.read_jsonl(common.DATA / "corpus" / "projects.jsonl")}
    for r in ledger["rows"]:
        desc = (projects[r["oss_id"]].get("description") or "").lower().replace("-", " ")
        rx = job_match._phrase_re(jobs[r["category"]]["core"][0])
        assert rx.search(desc), r["oss_id"]
        assert r["matched_phrase"] in desc and r["excerpt"] in desc, r["oss_id"]


@needs_ledger
def test_no_candidate_is_an_integration_or_a_list(ledger):
    projects = {p["id"]: p for p in common.read_jsonl(common.DATA / "corpus" / "projects.jsonl")}
    names = sorted({n for c in __import__("source_pipeline.catalogue", fromlist=["x"]).candidates()
                    for n in (c["name"], *c.get("aliases", []))})
    from source_pipeline.mcp_server import _norm

    norm = sorted({_norm(n) for n in names if len(_norm(n)) > 2}, key=len, reverse=True)
    companion = re.compile(job_match._COMPANION.format(names="|".join(re.escape(n) for n in norm)))
    for r in ledger["rows"]:
        assert job_match._integration(projects[r["oss_id"]], companion) is None, r["oss_id"]


@needs_ledger
def test_candidates_are_capped_per_product_and_healthy(ledger):
    per = {}
    for r in ledger["rows"]:
        assert r["quick_health"] >= job_match.HEALTH_MIN
        assert r["oss_id"].startswith("github.com/")
        for s in r["saas_ids"]:
            per[s] = per.get(s, 0) + 1
    assert max(per.values()) <= job_match.PER_PRODUCT


def test_companion_rule_marks_clients_for_a_named_product():
    rx = re.compile(job_match._COMPANION.format(names="youtube|obsidian|notion"))
    assert rx.search("an alternative front end to youtube")
    assert rx.search("self organizing ai second brain for obsidian")
    assert rx.search("a fast client for notion")
    assert not rx.search("a self hosted video platform like youtube")


# ---------------------------------------------------------------- the relationships it yields

@needs_ledger
def test_job_matched_relationships_are_labelled_and_never_on_a_listed_pair(relationships):
    jm = [e for e in relationships if "job_match" in e["source_ids"]]
    assert jm
    for e in jm:
        assert e["source_ids"] == ["job_match"], e["edge_id"]
        assert e["evidence_class"] == "job_matched"
        assert e["corroborated"] is False
        assert all(p["locator"].startswith("data/job_match/ledger.json#") for p in e["provenance"])
    for e in relationships:
        if "job_match" not in e["source_ids"]:
            assert e["evidence_class"] in ("listed", "self_described"), e["edge_id"]


@needs_ledger
def test_job_matches_do_not_count_as_sources_naming_a_product():
    for s in common.read_jsonl(common.DATASET / "saas_products.jsonl"):
        assert "job_match" not in s["imported"]["named_by_sources"], s["saas_id"]


def test_evidence_class_strongest_wins():
    cfg = {"sources": [{"source_id": "fossalt"}, {"source_id": "intake", "kind": "intake"},
                       {"source_id": "job_match", "kind": "job_match"}]}
    assert build.evidence_class(["job_match"], cfg) == "job_matched"
    assert build.evidence_class(["intake", "job_match"], cfg) == "self_described"
    assert build.evidence_class(["fossalt", "job_match"], cfg) == "listed"
    assert build.evidence_class(["fossalt"], cfg) == "listed"


@needs_ledger
def test_published_recommendations_carry_their_evidence_class():
    rel = common.read_jsonl(common.DATA / "canonical" / "relationships.jsonl")
    for r in rel:
        assert r["evidence_class"] in ("listed", "self_described", "job_matched")
    for f in sorted((common.ROOT / "api" / "v1" / "saas").glob("*.json")):
        for rec in json.loads(f.read_text())["recommendations"]:
            assert rec["evidence"]["relationship"]["evidence_class"] in ("listed", "self_described", "job_matched")


@needs_ledger
def test_a_job_match_is_recommended_only_after_the_full_evaluation():
    from source_pipeline import catalogue

    for r in common.read_jsonl(common.DATA / "canonical" / "relationships.jsonl"):
        if r["evidence_class"] == "job_matched" and r["recommended"]:
            fit = r["replacement_fit"]
            assert fit["scoring_path"] in catalogue.FULL_PATHS
            assert fit["credible_alternative"] and fit["publish"] and fit["core_gate"] == "passed"
