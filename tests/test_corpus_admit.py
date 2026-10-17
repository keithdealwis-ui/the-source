"""Corpus admission (source_pipeline/corpus_admit.py): new members for data/corpus, their counts in the run
record and the MCP last_run block, and the intake anomaly flags. No network."""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from source_pipeline import common, corpus as C, corpus_admit as A
from source_pipeline import mcp_server as M

from test_corpus import FakeApi, _meta, _rec

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)


@pytest.fixture
def layer(tmp_path, monkeypatch):
    """A scratch root with the real corpus, policy and discover configs and a two-project data/corpus."""
    (tmp_path / "config").mkdir()
    for name in ("corpus.yaml", "policy.yaml", "discover.yaml"):
        shutil.copy(ROOT / "config" / name, tmp_path / "config" / name)
    monkeypatch.setattr(common, "CONFIG", tmp_path / "config")
    src = tmp_path / "data" / "discover" / "corpus"
    src.mkdir(parents=True)
    common.write_jsonl(src / "projects.jsonl", [_rec("alice/scraper", stars=500), _rec("bob/tool")])
    common.write_json(src / "MANIFEST.json", {"as_of": "2026-09-29T10:34:38Z", "schema": "the-source.discover/1",
                                              "sha256": {"projects.jsonl": common.sha256_file(src / "projects.jsonl")}})
    out = tmp_path / "data" / "corpus"
    C.export(src, out)
    return out


def _entry(key, provider="github_search", query="gh-topic-selfhosted-active", **kw):
    return {"key": key, "first_seen": "2026-10-10", "last_seen": "2026-10-17", "aliases": [],
            "providers": [{"provider": provider, "query": query}], "status": "awaiting_relationship_evidence", **kw}


def _pool(*entries):
    return {e["key"]: e for e in entries}


def _m(nwo, stars, db, **kw):
    flags = {"disabled": False, "empty": False, "is_fork": False, "is_mirror": False, "private": False}
    return _meta(nwo, stars, database_id=db, **{**flags, **kw})


def test_new_candidates_are_admitted_and_counted(layer):
    pool = _pool(_entry("github.com/new/one"),
                 _entry("github.com/new/two", "ecosystems_awesome", "ecosystems-awesome-selfhosted"),
                 _entry("github.com/new/radar", "community_radar", "hn"),
                 _entry("github.com/alice/scraper"))  # already a member
    api = FakeApi({"new/one": _m("New/One", 900, 11), "new/two": _m("new/two", 40, 12, licence_spdx=None),
                   "new/radar": _m("new/radar", 70, 13)})
    before = common.read_json(layer / "MANIFEST.json")
    out = A.admit(pool, NOW, layer, api)
    assert (out["membership_before"], out["membership_after"], out["corpus_added"]) == (2, 5, 3)
    assert (out["corpus_removed"], out["corpus_collapsed"]) == (0, 0)
    assert out["dropped"] == {"already_in_corpus": 1}
    assert out["membership_as_of"] == "2026-10-17T12:00:00Z" != before["membership"]["as_of"]
    assert out["manifest_sha256_after"] != out["manifest_sha256_before"]
    assert [t["id"] for t in out["top_new_by_stars"]] == ["github.com/new/one", "github.com/new/radar",
                                                         "github.com/new/two"]
    by = out["candidates_found_by_strategy"]
    assert by["github_search:gh-topic-selfhosted-active"] == {"found": 2, "new_to_corpus": 1, "admitted": 1}
    assert by["community_radar:hn"]["admitted"] == 1
    assert C.validate(layer)["failed_checks"] == []
    rows = {p["id"]: p for p in common.read_jsonl(layer / "projects.jsonl")}
    assert rows["github.com/new/two"]["licence"]["status"] == "issue"  # retained, never recommended
    assert rows["github.com/new/two"]["licence"]["issue"]["category"] == "missing"
    assert rows["github.com/new/one"]["provenance"][0]["strategy"] == "discovery_search"
    assert rows["github.com/new/radar"]["provenance"][0]["strategy"] == "community_radar"
    m = common.read_json(layer / "MANIFEST.json")
    assert m["counts"]["projects"] == 5 and m["membership"]["admission"]["last"]["admitted"] == 3


