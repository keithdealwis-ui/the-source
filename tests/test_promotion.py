"""Promotion queue (stress test 2026-11-24): strong discovery-tier pairings go through full scoring. No network."""
from __future__ import annotations

import pytest

from source_pipeline import common, cycle, intake, promotion

VW, CAL, YAAK, SMALL = ("github.com/dani-garcia/vaultwarden", "github.com/calcom/cal.diy", "github.com/mountain-loop/yaak",
                        "github.com/o/small")
JF = "github.com/jellyfin/jellyfin"


def _pair(oid, sid, fit, source="replace_lane", elig="eligible"):
    return {"oss_id": oid, "saas_id": sid, "saas_name": sid.title(), "category": "Cat", "licence_eligibility": elig,
            "quick_fit": {"score": fit}, "evidence": [{"source": source}]}


def _edge(sid, oid, status, reasons=()):
    return {"saas_id": sid, "oss_id": oid, "validation": {"status": status, "reasons": list(reasons)}}


@pytest.fixture
def root(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(common, "DATA", data)
    monkeypatch.setattr(common, "DATASET", data / "dataset")
    monkeypatch.setattr(intake, "LEDGER", data / "intake" / "admitted")
    common.write_jsonl(data / "corpus" / "live.jsonl", [
        {"id": VW, "stars": 71000}, {"id": CAL, "stars": 50339}, {"id": YAAK, "stars": 19535},
        {"id": SMALL, "stars": 900}, {"id": JF, "stars": 59669}])
    common.write_jsonl(data / "corpus" / "projects.jsonl", [
        {"id": YAAK, "name": "yaak", "full_name": "mountain-loop/yaak", "licence": {"spdx": "MIT"}}])
    common.write_jsonl(data / "quick" / "pairings.jsonl", [
        _pair(VW, "1password", 80.0), _pair(CAL, "calendly", 98.4), _pair(JF, "netflix", 90.0),
        _pair(YAAK, "postman", 76.0, source="topic"), _pair(SMALL, "notion", 99.0, source="description"),
        _pair(YAAK, "insomnia", 74.9, source="topic"),                       # under the Fit threshold
        _pair(VW, "bitwarden-cloud", 90.0, elig="conditional")])             # licence doubted by discovery
    hold = "held_for_review: licence--{} (contradictory_evidence; see review_queue.jsonl)"
    common.write_jsonl(data / "dataset" / "relationships.jsonl", [
        _edge("1password", VW, "rejected", ["evidence_not_contradicted", hold.format(VW)]),
        _edge("calendly", CAL, "rejected", ["evidence_not_contradicted", hold.format(CAL)]),
        _edge("netflix", JF, "validated")])
    common.write_jsonl(data / "dataset" / "oss_projects.jsonl", [{"oss_id": o} for o in (VW, CAL, JF)])
    common.write_jsonl(data / "scores" / "fit.jsonl", [])
    common.write_jsonl(data / "canonical" / "relationships.jsonl", [])
    return data


def test_thresholds_select_strong_popular_pairs_only(root):
    got = {(c["oss_id"], c["saas_id"]) for c in promotion.candidates(promotion.DEFAULTS)}
    assert got == {(VW, "1password"), (CAL, "calendly"), (JF, "netflix"), (YAAK, "postman")}
    tighter = {**promotion.DEFAULTS, "min_stars": 55000, "min_quick_fit": 85}
    assert {c["oss_id"] for c in promotion.candidates(tighter)} == {JF}


def test_run_admits_self_described_reports_blocked_and_prioritises_validated(root):
    out = promotion.run(promotion.DEFAULTS)
    assert out["scoring_priority"] == [JF]
    assert out["admitted_to_intake_ledger"] == [{"oss_id": YAAK, "saas_ids": ["postman"]}]
    row = common.read_json(intake.LEDGER / f"{promotion._request_id(YAAK)}.json")
    assert row["repo_url"] == "https://github.com/mountain-loop/yaak" and row["admitted_by"] == "promotion_queue"
    assert row["saas_names"] == ["Postman"] and row["basis"][0]["quick_fit"] == 76.0
    blocked = {b["oss_id"]: b for b in out["blocked"]}
    assert set(blocked) == {VW, CAL} and out["blocked_count"] == 2
    assert any("held_for_review" in r for r in blocked[VW]["reasons"])
    # idempotent: a second run rewrites nothing and admits the same row
    before = (intake.LEDGER / f"{promotion._request_id(YAAK)}.json").read_bytes()
    promotion.run(promotion.DEFAULTS)
    assert (intake.LEDGER / f"{promotion._request_id(YAAK)}.json").read_bytes() == before


def test_a_human_intake_row_is_never_rewritten_and_canonical_pairs_are_skipped(root):
    common.write_json(intake.LEDGER / "kei-human-01.json", {"request_id": "kei-human-01", "oss_id": YAAK,
                                                             "saas_ids": ["insomnia"], "saas_names": ["Insomnia"]})
    common.write_jsonl(root / "canonical" / "relationships.jsonl", [{"saas_id": "netflix", "oss_id": JF}])
    out = promotion.run(promotion.DEFAULTS)
    assert out["admitted_to_intake_ledger"] == [] and out["scoring_priority"] == []
    assert sorted(p.name for p in intake.LEDGER.glob("*.json")) == ["kei-human-01.json"]


def test_full_scored_validated_pairs_are_counted_not_rescored(root):
    common.write_jsonl(root / "scores" / "fit.jsonl", [{"saas_id": "netflix", "oss_id": JF}])
    out = promotion.run(promotion.DEFAULTS)
    assert out["scoring_priority"] == [] and out["full_scored_not_canonical"] == 1


def test_admissions_are_capped_per_cycle(root):
    out = promotion.run({**promotion.DEFAULTS, "max_admissions_per_cycle": 0})
    assert out["admitted_to_intake_ledger"] == [] and out["deferred_admissions"] == 1


def test_schedule_carries_the_thresholds():
    cfg = promotion.load_cfg()
    assert (cfg["min_quick_fit"], cfg["min_stars"]) == (75, 5000)
    assert "promotion_queue" in cycle.load_schedule()["strategies"] and "data/intake/admitted" in cycle.PROMOTE


def test_score_new_asks_the_promotion_queue_first(tmp_path, monkeypatch):
    from source_pipeline import canonical, score_build, score_one

    data = tmp_path / "data"
    monkeypatch.setattr(common, "DATASET", data / "dataset")
    monkeypatch.setattr(common, "LIVE", data / "live")
    oss = ["github.com/a/a", "github.com/b/b", "github.com/z/z"]
    common.write_jsonl(data / "dataset" / "oss_projects.jsonl", [
        {"oss_id": o, "host": "github.com", "derived": {"lane": "recommendable", "licence": {"spdx": "MIT"},
                                                        "evaluated_as_of": "2026-11-24T00:00:00Z"}} for o in oss])
    common.write_jsonl(data / "dataset" / "relationships.jsonl", [
        {"saas_id": "notes", "oss_id": o, "source_count": 1, "corroborated": False,
         "validation": {"status": "validated", "plausibility_review": {"outcome": "not reviewed"}}} for o in oss])
    common.write_jsonl(data / "dataset" / "saas_products.jsonl", [{"saas_id": "notes", "category": "Note taking"}])
    common.write_json(data / "live" / "snapshot.json", {"projects": {o: {"found": True} for o in oss}})
    monkeypatch.setattr(canonical, "_load_scores", lambda: ({}, {}))
    monkeypatch.setattr(score_build, "load_jobs", lambda: {"Note taking": {}})
    calls = []
    monkeypatch.setattr(score_one, "score", lambda key, *a, **k: calls.append(key) or
                        {"health": {"oss_id": key}, "fit": [], "errors": [], "unscorable": []})
    monkeypatch.setattr(score_one, "store", lambda r: 0)
    res = {"strategies": []}
    cycle._score_new(2, res, priority=["github.com/z/z", "github.com/not/in-todo"])
    assert calls == ["github.com/z/z", "github.com/a/a"]
    assert res["strategies"][0]["promoted_first"] == ["github.com/z/z"]
