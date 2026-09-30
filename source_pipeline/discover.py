"""Discover lane (KEI-811) — the two stages that talk to GitHub.

    discover-harvest   run every strategy in config/discover.yaml; record what each
                       returned in data/discover/frontier.json
    discover-verify    ask the host about every candidate; record the answers in
                       data/discover/live.json and append them to
                       data/discover/history/observations.jsonl

Neither stage decides anything. Deciding is `discover-build`, which reads only these
records, so a build can be repeated offline and reproduces the same corpus.
"""
from __future__ import annotations

import base64
import json
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import common, enrich
from .activity import Rules, iso
from .discover_universe import (depth1_routes, extract_github_repos, parse_seed_file, rank_topics,
                                recursion_frontier, all_routes, order_for_cap)

DISCOVER = common.DATA / "discover"
FRONTIER = DISCOVER / "frontier.json"
LIVE = DISCOVER / "live.json"
HISTORY = DISCOVER / "history" / "observations.jsonl"


def load_cfg() -> dict:
    return common.load_yaml(common.CONFIG / "discover.yaml")


def cfg_fingerprint(cfg: dict) -> str:
    import hashlib

    return hashlib.sha256(common.dumps(cfg).encode()).hexdigest()


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


META_FIELDS = """
  nameWithOwner databaseId isArchived isDisabled isEmpty isFork isMirror isPrivate isTemplate
  parent{nameWithOwner databaseId} stargazerCount forkCount pushedAt createdAt updatedAt
  description homepageUrl licenseInfo{spdxId name key}
  primaryLanguage{name} languages(first:5, orderBy:{field:SIZE, direction:DESC}){nodes{name}}
  repositoryTopics(first:20){nodes{topic{name}}} owner{login __typename}
  latestRelease{tagName publishedAt}
"""


class Api:
    def __init__(self):
        self.http = enrich.Http()
        self._search_lock = threading.Lock()
        self._last_search = 0.0

    def rest(self, path: str):
        return self.http.call("https://api.github.com/" + path.lstrip("/"), auth=True)

    def gql(self, query: str, variables: dict | None = None):
        return self.http.call("https://api.github.com/graphql",
                              {"query": query, "variables": variables or {}}, auth=True)

    def search(self, q: str, page: int):
        # Search allows 30 requests a minute; stay under it rather than lean on retries.
        with self._search_lock:
            wait = self._last_search + 2.2 - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last_search = time.time()
        return self.rest("search/repositories?" + urllib.parse.urlencode(
            {"q": q, "sort": "stars", "order": "desc", "per_page": 100, "page": page}))

    def readme(self, slug: str, ref: str | None = None) -> tuple[int, str | None]:
        path = f"repos/{slug}/readme" + (f"?ref={urllib.parse.quote(ref)}" if ref else "")
        st, doc = self.rest(path)
        if st == 200 and isinstance(doc, dict) and doc.get("encoding") == "base64":
            return st, base64.b64decode(doc.get("content") or "").decode("utf-8", errors="replace")
        if st == 200 and isinstance(doc, dict) and doc.get("download_url"):
            # Files over 1 MB come back without inline content; read the blob itself.
            return self.raw(doc["download_url"])
        return st, None

    def raw(self, url: str) -> tuple[int, str | None]:
        import urllib.error
        import urllib.request

        common.refuse_network(url)
        for attempt in range(4):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": enrich.UA})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return resp.status, resp.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as err:
                if err.code == 404:
                    return 404, None
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                pass
            time.sleep(2 ** attempt)
        self.http.failures.append(f"raw {url}")
        return 0, None

    def meta_batch(self, slugs: list[str], size: int = 40) -> dict[str, dict]:
        """Metadata for many repositories, several per GraphQL query."""
        out: dict[str, dict] = {}
        chunks = [slugs[i:i + size] for i in range(0, len(slugs), size)]

        def one(chunk):
            parts = []
            for i, slug in enumerate(chunk):
                owner, name = slug.split("/", 1)
                parts.append(f"r{i}: repository(owner:{json.dumps(owner)}, name:{json.dumps(name)}){{{META_FIELDS}}}")
            st, doc = self.gql("query{" + "\n".join(parts) + " rateLimit{cost remaining}}")
            if st != 200 or not isinstance(doc, dict) or doc.get("data") is None:
                if len(chunk) > 1:  # a heavy batch can time out; halve it rather than give up
                    half = len(chunk) // 2
                    return {**one(chunk[:half]), **one(chunk[half:])}
                return {chunk[0]: {"found": False, "error": f"graphql_http_{st}"}}
            data = doc["data"]
            errors = {tuple(e.get("path") or [])[:1]: e.get("type") for e in doc.get("errors") or []}
            res = {}
            for i, slug in enumerate(chunk):
                repo = data.get(f"r{i}")
                if repo:
                    res[slug] = {"found": True, **_meta_view(repo)}
                else:
                    kind = errors.get((f"r{i}",)) or "unknown"
                    res[slug] = {"found": False, "error": "not_found" if kind == "NOT_FOUND" else f"graphql_{kind}"}
            return res

        with ThreadPoolExecutor(max_workers=4) as pool:
            for n, res in enumerate(pool.map(one, chunks), 1):
                out.update(res)
                if n % 25 == 0:
                    log(f"  metadata {n * size}/{len(slugs)}")
        # GraphQL follows most renames but not every transfer; REST follows the rest.
        for slug, m in sorted(out.items()):
            if m.get("error") == "not_found":
                st, rest = self.rest(f"repos/{slug}")
                moved = (rest or {}).get("full_name") if st == 200 else None
                if moved and moved.lower() != slug:
                    again = self.meta_batch([moved.lower()], size=1).get(moved.lower())
                    if again and again.get("found"):
                        out[slug] = {**again, "resolved_via": "rest_redirect"}
                elif st not in (404, 451, 200):
                    out[slug] = {"found": False, "error": f"rest_http_{st}"}
        return out


