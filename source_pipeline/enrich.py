"""Stage 3 — independent live verification.

Nothing an upstream list says about a project's licence, archive state or activity is
trusted. Each candidate repository is asked directly, through its host's API, and the
answers are stored as a dated snapshot under data/live/. Later stages read only the
snapshot, so a run can be repeated offline and reproduce the same dataset.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import common
from .activity import Rules, iso, parse_ts
from .harvest import _token

UA = "the-source-verify/1.0"

GQL = """
query($owner:String!,$name:String!,$since:GitTimestamp!,$sinceDT:DateTime!,$n:Int!,$prs:Int!){
  repository(owner:$owner,name:$name){
    nameWithOwner databaseId isArchived isDisabled isEmpty isFork isMirror isPrivate
    stargazerCount forkCount pushedAt createdAt
    licenseInfo{spdxId name key}
    defaultBranchRef{ name target{ ... on Commit{ history(first:$n, since:$since){
      totalCount nodes{ oid committedDate author{user{login} name} } } } } }
    releases(first:3, orderBy:{field:CREATED_AT,direction:DESC}){
      nodes{tagName publishedAt isDraft isPrerelease tagCommit{oid}}}
    refs(refPrefix:"refs/tags/", first:3, orderBy:{field:TAG_COMMIT_DATE,direction:DESC}){
      nodes{name target{__typename ... on Commit{oid committedDate}
        ... on Tag{target{__typename ... on Commit{oid committedDate}}}}}}
    pullRequests(states:MERGED, first:$prs, orderBy:{field:UPDATED_AT,direction:DESC}){
      nodes{number mergedAt author{login} files(first:50){nodes{path}}}}
    closedIssues: issues(states:CLOSED, filterBy:{since:$sinceDT}){totalCount}
  }
  rateLimit{remaining resetAt cost}
}
"""


class Http:
    """HTTP with retry. Transient failures are counted: a run with any is not trusted."""

    def __init__(self):
        import threading

        self.token = _token()
        self.failures: list[str] = []
        self.calls = 0
        self._lock = threading.Lock()

    def call(self, url, data=None, auth=False, tries=5):
        common.refuse_network(url)
        headers = {"User-Agent": UA, "Accept": "application/json"}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = json.dumps(data).encode() if data is not None else None
        if body:
            headers["Content-Type"] = "application/json"
        delay = 2.0
        status = 0
        for attempt in range(tries):
            req = urllib.request.Request(url, data=body, headers=headers)
            with self._lock:
                self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8") or "null")
            except urllib.error.HTTPError as err:
                status = err.code
                if err.code in (404, 410, 451, 409, 422):
                    return err.code, None  # a real answer, not a failure
                if err.headers.get("cf-mitigated") == "challenge":
                    # A Cloudflare managed challenge wants a browser, not a retry (KEI-914).
                    # Still a failure: the answer is unknown, not absent.
                    break
                if attempt < tries - 1:
                    wait = delay
                    if err.headers.get("X-RateLimit-Remaining") == "0" and err.headers.get("X-RateLimit-Reset"):
                        wait = max(1.0, float(err.headers["X-RateLimit-Reset"]) - time.time() + 2)
                    elif (err.headers.get("Retry-After") or "").isdigit():
                        wait = float(err.headers["Retry-After"])
                    time.sleep(min(wait, 1800))
                    delay *= 2
                    continue
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
                status = 0
                if attempt < tries - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
        with self._lock:
            self.failures.append(f"{status} {url.split('?')[0]}")
        return status, None

    def child(self) -> "Http":
        """Same token, lock and budget; its own failure list, so a failure can be pinned
        to the repository whose lookup produced it (KEI-807: retry only what failed)."""
        import copy

        c = copy.copy(self)
        c.failures = []
        c.calls = 0
        return c


class CommitCache:
    """Paths touched by a commit never change, so they are fetched once per commit."""

    def __init__(self):
        import threading

        self.path = common.LIVE / "commit_paths_cache.json"
        self.data = common.read_json(self.path) if self.path.exists() else {}
        self._lock = threading.Lock()

    def get(self, key, fetch):
        if key in self.data:
            return self.data[key]
        value = fetch()
        if value is not None:
            with self._lock:
                self.data[key] = value
        return value

    def save(self):
        common.write_json(self.path, self.data)


CACHE: CommitCache | None = None


def cached(key, fetch):
    return CACHE.get(key, fetch) if CACHE is not None else fetch()


def _attach_release_paths(obs: dict, rules: Rules, commit_paths, compare_paths=None) -> None:
    """Read what the newest version release and the newest version tag actually shipped.

    What a release ships is the difference from the previous version, so that is what
    is read where the host can compare two tags. The tagged commit alone is the
    fallback; on its own it is often a merge or a version bump and says little.
    """
    known: dict = {}
    for key in ("releases", "tags"):
        versions = [i for i in obs.get(key) or [] if not i.get("draft") and rules.is_version(i.get("ref"))]
        if not versions:
            continue
        newest = versions[0]
        older = next((v for v in versions[1:] if v.get("ref") != newest.get("ref")), None)
        ident = (newest.get("ref"), older.get("ref") if older else None)
        if ident not in known:
            paths = None
            if older and compare_paths:
                paths = compare_paths(older["ref"], newest["ref"])
                if paths:
                    newest["paths_basis"] = f"compare {older['ref']}...{newest['ref']}"
            if not paths and newest.get("commit"):
                paths = commit_paths(newest["commit"])
                if paths:
                    newest["paths_basis"] = "tagged commit"
            known[ident] = (paths, newest.get("paths_basis"))
        newest["paths"], basis = known[ident]
        if basis:
            newest["paths_basis"] = basis


def policy_fingerprint(policy: dict) -> str:
    import hashlib

    return hashlib.sha256(common.dumps(policy["maintenance"]).encode()).hexdigest()


def _base(provider, fetched_at):
    return {"provider": provider, "fetched_at": fetched_at, "found": False}


def _github_probe_licence(http: Http, full_name: str) -> dict:
    """Collect what the second-opinion licence detector needs: root listing + licence texts."""
    import base64

    from .licence_detect import ENTERPRISE_DIR, LICENCE_FILE

    probe = {"root": [], "texts": {}}
    st, listing = http.call(f"https://api.github.com/repos/{full_name}/contents/", auth=True)
    if st != 200 or not isinstance(listing, list):
        probe["error"] = f"root_listing_http_{st}"
        return probe
    for entry in listing:
        name, kind = entry.get("name", ""), entry.get("type")
        if (kind == "file" and LICENCE_FILE.match(name)) or (kind == "dir" and ENTERPRISE_DIR.match(name)):
            probe["root"].append({"name": name, "type": kind})
    for entry in probe["root"]:
        if entry["type"] != "file":
            continue
        st, blob = http.call(
            f"https://api.github.com/repos/{full_name}/contents/{urllib.parse.quote(entry['name'])}", auth=True
        )
        if st == 200 and isinstance(blob, dict) and blob.get("encoding") == "base64":
            text = base64.b64decode(blob.get("content") or "").decode("utf-8", errors="replace")
            probe["texts"][entry["name"]] = text[:200000]
    probe["root"].sort(key=lambda e: e["name"])
    return probe


def _github_probe_licence_gql(http: Http, full_name: str) -> dict:
    """The REST probe's result, read through GraphQL: root listing, then licence texts.

    KEI-811: same shape and the same files as `_github_probe_licence`, drawn from the
    GraphQL rate budget instead of the REST one.
    """
    from .licence_detect import ENTERPRISE_DIR, LICENCE_FILE

    owner, name = full_name.split("/", 1)
    probe = {"root": [], "texts": {}}
    q = ('query($o:String!,$n:String!){repository(owner:$o,name:$n){object(expression:"HEAD:")'
         '{... on Tree{entries{name type}}}}}')
    st, doc = http.call("https://api.github.com/graphql", {"query": q, "variables": {"o": owner, "n": name}},
                        auth=True)
    tree = ((((doc or {}).get("data") or {}).get("repository") or {}).get("object") or {})
    if st != 200 or "entries" not in tree:
        probe["error"] = f"root_listing_graphql_{st}"
        return probe
    for e in tree["entries"]:
        kind = {"blob": "file", "tree": "dir"}.get(e.get("type"))
        n = e.get("name", "")
        if (kind == "file" and LICENCE_FILE.match(n)) or (kind == "dir" and ENTERPRISE_DIR.match(n)):
            probe["root"].append({"name": n, "type": kind})
    files = [e["name"] for e in probe["root"] if e["type"] == "file"]
    if files:
        parts = " ".join(f'f{i}: object(expression:{json.dumps("HEAD:" + f)}){{... on Blob{{text isBinary}}}}'
                         for i, f in enumerate(files))
        st, doc = http.call("https://api.github.com/graphql",
                            {"query": f"query($o:String!,$n:String!){{repository(owner:$o,name:$n){{{parts}}}}}",
                             "variables": {"o": owner, "n": name}}, auth=True)
        repo = ((doc or {}).get("data") or {}).get("repository") or {}
        for i, f in enumerate(files):
            blob = repo.get(f"f{i}") or {}
            if blob.get("text") is not None and not blob.get("isBinary"):
                probe["texts"][f] = blob["text"][:200000]
    probe["root"].sort(key=lambda e: e["name"])
    return probe


def github(http: Http, key: str, since: datetime, rules: Rules, policy: dict, fetched_at: str,
           _redirected: bool = False, stop_when_active: bool = False) -> dict:
    """Observe one GitHub repository.

    stop_when_active (KEI-811, off by default so KEI-805 is unchanged): when the merged
    pull requests GraphQL already returned include a human-authored one that touches code
    inside the active window, the maintenance class is decided (`active`), so the
    per-commit and release-diff lookups are skipped. The same rule as stopping at the
    first qualifying commit, applied one step earlier.
    """
    live = _base("github", fetched_at)
    _, owner, name = key.split("/", 2)
    m = policy["maintenance"]
    status, doc = http.call(
        "https://api.github.com/graphql",
        {
            "query": GQL,
            "variables": {
                "owner": owner, "name": name, "since": iso(since), "sinceDT": iso(since),
                "n": int(m["commit_lookback"]), "prs": int(m["merged_pr_lookback"]),
            },
        },
        auth=True,
    )
    if status != 200 or not doc:
        live["error"] = f"graphql_http_{status}"
        return live
    repo = (doc.get("data") or {}).get("repository")
    if not repo:
        errs = doc.get("errors") or []
        kind = errs[0].get("type") if errs else "unknown"
        if kind == "NOT_FOUND" and not _redirected:
            # GraphQL does not follow every rename or transfer; the REST endpoint does.
            st, rest = http.call(f"https://api.github.com/repos/{owner}/{name}", auth=True)
            moved = (rest or {}).get("full_name") if st == 200 else None
            if moved and moved.lower() != f"{owner}/{name}".lower():
                out = github(http, "github.com/" + moved.lower(), since, rules, policy, fetched_at, True,
                             stop_when_active)
                out["resolved_from"] = key
                return out
        live["error"] = "not_found" if kind == "NOT_FOUND" else f"graphql_{kind}"
        return live
    lic = repo.get("licenseInfo") or {}
    live.update(
        found=True,
        canonical_id="github.com/" + repo["nameWithOwner"].lower(),
        canonical_name=repo["nameWithOwner"],
        host_repo_id=repo.get("databaseId"),
        archived=bool(repo["isArchived"]),
        disabled=bool(repo["isDisabled"]),
        empty=bool(repo["isEmpty"]),
        is_fork=bool(repo["isFork"]),
        is_mirror=bool(repo["isMirror"]),
        licence_spdx=lic.get("spdxId"),
        licence_name=lic.get("name"),
        stars=repo.get("stargazerCount"),
        forks=repo.get("forkCount"),
        pushed_at=repo.get("pushedAt"),
        created_at=repo.get("createdAt"),
        default_branch=(repo.get("defaultBranchRef") or {}).get("name"),
    )
    obs = {"commits": [], "merged_prs": [], "releases": [], "tags": []}
    history = (((repo.get("defaultBranchRef") or {}).get("target") or {}).get("history")) or {}
    obs["commits_in_window"] = history.get("totalCount")
    merged_prs = []
    for pr in (repo.get("pullRequests") or {}).get("nodes") or []:
        if not pr.get("mergedAt"):
            continue
        merged_prs.append(
            {
                "ref": f"#{pr['number']}",
                "at": pr["mergedAt"],
                "author": (pr.get("author") or {}).get("login"),
                "paths": [f["path"] for f in ((pr.get("files") or {}).get("nodes") or [])],
            }
        )
    decided = None
    if stop_when_active:
        from .activity import parse_ts
        active_from = parse_ts(fetched_at) - timedelta(days=rules.active_days)
        for pr in merged_prs:
            at = parse_ts(pr["at"])
            if rules.change_qualifies(pr)[0] and at and at >= active_from:
                decided = pr["ref"]
                break
    floor = None
    if stop_when_active and not decided:
        from .activity import parse_ts
        dates = [parse_ts(p["at"]) for p in merged_prs if rules.change_qualifies(p)[0] and parse_ts(p["at"])]
        floor = max(dates) if dates else None
    if decided:
        obs["inspection_stopped"] = f"class decided by qualifying merged PR {decided} inside the active window"
    for node in ([] if decided else history.get("nodes") or []):
        if floor is not None:
            from .activity import parse_ts
            if (parse_ts(node["committedDate"]) or floor) <= floor:
                obs["inspection_stopped"] = "older commits cannot be newer than the qualifying merged PR already seen"
                break
        author = ((node.get("author") or {}).get("user") or {}).get("login") or (node.get("author") or {}).get("name")
        change = {"ref": node["oid"], "at": node["committedDate"], "author": author, "paths": None}
        if not rules.is_bot(author):
            def fetch(oid=node["oid"]):
                st, detail = http.call(
                    f"https://api.github.com/repos/{repo['nameWithOwner']}/commits/{oid}", auth=True
                )
                if st == 200 and detail:
                    return [f["filename"] for f in (detail.get("files") or [])][:100]
                return None
            change["paths"] = cached(f"github:{repo['databaseId']}:{node['oid']}", fetch)
        obs["commits"].append(change)
        if rules.change_qualifies(change)[0]:
            break  # newest qualifying commit found; older ones cannot change the class
    obs["merged_prs"] = merged_prs
    for rel in (repo.get("releases") or {}).get("nodes") or []:
        obs["releases"].append(
            {"ref": rel["tagName"], "at": rel.get("publishedAt"), "draft": bool(rel.get("isDraft")),
             "prerelease": bool(rel.get("isPrerelease")), "commit": (rel.get("tagCommit") or {}).get("oid")}
        )
    for ref in (repo.get("refs") or {}).get("nodes") or []:
        target = ref.get("target") or {}
        if target.get("__typename") == "Tag":
            target = target.get("target") or {}
        obs["tags"].append({"ref": ref["name"], "at": target.get("committedDate"), "commit": target.get("oid")})

    def commit_paths(oid):
        def fetch():
            st, detail = http.call(f"https://api.github.com/repos/{repo['nameWithOwner']}/commits/{oid}", auth=True)
            if st == 200 and detail:
                return [f["filename"] for f in (detail.get("files") or [])][:100]
            return None
        return cached(f"github:{repo['databaseId']}:{oid}", fetch)

    def compare_paths(base, head):
        def fetch():
            q = urllib.parse.quote
            st, detail = http.call(
                f"https://api.github.com/repos/{repo['nameWithOwner']}/compare/{q(base, safe='')}...{q(head, safe='')}"
                "?per_page=1", auth=True)
            if st == 200 and detail:
                return [f["filename"] for f in (detail.get("files") or [])][:300]
            return None
        return cached(f"github:{repo['databaseId']}:compare:{base}...{head}", fetch)

    if not decided:
        newest_seen = None
        if stop_when_active:
            # A release older than qualifying evidence already in hand cannot change the result.
            from .activity import parse_ts
            seen = [parse_ts(c["at"]) for c in obs["commits"] + obs["merged_prs"] if rules.change_qualifies(c)[0]]
            seen = [d for d in seen if d]
            newest_seen = max(seen) if seen else None
        if newest_seen is None:
            _attach_release_paths(obs, rules, commit_paths, compare_paths)
        else:
            from .activity import parse_ts
            newer = {k: [i for i in obs[k] if (parse_ts(i.get("at")) or newest_seen) > newest_seen]
                     for k in ("releases", "tags")}
            if newer["releases"] or newer["tags"]:
                _attach_release_paths(obs, rules, commit_paths, compare_paths)
            else:
                obs["release_inspection_skipped"] = "no release or tag newer than qualifying evidence already seen"
    obs["issues_closed_12m"] = (repo.get("closedIssues") or {}).get("totalCount")
    live["observations"] = obs
    if live["licence_spdx"] not in set(policy["licence"]["recognised_spdx"]):
        live["licence_probe"] = (_github_probe_licence_gql if stop_when_active
                                 else _github_probe_licence)(http, repo["nameWithOwner"])
    return live


GITLAB_SPDX = {
    "mit": "MIT", "apache-2.0": "Apache-2.0", "agpl-3.0": "AGPL-3.0", "gpl-3.0": "GPL-3.0",
    "gpl-2.0": "GPL-2.0", "lgpl-3.0": "LGPL-3.0", "lgpl-2.1": "LGPL-2.1", "mpl-2.0": "MPL-2.0",
    "bsd-2-clause": "BSD-2-Clause", "bsd-3-clause": "BSD-3-Clause", "unlicense": "Unlicense",
    "isc": "ISC", "epl-2.0": "EPL-2.0",
}


def _gitlab_commits(http: Http, api: str, branch: str | None, since: datetime, n: int, rules: Rules) -> list[dict]:
    """The newest commits on the default branch, newest first, stopping at the first that
    qualifies, at `n`, or once older than `since`.

    GitLab's commit *list* endpoint (`/repository/commits?since=`) now sits behind a
    Cloudflare managed challenge for anonymous callers (KEI-914, observed 2026-10-10);
    the branch, single-commit and diff endpoints do not. So the branch head is read and
    the first-parent chain walked one commit at a time — the same observations the list
    gave, one call per commit instead of one for the page.
    """
    out: list[dict] = []
    if not branch:
        return out
    st, head = http.call(f"{api}/repository/branches/{urllib.parse.quote(branch, safe='')}")
    c = (head or {}).get("commit") if st == 200 else None
    while c and len(out) < n:
        at = parse_ts(c.get("committed_date"))
        if at and at < since:
            break
        change = {"ref": c["id"], "at": c.get("committed_date"), "author": c.get("author_name"), "paths": None}
        if not rules.is_bot(change["author"]):
            st, diff = http.call(f"{api}/repository/commits/{c['id']}/diff?per_page=100")
            if st == 200 and diff is not None:
                change["paths"] = [d["new_path"] for d in diff]
        out.append(change)
        if rules.change_qualifies(change)[0]:
            break
        parents = c.get("parent_ids") or []
        if not parents:
            break
        st, c = http.call(f"{api}/repository/commits/{parents[0]}")
        if st != 200:
            c = None
    return out


def gitlab(http: Http, key: str, since: datetime, rules: Rules, policy: dict, fetched_at: str) -> dict:
    live = _base("gitlab", fetched_at)
    path = key.split("/", 1)[1]
    api = "https://gitlab.com/api/v4/projects/" + urllib.parse.quote(path, safe="")
    status, repo = http.call(api + "?license=true")
    if status != 200 or not repo:
        live["error"] = "not_found" if status == 404 else f"http_{status}"
        return live
    lic = repo.get("license") or {}
    live.update(
        found=True,
        canonical_id="gitlab.com/" + repo["path_with_namespace"].lower(),
        canonical_name=repo["path_with_namespace"],
        host_repo_id=repo.get("id"),
        archived=bool(repo.get("archived")),
        disabled=False,
        empty=bool(repo.get("empty_repo")),
        is_fork=bool(repo.get("forked_from_project")),
        is_mirror=bool(repo.get("mirror")),
        licence_spdx=GITLAB_SPDX.get((lic.get("key") or "").lower()),
        licence_name=lic.get("name"),
        stars=repo.get("star_count"),
        forks=repo.get("forks_count"),
        pushed_at=repo.get("last_activity_at"),
        created_at=repo.get("created_at"),
        default_branch=repo.get("default_branch"),
    )
    obs = {"commits": [], "merged_prs": [], "releases": [], "tags": []}
    n = int(policy["maintenance"]["commit_lookback"])
    obs["commits"] = _gitlab_commits(http, api, repo.get("default_branch"), since, n, rules)
    st, rels = http.call(f"{api}/releases?per_page=3")
    for r in rels or []:
        obs["releases"].append({"ref": r.get("tag_name"), "at": r.get("released_at"), "draft": False,
                                "commit": (r.get("commit") or {}).get("id")})
    st, tags = http.call(f"{api}/repository/tags?per_page=3&order_by=updated")
    for t in tags or []:
        obs["tags"].append({"ref": t.get("name"), "at": (t.get("commit") or {}).get("committed_date"),
                            "commit": (t.get("commit") or {}).get("id")})

    def commit_paths(oid):
        st, diff = http.call(f"{api}/repository/commits/{oid}/diff?per_page=100")
        return [d["new_path"] for d in diff] if st == 200 and diff is not None else None

    def compare_paths(base, head):
        q = urllib.parse.quote
        st, cmp_ = http.call(f"{api}/repository/compare?from={q(base, safe='')}&to={q(head, safe='')}")
        return [d["new_path"] for d in (cmp_.get("diffs") or [])][:300] if st == 200 and cmp_ else None

    _attach_release_paths(obs, rules, commit_paths, compare_paths)
    live["observations"] = obs
    return live


def _gitea_probe_licence(http: Http, api: str) -> dict:
    import base64

    from .licence_detect import ENTERPRISE_DIR, LICENCE_FILE

    probe = {"root": [], "texts": {}}
    st, listing = http.call(f"{api}/contents")
    if st != 200 or not isinstance(listing, list):
        probe["error"] = f"root_listing_http_{st}"
        return probe
    for entry in listing:
        name, kind = entry.get("name", ""), entry.get("type")
        if (kind == "file" and LICENCE_FILE.match(name)) or (kind == "dir" and ENTERPRISE_DIR.match(name)):
            probe["root"].append({"name": name, "type": kind})
    for entry in probe["root"]:
        if entry["type"] != "file":
            continue
        st, blob = http.call(f"{api}/contents/{urllib.parse.quote(entry['name'])}")
        if st == 200 and isinstance(blob, dict) and blob.get("encoding") == "base64":
            probe["texts"][entry["name"]] = base64.b64decode(blob.get("content") or "").decode(
                "utf-8", errors="replace")[:200000]
    probe["root"].sort(key=lambda e: e["name"])
    return probe


def codeberg(http: Http, key: str, since: datetime, rules: Rules, policy: dict, fetched_at: str) -> dict:
    live = _base("codeberg", fetched_at)
    path = key.split("/", 1)[1]
    api = "https://codeberg.org/api/v1/repos/" + path
    status, repo = http.call(api)
    if status != 200 or not repo:
        live["error"] = "not_found" if status == 404 else f"http_{status}"
        return live
    licences = repo.get("licenses") or []
    live.update(
        found=True,
        canonical_id="codeberg.org/" + repo["full_name"].lower(),
        canonical_name=repo["full_name"],
        host_repo_id=repo.get("id"),
        archived=bool(repo.get("archived")),
        disabled=False,
        empty=bool(repo.get("empty")),
        is_fork=bool(repo.get("fork")),
        is_mirror=bool(repo.get("mirror")),
        licence_spdx=licences[0] if len(licences) == 1 else None,
        licence_name=", ".join(licences) or None,
        stars=repo.get("stars_count"),
        forks=repo.get("forks_count"),
        pushed_at=repo.get("updated_at"),
        created_at=repo.get("created_at"),
        default_branch=repo.get("default_branch"),
    )
    if live["licence_spdx"] not in set(policy["licence"]["recognised_spdx"]):
        live["licence_probe"] = _gitea_probe_licence(http, api)
    obs = {"commits": [], "merged_prs": [], "releases": [], "tags": []}
    n = int(policy["maintenance"]["commit_lookback"])
    st, commits = http.call(f"{api}/commits?limit={n}&stat=false&files=true&verification=false")
    for c in commits or []:
        at = (c.get("commit") or {}).get("committer", {}).get("date") or c.get("created")
        author = (c.get("author") or {}).get("login") or ((c.get("commit") or {}).get("author") or {}).get("name")
        change = {"ref": c["sha"], "at": at, "author": author,
                  "paths": [f["filename"] for f in (c.get("files") or [])]}
        obs["commits"].append(change)
        if rules.change_qualifies(change)[0]:
            break
    st, rels = http.call(f"{api}/releases?limit=3")
    for r in rels or []:
        obs["releases"].append({"ref": r.get("tag_name"), "at": r.get("published_at"),
                                "draft": bool(r.get("draft")), "commit": r.get("tag_name")})

    def commit_paths(ref):
        st, found = http.call(f"{api}/commits?sha={urllib.parse.quote(str(ref))}&limit=1&stat=false&files=true&verification=false")
        if st == 200 and found:
            return [f["filename"] for f in (found[0].get("files") or [])]
        return None

    _attach_release_paths(obs, rules, commit_paths)
    live["observations"] = obs
    return live


ADAPTERS = {"github.com": github, "gitlab.com": gitlab, "codeberg.org": codeberg}


def observe(keys: list[str], as_of: datetime | None = None, workers: int = 8, progress=None,
            save_cache: bool = True) -> dict:
    """Ask every host about every key. Returns a snapshot; writes only the commit cache,
    and not even that with save_cache=False (intake: the cache belongs to the cycle)."""
    policy = common.load_policy()
    rules = Rules(policy)
    as_of = as_of or datetime.now(timezone.utc).replace(microsecond=0)
    since = as_of - timedelta(days=int(policy["maintenance"]["maintained_days"]))
    supported = set(policy["hosts"]["supported"])
    http = Http()
    stamp = iso(as_of)
    global CACHE
    CACHE = CommitCache()

    def one(key):
        host = key.split("/", 1)[0]
        if host not in supported:
            return key, dict(_base(host, stamp), error="unsupported_host"), [], 0
        h = http.child()
        try:
            live = ADAPTERS[host](h, key, since, rules, policy, stamp)
        except Exception as exc:  # recorded, never swallowed: the project is then rejected
            live = dict(_base(host, stamp), error=f"adapter_exception:{type(exc).__name__}:{exc}")
        return key, live, h.failures, h.calls

    projects, failures, failed_keys, calls = {}, [], [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (key, live, fails, n) in enumerate(pool.map(one, sorted(keys)), 1):
            projects[key] = live
            calls += n
            if fails:
                failures.extend(fails)
                failed_keys.append(key)
            if progress and i % 50 == 0:
                progress(i, len(keys))
    if save_cache:
        CACHE.save()
    return {
        "http_calls": calls,
        "lookup_failures": sorted(failures),
        "failed_keys": sorted(failed_keys),
        "schema_version": common.SCHEMA_VERSION,
        "as_of": stamp,
        "window_start": iso(since),
        "maintenance_policy_sha256": policy_fingerprint(policy),
        "projects": dict(sorted(projects.items())),
    }


def retry_failed(snapshot: dict, workers: int = 8) -> dict:
    """Re-ask only the repositories whose lookup failed, under the snapshot's own clock,
    and merge. A retry that still fails leaves the snapshot untrusted (build refuses it)."""
    keys = snapshot.get("failed_keys") or []
    if not keys:
        return snapshot
    as_of = datetime.strptime(snapshot["as_of"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    again = observe(keys, as_of=as_of, workers=workers)
    merged = dict(snapshot)
    merged["projects"] = dict(sorted({**snapshot["projects"], **again["projects"]}.items()))
    merged["http_calls"] = snapshot["http_calls"] + again["http_calls"]
    merged["lookup_failures"] = again["lookup_failures"]
    merged["failed_keys"] = again["failed_keys"]
    return merged


def write_snapshot(snapshot: dict) -> None:
    common.write_json(common.LIVE / "snapshot.json", snapshot)


def run(keys: list[str], as_of: datetime | None = None, workers: int = 8, progress=None) -> dict:
    snapshot = observe(keys, as_of=as_of, workers=workers, progress=progress)
    write_snapshot(snapshot)
    return snapshot
