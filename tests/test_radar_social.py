"""KEI-913: Community Radar social sources (Bluesky live; X and Instagram credential-gated),
independent-author and velocity scoring, and the daily candidate list. No network.

Fixtures follow each operator's documented response shape (app.bsky.feed.searchPosts, the X
API v2 recent search, the Instagram Platform hashtag endpoints) with synthetic content: no
real person's post is stored in this repository."""
from __future__ import annotations

import json
import urllib.parse

import pytest

from source_pipeline import common, daily, radar
from test_radar import AS_OF, CORPUS, FOREM, GH, HN, POOL, FakeHttp, _bluesky_urls, _cfg, _mention, _root, _sources, _story

FX = common.ROOT / "tests" / "fixtures" / "radar"
X_API = "https://api.x.com/2"
IG_API = "https://graph.facebook.com/v21.0"


def _fx(name):
    return json.loads((FX / name).read_text())


def _x_url(cfg=None):
    src = (cfg or radar.load_config())["sources"]["x"]
    q = urllib.parse.urlencode({"query": src["queries"][0], "max_results": int(src["max_results"]),
                                "tweet.fields": "id,author_id,created_at,public_metrics,entities", "sort_order": "recency"})
    return f"{X_API}/tweets/search/recent?{q}"


def _ig_urls(user="17841400000000000", cfg=None):
    src = (cfg or radar.load_config())["sources"]["instagram"]
    out = {}
    for tag in src["hashtags"]:
        out[f"{IG_API}/ig_hashtag_search?{urllib.parse.urlencode({'user_id': user, 'q': tag})}"] = (200, _fx("instagram_hashtag_search.json"))
    q = urllib.parse.urlencode({"user_id": user, "limit": int(src["max_items_per_hashtag"]),
                                "fields": "id,permalink,timestamp,like_count,comments_count,caption,media_type"})
    out[f"{IG_API}/17841562000000001/{src['edge']}?{q}"] = (200, _fx("instagram_hashtag_top_media.json"))
    return out


def _social_world(monkeypatch, with_credentials=True):
    """HN quiet, Forem quiet, Bluesky answering on its fallback host, X and Instagram answering
    their documented shapes when credentials are set."""
    public, api = _bluesky_urls()
    r = _sources(hn_lists={"beststories": []})
    r[public] = (403, None)                       # the documented public host refuses the search method
    r[api] = (200, _fx("bluesky_searchPosts.json"))
    if with_credentials:
        monkeypatch.setenv("X_BEARER_TOKEN", "x-secret-token")
        monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "ig-secret-token")
        monkeypatch.setenv("INSTAGRAM_USER_ID", "17841400000000000")
        r[_x_url()] = (200, _fx("x_search_recent.json"))
        r.update(_ig_urls())
    else:
        for k in ("X_BEARER_TOKEN", "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_USER_ID"):
            monkeypatch.delenv(k, raising=False)
    return FakeHttp(r)


# ---------------------------------------------------------------- collection

def test_bluesky_reads_links_and_opaque_authors_never_text_or_handles(monkeypatch):
    http = _social_world(monkeypatch, with_credentials=False)
    got = radar.collect(AS_OF, http)
    bs = [m for m in got["mentions"] if m["source"] == "bluesky"]
    assert [m["item_id"].split("/")[1] for m in bs] == ["3lxa1", "3lxb2", "3lxc3", "3lxa4", "3lxd5"]
    assert all(m["repos"] == ["github.com/fresh/social-thing"] for m in bs[:4])   # case, deep path and fragment normalise
    assert bs[1]["published_at"] == "2026-10-05T12:00:00Z"                        # fractional seconds dropped
    assert bs[0]["author"] == bs[3]["author"] and len({m["author"] for m in bs}) == 4
    assert all(len(m["author"]) == 16 for m in bs)
    assert all(m["permalink"].startswith("https://bsky.app/profile/did:plc:") for m in bs)
    dumped = json.dumps(got["mentions"])
    for forbidden in ("Someone Real", "example.test", "nobody may store", "handle", "displayName", "text"):
        assert forbidden not in dumped
    st = got["sources"]["bluesky"]
    assert st["status"] == "ok" and st["host"] == "https://api.bsky.app" and st["listed"] == 8 and st["mentions"] == 5
    assert got["sources"]["x"]["status"] == "unconfigured" and got["sources"]["instagram"]["status"] == "unconfigured"
    assert not any("api.x.com" in u or "graph.facebook.com" in u for u in http.seen)   # asked nothing