def _meta_view(repo: dict) -> dict:
    lic = repo.get("licenseInfo") or {}
    parent = repo.get("parent") or None
    return {
        "name_with_owner": repo["nameWithOwner"],
        "canonical_slug": repo["nameWithOwner"].lower(),
        "database_id": repo["databaseId"],
        "archived": bool(repo["isArchived"]),
        "disabled": bool(repo["isDisabled"]),
        "empty": bool(repo["isEmpty"]),
        "is_fork": bool(repo["isFork"]),
        "is_mirror": bool(repo["isMirror"]),
        "is_template": bool(repo.get("isTemplate")),
        "private": bool(repo.get("isPrivate")),
        "parent": {"slug": parent["nameWithOwner"].lower(), "database_id": parent["databaseId"]} if parent else None,
        "stars": repo.get("stargazerCount"),
        "forks": repo.get("forkCount"),
        "pushed_at": repo.get("pushedAt"),
        "created_at": repo.get("createdAt"),
        "updated_at": repo.get("updatedAt"),
        "description": repo.get("description"),
        "homepage": repo.get("homepageUrl") or None,
        "licence_spdx": lic.get("spdxId"),
        "licence_name": lic.get("name"),
        "primary_language": (repo.get("primaryLanguage") or {}).get("name"),
        "languages": [n["name"] for n in (repo.get("languages") or {}).get("nodes") or []],
        "topics": sorted(n["topic"]["name"] for n in (repo.get("repositoryTopics") or {}).get("nodes") or []),
        "owner": {"login": repo["owner"]["login"], "type": repo["owner"]["__typename"]},
        "latest_release": repo.get("latestRelease"),
    }


