"""KEI-848: daily intelligence cycle. No network."""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone

import pytest

from source_pipeline import common, corroborate, cycle, daily

POLICY = {"maintenance": {"active_days": 90, "maintained_days": 365}}
DEEP = {"max_age_days": 7, "max_per_cycle": 3, "material_star_change": {"absolute": 500, "relative": 0.1}}
AS_OF = "2026-10-02T00:00:00Z"


def _rec(**kw):
    base = {"found": True, "canonical_id": "github.com/o/a", "host_repo_id": 1, "archived": False, "disabled": False,
            "licence_spdx": "MIT", "stars": 1000, "forks": 10, "pushed_at": "2026-09-01T00:00:00Z",
            "fetched_at": "2026-09-30T00:00:00Z"}
    return {**base, **kw}


def _root(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "DATA", tmp_path / "data")
    return tmp_path


# ---------------------------------------------------------------- schedule

def _schedule(tmp_path, **over):
    cfg = {"cadence": "daily", "timezone": "Asia/Dubai", "time": "03:00", "window_hours": 4,
           "catch_up_hours": 16, "max_attempts": 3}
    cfg.update(over)
    p = tmp_path / "schedule.yaml"
    p.write_text(json.dumps(cfg))
    return p


def test_daily_cadence_is_due_every_day(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle, "SCHEDULE", _schedule(tmp_path))
    monkeypatch.setattr(cycle, "RUNS", tmp_path / "runs")
    for day in range(1, 8):  # every day of a week, 03:30 Dubai = 23:30 UTC the day before
        d = cycle.due(datetime(2026, 10, day, 23, 30, tzinfo=timezone.utc))
        assert d["due"] and d["cadence"] == "daily" and d["cycle_key"] == f"2026-10-{day + 1:02d}"


def test_daily_catch_up_and_success_are_per_day(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle, "SCHEDULE", _schedule(tmp_path))
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(cycle, "RUNS", runs)
    late = cycle.due(datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc))  # 16:00 Dubai: window over, catch-up on
    assert late["due"] and late["late"] and late["cycle_key"] == "2026-10-02"
    (runs / "r.json").write_text(json.dumps({"cycle_key": "2026-10-02", "trigger": "schedule", "status": "succeeded"}))
    assert cycle.due(datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc))["reason"] == "today's cycle already succeeded"
    assert cycle.due(datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc))["due"]  # next day is its own cycle
    assert not cycle.due(datetime(2026, 10, 2, 21, 30, tzinfo=timezone.utc))["due"]  # 01:30 Dubai, between


def test_window_plus_catch_up_must_fit_the_period(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle, "SCHEDULE", _schedule(tmp_path, catch_up_hours=20))
    monkeypatch.setattr(cycle, "RUNS", tmp_path / "runs")
    with pytest.raises(cycle.CycleError):
        cycle.due(datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc))


def test_committed_schedule_is_daily():
    cfg = common.load_yaml(common.CONFIG / "schedule.yaml")
    assert cfg["cadence"] == "daily" and cfg["window_hours"] + cfg["catch_up_hours"] < 24


# ---------------------------------------------------------------- light refresh

class FakeHttp:
    def __init__(self, responses):
        self.responses, self.calls, self.seen = responses, 0, []

    def call(self, url, data=None, auth=False, tries=5):
        self.calls += 1
        self.seen.append((url, data))
        for prefix, resp in self.responses:
            if url.startswith(prefix):
                return resp(url, data) if callable(resp) else resp
        return 500, None


def _gql_answer(url, data):
    q = data["query"]
    out, errors = {}, []
    for i in range(q.count("repository(")):
        if f'r{i}: repository(owner:"o", name:"gone")' in q:
            out[f"r{i}"] = None
            errors.append({"path": [f"r{i}"], "type": "NOT_FOUND"})
        else:
            out[f"r{i}"] = {"nameWithOwner": "o/A", "databaseId": 7, "isArchived": False, "isDisabled": False,
                            "stargazerCount": 5, "forkCount": 1, "pushedAt": "2026-10-01T00:00:00Z",
                            "licenseInfo": {"spdxId": "MIT"}, "issues": {"totalCount": 3},
                            "latestRelease": {"tagName": "v1", "publishedAt": "2026-09-01T00:00:00Z"},
                            "defaultBranchRef": {"target": {"oid": "abc", "committedDate": "2026-10-01T00:00:00Z"}}}
    return 200, {"data": out, "errors": errors}