def test_bluesky_failing_on_every_host_is_a_source_failure_not_a_crash(monkeypatch):
    http = _social_world(monkeypatch, with_credentials=False)
    for u in _bluesky_urls():
        http.responses[u] = (503, None)
    got = radar.collect(AS_OF, http)
    assert got["sources"]["bluesky"]["status"] == "failed" and got["sources"]["bluesky"]["errors"]
    assert got["sources"]["hn"]["status"] == "ok"


def test_x_and_instagram_collect_under_their_credentials_and_store_no_content(monkeypatch):
    http = _social_world(monkeypatch)
    got = radar.collect(AS_OF, http)
    x = [m for m in got["mentions"] if m["source"] == "x"]
    ig = [m for m in got["mentions"] if m["source"] == "instagram"]
    assert [(m["item_id"], m["repos"], m["points"], m["comments"]) for m in x] == [
        ("1900000000000000001", ["github.com/fresh/social-thing"], 300, 20),
        ("1900000000000000002", ["github.com/old-org/tool"], 45, 2)]
    assert x[0]["permalink"] == "https://x.com/i/web/status/1900000000000000001" and len(x[0]["author"]) == 16
    assert "author" not in ig[0]                                                   # hashtag media carries no owner
    assert [(m["item_id"], m["repos"], m["permalink"]) for m in ig] == [
        ("18000000000000001", ["github.com/fresh/social-thing"], "https://www.instagram.com/p/C1abcDEfgh1/"),
        ("18000000000000002", ["github.com/old-org/tool"], "https://www.instagram.com/reel/C1abcDEfgh2/")]
    assert ig[0]["published_at"] == "2026-10-05T14:00:00Z"                          # +0000 normalised
    dumped = json.dumps(got["mentions"])
    for forbidden in ("someone", "Someone Real", "nobody may store", "caption", "walkthrough", "secret"):
        assert forbidden not in dumped
    assert set(http.tokens) == {"x-secret-token", "ig-secret-token"}                # sent as bearer headers
    assert not any("secret" in u for u in http.seen)                                # never in an address
    assert "-is%3Aretweet" in next(u for u in http.seen if u.startswith(X_API))      # reposts dropped at the source
    assert got["sources"]["x"]["status"] == "ok" and got["sources"]["instagram"]["status"] == "ok"
    # Instagram asks the configured number of hashtags, each once
    assert sum("ig_hashtag_search" in u for u in http.seen) == len(radar.load_config()["sources"]["instagram"]["hashtags"])


def test_a_credentialed_source_outage_degrades_only_that_source(monkeypatch):
    http = _social_world(monkeypatch)
    http.responses[_x_url()] = (429, None)
    got = radar.collect(AS_OF, http)
    assert got["sources"]["x"]["status"] == "failed" and got["sources"]["instagram"]["status"] == "ok"
    assert got["sources"]["bluesky"]["status"] == "ok"


def test_the_schema_refuses_a_mention_that_carries_a_username_or_text():
    from jsonschema import Draft202012Validator

    schema = common.read_json(common.SCHEMA / "the-source.radar.schema.json")
    v = Draft202012Validator({**schema, "$ref": "#/$defs/mention"})
    ok = {**_mention("hn", 1, ["github.com/a/b"]), "source": "bluesky", "item_id": "did:plc:abc123/3lxa1",
          "permalink": "https://bsky.app/profile/did:plc:abc123/post/3lxa1", "author": "0123456789abcdef"}
    assert not list(v.iter_errors(ok))
    for bad in ({**ok, "username": "someone"}, {**ok, "text": "hello"}, {**ok, "author": "someone"},
                {**ok, "permalink": "https://bsky.app/profile/someone.bsky.social/post/3lxa1"}):
        assert list(v.iter_errors(bad))


