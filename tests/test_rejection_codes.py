"""KEI-919 S1.5: enumerated rejection codes and the contradictory-evidence review queue."""
import jsonschema
import pytest

from source_pipeline import common, reasons

SCHEMA = common.read_json(common.SCHEMA / "the-source.schema.json")
HAVE_DATASET = (common.DATASET / "rejections.jsonl").exists()


def validator(name):
    return jsonschema.Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": SCHEMA["$defs"]})


def test_schema_enum_equals_module_codes():
    assert sorted(SCHEMA["$defs"]["rejection_code"]["enum"]) == sorted(reasons.CODES)


def test_every_raw_reason_maps_into_the_enum():
    for code in list(reasons.ELIGIBILITY_CODE.values()) + list(reasons.NORMALISE_CODE.values()):
        assert code in reasons.CODES


@pytest.mark.parametrize("record,code", [
    ({"stage": "eligibility", "reason": "archived"}, "archived"),
    ({"stage": "eligibility", "reason": "stale_no_meaningful_activity_12_months;archived"}, "archived"),
    ({"stage": "eligibility", "reason": "no_recognised_open_source_licence;stale_no_meaningful_activity_12_months"},
     "licence_not_open"),
    ({"stage": "eligibility", "reason": "repository_not_found"}, "not_found"),
    ({"stage": "eligibility", "reason": "repository_identity_changed_since_listed"}, "identity_unverified"),
    ({"stage": "normalise", "reason": "repo_url_not_a_repository", "repo_url": "https://x.org"}, "not_a_repository"),
    ({"stage": "normalise", "reason": "saas_not_in_catalogue", "saas_name": "Foo"}, "saas_not_in_catalogue"),
    ({"stage": "plausibility_review", "reason": "search library; not a product"}, "not_a_replacement"),
])
def test_code_for_maps_one_code_with_detail(record, code):
    got, detail = reasons.code_for(record)
    assert got == code and detail
    assert not validator("rejection").is_valid(dict(record))  # uncoded record is invalid
    assert validator("rejection").is_valid(dict(record, code=got, detail=detail))


@pytest.mark.parametrize("record", [
    {"stage": "eligibility", "reason": "archived;something_new"},
    {"stage": "normalise", "reason": "brand_new_reason"},
    {"stage": "mystery", "reason": "x"},
])
def test_unknown_reason_fails_closed(record):
    with pytest.raises(reasons.UnknownReason):
        reasons.code_for(record)


def test_schema_rejects_unknown_code():
    rec = {"stage": "eligibility", "reason": "archived", "code": "something_else", "detail": "x"}
    assert not validator("rejection").is_valid(rec)


def project(oss_id, declared, host=None, files=None):
    det = {"method": "licence_text", "spdx": files} if files else {"method": "host_api"}
    return {"oss_id": oss_id,
            "imported": {"declared_licences": [{"source_id": s, "value": v} for s, v in declared]},
            "live": {"licence_spdx": host},
            "derived": {"licence": {"detection": det}}}


AS_OF = "2026-10-17T00:00:00Z"


def test_queue_needs_three_sources_and_a_disagreement():
    projects = {
        "github.com/a/agree": project("github.com/a/agree", [("l1", "MIT"), ("l2", "MIT License")], host="MIT"),
        "github.com/a/two": project("github.com/a/two", [("l1", "MIT")], host="GPL-3.0"),
        "github.com/a/three": project("github.com/a/three", [("l1", "MIT"), ("l2", "AGPL-3.0")], host="AGPL-3.0-only"),
        "github.com/a/other": project("github.com/a/other", [("l1", "MIT"), ("l2", "Other")], host="AGPL-3.0"),
        "github.com/a/files": project("github.com/a/files", [("l1", "MIT"), ("l2", "MIT")], files="GPL-3.0"),
    }
    edges = [{"saas_id": "s", "oss_id": k} for k in projects]
    q = reasons.review_queue(projects, edges, AS_OF, resolutions={})
    assert [i["claim"]["oss_id"] for i in q] == ["github.com/a/files", "github.com/a/three"]
    v = validator("review_queue_item")
    for item in q:
        assert v.is_valid(item), list(v.iter_errors(item))
        assert item["code"] == "contradictory_evidence" and item["status"] == "open"
        assert item["affected_edges"] == [f"s--{item['claim']['oss_id']}"]


def test_resolution_closes_an_item():
    projects = {"github.com/a/x": project("github.com/a/x", [("l1", "MIT"), ("l2", "AGPL-3.0")], host="AGPL-3.0")}
    res = {"licence--github.com/a/x": {"queue_id": "licence--github.com/a/x", "resolution": "host is right",
                                       "reviewed_by": "keith", "reviewed_at": "2026-10-17", "approval_ref": "KEI-919"}}
    [item] = reasons.review_queue(projects, [], AS_OF, resolutions=res)
    assert item["status"] == "resolved" and item["resolution"]["reviewed_by"] == "keith"


def test_executor_cannot_resolve(tmp_path, monkeypatch):
    (tmp_path / "evidence_review.yaml").write_text(
        "resolved:\n  - {queue_id: licence--x, resolution: ok, reviewed_by: claude, approval_ref: KEI-1}\n")
    monkeypatch.setattr(common, "CONFIG", tmp_path)
    with pytest.raises(ValueError):
        reasons.load_resolutions()


@pytest.mark.skipif(not HAVE_DATASET, reason="needs the committed dataset")
def test_committed_rejections_are_all_coded():
    rows = common.read_jsonl(common.DATASET / "rejections.jsonl")
    v = validator("rejection")
    assert rows and all(v.is_valid(r) for r in rows)
    assert all(reasons.code_for(r)[0] == r["code"] for r in rows)
    summary = common.read_json(common.DATASET / "summary.json")
    assert sum(summary["rejections"]["by_code"].values()) == len(rows)


@pytest.mark.skipif(not HAVE_DATASET, reason="needs the committed dataset")
def test_committed_queue_holds_its_relationships():
    queue = common.read_jsonl(common.DATASET / "review_queue.jsonl")
    edges = common.read_jsonl(common.DATASET / "relationships.jsonl")
    held = {q["claim"]["oss_id"] for q in queue if q["status"] == "open"}
    for e in edges:
        assert e["validation"]["checks"]["evidence_not_contradicted"] == (e["oss_id"] not in held)
        if e["oss_id"] in held:
            assert not e["recommendation_eligible"] and not e["launch"]