def test_the_gate_and_dedupe_drop_with_reasons(layer):
    alice_id = common.read_jsonl(layer / "projects.jsonl")[0]["host_repo_id"]
    pool = _pool(_entry("github.com/x/archived"), _entry("github.com/x/fork"), _entry("github.com/x/stale"),
                 _entry("github.com/x/gone"), _entry("github.com/x/renamed-alice"), _entry("gitlab.com/x/y"))
    api = FakeApi({"x/archived": _m("x/archived", 99, 21, archived=True),
                   "x/fork": _m("x/fork", 99, 22, is_fork=True),
                   "x/stale": _m("x/stale", 99, 23, pushed_at="2024-01-01T00:00:00Z"),
                   "x/gone": {"found": False, "error": "not_found"},
                   "x/renamed-alice": _m("alice/scraper", 500, alice_id)})
    sha = A.manifest_sha(layer)
    out = A.admit(pool, NOW, layer, api)
    assert out["corpus_added"] == 0 and out["manifest_sha256_after"] == sha == A.manifest_sha(layer)
    assert out["dropped"] == {"archived": 1, "fork_alias": 1, "stale_no_meaningful_activity_12_months": 1,
                              "repository_not_found": 1, "already_in_corpus_after_rename": 1,
                              "host_not_independently_verifiable": 1}


def test_a_re_export_keeps_admitted_rows(layer):
    A.admit(_pool(_entry("github.com/new/one")), NOW, layer, FakeApi({"new/one": _m("new/one", 9, 31)}))
    C.export(layer.parent / "discover" / "corpus", layer)
    ids = {p["id"] for p in common.read_jsonl(layer / "projects.jsonl")}
    assert "github.com/new/one" in ids and C.validate(layer)["failed_checks"] == []


# ---------------------------------------------------------------- anomalies

SCHED = {"cadence": "daily"}


def _run(rid, key, added, trigger="schedule", status="succeeded", sha=("a", "b")):
    return {"run_id": rid, "cycle_key": key, "trigger": trigger, "status": status, "started_at": key + "T00:00:00Z",
            "corpus": {"corpus_added": added, "manifest_sha256_before": sha[0], "manifest_sha256_after": sha[1]}}


def _codes(flags):
    return {f["code"] for f in flags}


def test_no_anomaly_on_a_healthy_run():
    prev = [_run("r1", "2026-10-15", 4), _run("r2", "2026-10-16", 0)]
    assert A.anomalies(_run("r3", "2026-10-17", 2), prev, SCHED) == []


def test_zero_additions_on_two_consecutive_scheduled_runs_is_flagged():
    prev = [_run("r1", "2026-10-15", 3), _run("r2", "2026-10-16", 0)]
    assert "zero_corpus_additions_consecutive" in _codes(A.anomalies(_run("r3", "2026-10-17", 0), prev, SCHED))
    # one quiet run is not an anomaly, a manual run does not count as scheduled, and a record from before
    # admission existed proves nothing
    assert "zero_corpus_additions_consecutive" not in _codes(
        A.anomalies(_run("r3", "2026-10-17", 0), [_run("r2", "2026-10-16", 2)], SCHED))
    assert "zero_corpus_additions_consecutive" not in _codes(
        A.anomalies(_run("m", "manual-x", 0, "manual"), prev, SCHED))
    old = {"run_id": "r2", "cycle_key": "2026-10-16", "trigger": "schedule", "status": "succeeded"}
    assert "zero_corpus_additions_consecutive" not in _codes(A.anomalies(_run("r3", "2026-10-17", 0), [old], SCHED))


