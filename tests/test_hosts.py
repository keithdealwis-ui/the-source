"""KEI-923: GitLab and Codeberg host adapter returns the GitHub meta view, offline."""
import json

import pytest

from source_pipeline import discover, hosts

GL = "https://gitlab.com/api/v4"
GQL = "https://gitlab.com/api/graphql"
CB = "https://codeberg.org/api/v1"
WINDOW = "2025-10-20T00:00:00Z"
CFG = {"aliases": {"admitted_forks": {}}}
GITHUB_KEYS = set(discover._meta_view({
    "nameWithOwner": "a/b", "databaseId": 1, "isArchived": False, "isDisabled": False, "isEmpty": False,
    "isFork": False, "isMirror": False, "owner": {"login": "a", "__typename": "User"}}))


class FakeHttp:
    """Routes match on exact URL (REST) or on the GraphQL `p` variable (POST)."""

    def __init__(self, routes, gql=None):
        self.routes, self.gql, self.seen, self.failures, self.calls = routes, gql or {}, [], [], 0

    def call(self, url, data=None, auth=False, tries=5, token=None):
        assert not auth and not token, "no credential may go to another host"
        self.calls += 1
        self.seen.append(url)
        if data is not None:
            path = data["variables"]["p"]
            if path in self.gql:
                return self.gql[path]
            return 200, {"data": {"project": None}}
        return self.routes.get(url, (404, None))


def _gl_rest(path, pid=7, **kw):
    doc = {"id": pid, "path_with_namespace": path, "star_count": 12, "forks_count": 3,
           "last_activity_at": "2026-10-01T10:20:30.123Z", "created_at": "2020-01-02T03:04:05.000+02:00",
           "visibility": "public", "description": "Thing", "topics": ["zeta", "alpha"],
           "namespace": {"full_path": path.rsplit("/", 1)[0], "kind": "group"}, "license": None}
    doc.update(kw)
    return doc


def _gl_gql(path, archived=False, forked_from=None, empty=False, langs=None, release=None):
    return 200, {"data": {"project": {
        "fullPath": path, "archived": archived, "isForked": bool(forked_from), "forkedFrom": forked_from,
        "repository": {"empty": empty},
        "languages": langs if langs is not None else [{"name": "C", "share": 5.0}, {"name": "Go", "share": 90.0}],
        "releases": {"nodes": [release] if release else []}}}}


def _gl(slug, path=None, rest=None, gql=None):
    path = path or slug
    url = f"{GL}/projects/{slug.replace('/', '%2F')}?license=true"
    return {url: (200, rest or _gl_rest(path))}, {path: gql or _gl_gql(path)}


# ---------------------------------------------------------------- GitLab

def test_gitlab_found_matches_github_shape_and_passes_prefilter():
    routes, gql = _gl("grp/proj", path="Grp/Proj",
                      gql=_gl_gql("Grp/Proj", release={"tagName": "v1.2", "releasedAt": "2026-09-01T12:00:00.5+02:00"}))
    v = hosts.meta_batch(FakeHttp(routes, gql), "gitlab.com", ["grp/proj"])["grp/proj"]
    assert v["found"] is True
    assert set(v) - {"found", "host"} == GITHUB_KEYS
    assert v["host"] == "gitlab.com"
    assert v["name_with_owner"] == "Grp/Proj" and v["canonical_slug"] == "grp/proj"
    assert v["database_id"] == 7 and v["stars"] == 12 and v["forks"] == 3
    assert v["pushed_at"] == "2026-10-01T10:20:30Z"
    assert v["created_at"] == "2020-01-02T01:04:05Z"
    assert v["languages"] == ["Go", "C"] and v["primary_language"] == "Go"
    assert v["topics"] == ["alpha", "zeta"]
    assert v["owner"] == {"login": "Grp", "type": "Organization"}
    assert v["latest_release"] == {"tagName": "v1.2", "publishedAt": "2026-09-01T10:00:00Z"}
    assert v["parent"] is None and v["is_fork"] is False and v["private"] is False
    assert discover.prefilter(v, WINDOW, CFG) == []