def test_light_refresh_batches_and_separates_real_answers_from_failures(monkeypatch):
    monkeypatch.setattr(common, "load_policy", lambda: {"hosts": {"supported": ["github.com", "gitlab.com"]}})
    http = FakeHttp([("https://api.github.com/graphql", _gql_answer),
                     ("https://api.github.com/repos/o/gone", (404, None)),
                     ("https://gitlab.com/", (503, None))])
    keys = [f"github.com/o/a{i}" for i in range(120)] + ["github.com/o/gone", "gitlab.com/g/p", "example.org/x/y"]
    got = daily.light_refresh(keys, batch_size=50, as_of=datetime(2026, 10, 2, tzinfo=timezone.utc), http=http)
    gql_calls = [u for u, _ in http.seen if u.endswith("graphql")]
    assert len(gql_calls) == 3  # 121 GitHub keys in batches of 50
    p = got["projects"]
    assert p["github.com/o/a0"]["found"] and p["github.com/o/a0"]["head"]["oid"] == "abc"
    assert p["github.com/o/gone"]["error"] == "not_found" and not p["github.com/o/gone"].get("transient")
    assert p["example.org/x/y"]["error"] == "unsupported_host"
    assert got["failed_keys"] == ["gitlab.com/g/p"]  # only the transient failure is a failure


def test_batch_http_failure_marks_every_key_transient():
    http = FakeHttp([("https://api.github.com/graphql", (502, None))])
    out = daily._light_github_batch(http, ["github.com/o/a", "github.com/o/b"], AS_OF)
    assert all(v["transient"] for v in out.values())


# ---------------------------------------------------------------- change detection

def test_classify_new_changed_stale_unchanged_failed():
    previous = {
        "github.com/o/same": _rec(fetched_at="2026-09-30T00:00:00Z"),
        "github.com/o/old": _rec(fetched_at="2026-09-20T00:00:00Z"),
        "github.com/o/arch": _rec(),
        "github.com/o/lic": _rec(),
        "github.com/o/moved": _rec(),
        "github.com/o/stars": _rec(stars=10000),
        "github.com/o/drift": _rec(stars=10000),
        "github.com/o/flaky": _rec(),
        "github.com/o/rel": {**_rec(), "latest_release_tag": "v1"},
        "github.com/o/pre": {**_rec(), "observations": {"releases": [{"ref": "v3-beta"}]}},
    }
    light = {"projects": {
        "github.com/o/same": _rec(stars=1010),
        "github.com/o/old": _rec(),
        "github.com/o/arch": _rec(archived=True),
        "github.com/o/lic": _rec(licence_spdx="BUSL-1.1"),
        "github.com/o/moved": _rec(canonical_id="github.com/n/a", host_repo_id=1),
        "github.com/o/stars": _rec(stars=11500),
        "github.com/o/drift": _rec(stars=10400),
        "github.com/o/flaky": {"found": False, "transient": True, "error": "graphql_http_502"},
        "github.com/o/rel": _rec(latest_release={"tag": "v2"}),
        "github.com/o/pre": _rec(latest_release={"tag": "v2"}),
        "github.com/o/brand-new": _rec(),
    }}
    c = daily.classify(light, previous, DEEP, POLICY, AS_OF)
    assert c["github.com/o/same"]["class"] == "unchanged"
    assert c["github.com/o/drift"]["class"] == "unchanged"  # 4% star drift is not material
    assert c["github.com/o/old"]["class"] == "stale"
    assert c["github.com/o/pre"]["class"] == "unchanged"  # no light answer yet: nothing like-for-like
    assert c["github.com/o/brand-new"]["class"] == "new"
    assert c["github.com/o/flaky"]["class"] == "failed"
    for k, why in [("arch", "archived"), ("lic", "licence"), ("moved", "identity"), ("stars", "stars"),
                   ("rel", "release")]:
        assert c[f"github.com/o/{k}"]["class"] == "changed", k
        assert any(r.startswith(why) for r in c[f"github.com/o/{k}"]["reasons"]), (k, c[f"github.com/o/{k}"])


def test_classify_reenriches_near_a_maintenance_boundary_only_when_pushed():
    prev = {"github.com/o/a": _rec(fetched_at="2026-09-30T00:00:00Z")}
    pushed = {"projects": {"github.com/o/a": _rec(pushed_at="2026-10-01T00:00:00Z")}}
    quiet = {"projects": {"github.com/o/a": _rec(pushed_at="2026-09-01T00:00:00Z")}}
    near = {"github.com/o/a": "2026-07-06T00:00:00Z"}  # 88 days before AS_OF: near the 90-day boundary
    far = {"github.com/o/a": "2026-09-20T00:00:00Z"}
    assert daily.classify(pushed, prev, DEEP, POLICY, AS_OF, near)["github.com/o/a"]["class"] == "changed"
    assert daily.classify(quiet, prev, DEEP, POLICY, AS_OF, near)["github.com/o/a"]["class"] == "unchanged"
    assert daily.classify(pushed, prev, DEEP, POLICY, AS_OF, far)["github.com/o/a"]["class"] == "unchanged"