def _resolve_query(q: str, as_of: datetime) -> str:
    import re

    def sub(m):
        return (as_of - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    return re.sub(r"\{as_of-(\d+)d\}", sub, q)


def _owner_repos(api: Api, owners: list[str], per_owner: int) -> dict[str, dict]:
    q = """repositoryOwner(login:%s){ login repositories(first:%d, privacy:PUBLIC, isFork:false,
           ownerAffiliations:OWNER, orderBy:{field:STARGAZERS, direction:DESC}){ nodes{nameWithOwner} } }"""
    out = {}
    for i in range(0, len(owners), 20):
        chunk = owners[i:i + 20]
        body = " ".join(f"o{j}: " + q % (json.dumps(o), per_owner) for j, o in enumerate(chunk))
        st, doc = api.gql("query{" + body + "}")
        data = (doc or {}).get("data") or {}
        for j, o in enumerate(chunk):
            node = data.get(f"o{j}")
            if st != 200:
                out[o] = {"status": f"graphql_http_{st}", "repos": []}
            elif not node:
                out[o] = {"status": "owner_not_found", "repos": []}
            else:
                out[o] = {"status": "ok", "repos": sorted(n["nameWithOwner"].lower()
                                                          for n in node["repositories"]["nodes"])}
    return out


def _readme_links(api: Api, slugs: list[str], cap: int) -> dict[str, dict]:
    import hashlib

    def one(slug):
        st, text = api.readme(slug)
        if text is None:
            return slug, {"status": f"http_{st}", "links": []}
        links = [[s, ln] for s, ln in extract_github_repos(text) if s != slug]
        return slug, {"status": "ok", "readme_sha256": hashlib.sha256(text.encode()).hexdigest(),
                      "links_found": len(links), "links": links[:cap]}

    with ThreadPoolExecutor(max_workers=6) as pool:
        return dict(sorted(pool.map(one, slugs)))


def harvest(as_of: datetime | None = None) -> dict:
    """Run every discovery strategy and record what each returned."""
    import hashlib

    cfg = load_cfg()
    as_of = as_of or datetime.now(timezone.utc).replace(microsecond=0)
    api = Api()
    seed_path = common.ROOT / cfg["seeds"]["keith_seed_file"]
    seed_text = seed_path.read_text(encoding="utf-8")

    # Depth 0.
    keith = parse_seed_file(seed_text)
    accounts = {}
    for login in cfg["seeds"]["github_accounts"]:
        acct = {}
        for kind, path in (("starred", f"users/{login}/starred"), ("watching", f"users/{login}/subscriptions")):
            repos, page, status = [], 1, "ok"
            while True:
                st, rows = api.rest(f"{path}?per_page=100&page={page}")
                if st != 200 or not isinstance(rows, list):
                    status = f"http_{st}"
                    break
                repos += [r["full_name"].lower() for r in rows]
                if len(rows) < 100:
                    break
                page += 1
            acct[kind] = sorted(set(repos))
            acct[f"{kind}_status"] = status
        accounts[login] = acct
        log(f"account {login}: {len(acct['starred'])} starred, {len(acct['watching'])} watching")
    replace = sorted({
        r["oss_id"].split("/", 1)[1] for r in common.read_jsonl(common.ROOT / cfg["seeds"]["replace_lane_dataset"])
        if r["oss_id"].startswith("github.com/")})

    seed_slugs = sorted({r["slug"] for r in keith if r.get("slug")}
                        | {s for a in accounts.values() for k in ("starred", "watching") for s in a[k]})
    log(f"{len(seed_slugs)} seeds from Keith's corpus and accounts; {len(replace)} Replace-lane repositories")
    seed_meta = api.meta_batch(seed_slugs)

    st_cfg = cfg["strategies"]
    strategies: dict = {}

    # Owner neighbourhoods of every seed (resolved owner, so a renamed seed counts once).
    owners: dict[str, set] = {}
    for slug in seed_slugs:
        m = seed_meta.get(slug) or {}
        if m.get("found"):
            owners.setdefault(m["owner"]["login"].lower(), set()).add(slug)
    got = _owner_repos(api, sorted(owners), int(st_cfg["owner_neighbourhood"]["per_owner"]))
    strategies["owner_neighbourhood"] = {o: {**got[o], "via": sorted(owners[o])} for o in sorted(owners)}
    log(f"owner neighbourhoods: {sum(len(v['repos']) for v in got.values())} repositories from {len(owners)} owners")

    # README links of every resolved seed.
    resolved = sorted({(seed_meta[s] or {}).get("canonical_slug") for s in seed_slugs if (seed_meta.get(s) or {}).get("found")})
    strategies["readme_links"] = _readme_links(api, resolved, int(st_cfg["readme_links"]["per_repo"]))
    log(f"readme links: {sum(len(v['links']) for v in strategies['readme_links'].values())} from {len(resolved)} READMEs")

    # Topic search.
    ts = st_cfg["topic_search"]
    ranked = rank_topics({s: m for s, m in seed_meta.items() if m.get("found")}, cfg)
    results = {}
    for topic, _ in ranked:
        q = f"topic:{topic} stars:>={int(ts['min_stars'])}"
        stc, doc = api.search(q, 1)
        items = (doc or {}).get("items") or []
        results[topic] = {"query": q, "status": "ok" if stc == 200 else f"http_{stc}",
                          "total_count": (doc or {}).get("total_count"),
                          "repos": [i["full_name"].lower() for i in items][: int(ts["per_topic"])]}
    strategies["topic_search"] = {"ranked_topics": ranked, "results": results}
    log(f"topic search: {len(ranked)} topics, {sum(len(v['repos']) for v in results.values())} results")

    # Curated lists, at a pinned commit, licence-checked.
    lists = {}
    allowed = set(st_cfg["curated_lists"]["allowed_licences"])
    for name in st_cfg["curated_lists"]["lists"]:
        stc, repo = api.rest(f"repos/{name}")
        if stc != 200 or not repo:
            lists[name] = {"decision": "unavailable", "reason": f"http_{stc}", "links": []}
            continue
        spdx = (repo.get("license") or {}).get("spdx_id")
        stc, head = api.rest(f"repos/{name}/commits/{urllib.parse.quote(repo['default_branch'])}")
        commit = (head or {}).get("sha")
        rec = {"licence_spdx": spdx, "commit": commit, "default_branch": repo["default_branch"]}
        if spdx not in allowed:
            rec.update(decision="refuse", links=[],
                       reason=f"licence {spdx or 'none'} is not in allowed_licences; its selection is not used")
        elif not commit:
            rec.update(decision="unavailable", links=[], reason="could not pin a commit")
        else:
            stc, text = api.readme(name, commit)
            if text is None:
                rec.update(decision="unavailable", links=[], reason=f"readme_http_{stc}")
            else:
                rec.update(decision="allow", readme_sha256=hashlib.sha256(text.encode()).hexdigest(),
                           links=[[s, ln] for s, ln in extract_github_repos(text)],
                           reason="recognised licence; only repository URLs are used, no list text is kept")
        lists[name] = rec
        log(f"list {name}: {rec['decision']} ({len(rec['links'])} links)")
    strategies["curated_lists"] = lists

    # Trending / highly starred.
    trending = {}
    for qd in st_cfg["trending"]["queries"]:
        q = _resolve_query(qd["q"], as_of)
        repos, status = [], "ok"
        for page in range(1, int(qd["pages"]) + 1):
            stc, doc = api.search(q, page)
            if stc != 200:
                status = f"http_{stc}_page_{page}"
                break
            items = (doc or {}).get("items") or []
            repos += [i["full_name"].lower() for i in items]
            if len(items) < 100:
                break
        trending[qd["id"]] = {"query": q, "status": status, "repos": repos}
    strategies["trending"] = trending
    log(f"trending: {sum(len(v['repos']) for v in trending.values())} results")

    frontier = {
        "as_of": iso(as_of),
        "config_sha256": cfg_fingerprint(cfg),
        "seed_file_sha256": hashlib.sha256(seed_text.encode()).hexdigest(),
        "seeds": {"keith": keith, "accounts": accounts, "replace": replace},
        "seed_meta": seed_meta,
        "strategies": strategies,
    }

    # Depth 2: recurse from the depth-1 candidates that several routes agree on.
    rc = cfg["recursion"]
    chosen = recursion_frontier(depth1_routes(frontier, cfg), cfg) if int(rc["max_depth"]) >= 2 else []
    log(f"recursion frontier: {len(chosen)} repositories")
    fmeta = api.meta_batch(chosen)
    r_owners: dict[str, set] = {}
    r_resolved = []
    for slug in chosen:
        m = fmeta.get(slug) or {}
        if m.get("found") and not m.get("archived"):
            r_owners.setdefault(m["owner"]["login"].lower(), set()).add(slug)
            r_resolved.append(m["canonical_slug"])
    r_owners = {o: v for o, v in r_owners.items() if o not in strategies["owner_neighbourhood"]}
    got = _owner_repos(api, sorted(r_owners), int(rc["owner_neighbourhood_per_owner"]))
    strategies["recursion"] = {
        "frontier": chosen,
        "owner_neighbourhood": {o: {**got[o], "via": sorted(r_owners[o])} for o in sorted(r_owners)},
        "readme_links": _readme_links(api, sorted(set(r_resolved) - set(resolved)), int(rc["readme_links_per_repo"])),
    }
    frontier["http_calls"] = api.http.calls
    frontier["lookup_failures"] = sorted(api.http.failures)
    common.write_json(FRONTIER, frontier)
    universe = all_routes(frontier, cfg)
    log(f"universe: {len(universe)} candidate slugs; {len(api.http.failures)} lookup failures")
    return {"as_of": frontier["as_of"], "candidates": len(universe), "lookup_failures": len(api.http.failures)}


def _replace_ids() -> dict[str, dict]:
    """KEI-805 records by lower-case slug, with the host id KEI-805 observed."""
    out = {}
    for r in common.read_jsonl(common.DATASET / "oss_projects.jsonl"):
        if r["oss_id"].startswith("github.com/"):
            out[r["oss_id"].split("/", 1)[1]] = r
    return out


def prefilter(meta: dict, window_start: str, cfg: dict) -> list[str]:
    """Reasons a repository cannot pass the quality bar, readable from metadata alone.

    Each is a sufficient reason under config/policy.yaml, not a new rule: an archived
    repository is excluded however active; a repository with no push since the window
    opened cannot have a commit, tag or release inside it.
    """
    from .activity import parse_ts

    reasons = []
    if not meta.get("found"):
        return ["repository_not_found" if meta.get("error") == "not_found" else "live_verification_failed"]
    if meta.get("private"):
        reasons.append("not_public")
    if meta.get("archived"):
        reasons.append("archived")
    if meta.get("disabled"):
        reasons.append("disabled")
    if meta.get("empty"):
        reasons.append("empty_repository")
    admitted = {k.lower() for k in (cfg["aliases"].get("admitted_forks") or {})}
    if meta.get("is_fork") and meta["canonical_slug"] not in admitted:
        reasons.append("fork_alias")
    if meta.get("is_mirror"):
        reasons.append("mirror_alias")
    pushed = parse_ts(meta.get("pushed_at"))
    if pushed is None or pushed < parse_ts(window_start):
        reasons.append("stale_no_meaningful_activity_12_months")
    return reasons


def verify(as_of: datetime | None = None, workers: int = 8, only_failed: bool = False) -> dict:
    """Ask the host about every candidate in the recorded universe."""
    cfg = load_cfg()
    policy = common.load_policy()
    rules = Rules(policy)
    frontier = common.read_json(FRONTIER)
    universe = all_routes(frontier, cfg)
    ordered = order_for_cap(universe)[: int(cfg["raw_universe_cap"])]
    replace = _replace_ids()

    previous = common.read_json(LIVE) if (only_failed and LIVE.exists()) else None
    if previous:
        as_of_dt = datetime.fromisoformat(previous["as_of"].replace("Z", "+00:00"))
    else:
        as_of_dt = as_of or datetime.now(timezone.utc).replace(microsecond=0)
    since = as_of_dt - timedelta(days=int(policy["maintenance"]["maintained_days"]))
    stamp = iso(as_of_dt)
    api = Api()

    # 1. Metadata for every candidate not already judged in the Replace lane.
    # Replace-lane repositories get metadata (owner, topics, languages) for the graph, but
    # keep their KEI-805 verdict: they are not verified again here.
    todo = list(ordered)
    meta = dict(previous["meta"]) if previous else {}
    if previous:
        todo = [s for s in todo if not (meta.get(s) or {}).get("found") and (meta.get(s) or {}).get("error") != "not_found"]
    log(f"metadata for {len(todo)} candidates")
    meta.update(api.meta_batch(todo))

    # 2. Deep verification, once per repository, for those metadata cannot already reject.
    deep = dict(previous["deep"]) if previous else {}
    by_id: dict[int, str] = {}
    replace_host_ids = {r["live"].get("host_repo_id") for r in replace.values() if r.get("live")}
    for slug in ordered:
        m = meta.get(slug) or {}
        if m.get("found") and not prefilter(m, iso(since), cfg) and m["database_id"] not in replace_host_ids:
            by_id.setdefault(m["database_id"], m["canonical_slug"])
    targets = sorted(set(by_id.values()))
    if previous:
        targets = [t for t in targets if not (deep.get(t) or {}).get("found") or (deep.get(t) or {}).get("incomplete")]
    log(f"deep verification of {len(targets)} repositories")
    enrich.CACHE = enrich.CommitCache()
    failures_before = 0

    def one(slug):
        try:
            live = enrich.github(api.http, "github.com/" + slug, since, rules, policy, stamp, stop_when_active=True)
        except Exception as exc:  # recorded, never swallowed: the project is then rejected
            live = {"provider": "github", "fetched_at": stamp, "found": False,
                    "error": f"adapter_exception:{type(exc).__name__}:{exc}"}
        return slug, live

    def snapshot_of(complete: bool) -> dict:
        # The clock the build judges by is when observation ENDED: a run takes hours, and
        # activity seen during it must not fall after the clock and be discarded.
        return {
            "complete": complete,
            "started_at": (previous or {}).get("started_at", stamp),
            "as_of": iso(datetime.now(timezone.utc).replace(microsecond=0)) if complete else stamp,
            "window_start": iso(since),
            "maintenance_policy_sha256": enrich.policy_fingerprint(policy),
            "frontier_as_of": frontier["as_of"],
            "universe_evaluated": len(ordered),
            "http_calls": api.http.calls + (previous or {}).get("http_calls", 0),
            "lookup_failures": sorted(set(api.http.failures)),
            "meta": dict(sorted(meta.items())),
            "deep": dict(sorted(deep.items())),
        }

    # Checkpoint, so an interrupted run resumes with `--only-failed` instead of starting over.
    common.write_json(LIVE, snapshot_of(False))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for slug, live in pool.map(one, targets):
            deep[slug] = live
            done += 1
            if done % 200 == 0:
                log(f"  deep {done}/{len(targets)} (http calls {api.http.calls}, failures {len(api.http.failures)})")
                enrich.CACHE.save()
            if done % 1000 == 0:
                common.write_json(LIVE, snapshot_of(False))
    enrich.CACHE.save()
    # A REST lookup that failed for a repository leaves its evidence incomplete. Mark it,
    # so the build rejects it with that reason rather than read the gap as inactivity.
    failed_urls = api.http.failures[failures_before:]
    for slug in targets:
        needle = f"/repos/{deep[slug].get('canonical_name') or slug}/".lower()
        if any(needle in u.lower() for u in failed_urls):
            deep[slug]["incomplete"] = True
        else:
            deep[slug].pop("incomplete", None)

    snapshot = snapshot_of(True)
    common.write_json(LIVE, snapshot)
    appended = append_history(snapshot)
    log(f"verify done: {len(meta)} metadata, {len(deep)} deep, {len(api.http.failures)} failures, {appended} history rows appended")
    return {"as_of": stamp, "metadata": len(meta), "deep": len(deep), "lookup_failures": len(api.http.failures)}


def history_row(meta: dict, deep: dict | None, as_of: str) -> dict:
    obs = (deep or {}).get("observations") or {}
    return {
        "id": f"github:{meta['database_id']}",
        "as_of": as_of,
        "slug": meta["canonical_slug"],
        "stars": meta.get("stars"),
        "forks": meta.get("forks"),
        "pushed_at": meta.get("pushed_at"),
        "archived": meta.get("archived"),
        "licence_spdx": meta.get("licence_spdx"),
        "latest_release": meta.get("latest_release"),
        "commits_in_12m_window": obs.get("commits_in_window"),
        "issues_closed_12m": obs.get("issues_closed_12m"),
    }


def append_history(snapshot: dict) -> int:
    """Append one observation per repository per verify run. Never rewrites a row."""
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    have = set()
    if HISTORY.exists():
        for row in common.read_jsonl(HISTORY):
            have.add((row["id"], row["as_of"]))
    rows = {}
    for slug, m in snapshot["meta"].items():
        if m.get("found"):
            row = history_row(m, snapshot["deep"].get(m["canonical_slug"]), snapshot["as_of"])
            rows.setdefault(row["id"], row)
    new = [r for k, r in sorted(rows.items()) if (k, r["as_of"]) not in have]
    with open(HISTORY, "a", encoding="utf-8") as fh:
        for r in new:
            fh.write(common.dumps(r) + "\n")
    return len(new)