def test_gitlab_not_found():
    http = FakeHttp({})
    v = hosts.meta_batch(http, "gitlab.com", ["no/such"])["no/such"]
    assert v == {"found": False, "error": "not_found"}
    assert discover.prefilter(v, WINDOW, CFG) == ["repository_not_found"]


def test_gitlab_http_error_and_graphql_failure_fail_closed():
    routes, _ = _gl("a/b")
    routes[f"{GL}/projects/x%2Fy?license=true"] = (503, None)
    gql = {"a/b": (502, None)}
    out = hosts.meta_batch(FakeHttp(routes, gql), "gitlab.com", ["a/b", "x/y"])
    assert out["x/y"] == {"found": False, "error": "http_503"}
    assert out["a/b"] == {"found": False, "error": "graphql_http_502"}
    assert discover.prefilter(out["a/b"], WINDOW, CFG) == ["live_verification_failed"]


def test_gitlab_fork():
    routes, gql = _gl("me/proj", gql=_gl_gql("me/proj", forked_from={"fullPath": "Up/Proj",
                                                                      "id": "gid://gitlab/Project/99"}))
    v = hosts.meta_batch(FakeHttp(routes, gql), "gitlab.com", ["me/proj"])["me/proj"]
    assert v["is_fork"] is True
    assert v["parent"] == {"slug": "up/proj", "database_id": 99}
    assert discover.prefilter(v, WINDOW, CFG) == ["fork_alias"]
    assert discover.prefilter(v, WINDOW, {"aliases": {"admitted_forks": {"me/proj": "x"}}}) == []


def test_gitlab_archived_and_empty():
    routes, gql = _gl("a/b", gql=_gl_gql("a/b", archived=True, empty=True))
    v = hosts.meta_batch(FakeHttp(routes, gql), "gitlab.com", ["a/b"])["a/b"]
    assert v["archived"] is True and v["empty"] is True
    assert discover.prefilter(v, WINDOW, CFG) == ["archived", "empty_repository"]


@pytest.mark.parametrize("key,spdx", [("mit", "MIT"), ("apache-2.0", "Apache-2.0"), ("AGPL-3.0", "AGPL-3.0"),
                                      ("eupl-1.2", "EUPL-1.2"), ("cc0-1.0", "CC0-1.0"), ("0bsd", "0BSD"),
                                      ("other", None)])
def test_gitlab_licence_mapping(key, spdx):
    rest = _gl_rest("a/b", license={"key": key, "name": f"{key} name", "nickname": None})
    routes, gql = _gl("a/b", rest=rest)
    v = hosts.meta_batch(FakeHttp(routes, gql), "gitlab.com", ["a/b"])["a/b"]
    assert v["licence_spdx"] == spdx
    assert v["licence_name"] == f"{key} name"


def test_gitlab_nested_group_path_is_fully_encoded():
    slug = "group/sub/deeper/repo"
    routes, gql = _gl(slug, rest=_gl_rest(slug, namespace={"full_path": "group/sub/deeper", "kind": "group"}))
    http = FakeHttp(routes, gql)
    v = hosts.meta_batch(http, "gitlab.com", [slug])[slug]
    assert http.seen[0] == f"{GL}/projects/group%2Fsub%2Fdeeper%2Frepo?license=true"
    assert v["canonical_slug"] == slug and v["owner"]["login"] == "group/sub/deeper"


def test_gitlab_private_and_user_namespace():
    rest = _gl_rest("u/p", visibility="internal", namespace={"full_path": "u", "kind": "user"})
    routes, gql = _gl("u/p", rest=rest)
    v = hosts.meta_batch(FakeHttp(routes, gql), "gitlab.com", ["u/p"])["u/p"]
    assert v["private"] is True and v["owner"]["type"] == "User"
    assert "not_public" in discover.prefilter(v, WINDOW, CFG)


@pytest.mark.parametrize("raw,want", [("2026-10-01T10:20:30.123456Z", "2026-10-01T10:20:30Z"),
                                      ("2026-10-01T12:20:30+02:00", "2026-10-01T10:20:30Z"),
                                      ("2026-10-01T12:20:30.9+02:00", "2026-10-01T10:20:30Z"),
                                      ("2026-10-01T10:20:30Z", "2026-10-01T10:20:30Z"),
                                      (None, None), ("garbage", None)])