def test_select_deep_takes_new_and_changed_then_oldest_rotation_within_budget():
    classes = {"n": {"class": "new"}, "c": {"class": "changed"},
               "s1": {"class": "stale", "deep_age_days": 8}, "s2": {"class": "stale", "deep_age_days": 30},
               "s3": {"class": "stale", "deep_age_days": 9}, "u": {"class": "unchanged", "deep_age_days": 1}}
    sel, deferred = daily.select_deep(classes, 3)
    assert sel == ["c", "n", "s2"] and deferred == ["s3", "s1"]
    sel, deferred = daily.select_deep({"a": {"class": "new"}, "b": {"class": "new"}}, 1)
    assert sel == ["a", "b"]  # new and changed are never deferred


def test_merge_snapshot_overlays_light_and_keeps_deep_evidence():
    prev = {"projects": {"github.com/o/a": {**_rec(), "observations": {"commits": [1]}},
                         "github.com/o/dropped": _rec()}}
    light = {"as_of": AS_OF, "http_calls": 2, "projects": {
        "github.com/o/a": _rec(stars=1100, fetched_at=AS_OF), "github.com/o/n": _rec(fetched_at=AS_OF)}}
    deep = {"projects": {"github.com/o/n": {**_rec(fetched_at=AS_OF), "observations": {"commits": [2]}}},
            "http_calls": 5}
    m = daily.merge_snapshot(prev, light, deep, ["github.com/o/a", "github.com/o/n"])
    a = m["projects"]["github.com/o/a"]
    assert a["stars"] == 1100 and a["fetched_at"] == AS_OF and a["deep_fetched_at"] == "2026-09-30T00:00:00Z"
    assert a["observations"] == {"commits": [1]}
    assert m["projects"]["github.com/o/n"]["deep_fetched_at"] == AS_OF
    assert "latest_release_tag" in a and a["latest_release_tag"] is None
    assert "github.com/o/dropped" not in m["projects"] and m["http_calls"] == 7


# ---------------------------------------------------------------- history

