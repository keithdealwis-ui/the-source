"""GitLab and Codeberg host adapter for the discovery corpus (KEI-923).

Admission and corpus refresh read github.com through `discover.Api.meta_batch`, whose
per-repository view is built by `discover._meta_view`. This module answers the same
question for gitlab.com and codeberg.org and returns the same view, so `discover.prefilter`
and `corpus_admit` judge every host by one rule set.

All calls are anonymous (the GitHub token is never sent to another host).

GitLab quirk (observed 2026-10-20): the anonymous REST project view is the reduced
"simple" one — no `archived`, `empty_repo`, `forked_from_project` or `mirror`. Those are
read from the public GraphQL endpoint instead, which answers anonymously. Pull mirrors are
not exposed to anonymous callers by either API, so `is_mirror` is False unless REST says
otherwise. If the GraphQL supplement fails the repository is reported as a failed lookup,
not as a clean one: an archived or forked project must never pass because a call failed.
"""
from __future__ import annotations

import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from .activity import iso, parse_ts
from .enrich import GITLAB_SPDX

HOSTS = ("gitlab.com", "codeberg.org")
GITLAB_API = "https://gitlab.com/api/v4"
GITLAB_GQL = "https://gitlab.com/api/graphql"
CODEBERG_API = "https://codeberg.org/api/v1"

# GitLab licence keys -> SPDX ids. Extends enrich.GITLAB_SPDX; unknown keys give None.
SPDX = {**GITLAB_SPDX, "cc0-1.0": "CC0-1.0", "eupl-1.2": "EUPL-1.2", "0bsd": "0BSD"}

GITLAB_SUPPLEMENT = """query($p:ID!){ project(fullPath:$p){
  fullPath archived isForked forkedFrom{ fullPath id }
  repository{ empty } languages{ name share }
  releases(first:1, sort:RELEASED_AT_DESC){ nodes{ tagName releasedAt } } } }"""


def _ts(value: str | None) -> str | None:
    """Any ISO 8601 timestamp -> UTC `YYYY-MM-DDTHH:MM:SSZ` (the corpus's only format)."""
    dt = parse_ts(value)
    return iso(dt) if dt else None


def _gid(value) -> int | None:
    """GraphQL global id `gid://gitlab/Project/123` -> 123."""
    tail = str(value or "").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _failed(st: int) -> dict:
    return {"found": False, "error": "not_found" if st in (404, 410) else f"http_{st}"}


# ---------------------------------------------------------------- GitLab