def test_timestamp_normalisation(raw, want):
    assert hosts._ts(raw) == want


def test_stale_gitlab_project_is_stale():
    routes, gql = _gl("a/b", rest=_gl_rest("a/b", last_activity_at="2024-01-01T00:00:00.000Z"))
    v = hosts.meta_batch(FakeHttp(routes, gql), "gitlab.com", ["a/b"])["a/b"]
    assert discover.prefilter(v, WINDOW, CFG) == ["stale_no_meaningful_activity_12_months"]


# ---------------------------------------------------------------- Codeberg

def _cb_repo(full_name, **kw):
    doc = {"id": 55, "full_name": full_name, "owner": {"login": full_name.split("/")[0]},
           "empty": False, "private": False, "fork": False, "mirror": False, "archived": False,
           "parent": None, "stars_count": 40, "forks_count": 2, "website": "https://x.org", "language": "Go",
           "topics": ["b", "a"], "updated_at": "2026-10-20T15:19:45+02:00",
           "created_at": "2022-11-06T07:24:57+01:00", "description": "D"}
    doc.update(kw)
    return doc


def _cb(slug, repo, release=(404, None), org=True):
    api = f"{CB}/repos/{slug}"
    return {api: (200, repo), f"{api}/languages": (200, {"TypeScript": 10, "Go": 900}),
            f"{api}/releases/latest": release,
            f"{CB}/orgs/{repo['owner']['login']}": (200, {}) if org else (404, None)}


def test_codeberg_found_with_release():
    routes = _cb("own/repo", _cb_repo("Own/Repo", licenses=["MIT"]),
                 release=(200, {"tag_name": "v2", "published_at": "2026-10-01T09:00:00+02:00"}))
    v = hosts.meta_batch(FakeHttp(routes), "codeberg.org", ["own/repo"])["own/repo"]
    assert set(v) - {"found", "host"} == GITHUB_KEYS
    assert v["host"] == "codeberg.org" and v["canonical_slug"] == "own/repo" and v["name_with_owner"] == "Own/Repo"
    assert v["pushed_at"] == "2026-10-20T13:19:45Z" and v["created_at"] == "2022-11-06T06:24:57Z"
    assert v["languages"] == ["Go", "TypeScript"] and v["primary_language"] == "Go"
    assert v["topics"] == ["a", "b"] and v["homepage"] == "https://x.org"
    assert v["licence_spdx"] == "MIT" and v["owner"] == {"login": "Own", "type": "Organization"}
    assert v["latest_release"] == {"tagName": "v2", "publishedAt": "2026-10-01T07:00:00Z"}
    assert discover.prefilter(v, WINDOW, CFG) == []


def test_codeberg_release_404_is_none_and_user_owner():
    routes = _cb("me/repo", _cb_repo("me/repo"), org=False)
    v = hosts.meta_batch(FakeHttp(routes), "codeberg.org", ["me/repo"])["me/repo"]
    assert v["found"] and v["latest_release"] is None
    assert v["licence_spdx"] is None and v["owner"]["type"] == "User"


def test_codeberg_mirror_and_fork():
    routes = {**_cb("a/m", _cb_repo("a/m", mirror=True)),
              **_cb("a/f", _cb_repo("a/f", fork=True, parent={"full_name": "Up/F", "id": 9}))}
    out = hosts.meta_batch(FakeHttp(routes), "codeberg.org", ["a/m", "a/f"])
    assert discover.prefilter(out["a/m"], WINDOW, CFG) == ["mirror_alias"]
    assert out["a/f"]["parent"] == {"slug": "up/f", "database_id": 9}
    assert discover.prefilter(out["a/f"], WINDOW, CFG) == ["fork_alias"]


def test_codeberg_not_found_and_malformed_do_not_abort_batch():
    routes = {**_cb("ok/repo", _cb_repo("ok/repo")), f"{CB}/repos/bad/repo": (200, {"id": 1})}
    out = hosts.meta_batch(FakeHttp(routes), "codeberg.org", ["ok/repo", "no/repo", "bad/repo"])
    assert out["ok/repo"]["found"] is True
    assert out["no/repo"] == {"found": False, "error": "not_found"}
    assert out["bad/repo"]["found"] is False and out["bad/repo"]["error"].startswith("malformed_")