# ---------------------------------------------------------------- ranking

def _ranked(tmp_path, monkeypatch, mentions, cfg=None, pool=(), corpus=(), canonical=(), resolved=()):
    data = _root(tmp_path, monkeypatch, corpus=corpus, canonical=canonical, pool=pool)
    common.write_jsonl(data / "radar" / "mentions.jsonl", mentions)
    common.write_jsonl(data / "radar" / "resolved.jsonl", list(resolved))
    out = radar.compute("2026-10-06T00:00:00Z", cfg)
    return {r["key"]: r for r in out["rows"]}, out


def _social(source, item, repos, author, points=100, comments=5, age=1.0, observations=None):
    m = {**_mention("hn", 1, repos, points=points, comments=comments, age=age), "source": source,
         "item_id": str(item), "author": author, "via": ["github"]}
    m["permalink"] = f"https://bsky.app/profile/did:plc:{author}/post/{item}" if source == "bluesky" \
        else f"https://x.com/i/web/status/{item}"
    if observations:
        m["observations"] = observations
    return m


def test_three_independent_authors_outrank_one_author_posting_three_times(tmp_path, monkeypatch):
    rows, _ = _ranked(tmp_path, monkeypatch, [
        _social("bluesky", "3a", ["github.com/many/authors"], "aaaaaaaaaaaaaaaa"),
        _social("bluesky", "3b", ["github.com/many/authors"], "bbbbbbbbbbbbbbbb"),
        _social("bluesky", "3c", ["github.com/many/authors"], "cccccccccccccccc"),
        _social("bluesky", "3d", ["github.com/one/author"], "dddddddddddddddd"),
        _social("bluesky", "3e", ["github.com/one/author"], "dddddddddddddddd"),
        _social("bluesky", "3f", ["github.com/one/author"], "dddddddddddddddd")])
    many, one = rows["github.com/many/authors"], rows["github.com/one/author"]
    assert many["signals"]["authors"] == 3 and one["signals"]["authors"] == 1
    assert many["score"] > one["score"]
    cut = [m for m in one["mentions"] if m.get("repeat_author")]
    assert len(cut) == 2 and all(m["counted"] for m in one["mentions"])
    full = next(m for m in one["mentions"] if not m.get("repeat_author"))
    assert cut[0]["contribution"] == pytest.approx(full["contribution"] * 0.25, rel=1e-3)
    assert "3 author(s)" in many["explanation"] and "2 repeat(s) x0.25" in one["explanation"]
    # the same author on two different sources is two authors: sources do not share identity
    rows2, _ = _ranked(tmp_path, monkeypatch, [
        _social("bluesky", "3a", ["github.com/many/authors"], "aaaaaaaaaaaaaaaa"),
        _social("x", "1", ["github.com/many/authors"], "aaaaaaaaaaaaaaaa")])
    assert rows2["github.com/many/authors"]["signals"]["authors"] == 2
    assert not any(m.get("repeat_author") for m in rows2["github.com/many/authors"]["mentions"])


def test_hn_and_forem_mentions_carry_no_author_and_are_never_cut(tmp_path, monkeypatch):
    rows, _ = _ranked(tmp_path, monkeypatch, [_mention("hn", 1, ["github.com/a/b"]), _mention("hn", 2, ["github.com/a/b"]),
                                              _mention("forem", 3, ["github.com/a/b"])])
    r = rows["github.com/a/b"]
    assert r["signals"]["authors"] is None and not any(m.get("repeat_author") for m in r["mentions"])
    assert all(m["counted"] for m in r["mentions"])