def test_unchanged_manifest_sha_is_flagged():
    flags = A.anomalies(_run("r3", "2026-10-17", 0, sha=("abc", "abc")), [], SCHED)
    assert _codes(flags) == {"corpus_manifest_unchanged"}
    assert A.anomalies(_run("r3", "2026-10-17", 1, sha=("abc", "def")), [], SCHED) == []


def test_a_scheduled_run_missing_from_the_cadence_window_is_flagged():
    prev = [_run("r1", "2026-10-13", 1), _run("r2", "2026-10-14", 1)]
    flags = A.anomalies(_run("r5", "2026-10-17", 1), prev, SCHED)
    miss = next(f for f in flags if f["code"] == "scheduled_run_missing")
    assert miss["missing"] == ["2026-10-15", "2026-10-16"]
    # a manual run judged against today's cycle key sees the same gap
    flags = A.anomalies(_run("m", "manual-x", 1, "manual"), prev, SCHED, cycle_key_now="2026-10-17")
    assert "scheduled_run_missing" in _codes(flags)
    # weekly cadence: one record a week is complete
    weekly = [_run("w1", "2026-10-03", 1), _run("w2", "2026-10-10", 1)]
    assert A.anomalies(_run("w3", "2026-10-17", 1), weekly, {"cadence": "weekly"}) == []


# ---------------------------------------------------------------- MCP last_run


def test_last_run_view_carries_corpus_counts_and_anomalies():
    rec = {"run_id": "r", "status": "succeeded", "counts": {"corpus_added": 3, "membership_after": 9366},
           "corpus": {"corpus_added": 3, "corpus_removed": 0, "corpus_collapsed": 0, "membership_before": 9363,
                      "membership_after": 9366, "membership_as_of": "2026-10-17T12:00:00Z",
                      "candidates_found_by_strategy": {"github_search:x": {"found": 3, "new_to_corpus": 3,
                                                                           "admitted": 3}},
                      "top_new_by_stars": [{"id": "github.com/a/b", "stars": 9}], "manifest_sha256_before": "x"},
           "anomalies": [{"code": "scheduled_run_missing", "detail": "no scheduled run for 2026-10-16"}]}
    view = M._run_view(rec)
    assert view["counts"] == {"corpus_added": 3, "membership_after": 9366}
    assert view["corpus"]["membership_before"] == 9363 and view["corpus"]["top_new_by_stars"][0]["stars"] == 9
    assert "manifest_sha256_before" not in view["corpus"]
    assert view["anomalies"][0]["code"] == "scheduled_run_missing"
    assert M._run_view({"run_id": "old"})["corpus"] is None and M._run_view({"run_id": "old"})["anomalies"] == []



def test_cycle_step_puts_the_corpus_back_when_quick_scores_cannot_follow(layer, monkeypatch, tmp_path):
    from source_pipeline import cycle, daily, quick_score

    quick = tmp_path / "data" / "quick"
    quick.mkdir(parents=True)
    (quick / "MANIFEST.json").write_text("{}")
    real = A.admit
    monkeypatch.setattr(C, "OUT", layer)
    monkeypatch.setattr(quick_score, "OUT", quick)
    monkeypatch.setattr(daily, "load_pool", lambda: _pool(_entry("github.com/new/one")))
    monkeypatch.setattr(A, "admit", lambda pool, at: real(pool, at, layer, FakeApi({"new/one": _m("new/one", 9, 41)})))
    monkeypatch.setattr(quick_score, "run", lambda at: {"last_run": {}})
    monkeypatch.setattr(quick_score, "validate",
                        lambda: {"failed_checks": 1, "checks": [{"id": "corpus_version", "ok": False}]})
    before = {p.name: p.read_bytes() for p in layer.iterdir()}
    res = {"failures": [], "counts": {}}
    step = cycle._corpus_step(NOW, res)
    assert step["status"] == "failed" and "quick scores" in step["errors"][0]
    assert {p.name: p.read_bytes() for p in layer.iterdir()} == before
    assert (quick / "MANIFEST.json").read_text() == "{}"