def test_unsupported_host():
    with pytest.raises(ValueError):
        hosts.meta_batch(FakeHttp({}), "github.com", ["a/b"])


# ---------------------------------------------------------------- search

def _gl_page(page, stars):
    return [{"id": page * 1000 + i, "path_with_namespace": f"G/p{page}-{i}", "star_count": s,
             "last_activity_at": "2026-10-01T00:00:00.5Z", "created_at": "2020-01-01T00:00:00Z",
             "description": "x" * 400, "topics": ["self-hosted"]} for i, s in enumerate(stars)]


def _gl_search_url(page):
    return (f"{GL}/projects?topic=self-hosted&order_by=star_count&sort=desc&archived=false"
            f"&visibility=public&per_page=100&page={page}")


def test_gitlab_search_paginates_and_stops_at_min_stars():
    routes = {_gl_search_url(1): (200, _gl_page(1, [500] * 100)),
              _gl_search_url(2): (200, _gl_page(2, [60] * 5 + [10] * 95))}
    http = FakeHttp(routes)
    got = hosts.search(http, "gitlab.com", "self-hosted", 50, 1000)
    assert len(got) == 105 and len(http.seen) == 2
    first = got[0]
    assert first["key"] == "gitlab.com/g/p1-0" and first["provider"] == "gitlab_search"
    assert first["query"] == "self-hosted" and first["canonical_name"] == "G/p1-0"
    assert first["pushed_at"] == "2026-10-01T00:00:00Z" and len(first["description"]) == 300
    assert first["topics"] == ["self-hosted"] and first["host_repo_id"] == 1000


def test_gitlab_search_stops_at_limit_and_short_page():
    routes = {_gl_search_url(1): (200, _gl_page(1, [500] * 100))}
    assert len(hosts.search(FakeHttp(routes), "gitlab.com", "self-hosted", 50, 20)) == 20
    routes = {_gl_search_url(1): (200, _gl_page(1, [500] * 7))}
    http = FakeHttp(routes)
    assert len(hosts.search(http, "gitlab.com", "self-hosted", 50, 100)) == 7 and len(http.seen) == 1


def _cb_search_url(page):
    return f"{CB}/repos/search?q=self-hosted&topic=true&sort=stars&order=desc&limit=50&page={page}"


def test_codeberg_search_paginates_and_stops_at_min_stars():
    def rows(page, stars):
        return [{"id": page * 100 + i, "full_name": f"O/r{page}-{i}", "stars_count": s, "archived": i == 0,
                 "updated_at": "2026-10-20T15:19:45+02:00", "licenses": ["MIT"], "topics": ["b", "a"]}
                for i, s in enumerate(stars)]
    routes = {_cb_search_url(1): (200, {"ok": True, "data": rows(1, [90] * 50)}),
              _cb_search_url(2): (200, {"ok": True, "data": rows(2, [30, 25, 5] + [1] * 47)})}
    http = FakeHttp(routes)
    got = hosts.search(http, "codeberg.org", "self-hosted", 20, 1000)
    assert len(got) == 52 and len(http.seen) == 2
    assert got[0]["key"] == "codeberg.org/o/r1-0" and got[0]["provider"] == "codeberg_search"
    assert got[0]["archived"] is True and got[0]["licence_spdx"] == "MIT"
    assert got[0]["pushed_at"] == "2026-10-20T13:19:45Z" and got[0]["topics"] == ["a", "b"]


def test_search_http_failure_returns_partial():
    routes = {_cb_search_url(1): (200, {"ok": True, "data": [
        {"id": i, "full_name": f"o/r{i}", "stars_count": 99} for i in range(50)]})}
    assert len(hosts.search(FakeHttp(routes), "codeberg.org", "self-hosted", 20, 1000)) == 50


def test_view_is_json_serialisable():
    routes, gql = _gl("a/b")
    v = hosts.meta_batch(FakeHttp(routes, gql), "gitlab.com", ["a/b"])["a/b"]
    json.dumps(v)