def test_a_rising_mention_outscores_a_static_one_with_the_same_points(tmp_path, monkeypatch):
    obs = [{"at": "2026-10-04T00:00:00Z", "points": 10, "comments": 1}, {"at": "2026-10-06T00:00:00Z", "points": 100, "comments": 5}]
    rows, _ = _ranked(tmp_path, monkeypatch, [
        _social("bluesky", "3r", ["github.com/rising/thing"], "aaaaaaaaaaaaaaaa", observations=obs),
        _social("bluesky", "3s", ["github.com/static/thing"], "bbbbbbbbbbbbbbbb")])
    rising, static = rows["github.com/rising/thing"], rows["github.com/static/thing"]
    assert rising["score"] > static["score"]
    assert rising["signals"]["points_per_day"] == 45.0 and static["signals"]["points_per_day"] == 0.0
    assert rising["mentions"][0]["points_per_day"] == 45.0 and "points_per_day" not in static["mentions"][0]
    assert "+45.00 points/day" in rising["explanation"]
    # one observation, or observations an hour apart, is not a trend: the interval has a one-day floor
    assert radar.points_per_day({"observations": [{"at": "2026-10-06T00:00:00Z", "points": 10, "comments": 0},
                                                  {"at": "2026-10-06T01:00:00Z", "points": 34, "comments": 0}]}) == 24.0
    assert radar.points_per_day({}) == 0.0
    # a reading that fell does not count as negative attention
    assert radar.points_per_day({"observations": [obs[1], {**obs[0], "at": "2026-10-07T00:00:00Z"}]}) == 0.0


def test_corroboration_bonus_grows_per_source_and_is_capped(tmp_path, monkeypatch):
    def world(sources):
        ms = [{**_mention("hn", i + 10, ["github.com/c/d"])} for i, _ in enumerate(sources)]
        for m, s in zip(ms, sources):
            m["source"] = s
            if s == "bluesky":
                m.update(item_id="did:plc:x/3l" + m["item_id"], permalink="https://bsky.app/profile/did:plc:x/post/3l")
            if s in ("x",):
                m["permalink"] = f"https://x.com/i/web/status/{m['item_id']}"
            if s == "instagram":
                m["permalink"] = "https://www.instagram.com/p/C1abc/"
            if s == "forem":
                m["permalink"] = "https://dev.to/x/1"
        return ms

    def mult(sources):
        rows, _ = _ranked(tmp_path, monkeypatch, world(sources))
        r = rows["github.com/c/d"]
        base = sum(m["contribution"] for m in r["mentions"] if m["counted"])
        return round(r["score"] / base, 4), r["signals"]["corroborating_sources"]

    assert mult(["hn"]) == (1.0, 1)
    assert mult(["hn", "bluesky"]) == (1.25, 2)
    assert mult(["hn", "bluesky", "x"]) == (1.5, 3)
    assert mult(["hn", "bluesky", "x", "instagram"]) == (1.5, 4)          # capped at max_corroborating_sources


def test_changing_the_new_ranking_parameters_needs_a_version_bump():
    for k in ("velocity_weight", "same_author_factor", "max_corroborating_sources", "max_observations"):
        assert "without a version bump" in radar.check_version(_cfg(**{f"ranking.{k}": 9}))[0]
    assert "without a version bump" in radar.check_version(_cfg(**{"reconcile.max_candidates": 1}))[0]
    # social collection budgets are operational
    assert radar.check_version(_cfg(**{"sources.bluesky.limit": 5, "sources.x.max_results": 10})) == []


# ---------------------------------------------------------------- the candidate list