def _gitlab_one(http, slug: str) -> dict:
    st, repo = http.call(f"{GITLAB_API}/projects/{urllib.parse.quote(slug, safe='')}?license=true")
    if st != 200 or not isinstance(repo, dict):
        return _failed(st)
    st, doc = http.call(GITLAB_GQL, data={"query": GITLAB_SUPPLEMENT, "variables": {"p": repo["path_with_namespace"]}})
    gql = ((doc or {}).get("data") or {}).get("project") if st == 200 and isinstance(doc, dict) else None
    full_rest = "archived" in repo  # an authenticated caller gets the full REST view
    if gql is None and not full_rest:
        return {"found": False, "error": f"graphql_http_{st}" if st != 200 else "graphql_no_project"}
    gql = gql or {}

    fork = gql.get("forkedFrom") if gql else None
    if fork:
        parent = {"slug": fork["fullPath"].lower(), "database_id": _gid(fork.get("id"))}
    elif repo.get("forked_from_project"):
        f = repo["forked_from_project"]
        parent = {"slug": f["path_with_namespace"].lower(), "database_id": f.get("id")}
    else:
        parent = None

    if gql:
        langs = [l["name"] for l in sorted(gql.get("languages") or [], key=lambda l: -(l.get("share") or 0))]
        nodes = ((gql.get("releases") or {}).get("nodes") or [])
        rel = nodes[0] if nodes else None
        latest = {"tagName": rel["tagName"], "publishedAt": _ts(rel.get("releasedAt"))} if rel else None
    else:
        st, ld = http.call(f"{GITLAB_API}/projects/{repo['id']}/languages")
        langs = [k for k, _ in sorted((ld or {}).items(), key=lambda kv: -kv[1])] if st == 200 else []
        st, rels = http.call(f"{GITLAB_API}/projects/{repo['id']}/releases?per_page=1")
        rel = rels[0] if st == 200 and rels else None
        latest = {"tagName": rel["tag_name"], "publishedAt": _ts(rel.get("released_at"))} if rel else None

    lic = repo.get("license") or {}
    ns = repo.get("namespace") or {}
    path = repo["path_with_namespace"]
    return {
        "found": True,
        "host": "gitlab.com",
        "name_with_owner": path,
        "canonical_slug": path.lower(),
        "database_id": repo.get("id"),
        "archived": bool(gql["archived"] if "archived" in gql else repo.get("archived")),
        "disabled": False,
        "empty": bool((gql.get("repository") or {}).get("empty") if gql else repo.get("empty_repo")),
        "is_fork": bool(gql.get("isForked") if gql else parent),
        "is_mirror": bool(repo.get("mirror")),
        "is_template": False,
        "private": (repo.get("visibility") or "public") != "public",
        "parent": parent,
        "stars": repo.get("star_count"),
        "forks": repo.get("forks_count"),
        "pushed_at": _ts(repo.get("last_activity_at")),
        "created_at": _ts(repo.get("created_at")),
        "updated_at": _ts(repo.get("updated_at") or repo.get("last_activity_at")),
        "description": repo.get("description") or None,
        "homepage": None,  # GitLab has no project homepage field
        "licence_spdx": SPDX.get((lic.get("key") or "").lower()),
        "licence_name": lic.get("name"),
        "primary_language": langs[0] if langs else None,
        "languages": langs,
        "topics": sorted(repo.get("topics") or repo.get("tag_list") or []),
        "owner": {"login": ns.get("full_path") or path.rsplit("/", 1)[0],
                  "type": "Organization" if ns.get("kind") == "group" else "User"},
        "latest_release": latest,
    }


# ---------------------------------------------------------------- Codeberg

def _codeberg_one(http, slug: str, org_cache: dict) -> dict:
    api = f"{CODEBERG_API}/repos/" + "/".join(urllib.parse.quote(p, safe="") for p in slug.split("/", 1))
    st, repo = http.call(api)
    if st != 200 or not isinstance(repo, dict):
        return _failed(st)
    st, ld = http.call(f"{api}/languages")
    langs = [k for k, _ in sorted((ld or {}).items(), key=lambda kv: -kv[1])] if st == 200 and isinstance(ld, dict) else []
    st, rel = http.call(f"{api}/releases/latest")
    latest = ({"tagName": rel.get("tag_name"), "publishedAt": _ts(rel.get("published_at"))}
              if st == 200 and isinstance(rel, dict) else None)
    owner = (repo.get("owner") or {}).get("login") or slug.split("/", 1)[0]
    if owner not in org_cache:  # a Gitea user object does not say whether it is an org
        st, _ = http.call(f"{CODEBERG_API}/orgs/{urllib.parse.quote(owner, safe='')}")
        org_cache[owner] = "Organization" if st == 200 else "User"
    lic = repo.get("licenses") or []
    parent = repo.get("parent") or None
    return {
        "found": True,
        "host": "codeberg.org",
        "name_with_owner": repo["full_name"],
        "canonical_slug": repo["full_name"].lower(),
        "database_id": repo.get("id"),
        "archived": bool(repo.get("archived")),
        "disabled": False,
        "empty": bool(repo.get("empty")),
        "is_fork": bool(repo.get("fork")),
        "is_mirror": bool(repo.get("mirror")),
        "is_template": False,
        "private": bool(repo.get("private")) or bool(repo.get("internal")),
        "parent": {"slug": parent["full_name"].lower(), "database_id": parent.get("id")} if parent else None,
        "stars": repo.get("stars_count"),
        "forks": repo.get("forks_count"),
        "pushed_at": _ts(repo.get("updated_at")),  # Gitea has no pushed_at
        "created_at": _ts(repo.get("created_at")),
        "updated_at": _ts(repo.get("updated_at")),
        "description": repo.get("description") or None,
        "homepage": repo.get("website") or None,
        "licence_spdx": lic[0] if len(lic) == 1 else None,
        "licence_name": ", ".join(lic) or None,
        "primary_language": repo.get("language") or (langs[0] if langs else None),
        "languages": langs,
        "topics": sorted(repo.get("topics") or []),
        "owner": {"login": owner, "type": org_cache[owner]},
        "latest_release": latest,
    }


