"""KEI-850: Community Radar. No network."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

from source_pipeline import common, daily, radar

AS_OF = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)
HN = "https://hacker-news.firebaseio.com/v0"
FOREM = "https://dev.to/api"
GH = "https://api.github.com/repos/"
EPOCH = lambda days: int(AS_OF.timestamp() - days * 86400)  # noqa: E731


class FakeHttp:
    def __init__(self, responses):
        self.responses, self.calls, self.seen = responses, 0, []

    def call(self, url, data=None, auth=False, tries=5, token=None):
        self.calls += 1
        self.seen.append(url)
        if token:
            self.tokens = getattr(self, "tokens", []) + [token]
        return self.responses.get(url, (404, None))


def _cfg(**over):
    cfg = copy.deepcopy(radar.load_config())
    for path, v in over.items():
        node = cfg
        *head, last = path.split(".")
        for h in head:
            node = node[h]
        node[last] = v
    return cfg


def _root(tmp_path, monkeypatch, corpus=(), canonical=(), pool=()):
    """A scratch DATA tree with a small corpus: [(oss_id, harvested_as, canonical_id, host_repo_id)]."""
    data = tmp_path / "data"
    monkeypatch.setattr(common, "DATA", data)
    monkeypatch.setattr(common, "DATASET", data / "dataset")
    monkeypatch.setattr(common, "LIVE", data / "live")
    common.write_jsonl(data / "dataset" / "oss_projects.jsonl",
                       [{"oss_id": o, "harvested_as": h} for o, h, _, _ in corpus])
    common.write_json(data / "live" / "snapshot.json", {"projects": {
        h[0]: {"found": True, "canonical_id": c, "host_repo_id": i} for _, h, c, i in corpus}})
    common.write_jsonl(data / "canonical" / "oss_projects.jsonl", [{"oss_id": o} for o in canonical])
    common.write_jsonl(data / "discovery" / "candidates.jsonl", list(pool))
    return data


def _story(i, url=None, text=None, score=100, comments=10, age=1.0, **kw):
    return {"id": i, "type": "story", "time": EPOCH(age), "score": score, "descendants": comments,
            "url": url, "text": text, "title": "A title nobody may store", "by": "someone", **kw}


def _article(i, reactions=50, comments=5, age=1.0, url=None):
    pub = datetime.fromtimestamp(EPOCH(age), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": i, "positive_reactions_count": reactions, "comments_count": comments, "published_at": pub,
            "url": url or f"https://dev.to/someone/post-{i}", "title": "t", "user": {"username": "someone"}}


def _mention(source, item, repos, points=100, comments=10, age=1.0, observed="2026-10-06T00:00:00Z"):
    pub = datetime.fromtimestamp(EPOCH(age), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    link = f"https://news.ycombinator.com/item?id={item}" if source == "hn" else f"https://dev.to/x/{item}"
    return {"source": source, "item_id": str(item), "permalink": link, "published_at": pub, "observed_at": observed,
            "points": points, "comments": comments, "via": ["beststories" if source == "hn" else "opensource"],
            "repos": list(repos)}


# ---------------------------------------------------------------- versioning

def test_committed_parameters_match_their_released_version():
    assert radar.check_version(radar.load_config()) == []


def test_changing_a_ranking_parameter_without_a_version_bump_is_refused():
    cfg = _cfg(**{"ranking.half_life_days": 4})
    assert "without a version bump" in radar.check_version(cfg)[0]
    # collection budgets are operational, not methodology
    assert radar.check_version(_cfg(**{"sources.hn.max_items": 10, "resolve.max_lookups": 1})) == []


# ---------------------------------------------------------------- extraction

def test_links_are_normalised_deduplicated_and_capped():
    ex = radar.load_config()["extract"]
    got = radar.extract_repos([
        "See https://github.com/Foo/Bar.git and https://www.github.com/foo/bar/tree/main/src, "
        "https://github.com/sponsors/foo, https://github.com/user-attachments/assets/x, "
        "https://gist.github.com/a/b, https://example.com/github.com/x/y, (https://codeberg.org/forgejo/forgejo).",
        "https:&#x2F;&#x2F;github.com&#x2F;Org&#x2F;Thing&#x2F;issues",
        "https://gitlab.com/a/b https://github.com/c/d"], ex)
    assert got == ["github.com/foo/bar", "codeberg.org/forgejo/forgejo", "github.com/org/thing"]


# ---------------------------------------------------------------- collection

def _bluesky_urls(as_of=AS_OF, cfg=None):
    """The exact search addresses the collector asks, on each configured host."""
    import urllib.parse
    from datetime import timedelta

    src = (cfg or radar.load_config())["sources"]["bluesky"]
    since = radar.iso(as_of - timedelta(days=float(src["max_age_days"])))
    out = []
    for q in src["queries"]:
        params = urllib.parse.urlencode({"q": q, "domain": src["domain"], "sort": src.get("sort", "top"),
                                         "since": since, "limit": int(src["limit"])})
        out += [f"{h.rstrip('/')}/xrpc/app.bsky.feed.searchPosts?{params}" for h in src["hosts"]]
    return out


def _sources(hn_items=None, forem_tags=None, forem_bodies=None, hn_lists=None, bluesky=None):
    r = {}
    for u in _bluesky_urls():
        r[u] = (200, bluesky if bluesky is not None else {"posts": []})
    for name in ("showstories", "beststories", "topstories"):
        r[f"{HN}/{name}.json"] = (200, (hn_lists or {}).get(name, []))
    for i, it in (hn_items or {}).items():
        r[f"{HN}/item/{i}.json"] = (200, it)
    for tag in ("opensource", "selfhosted", "showdev", "github"):
        r[f"{FOREM}/articles?tag={tag}&top=7&per_page=30"] = (200, (forem_tags or {}).get(tag, []))
    for i, body in (forem_bodies or {}).items():
        r[f"{FOREM}/articles/{i}"] = (200, {"id": i, "body_markdown": body, "user": {"name": "x"}})
    return r


def test_collection_dedupes_items_filters_and_stores_no_authored_content():
    items = {1: _story(1, "https://github.com/a/one"), 2: _story(2, "https://github.com/b/two", score=5),
             3: _story(3, "https://github.com/c/three", age=9), 4: _story(4, "https://example.com/blog"),
             5: _story(5, None, text="Show HN: https:&#x2F;&#x2F;github.com&#x2F;e&#x2F;five"),
             6: _story(6, "https://github.com/f/six", dead=True), 7: {"id": 7, "type": "comment"}}
    arts = [_article(10), _article(11, reactions=3), _article(12), _article(13)]  # 13: listed, then removed
    http = FakeHttp(_sources(items, {"opensource": arts, "showdev": [arts[0]]},
                             {10: "Repo: https://github.com/g/ten", 12: "no links here"},
                             {"showstories": [5, 1], "beststories": [1, 2, 3, 4], "topstories": [6, 7, 1]}))
    got = radar.collect(AS_OF, http)
    hn = [m for m in got["mentions"] if m["source"] == "hn"]
    fm = [m for m in got["mentions"] if m["source"] == "forem"]
    assert [m["item_id"] for m in hn] == ["5", "1"] and hn[1]["via"] == ["beststories", "showstories", "topstories"]
    assert sum(u.endswith("/item/1.json") for u in http.seen) == 1          # one read per item, whatever the lists
    assert [(m["item_id"], m["repos"], m["via"]) for m in fm] == [("10", ["github.com/g/ten"], ["opensource", "showdev"])]
    assert not any(u.endswith("/articles/11") for u in http.seen)          # under min_reactions: never read
    allowed = {"source", "item_id", "permalink", "published_at", "observed_at", "points", "comments", "via", "repos"}
    assert all(set(m) == allowed for m in got["mentions"])
    assert "someone" not in json.dumps(got["mentions"]).replace("dev.to/someone", "")  # no usernames, only addresses
    assert got["sources"]["hn"]["status"] == "ok" and got["sources"]["hn"]["items_read"] == 7
    assert got["sources"]["forem"]["status"] == "ok" and got["sources"]["forem"]["gone"] == 1


def test_item_caps_bound_the_cost():
    ids = list(range(100, 140))
    items = {i: _story(i, f"https://github.com/o/r{i}") for i in ids}
    http = FakeHttp(_sources(items, hn_lists={"showstories": ids, "beststories": ids, "topstories": ids}))
    got = radar.collect(AS_OF, http, _cfg(**{"sources.hn.max_items_per_list": 10, "sources.hn.max_items": 15}))
    assert got["sources"]["hn"]["items_read"] == 10 and got["sources"]["hn"]["http_calls"] == 3 + 10


def test_a_source_outage_degrades_the_radar_never_the_other_source():
    http = FakeHttp({**_sources({1: _story(1, "https://github.com/a/one")}, hn_lists={"beststories": [1]}),
                     **{f"{FOREM}/articles?tag={t}&top=7&per_page=30": (503, None)
                        for t in ("opensource", "selfhosted", "showdev", "github")}})
    got = radar.collect(AS_OF, http)
    assert got["sources"]["forem"]["status"] == "failed" and got["sources"]["hn"]["status"] == "ok"
    assert len(got["mentions"]) == 1


def test_network_refusal_is_a_recorded_source_failure(monkeypatch):
    from source_pipeline.enrich import Http

    monkeypatch.setenv("THE_SOURCE_NO_NETWORK", "1")
    for k in ("X_BEARER_TOKEN", "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID"):
        monkeypatch.delenv(k, raising=False)
    got = radar.collect(AS_OF, Http())
    assert {s: v["status"] for s, v in got["sources"].items()} == {
        "hn": "failed", "forem": "failed", "bluesky": "failed", "x": "unconfigured", "instagram": "unconfigured"}
    assert got["mentions"] == []


def test_merge_keeps_the_newest_observation_and_prunes_old_mentions():
    old = _mention("hn", 1, ["github.com/a/b"], points=10, observed="2026-10-05T00:00:00Z")
    new = _mention("hn", 1, ["github.com/a/b"], points=90, observed="2026-10-06T00:00:00Z")
    stale = _mention("forem", 2, ["github.com/c/d"], age=40)
    got = radar.merge_mentions([old, stale], [new], AS_OF, 30)
    expect = {**new, "observations": [{"at": "2026-10-05T00:00:00Z", "points": 10, "comments": 10},
                                      {"at": "2026-10-06T00:00:00Z", "points": 90, "comments": 10}]}
    assert got == [expect]                                                   # the earlier reading is kept, oldest first
    assert radar.merge_mentions([new], [old], AS_OF, 30) == [expect]       # order of arrival does not matter
    assert radar.merge_mentions([new], [new], AS_OF, 30) == [new]          # the same reading twice is one observation


# ---------------------------------------------------------------- ranking

def _hold(data, mentions, resolved=(), coll=None):
    d = data / "radar"
    common.write_jsonl(d / "mentions.jsonl", mentions)
    common.write_jsonl(d / "resolved.jsonl", list(resolved))
    common.write_json(d / "collection.json", coll or {"as_of": "2026-10-06T00:00:00Z", "sources": {
        s: {"status": "ok", "listed": 1, "items_read": 1, "gone": 0, "mentions": 1, "errors": [], "http_calls": 1}
        for s in radar.SOURCES}})


def test_ranking_is_deterministic_capped_and_explained(tmp_path, monkeypatch):
    data = _root(tmp_path, monkeypatch)
    ms = [_mention("hn", 1, ["github.com/a/solo"], points=500, comments=200, age=0.5),
          _mention("hn", 2, ["github.com/x/one", "github.com/x/two", "github.com/x/three"], points=500,
                   comments=200, age=0.5),                                   # a list: weight shared three ways
          _mention("forem", 3, ["github.com/a/solo"], points=40, comments=4, age=1),
          _mention("hn", 4, ["github.com/z/old"], points=500, comments=200, age=6.5),
          _mention("hn", 5, ["github.com/z/gone"], points=900, comments=900, age=8)]  # outside the window
    _hold(data, list(reversed(ms)))
    a = radar.compute("2026-10-06T00:00:00Z")
    _hold(data, ms)
    b = radar.compute("2026-10-06T00:00:00Z")
    assert common.dumps(a) == common.dumps(b)
    rows = a["rows"]
    keys = [r["key"] for r in rows]
    assert keys[0] == "github.com/a/solo" and "github.com/z/gone" not in keys
    solo = rows[0]
    assert solo["sources"]["hn"]["mentions"] == 1 and solo["sources"]["forem"]["mentions"] == 1
    assert "x1.25 (2 sources)" in solo["explanation"]
    one = next(r for r in rows if r["key"] == "github.com/x/one")
    assert one["mentions"][0]["shared_with"] == 2
    assert pytest.approx(one["score"] * 3, abs=1e-3) == solo["mentions"][0]["contribution"]
    # the three list repositories tie on score: broken by key
    tied = [r["key"] for r in rows if r["score"] == one["score"]]
    assert tied == sorted(tied) and len(tied) == 3
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))

    capped = radar.compute("2026-10-06T00:00:00Z", _cfg(**{"ranking.max_ranked": 2}))
    assert len(capped["rows"]) == 2 and capped["manifest"]["counts"]["truncated"] == len(rows) - 2


def test_only_the_strongest_mentions_count(tmp_path, monkeypatch):
    data = _root(tmp_path, monkeypatch)
    _hold(data, [_mention("hn", i, ["github.com/a/b"], points=10 * i) for i in range(1, 9)])
    r = radar.compute("2026-10-06T00:00:00Z")["rows"][0]
    counted = [m for m in r["mentions"] if m["counted"]]
    assert len(r["mentions"]) == 8 and len(counted) == 5
    assert {m["item_id"] for m in counted} == {"4", "5", "6", "7", "8"}
    assert pytest.approx(r["score"], abs=1e-3) == sum(m["contribution"] for m in counted)


# ---------------------------------------------------------------- reconciliation

CORPUS = [("github.com/new-org/tool", ["github.com/old-org/tool"], "github.com/new-org/tool", 42),
          ("github.com/b/listed", ["github.com/b/listed"], "github.com/b/listed", 7)]
POOL = [{"key": "github.com/p/cand", "aliases": ["github.com/p/oldcand"], "host_repo_id": 99, "first_seen": "2026-10-02",
         "last_seen": "2026-10-04", "status": "awaiting_relationship_evidence",
         "providers": [{"provider": "github_search", "query": "x"}]}]


def test_every_address_reconciles_to_one_entity(tmp_path, monkeypatch):
    data = _root(tmp_path, monkeypatch, CORPUS, canonical=["github.com/new-org/tool"], pool=POOL)
    _hold(data, [_mention("hn", 1, ["github.com/old-org/tool"]),          # harvested (old) name
                 _mention("forem", 2, ["github.com/new-org/tool"]),       # current name
                 _mention("hn", 3, ["github.com/moved/tool"]),            # an address GitHub resolves to id 42
                 _mention("hn", 4, ["github.com/b/listed"]),
                 _mention("hn", 5, ["github.com/p/oldcand"]),
                 _mention("hn", 6, ["github.com/gone/away"]),
                 _mention("hn", 7, ["github.com/never/asked"]),
                 _mention("hn", 8, ["codeberg.org/some/repo"])],
          resolved=[{"key": "github.com/moved/tool", "found": True, "resolved_at": "2026-10-06T00:00:00Z",
                     "host_repo_id": 42, "canonical_name": "New-Org/tool", "archived": False, "fork": False},
                    {"key": "github.com/gone/away", "found": False, "resolved_at": "2026-10-06T00:00:00Z",
                     "http_status": 404}])
    rows = {r["key"]: r for r in radar.compute("2026-10-06T00:00:00Z")["rows"]}
    tool = rows["github.com/new-org/tool"]
    assert tool["status"] == "canonical" and tool["oss_id"] == "github.com/new-org/tool"
    assert tool["link_keys"] == ["github.com/moved/tool", "github.com/new-org/tool", "github.com/old-org/tool"]
    assert tool["sources"]["hn"]["mentions"] == 2 and tool["sources"]["forem"]["mentions"] == 1
    assert rows["github.com/b/listed"]["status"] == "corpus"
    assert rows["github.com/p/cand"]["status"] == "candidate" and rows["github.com/p/cand"]["pool_key"] == "github.com/p/cand"
    assert rows["github.com/gone/away"]["status"] == "not_found"
    assert "resolve budget" in rows["github.com/never/asked"]["status_reason"]
    assert rows["codeberg.org/some/repo"]["status"] == "unresolved"
    assert len(rows) == 6


def test_one_item_linking_two_names_of_one_repository_counts_once(tmp_path, monkeypatch):
    data = _root(tmp_path, monkeypatch, CORPUS, canonical=["github.com/new-org/tool"])
    _hold(data, [_mention("hn", 1, ["github.com/old-org/tool", "github.com/new-org/tool"])])
    (r,) = radar.compute("2026-10-06T00:00:00Z")["rows"]
    assert r["sources"]["hn"]["mentions"] == 1 and len(r["mentions"]) == 1


def test_resolution_is_budgeted_cached_and_retries_transient_failures():
    cfg = _cfg(**{"resolve.max_lookups": 2})
    http = FakeHttp({GH + "a/one": (200, {"id": 1, "full_name": "A/one"}), GH + "b/two": (502, None)})
    cache, st = radar.resolve(["github.com/a/one", "github.com/b/two", "github.com/c/three", "codeberg.org/x/y"],
                              {}, AS_OF, http, cfg)
    assert st == {"asked": 2, "resolved": 1, "transient_failures": ["github.com/b/two"], "deferred": 1}
    assert set(cache) == {"github.com/a/one"} and cache["github.com/a/one"]["host_repo_id"] == 1
    http2 = FakeHttp({})
    radar.resolve(["github.com/a/one"], cache, AS_OF, http2, cfg)
    assert http2.calls == 0                                                 # fresh enough: not asked again


# ---------------------------------------------------------------- the daily step, end to end

def _world(tmp_path, monkeypatch):
    data = _root(tmp_path, monkeypatch, CORPUS, canonical=["github.com/new-org/tool"], pool=POOL)
    items = {1: _story(1, "https://github.com/old-org/tool", score=800, comments=300),
             2: _story(2, "https://github.com/fresh/thing", score=600, comments=200),
             3: _story(3, "https://github.com/fresh/forked", score=600, comments=200),
             4: _story(4, "https://github.com/tiny/thing", score=12, comments=0, age=6),
             5: _story(5, "https://github.com/renamed/old", score=300, comments=50)}
    r = _sources(items, {"opensource": [_article(20, reactions=200, comments=30)]},
                 {20: "https://github.com/fresh/thing"}, {"beststories": [1, 2, 3, 4, 5]})
    r.update({GH + "fresh/thing": (200, {"id": 500, "full_name": "Fresh/Thing", "stargazers_count": 900,
                                         "archived": False, "fork": False, "license": {"spdx_id": "MIT"}}),
              GH + "fresh/forked": (200, {"id": 501, "full_name": "fresh/forked", "fork": True}),
              GH + "tiny/thing": (200, {"id": 502, "full_name": "tiny/thing"}),
              GH + "renamed/old": (200, {"id": 99, "full_name": "p/cand"})})  # a pool candidate under a third name
    light = {"github.com/old-org/tool": {"found": True, "canonical_id": "github.com/new-org/tool", "host_repo_id": 42}}
    return data, FakeHttp(r), light


def test_cycle_step_collects_reconciles_admits_and_validates(tmp_path, monkeypatch):
    data, http, light = _world(tmp_path, monkeypatch)
    out = radar.cycle_step(AS_OF, "2026-10-06", light, http)
    assert out["ok"], out["failed_checks"]
    rows = {r["key"]: r for r in common.read_jsonl(data / "radar" / "radar.jsonl")}
    assert rows["github.com/new-org/tool"]["status"] == "canonical"
    assert rows["github.com/p/cand"]["status"] == "candidate"             # renamed into an existing candidate
    assert rows["github.com/fresh/thing"]["status"] == "candidate"        # admitted to the pool
    assert rows["github.com/fresh/thing"]["sources"].keys() == {"hn", "forem"}
    assert rows["github.com/fresh/forked"]["status"] == "unresolved"      # a fork is not admitted
    assert rows["github.com/tiny/thing"]["status"] == "unresolved"        # below pool_min_score
    pool = daily.load_pool()
    assert out["pool_added"] == 1 and set(pool) == {"github.com/p/cand", "github.com/fresh/thing"}
    assert pool["github.com/fresh/thing"]["providers"] == [{"provider": "community_radar", "query": "forem+hn"}]
    assert pool["github.com/fresh/thing"]["host_repo_id"] == 500
    assert not any("old-org/tool" in u for u in http.seen if u.startswith(GH))  # matched by name: no lookup
    s = out["strategy"]
    assert s["id"] == "community_radar" and s["status"] == "ok" and s["top"][0]["rank"] == 1

    # the same day again: idempotent, nothing re-asked that is cached, nothing duplicated
    before = {f: (data / "radar" / f).read_bytes() for f in ("radar.jsonl", "mentions.jsonl")}
    http2 = copy.copy(http)
    http2.calls, http2.seen = 0, []
    again = radar.cycle_step(AS_OF, "2026-10-06", light, http2)
    assert again["ok"] and again["pool_added"] == 0
    assert not any(u.startswith(GH) for u in http2.seen)
    assert {f: (data / "radar" / f).read_bytes() for f in before} == before
    assert len(daily.load_pool()) == 2


def test_validate_catches_tampering_and_unreleased_parameters(tmp_path, monkeypatch):
    data, http, light = _world(tmp_path, monkeypatch)
    assert radar.cycle_step(AS_OF, "2026-10-06", light, http)["ok"]
    assert radar.validate()["ok"]
    p = data / "radar" / "radar.jsonl"
    rows = common.read_jsonl(p)
    rows[0]["score"] += 1
    common.write_jsonl(p, rows)
    failed = {c["id"] for c in radar.validate()["checks"] if not c["ok"]}
    assert {"files_hash", "rebuild_is_byte_identical"} <= failed
    radar.build()
    assert radar.validate()["ok"]
    m = common.read_jsonl(data / "radar" / "mentions.jsonl")
    m[0]["title"] = "smuggled"
    common.write_jsonl(data / "radar" / "mentions.jsonl", m)
    radar.build()
    failed = {c["id"] for c in radar.validate()["checks"] if not c["ok"]}
    assert {"schema_valid", "mentions_hold_no_personal_or_authored_content"} <= failed
    changed = _cfg(**{"ranking.max_ranked": 10})
    monkeypatch.setattr(radar, "load_config", lambda: changed)
    failed = {c["id"] for c in radar.validate()["checks"] if not c["ok"]}
    assert "parameters_match_released_version" in failed


def test_a_corpus_change_after_ranking_is_caught(tmp_path, monkeypatch):
    data, http, light = _world(tmp_path, monkeypatch)
    assert radar.cycle_step(AS_OF, "2026-10-06", light, http)["ok"]
    common.write_jsonl(data / "canonical" / "oss_projects.jsonl", [])     # tool dropped out of canonical
    failed = {c["id"] for c in radar.validate()["checks"] if not c["ok"]}
    assert {"statuses_reconcile_with_corpus_and_pool", "rebuild_is_byte_identical"} <= failed


def test_retrieval_by_any_name_and_with_source_health(tmp_path, monkeypatch):
    data, http, light = _world(tmp_path, monkeypatch)
    radar.cycle_step(AS_OF, "2026-10-06", light, http)
    got = radar.retrieve(["GitHub.com/Old-Org/Tool", "github.com/renamed/old", "github.com/nobody/here"])
    assert {e["key"] for e in got["entries"]} == {"github.com/new-org/tool", "github.com/p/cand"}
    assert got["unknown_keys"] == ["github.com/nobody/here"]
    assert got["sources"]["hn"]["status"] == "ok"
    forem = radar.retrieve(source="forem")
    assert [e["key"] for e in forem["entries"]] == ["github.com/fresh/thing"]
    assert radar.retrieve(status="canonical", limit=1)["matched"] == 1
    with pytest.raises(ValueError):
        radar.retrieve(status="trending")


def test_committed_radar_validates_when_present():
    if not (common.DATA / "radar" / "MANIFEST.json").exists():
        pytest.skip("no radar committed yet")
    report = radar.validate()
    assert report["ok"], [c for c in report["checks"] if not c["ok"]]


def test_absent_radar_is_accepted_only_until_a_cycle_produced_it(tmp_path, monkeypatch):
    _root(tmp_path, monkeypatch)
    monkeypatch.setattr(common, "ROOT", tmp_path)
    assert radar.validate()["ok"]
    common.write_json(tmp_path / "runs" / "r.json", {"status": "succeeded", "strategies": [{"id": "community_radar"}]})
    assert not radar.validate()["ok"]


def test_the_daily_cycle_runs_and_promotes_the_radar():
    import inspect

    from source_pipeline import cycle

    assert "data/radar" in cycle.PROMOTE
    assert "community_radar" in common.load_yaml(common.CONFIG / "schedule.yaml")["strategies"]
    src = inspect.getsource(cycle.work)
    assert src.index('step("history")') < src.index("radar.guarded_cycle_step(") < src.index("step(None)\n\n        canon_now")


def test_a_failing_radar_puts_the_last_good_radar_and_pool_back(tmp_path, monkeypatch):
    data, http, light = _world(tmp_path, monkeypatch)
    assert radar.guarded_cycle_step(AS_OF, "2026-10-06", light, http)["ok"]
    files = {p: p.read_bytes() for p in [*(data / "radar").iterdir(), data / "discovery" / "candidates.jsonl"]}
    world2 = FakeHttp({**http.responses, GH + "brand/new": (200, {"id": 777, "full_name": "brand/new"}),
                       f"{HN}/beststories.json": (200, [9]),
                       f"{HN}/item/9.json": (200, _story(9, "https://github.com/brand/new", score=900, comments=900))})
    real = radar.validate
    monkeypatch.setattr(radar, "validate", lambda: {**real(), "ok": False, "checks": [{"id": "boom", "ok": False}]})
    out = radar.guarded_cycle_step(AS_OF, "2026-10-06", light, world2)
    assert not out["ok"] and out["strategy"]["status"] == "failed" and out["strategy"]["kept_previous"]
    assert {p: p.read_bytes() for p in files} == files
    assert sorted((data / "radar").iterdir()) == sorted(p for p in files if p.parent.name == "radar")
    monkeypatch.setattr(radar, "cycle_step", lambda *a, **k: 1 / 0)
    crashed = radar.guarded_cycle_step(AS_OF, "2026-10-06", light, world2)
    assert not crashed["ok"] and "ZeroDivisionError" in crashed["failed_checks"][0]
    assert {p: p.read_bytes() for p in files} == files