def test_candidates_are_the_repositories_new_to_the_source_in_score_order(tmp_path, monkeypatch):
    corpus = [("github.com/known/tool", ["github.com/known/tool"], None, 7)]
    rows, out = _ranked(tmp_path, monkeypatch, [
        _mention("hn", 1, ["github.com/known/tool"], points=900, comments=400),
        _mention("hn", 2, ["github.com/pool/thing"], points=500, comments=100),
        _mention("hn", 3, ["github.com/never/seen"], points=200, comments=10),
        _mention("hn", 4, ["github.com/also/new"], points=50, comments=1),
        _mention("hn", 5, ["github.com/gone/away"], points=400, comments=10)],
        cfg=_cfg(**{"ranking.max_ranked": 2, "reconcile.max_candidates": 2}), corpus=corpus,
        pool=[{"key": "github.com/pool/thing", "aliases": [], "host_repo_id": 9}],
        resolved=[{"key": "github.com/gone/away", "found": False, "resolved_at": "2026-10-06T00:00:00Z", "http_status": 404}])
    assert [r["key"] for r in out["rows"]] == ["github.com/known/tool", "github.com/pool/thing"]   # the ranking is capped at 2
    cands = out["candidates"]
    assert [(c["rank"], c["key"], c["status"], c["radar_rank"]) for c in cands] == [
        (1, "github.com/pool/thing", "candidate", 2), (2, "github.com/gone/away", "unresolved", None)] or \
        [(c["rank"], c["key"], c["status"], c["radar_rank"]) for c in cands] == [
        (1, "github.com/pool/thing", "candidate", 2), (2, "github.com/never/seen", "unresolved", None)]
    assert all(c["key"] != "github.com/known/tool" for c in cands)          # in the corpus: an observation, not a candidate
    assert out["manifest"]["counts"]["candidates"] == 2
    # not_found never lists
    _, out2 = _ranked(tmp_path, monkeypatch, [_mention("hn", 5, ["github.com/gone/away"], points=400, comments=10)],
                      resolved=[{"key": "github.com/gone/away", "found": False, "resolved_at": "2026-10-06T00:00:00Z",
                                 "http_status": 404}])
    assert out2["candidates"] == []


# ---------------------------------------------------------------- the daily step, end to end

def test_cycle_step_with_social_sources_reconciles_corroborates_and_admits(tmp_path, monkeypatch):
    data = _root(tmp_path, monkeypatch, CORPUS, canonical=["github.com/new-org/tool"], pool=POOL)
    http = _social_world(monkeypatch)
    http.responses.update({
        f"{HN}/beststories.json": (200, [1, 2]),
        f"{HN}/item/1.json": (200, _story(1, "https://github.com/old-org/tool", score=800, comments=300)),
        f"{HN}/item/2.json": (200, _story(2, "https://github.com/fresh/social-thing", score=60, comments=5)),
        GH + "fresh/social-thing": (200, {"id": 600, "full_name": "Fresh/Social-Thing", "stargazers_count": 300,
                                          "archived": False, "fork": False, "license": {"spdx_id": "MIT"}})})
    light = {"github.com/old-org/tool": {"found": True, "canonical_id": "github.com/new-org/tool", "host_repo_id": 42}}
    out = radar.cycle_step(AS_OF, "2026-10-06", light, http)
    assert out["ok"], out["failed_checks"]
    rows = {r["key"]: r for r in common.read_jsonl(data / "radar" / "radar.jsonl")}
    known = rows["github.com/new-org/tool"]                                  # a known project: tracked, never re-admitted
    assert known["status"] == "canonical" and set(known["sources"]) == {"hn", "bluesky", "x", "instagram"}
    fresh = rows["github.com/fresh/social-thing"]
    assert fresh["status"] == "candidate" and set(fresh["sources"]) == {"hn", "bluesky", "x", "instagram"}
    assert fresh["signals"] == {"authors": 4, "points_per_day": 0.0, "corroborating_sources": 4}
    assert fresh["canonical_name"] == "Fresh/Social-Thing" and fresh["host_repo_id"] == 600
    pool = daily.load_pool()
    assert out["pool_added"] == 1 and pool["github.com/fresh/social-thing"]["providers"] == [
        {"provider": "community_radar", "query": "bluesky+hn+instagram+x"}]
    cands = common.read_jsonl(data / "radar" / "candidates.jsonl")
    assert [c["key"] for c in cands][:1] == ["github.com/fresh/social-thing"] and cands[0]["radar_rank"] == fresh["rank"]
    s = out["strategy"]
    assert s["status"] == "ok" and s["methodology_version"] == "1.1.0"
    assert {k: v["status"] for k, v in s["sources"].items()} == {
        "hn": "ok", "forem": "ok", "bluesky": "ok", "x": "ok", "instagram": "ok"}
    assert s["candidates"][0]["key"] == "github.com/fresh/social-thing"
    assert radar.validate()["ok"]

    # the next day: the same posts observed again with more likes become a velocity signal,
    # and nothing is re-admitted
    later = AS_OF.replace(day=7)
    bs = _fx("bluesky_searchPosts.json")
    for p in bs["posts"]:
        p["likeCount"] += 50
    public, api = _bluesky_urls(later)
    http.responses[public], http.responses[api] = (403, None), (200, bs)
    http.calls, http.seen = 0, []
    again = radar.cycle_step(later, "2026-10-07", light, http)
    assert again["ok"] and again["pool_added"] == 0
    held = {m["item_id"]: m for m in common.read_jsonl(data / "radar" / "mentions.jsonl") if m["source"] == "bluesky"}
    first = held["did:plc:aaaa1111/3lxa1"]
    assert first["points"] == 170 and first["observations"] == [
        {"at": "2026-10-06T00:00:00Z", "points": 120, "comments": 8}, {"at": "2026-10-07T00:00:00Z", "points": 170, "comments": 8}]
    rows2 = {r["key"]: r for r in common.read_jsonl(data / "radar" / "radar.jsonl")}
    assert rows2["github.com/fresh/social-thing"]["signals"]["points_per_day"] == 50.0
    # only the post that newly cleared min_points is resolved; yesterday's resolution is cached
    assert [u for u in http.seen if u.startswith(GH)] == [GH + "tiny/mention"]