def meta_batch(http, host: str, slugs: list[str], max_workers: int = 4) -> dict[str, dict]:
    """Metadata for each `owner/repo` slug (GitLab: nested groups allowed) on `host`, in
    the `discover._meta_view` shape plus `host`. One failed lookup never sinks the batch."""
    if host not in HOSTS:
        raise ValueError(f"unsupported host: {host}")
    org_cache: dict[str, str] = {}

    def one(slug):
        try:
            return slug, (_gitlab_one(http, slug) if host == "gitlab.com" else _codeberg_one(http, slug, org_cache))
        except (KeyError, TypeError, ValueError, AttributeError) as err:  # malformed answer
            return slug, {"found": False, "error": f"malformed_{type(err).__name__}"}

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        return dict(pool.map(one, slugs))


# ---------------------------------------------------------------- search

def _gitlab_search(http, topic: str, min_stars: int, limit: int) -> list[dict]:
    out: list[dict] = []
    page = 1
    while len(out) < limit:
        url = f"{GITLAB_API}/projects?" + urllib.parse.urlencode({
            "topic": topic, "order_by": "star_count", "sort": "desc", "archived": "false",
            "visibility": "public", "per_page": 100, "page": page})
        st, rows = http.call(url)
        if st != 200 or not isinstance(rows, list):
            break
        for r in rows:
            if (r.get("star_count") or 0) < min_stars or len(out) >= limit:
                return out
            path = r["path_with_namespace"]
            out.append({
                "key": "gitlab.com/" + path.lower(), "provider": "gitlab_search", "query": topic,
                "host_repo_id": r.get("id"), "canonical_name": path,
                "stars": r.get("star_count"), "created_at": _ts(r.get("created_at")),
                "pushed_at": _ts(r.get("last_activity_at")), "archived": False,  # filtered by the query
                "licence_spdx": SPDX.get(((r.get("license") or {}).get("key") or "").lower()),
                "description": (r.get("description") or "")[:300] or None,
                "topics": sorted(r.get("topics") or r.get("tag_list") or []) or None,
            })
        if len(rows) < 100:
            break
        page += 1
    return out


def _codeberg_search(http, topic: str, min_stars: int, limit: int) -> list[dict]:
    out: list[dict] = []
    page = 1
    while len(out) < limit:
        url = f"{CODEBERG_API}/repos/search?" + urllib.parse.urlencode({
            "q": topic, "topic": "true", "sort": "stars", "order": "desc", "limit": 50, "page": page})
        st, doc = http.call(url)
        rows = (doc or {}).get("data") if st == 200 and isinstance(doc, dict) else None
        if not isinstance(rows, list):
            break
        for r in rows:
            if (r.get("stars_count") or 0) < min_stars or len(out) >= limit:
                return out
            lic = r.get("licenses") or []
            out.append({
                "key": "codeberg.org/" + r["full_name"].lower(), "provider": "codeberg_search", "query": topic,
                "host_repo_id": r.get("id"), "canonical_name": r["full_name"],
                "stars": r.get("stars_count"), "created_at": _ts(r.get("created_at")),
                "pushed_at": _ts(r.get("updated_at")), "archived": bool(r.get("archived")),
                "licence_spdx": lic[0] if len(lic) == 1 else None,
                "description": (r.get("description") or "")[:300] or None,
                "topics": sorted(r.get("topics") or []) or None,
            })
        if len(rows) < 50:
            break
        page += 1
    return out


def search(http, host: str, topic: str, min_stars: int, limit: int) -> list[dict]:
    """Repositories tagged `topic` on `host`, most-starred first, as discovery candidates
    shaped like `discovery_search._github_search` entries. Stops at `limit`, at the first
    repository under `min_stars`, or at a short page."""
    if host == "gitlab.com":
        return _gitlab_search(http, topic, min_stars, limit)
    if host == "codeberg.org":
        return _codeberg_search(http, topic, min_stars, limit)
    raise ValueError(f"unsupported host: {host}")
