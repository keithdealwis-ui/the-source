"""KEI-914: GitLab's commit-list endpoint is behind a Cloudflare challenge for anonymous
callers; the adapter must observe commits through the endpoints that still answer."""
import io
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from source_pipeline import common, enrich
from source_pipeline.activity import Rules

API = "https://gitlab.com/api/v4/projects/grp%2Fproj"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=365)


def _commit(oid, days_ago, author="Alice", parents=None):
    return {"id": oid, "committed_date": (NOW - timedelta(days=days_ago)).isoformat(),
            "author_name": author, "parent_ids": parents if parents is not None else [oid + "p"]}


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.seen, self.failures, self.calls = routes, [], [], 0

    def call(self, url, data=None, auth=False, tries=5):
        self.calls += 1
        self.seen.append(url)
        for prefix, resp in self.routes:
            if url.startswith(prefix):
                return resp
        if "/repository/commits?" in url:
            raise AssertionError(f"commit list endpoint must not be called: {url}")
        return 404, None


def _routes(head, chain, diffs):
    routes = [(f"{API}?license=true", (200, {"path_with_namespace": "grp/proj", "id": 7,
                                              "default_branch": "main", "star_count": 1})),
              (f"{API}/repository/branches/main", (200, {"name": "main", "commit": head})),
              (f"{API}/releases?", (200, [])), (f"{API}/repository/tags?", (200, []))]
    for oid, c in chain.items():
        routes.append((f"{API}/repository/commits/{oid}/diff", (200, [{"new_path": p} for p in diffs.get(oid, [])])))
        routes.append((f"{API}/repository/commits/{oid}", (200, c)))
    for oid, paths in diffs.items():
        if oid not in chain:
            routes.append((f"{API}/repository/commits/{oid}/diff", (200, [{"new_path": p} for p in paths])))
    return routes


@pytest.fixture
def rules():
    return Rules(common.load_policy())


@pytest.fixture
def policy():
    p = common.load_policy()
    p["maintenance"]["commit_lookback"] = 3
    return p


def test_commits_come_from_the_branch_head_walk_not_the_list(rules, policy):
    head = _commit("h1", 1, author="dependabot[bot]", parents=["h2"])
    h2 = _commit("h2", 2, parents=["h3"])
    h3 = _commit("h3", 3, parents=["h4"])
    http = FakeHttp(_routes(head, {"h2": h2, "h3": h3},
                            {"h2": ["README.md"], "h3": ["src/core.py"]}))
    live = enrich.gitlab(http, "gitlab.com/grp/proj", SINCE, rules, policy, NOW.isoformat())
    refs = [c["ref"] for c in live["observations"]["commits"]]
    assert refs == ["h1", "h2", "h3"]  # bot head kept (no diff fetched), docs-only, then code: stop
    assert not any("/repository/commits?" in u for u in http.seen)
    assert f"{API}/repository/commits/h1/diff?per_page=100" not in http.seen  # bots are not inspected
    assert live["observations"]["commits"][2]["paths"] == ["src/core.py"]
    assert rules.change_qualifies(live["observations"]["commits"][2])[0]
    assert http.failures == []


def test_walk_stops_at_the_lookback_and_at_since(rules, policy):
    head = _commit("a", 1, parents=["b"])
    chain = {"b": _commit("b", 2, parents=["c"]), "c": _commit("c", 3, parents=["d"]), "d": _commit("d", 4, parents=["e"])}
    diffs = {k: ["docs/x.md"] for k in ("a", "b", "c", "d")}
    http = FakeHttp(_routes(head, chain, diffs))
    live = enrich.gitlab(http, "gitlab.com/grp/proj", SINCE, rules, policy, NOW.isoformat())
    assert [c["ref"] for c in live["observations"]["commits"]] == ["a", "b", "c"]  # lookback 3

    old = _commit("old", 400, parents=["older"])
    http = FakeHttp(_routes(_commit("new", 1, parents=["old"]), {"old": old}, {"new": ["docs/x.md"]}))
    live = enrich.gitlab(http, "gitlab.com/grp/proj", SINCE, rules, policy, NOW.isoformat())
    assert [c["ref"] for c in live["observations"]["commits"]] == ["new"]  # `old` predates the window


def test_empty_repository_has_no_commits_and_no_failure(rules, policy):
    http = FakeHttp([(f"{API}?license=true", (200, {"path_with_namespace": "grp/proj", "id": 7,
                                                     "default_branch": None, "empty_repo": True})),
                     (f"{API}/releases?", (200, [])), (f"{API}/repository/tags?", (200, []))])
    live = enrich.gitlab(http, "gitlab.com/grp/proj", SINCE, rules, policy, NOW.isoformat())
    assert live["found"] and live["observations"]["commits"] == []
    assert not any("/repository/branches/" in u for u in http.seen)


def test_cloudflare_challenge_is_a_failure_without_retries(monkeypatch):
    """A 403 carrying cf-mitigated: challenge will not clear on retry; one attempt, then
    recorded as a failure so the run still fails closed rather than trusting a blank."""
    attempts = []

    def challenged(req, timeout=60):
        attempts.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden",
                                     {"cf-mitigated": "challenge", "server": "cloudflare"}, io.BytesIO(b""))

    monkeypatch.setattr(enrich.urllib.request, "urlopen", challenged)
    monkeypatch.setattr(enrich.common, "refuse_network", lambda url: None)  # nothing leaves the process
    monkeypatch.setattr(enrich.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError("slept")))
    http = enrich.Http()
    st, body = http.call(f"{API}/repository/commits?per_page=1")
    assert (st, body) == (403, None)
    assert len(attempts) == 1
    assert http.failures == [f"403 {API}/repository/commits"]