def test_unconfigured_sources_leave_the_strategy_ok_and_say_so_in_retrieval(tmp_path, monkeypatch):
    data = _root(tmp_path, monkeypatch, CORPUS, canonical=["github.com/new-org/tool"], pool=POOL)
    http = _social_world(monkeypatch, with_credentials=False)
    http.responses.update({f"{HN}/beststories.json": (200, [1]),
                           f"{HN}/item/1.json": (200, _story(1, "https://github.com/old-org/tool", score=800, comments=300)),
                           GH + "fresh/social-thing": (200, {"id": 600, "full_name": "fresh/social-thing", "fork": False,
                                                             "archived": False})})
    out = radar.cycle_step(AS_OF, "2026-10-06", {}, http)
    assert out["ok"] and out["strategy"]["status"] == "ok"
    assert out["strategy"]["sources"]["x"]["status"] == "unconfigured" and out["strategy"]["sources"]["x"]["http_calls"] == 0
    got = radar.retrieve(candidates=True)
    assert got["sources"]["x"]["status"] == "unconfigured" and "X_BEARER_TOKEN" in got["sources"]["x"]["errors"][0]
    assert [c["key"] for c in got["candidates"]] == ["github.com/fresh/social-thing"]
    assert radar.retrieve(candidates=True, source="instagram")["candidates"] == []
    assert radar.retrieve(candidates=True, source="bluesky", limit=1)["matched"] == 1
    assert radar.retrieve(source="bluesky", status="candidate")["entries"][0]["key"] == "github.com/fresh/social-thing"
    assert radar.retrieve(source="bluesky", status="canonical")["entries"][0]["key"] == "github.com/new-org/tool"
    with pytest.raises(ValueError):
        radar.retrieve(source="threads")


def test_committed_radar_lists_candidates_and_validates():
    d = common.DATA / "radar"
    if not (d / "MANIFEST.json").exists():
        pytest.skip("no radar collected yet")
    report = radar.validate()
    assert report["ok"], [c for c in report["checks"] if not c["ok"]]
    m = common.read_json(d / "MANIFEST.json")
    assert m["methodology"]["version"] == "1.1.0" and "candidates.jsonl" in m["files"]
    cands = common.read_jsonl(d / "candidates.jsonl")
    assert 0 < len(cands) <= radar.load_config()["reconcile"]["max_candidates"]
    assert all(c["status"] in ("candidate", "unresolved") for c in cands)