def test_history_is_deterministic_and_answers_1_7_30_90_day_deltas(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    def light(day, stars):
        return {"as_of": f"{day}T00:00:00Z", "projects": {"github.com/o/a": _rec(stars=stars, fetched_at=f"{day}T00:00:00Z")}}
    days = {"2026-07-01": 100, "2026-09-01": 400, "2026-09-25": 900, "2026-10-01": 990, "2026-10-02": 1000}
    for d, s in days.items():
        daily.write_history(d, light(d, s), {}, "v", f"run-{d}")
    raw1 = (root / "data/history/daily/2026-10-02.jsonl.gz").read_bytes()
    daily.write_history("2026-10-02", light("2026-10-02", 1000), {}, "v", "run-2026-10-02")
    assert (root / "data/history/daily/2026-10-02.jsonl.gz").read_bytes() == raw1  # byte-identical rewrite
    assert json.loads(gzip.decompress(raw1))["stars"] == 1000
    want = {1: ("2026-10-01", 10, True), 7: ("2026-09-25", 100, True), 30: ("2026-09-01", 600, False),
            90: ("2026-07-01", 900, False)}
    for n, (base, d, exact) in want.items():
        r = daily.delta(n, "2026-10-02")
        assert r["ok"] and r["base"] == base and r["exact"] is exact and r["projects"][0]["stars_delta"] == d, n
    short = daily.delta(120, "2026-10-02")
    assert not short["ok"] and "insufficient history" in short["reason"]


def test_tampered_history_is_refused(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    daily.write_history("2026-10-02", {"as_of": AS_OF, "projects": {"k": _rec()}}, {}, "v", "r")
    p = root / "data/history/daily/2026-10-02.jsonl.gz"
    p.write_bytes(daily._gz_bytes('{"key":"k","stars":1}\n'))
    with pytest.raises(ValueError):
        daily.load_day("2026-10-02")


def test_prune_keeps_the_retention_window(tmp_path, monkeypatch):
    root = _root(tmp_path, monkeypatch)
    for d in ("2025-01-01", "2026-10-01"):
        daily.write_history(d, {"as_of": AS_OF, "projects": {}}, {}, "v", "r")
    assert daily.prune_history(400, "2026-10-02") == ["2025-01-01"]
    assert daily.history_days() == ["2026-10-01"]


# ---------------------------------------------------------------- discovery reconciliation

def test_candidates_never_duplicate_a_corpus_entity_or_each_other():
    corpus = {"github.com/old/name": _rec(canonical_id="github.com/new/name", host_repo_id=42),
              "github.com/o/p": _rec(canonical_id="github.com/o/p", host_repo_id=7)}
    found = [
        {"key": "github.com/O/P", "provider": "github_search", "query": "l1"},                # case variant
        {"key": "github.com/new/name", "provider": "github_search", "query": "l1"},           # renamed corpus repo
        {"key": "github.com/fork/elsewhere", "provider": "x", "query": "l2", "host_repo_id": 42},  # same id
        {"key": "github.com/z/fresh", "provider": "github_search", "query": "l1", "host_repo_id": 9, "stars": 60},
        {"key": "github.com/z/fresh", "provider": "ecosystems_awesome", "query": "l3", "host_repo_id": 9},
        {"key": "github.com/z/fresh-renamed", "provider": "github_search", "query": "l2", "host_repo_id": 9},
    ]
    r = daily.reconcile_candidates(found, corpus, {}, "2026-10-02")
    assert {k["matches"] for k in r["known"]} == {"github.com/o/p", "github.com/old/name"} and len(r["known"]) == 3
    assert r["new"] == ["github.com/z/fresh"] and list(r["pool"]) == ["github.com/z/fresh"]
    entry = r["pool"]["github.com/z/fresh"]
    assert entry["aliases"] == ["github.com/z/fresh-renamed"] and len(entry["providers"]) == 3
    again = daily.reconcile_candidates(found, corpus, r["pool"], "2026-10-03")
    assert again["new"] == [] and again["pool"]["github.com/z/fresh"]["first_seen"] == "2026-10-02"
    assert again["pool"] == {**r["pool"], "github.com/z/fresh": {**entry, "last_seen": "2026-10-03"}}
    joined = daily.reconcile_candidates(found, {**corpus, "github.com/z/fresh": _rec(canonical_id="github.com/z/fresh",
                                                                                      host_repo_id=9)},
                                        r["pool"], "2026-10-04")
    assert joined["pool"] == {}  # once it enters the corpus it leaves the pool


# ---------------------------------------------------------------- corroboration

def test_corroboration_flags_but_never_changes_canonical(monkeypatch):
    monkeypatch.setattr(corroborate, "load_cfg", lambda: {
        "sample_per_day": 10, "mailto": None, "tolerance": {"stars_absolute": 200, "stars_relative": 0.05},
        "degraded_if": {"error_rate_above": 0.2, "agreement_below": 0.8},
        "providers": {"ecosystems": {"trusted_for": ["x"]}, "depsdev": {"trusted_for": ["y"]}}})
    projects = {f"github.com/o/r{i}": _rec(host_repo_id=1000 + i, stars=10000, canonical_id=f"github.com/o/r{i}")
                for i in range(10)}
    before = json.dumps(projects, sort_keys=True)

    def eco(url, data):
        if "/r9" in url:
            return 404, None
        if "/r0" in url:
            return 200, {"stargazers_count": 3000, "archived": True, "license": "gpl-3.0", "uuid": "999"}
        return 200, {"stargazers_count": 10100, "archived": False, "license": "mit", "uuid": str(1000 + int(url.rsplit("/r", 1)[1]))}

    http = FakeHttp([("https://repos.ecosyste.ms/", eco), ("https://api.deps.dev/", (503, None))])
    out = corroborate.run(projects, datetime(2026, 10, 2, tzinfo=timezone.utc), http)["summary"]
    assert json.dumps(projects, sort_keys=True) == before
    e, d = out["providers"]["ecosystems"], out["providers"]["depsdev"]
    assert e["status"] == "ok" and e["agreement"] == round(1 - 1 / 9, 4) and e["coverage"] == 0.9
    assert e["not_covered"] == ["github.com/o/r9"]
    assert set(e["disagreeing_keys"]["github.com/o/r0"]) == {"stars", "archived", "licence", "identity"}
    assert d["status"] == "degraded" and d["errors"] == 10  # an outage is visible, not silent
    assert out["status"] == "degraded" and out["degraded_providers"] == ["depsdev"]


def test_corroboration_sample_rotates_and_covers_the_corpus():
    keys = [f"k{i}" for i in range(100)]
    seen = set()
    for day in range(1, 40):
        seen |= set(corroborate.sample(keys, 20, f"2026-10-{day:02d}" if day < 32 else f"2026-11-{day - 31:02d}"))
    assert len(seen) == 100
    assert corroborate.sample(keys, 20, "2026-10-02") == corroborate.sample(keys, 20, "2026-10-02")
