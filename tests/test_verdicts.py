"""KEI-783 AC4: Keith's verdicts are a separate, human-only layer beside the machine scores. No network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from source_pipeline import canonical, common, score_validate, verdicts

HAVE_DATASET = (common.DATASET / "summary.json").exists()
CANON = common.DATA / "canonical"


def _row(oss_id="github.com/acme/widget", verdict="recommended", saas_id=None, tested=True, at="2026-11-01T10:00:00Z"):
    return verdicts.make_row(oss_id, verdict, saas_id=saas_id, keith_tested=tested, notes="ran it for a week",
                             now=at)


def _fixture(v_for_project=None, v_for_pair=None):
    """One project, one relationship, one served recommendation, with the given keith_verdict on each."""
    fit = {"score": 80.0, "credible_alternative": True, "publish": True, "core_gate": "pass", "confidence": 84.5,
           "dimensions": {"core_use_case_coverage": 90.0}, "score_version": "1", "evaluated_at": "2026-10-01T00:00:00Z",
           "scoring_path": "full_lane"}
    health = {"score": 70.0, "band": "Healthy", "trajectory": "Stable", "confidence": 80.0, "watch_outs": [],
              "evidence_insufficient": False, "score_version": "1", "evaluated_at": "2026-10-01T00:00:00Z",
              "scoring_path": "full_lane"}
    project = {"oss_id": "github.com/acme/widget", "project_health": dict(health),
               **canonical._verdict_fields(v_for_project)}
    rel = {"saas_id": "trello", "oss_id": "github.com/acme/widget", "replacement_fit": dict(fit), "recommended": True,
           **canonical._verdict_fields(v_for_pair)}
    rec = {"oss_id": "github.com/acme/widget", "replacement_fit": {k: fit[k] for k in ("score", "confidence",
                                                                                       "score_version", "evaluated_at")},
           **canonical._verdict_fields(v_for_pair)}
    docs = {"trello": {"recommendations": [rec]}}
    scores = ({("trello", "github.com/acme/widget"): {**fit, "saas_id": "trello", "oss_id": "github.com/acme/widget"}},
              {"github.com/acme/widget": {**health, "oss_id": "github.com/acme/widget"}})
    return [project], [rel], docs, scores


# ---------------------------------------------------------------- no verdicts

def test_no_verdict_means_null_and_no_endorsement():
    idx = verdicts.index([])
    assert verdicts.for_project(idx, "github.com/acme/widget") is None
    assert verdicts.for_pair(idx, "trello", "github.com/acme/widget") is None
    assert verdicts.endorsement(None) == {"keith_tested": False, "keith_endorsed": False,
                                          "editorial_recommendation": False}
    projects, rels, docs, _ = _fixture()
    assert verdicts.surface_errors([], projects, rels, docs) == []
    assert verdicts.load(Path("/nonexistent/verdicts.jsonl")) == []


def test_records_built_before_kei783_read_as_no_verdict():
    projects, rels, docs, _ = _fixture()
    for row in projects + rels + docs["trello"]["recommendations"]:
        row.pop("keith_verdict"), row.pop("endorsement")
    assert verdicts.surface_errors([], projects, rels, docs) == []


@pytest.mark.skipif(not HAVE_DATASET, reason="needs the committed dataset")
def test_committed_verdicts_are_empty_and_nothing_claims_endorsement():
    # No verdict is ever invented: the committed layer starts empty, so nothing published is Keith's.
    assert verdicts.load() == []
    for name in ("oss_projects.jsonl", "relationships.jsonl"):
        for r in common.read_jsonl(CANON / name):
            assert r.get("keith_verdict") is None
            assert r.get("endorsement", verdicts.NO_ENDORSEMENT) == verdicts.NO_ENDORSEMENT
    doc = json.loads((common.ROOT / "api" / "v1" / "saas" / "notion.json").read_text())
    assert all(rec.get("keith_verdict") is None for rec in doc["recommendations"])


# ---------------------------------------------------------------- a verdict, surfaced distinctly

def test_pair_verdict_overrides_the_project_verdict_and_newest_wins():
    general = _row(verdict="conditional", at="2026-10-01T00:00:00Z")
    older = _row(saas_id="trello", verdict="not_recommended", at="2026-10-02T00:00:00Z")
    newer = _row(saas_id="trello", verdict="recommended", at="2026-10-03T00:00:00Z")
    idx = verdicts.index([general, newer, older])
    assert verdicts.for_project(idx, "github.com/acme/widget")["verdict"] == "conditional"
    assert verdicts.for_pair(idx, "trello", "github.com/acme/widget")["verdict"] == "recommended"
    assert verdicts.for_pair(idx, "asana", "github.com/acme/widget")["verdict"] == "conditional"
    e = verdicts.endorsement(verdicts.for_pair(idx, "trello", "github.com/acme/widget"))
    assert e == {"keith_tested": True, "keith_endorsed": True, "editorial_recommendation": False}
    assert verdicts.endorsement(verdicts.for_project(idx, "github.com/acme/widget"))["keith_endorsed"] is False


def test_a_verdict_never_moves_a_machine_score():
    v = _row(saas_id="trello")
    projects, rels, docs, (fit, health) = _fixture(None, verdicts.public(v))
    assert verdicts.surface_errors([v], projects, rels, docs) == []
    assert verdicts.score_errors(rels, projects, docs, fit, health) == []
    rels[0]["replacement_fit"]["score"] = 95.0  # what a verdict merged into the score would look like
    docs["trello"]["recommendations"][0]["replacement_fit"]["score"] = 95.0
    bad = verdicts.score_errors(rels, projects, docs, fit, health)
    assert any("Replacement Fit differs" in b for b in bad) and any("api/v1" in b for b in bad)


@pytest.mark.skipif(not HAVE_DATASET, reason="needs the committed dataset")
def test_canonical_build_surfaces_a_verdict_beside_unchanged_scores(tmp_path, monkeypatch):
    rel = next(r for r in common.read_jsonl(CANON / "relationships.jsonl") if r["recommended"])
    vfile = tmp_path / "verdicts.jsonl"
    v = _row(rel["oss_id"], saas_id=rel["saas_id"])
    vfile.write_text(common.dumps(v) + "\n")
    monkeypatch.setattr(verdicts, "VERDICTS", vfile)
    out = tmp_path / "root"
    canonical.build(out)
    rels = common.read_jsonl(out / "data" / "canonical" / "relationships.jsonl")
    projects = common.read_jsonl(out / "data" / "canonical" / "oss_projects.jsonl")
    committed = {(r["saas_id"], r["oss_id"]): r for r in common.read_jsonl(CANON / "relationships.jsonl")}
    built = {(r["saas_id"], r["oss_id"]): r for r in rels}
    key = (rel["saas_id"], rel["oss_id"])
    assert built[key]["keith_verdict"] == verdicts.public(v)
    assert built[key]["endorsement"]["keith_endorsed"] is True
    # Distinct, never merged: every machine field is exactly what it was without the verdict.
    for k, r in built.items():
        strip = lambda x: {f: y for f, y in x.items() if f not in ("keith_verdict", "endorsement")}
        assert strip(r) == strip(committed[k])
    assert sum(1 for r in rels if r["keith_verdict"]) == 1
    assert all(p["keith_verdict"] is None for p in projects)  # a pair verdict is not a verdict on the project
    doc = json.loads((out / "api" / "v1" / "saas" / f"{rel['saas_id']}.json").read_text())
    shown = [x for x in doc["recommendations"] if x["oss_id"] == rel["oss_id"]]
    if shown:
        assert shown[0]["keith_verdict"]["verdict"] == "recommended"
        assert shown[0]["replacement_fit"] == {k: rel["replacement_fit"][k] for k in shown[0]["replacement_fit"]}
    docs = {p.stem: json.loads(p.read_text()) for p in (out / "api" / "v1" / "saas").glob("*.json")}
    assert verdicts.surface_errors([v], projects, rels, docs) == []
    fit, health = canonical._load_scores()
    assert verdicts.score_errors(rels, projects, docs, fit, health) == []


# ---------------------------------------------------------------- validation refuses

def test_validation_rejects_an_unknown_project():
    v = _row("github.com/nobody/nothing")
    assert verdicts.unknown_references([v], {"github.com/acme/widget"}, {"trello"}) == \
        ["github.com/nobody/nothing: unknown project"]
    v2 = _row(saas_id="no-such-saas")
    assert "unknown SaaS product" in verdicts.unknown_references([v2], {"github.com/acme/widget"}, {"trello"})[0]


def test_validation_rejects_a_fabricated_endorsement():
    # A machine-derived row that claims Keith's endorsement with no verdict row behind it.
    projects, rels, docs, _ = _fixture()
    rels[0]["endorsement"] = {"keith_tested": True, "keith_endorsed": True, "editorial_recommendation": False}
    bad = verdicts.surface_errors([], projects, rels, docs)
    assert len(bad) == 1 and "not derived from its verdict" in bad[0]
    # ...or one that shows a verdict nobody recorded.
    projects, rels, docs, _ = _fixture(verdicts.public(_row()))
    assert any("keith_verdict" in b for b in verdicts.surface_errors([], projects, rels, docs))


def test_schema_rejects_rows_not_recorded_by_keith():
    good = _row()
    assert verdicts.schema_errors([good]) == []
    for bad in ({**good, "recorded_by": "claude"}, {**good, "verdict": "endorsed"},
                {**good, "keith_tested": True, "tested_at": None}, {**good, "keith_tested": False}):
        assert verdicts.schema_errors([bad]), bad


def test_verdict_add_appends_and_refuses_unknown(tmp_path, monkeypatch):
    path = tmp_path / "verdicts.jsonl"
    monkeypatch.setattr(verdicts, "known_projects", lambda root=None: {"github.com/acme/widget"})
    monkeypatch.setattr(verdicts, "known_saas", lambda root=None: {"trello"})
    verdicts.add(_row(saas_id="trello"), path=path)
    with pytest.raises(ValueError, match="unknown project"):
        verdicts.add(_row("github.com/nobody/nothing"), path=path)
    rows = verdicts.load(path)
    assert len(rows) == 1 and rows[0]["recorded_by"] == "keith" and rows[0]["tested_at"] == "2026-11-01"


def test_scored_record_endorsement_comes_from_the_verdict_layer_only():
    base = {"keith_tested": False, "keith_endorsed": False, "editorial_recommendation": False,
            "statement": "Automatically computed from public repository metadata. Not tested, reviewed or endorsed "
                         "by Keith de Alwis, and not an editorial recommendation."}
    assert verdicts.score_endorsement(base, None) is base
    e = verdicts.score_endorsement(base, verdicts.public(_row()))
    assert e["keith_tested"] and e["keith_endorsed"] and not e["editorial_recommendation"]
    assert not score_validate.FORBIDDEN.search(e["statement"])
