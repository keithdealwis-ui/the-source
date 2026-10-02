"""The daily intelligence cycle's own stages (KEI-848).

The KEI-807 cycle asked every host the full, expensive question about every repository
once a week. A daily cycle splits that in two:

  light refresh    one cheap, batched question per ~50 repositories, every day, for the
                   whole known corpus: stars, forks, pushed_at, archive state, licence,
                   newest release, default-branch head, repository id and current name.
  deep enrichment  the existing enrich.observe (commit and PR evidence, release diffs,
                   licence probes), only for repositories that are new, materially
                   changed, or whose deep observation is older than deep.max_age_days.

Every light refresh is kept as a dated, deterministic snapshot under data/history/, so
1-, 7-, 30- and 90-day deltas can be asked later (`history-delta`). Discovery candidates
from the daily search lanes are reconciled against the corpus by repository id before
anything is called new (data/discovery/). Upstream corroboration (corroborate.py) checks
the light refresh against independent providers; it can flag disagreement but never
changes a canonical value.

Nothing in this module writes outside THE_SOURCE_ROOT, so inside a cycle it only ever
touches the scratch copy; promotion is cycle.py's job.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import common
from .activity import iso, parse_ts

HISTORY_SCHEMA = "the-source.history/1"
LIGHT_FIELDS = ("found", "error", "canonical_id", "canonical_name", "host_repo_id", "archived", "disabled",
                "licence_spdx", "stars", "forks", "pushed_at", "open_issues", "latest_release", "head")

LIGHT_GQL_REPO = """
  {alias}: repository(owner:{owner}, name:{name}) {{
    nameWithOwner databaseId isArchived isDisabled stargazerCount forkCount pushedAt
    licenseInfo {{ spdxId }}
    issues(states: OPEN) {{ totalCount }}
    latestRelease {{ tagName publishedAt }}
    defaultBranchRef {{ target {{ oid ... on Commit {{ committedDate }} }} }}
  }}"""


def _paths():
    """Resolved at call time: tests and the cycle's scratch copy move common.ROOT."""
    return common.DATA / "history", common.DATA / "discovery"


# ---------------------------------------------------------------- light refresh

def _light_github_batch(http, keys: list[str], fetched_at: str) -> dict:
    parts = []
    for i, key in enumerate(keys):
        _, owner, name = key.split("/", 2)
        parts.append(LIGHT_GQL_REPO.format(alias=f"r{i}", owner=json.dumps(owner), name=json.dumps(name)))
    status, doc = http.call("https://api.github.com/graphql",
                            {"query": "query{" + "".join(parts) + "\n rateLimit{cost remaining}}"}, auth=True)
    out = {}
    if status != 200 or not isinstance(doc, dict) or doc.get("data") is None:
        for key in keys:
            out[key] = {"found": False, "error": f"graphql_http_{status}", "fetched_at": fetched_at, "transient": True}
        return out
    data = doc.get("data") or {}
    errors = {(e.get("path") or [None])[0]: e.get("type") for e in doc.get("errors") or []}
    for i, key in enumerate(keys):
        repo = data.get(f"r{i}")
        if not repo:
            kind = errors.get(f"r{i}") or "unknown"
            out[key] = {"found": False, "error": "not_found" if kind == "NOT_FOUND" else f"graphql_{kind}",
                        "fetched_at": fetched_at, "transient": kind not in ("NOT_FOUND",)}
            continue
        head = ((repo.get("defaultBranchRef") or {}).get("target")) or {}
        rel = repo.get("latestRelease") or {}
        out[key] = {
            "found": True, "error": None, "fetched_at": fetched_at,
            "canonical_id": "github.com/" + repo["nameWithOwner"].lower(),
            "canonical_name": repo["nameWithOwner"],
            "host_repo_id": repo.get("databaseId"),
            "archived": bool(repo.get("isArchived")), "disabled": bool(repo.get("isDisabled")),
            "licence_spdx": (repo.get("licenseInfo") or {}).get("spdxId"),
            "stars": repo.get("stargazerCount"), "forks": repo.get("forkCount"),
            "pushed_at": repo.get("pushedAt"),
            "open_issues": (repo.get("issues") or {}).get("totalCount"),
            "latest_release": {"tag": rel.get("tagName"), "published_at": rel.get("publishedAt")} if rel else None,
            "head": {"oid": head.get("oid"), "at": head.get("committedDate")} if head else None,
        }
    return out


def _light_github_moved(http, key: str, fetched_at: str) -> dict | None:
    """GraphQL does not follow every rename or transfer; the REST endpoint does."""
    _, owner, name = key.split("/", 2)
    st, rest = http.call(f"https://api.github.com/repos/{owner}/{name}", auth=True)
    moved = (rest or {}).get("full_name") if st == 200 else None
    if not moved or moved.lower() == f"{owner}/{name}".lower():
        return None
    got = _light_github_batch(http, ["github.com/" + moved.lower()], fetched_at)
    rec = next(iter(got.values()))
    if rec.get("found"):
        rec["resolved_from"] = key
    return rec


def _light_rest(http, key: str, fetched_at: str) -> dict:
    """gitlab.com and codeberg.org: one REST call each (a handful of repositories)."""
    from .enrich import GITLAB_SPDX

    host, path = key.split("/", 1)
    if host == "gitlab.com":
        st, repo = http.call("https://gitlab.com/api/v4/projects/" + urllib.parse.quote(path, safe="") + "?license=true")
        if st != 200 or not repo:
            return {"found": False, "error": "not_found" if st == 404 else f"http_{st}", "fetched_at": fetched_at,
                    "transient": st != 404}
        return {"found": True, "error": None, "fetched_at": fetched_at,
                "canonical_id": "gitlab.com/" + repo["path_with_namespace"].lower(),
                "canonical_name": repo["path_with_namespace"], "host_repo_id": repo.get("id"),
                "archived": bool(repo.get("archived")), "disabled": False,
                "licence_spdx": GITLAB_SPDX.get(((repo.get("license") or {}).get("key") or "").lower()),
                "stars": repo.get("star_count"), "forks": repo.get("forks_count"),
                "pushed_at": repo.get("last_activity_at"), "open_issues": repo.get("open_issues_count"),
                "latest_release": None, "head": None}
    st, repo = http.call("https://codeberg.org/api/v1/repos/" + path)
    if st != 200 or not repo:
        return {"found": False, "error": "not_found" if st == 404 else f"http_{st}", "fetched_at": fetched_at,
                "transient": st != 404}
    lic = repo.get("licenses") or []
    return {"found": True, "error": None, "fetched_at": fetched_at,
            "canonical_id": "codeberg.org/" + repo["full_name"].lower(), "canonical_name": repo["full_name"],
            "host_repo_id": repo.get("id"), "archived": bool(repo.get("archived")), "disabled": False,
            "licence_spdx": lic[0] if len(lic) == 1 else None,
            "stars": repo.get("stars_count"), "forks": repo.get("forks_count"),
            "pushed_at": repo.get("updated_at"), "open_issues": repo.get("open_issues_count"),
            "latest_release": None, "head": None}


def light_refresh(keys: list[str], batch_size: int = 50, as_of: datetime | None = None, http=None) -> dict:
    """One light observation per key. Returns {as_of, http_calls, failed_keys, projects}.

    A failure is transient (HTTP error, rate limit, timeout) or a real answer (not found,
    unsupported host). Only transient failures are retried and only they can fail a cycle.
    """
    from .enrich import Http

    policy = common.load_policy()
    supported = set(policy["hosts"]["supported"])
    as_of = as_of or datetime.now(timezone.utc).replace(microsecond=0)
    stamp = iso(as_of)
    http = http or Http()
    projects: dict[str, dict] = {}
    gh = sorted(k for k in keys if k.startswith("github.com/"))
    other = sorted(k for k in keys if not k.startswith("github.com/"))
    batches = [gh[i:i + batch_size] for i in range(0, len(gh), batch_size)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for res in pool.map(lambda b: _light_github_batch(http, b, stamp), batches):
            projects.update(res)
    for key in [k for k in gh if projects[k].get("error") == "not_found"]:
        moved = _light_github_moved(http, key, stamp)
        if moved:
            projects[key] = moved
    for key in other:
        if key.split("/", 1)[0] not in supported:
            projects[key] = {"found": False, "error": "unsupported_host", "fetched_at": stamp}
        else:
            projects[key] = _light_rest(http, key, stamp)
    failed = sorted(k for k, v in projects.items() if v.get("transient"))
    return {"as_of": stamp, "http_calls": http.calls, "failed_keys": failed,
            "projects": dict(sorted(projects.items()))}


def retry_light(light: dict, batch_size: int = 50, http=None) -> dict:
    keys = light["failed_keys"]
    if not keys:
        return light
    as_of = parse_ts(light["as_of"])
    again = light_refresh(keys, batch_size, as_of, http)
    merged = dict(light)
    merged["projects"] = dict(sorted({**light["projects"], **again["projects"]}.items()))
    merged["http_calls"] = light["http_calls"] + again["http_calls"]
    merged["failed_keys"] = again["failed_keys"]
    return merged


# ---------------------------------------------------------------- change detection

def _deep_at(prev: dict) -> str | None:
    return prev.get("deep_fetched_at") or prev.get("fetched_at")


def classify(light: dict, previous: dict, deep_cfg: dict, policy: dict, as_of: str,
             last_activity: dict | None = None) -> dict:
    """Decide, per key, whether today's light observation needs deep enrichment.

    last_activity  key -> last meaningful activity (the dataset's maintenance evaluation)
    Returns {key: {"class": new|changed|stale|unchanged|failed, "reasons": [..], "deep_age_days": n}}.
    `changed` means a material change: one that can move a lane, a licence verdict, an
    identity, maintenance status or a score. A star drift below the threshold is not.
    """
    now = parse_ts(as_of)
    active, maintained = int(policy["maintenance"]["active_days"]), int(policy["maintenance"]["maintained_days"])
    max_age = int(deep_cfg["max_age_days"])
    rel_stars = float(deep_cfg["material_star_change"]["relative"])
    abs_stars = int(deep_cfg["material_star_change"]["absolute"])
    out = {}
    for key, cur in light["projects"].items():
        prev = previous.get(key)
        if cur.get("transient"):
            out[key] = {"class": "failed", "reasons": [cur.get("error") or "lookup failed"]}
            continue
        if prev is None or "found" not in prev:
            out[key] = {"class": "new", "reasons": ["not in the previous snapshot"]}
            continue
        deep_at = parse_ts(_deep_at(prev))
        age = (now - deep_at).days if deep_at else None
        reasons = []
        if bool(cur.get("found")) != bool(prev.get("found")):
            reasons.append(f"found {bool(prev.get('found'))} -> {bool(cur.get('found'))}")
        if cur.get("found") and prev.get("found"):
            if cur.get("canonical_id") != prev.get("canonical_id") or (
                    prev.get("host_repo_id") and cur.get("host_repo_id") != prev.get("host_repo_id")):
                reasons.append(f"identity {prev.get('canonical_id')} -> {cur.get('canonical_id')}")
            for f in ("archived", "disabled"):
                if bool(cur.get(f)) != bool(prev.get(f)):
                    reasons.append(f"{f} {bool(prev.get(f))} -> {bool(cur.get(f))}")
            if cur.get("licence_spdx") != prev.get("licence_spdx"):
                reasons.append(f"licence {prev.get('licence_spdx')} -> {cur.get('licence_spdx')}")
            prev_rel = _newest_release(prev)
            cur_rel = (cur.get("latest_release") or {}).get("tag")
            if cur_rel and "latest_release_tag" in prev and cur_rel != prev_rel:
                reasons.append(f"release {prev_rel} -> {cur_rel}")
            ps, cs = prev.get("stars") or 0, cur.get("stars") or 0
            if abs(cs - ps) >= max(abs_stars, rel_stars * ps):
                reasons.append(f"stars {ps} -> {cs}")
            # Deep evidence ages between enrichments. Where that age could move the
            # maintenance class across a boundary and the repository has been pushed since,
            # re-read the evidence rather than let the class decay on stale evidence.
            last = parse_ts((last_activity or {}).get(key))
            pushed_since = (parse_ts(cur.get("pushed_at")) or now) > (deep_at or now)
            if last and pushed_since:
                days = (now - last).days
                if any(b - max_age <= days <= b + max_age for b in (active, maintained)):
                    reasons.append(f"maintenance boundary: last meaningful activity {days} d ago, pushed since")
        if reasons:
            out[key] = {"class": "changed", "reasons": reasons, "deep_age_days": age}
        elif age is None or age >= max_age:
            out[key] = {"class": "stale", "reasons": [f"deep observation {age} d old (max {max_age})"],
                        "deep_age_days": age}
        else:
            out[key] = {"class": "unchanged", "reasons": [], "deep_age_days": age}
    return out


def _newest_release(prev: dict) -> str | None:
    """The light tier's own release answer, carried in the snapshot (merge_snapshot). The
    deep observation's newest release can be a prerelease, which GraphQL latestRelease
    excludes, so comparing the two would invent changes; without a light answer yet (the
    first daily cycle) there is nothing like-for-like to compare and no change is claimed."""
    return prev.get("latest_release_tag")


def select_deep(classes: dict, budget: int) -> tuple[list[str], list[str]]:
    """New and changed always go deep (they are what the cycle exists to catch). Stale
    rotation fills the remaining budget, oldest first; the rest is deferred, never lost."""
    must = sorted(k for k, c in classes.items() if c["class"] in ("new", "changed"))
    stale = sorted((k for k, c in classes.items() if c["class"] == "stale"),
                   key=lambda k: (-(classes[k].get("deep_age_days") or 10 ** 6), k))
    room = max(0, budget - len(must))
    return must + stale[:room], stale[room:]


def merge_snapshot(previous: dict, light: dict, deep: dict | None, keys: list[str]) -> dict:
    """Today's snapshot: deep observations where taken today, otherwise the previous deep
    observation with today's light fields laid over it. Only `keys` survive (the corpus)."""
    from .enrich import policy_fingerprint

    policy = common.load_policy()
    deep_projects = (deep or {}).get("projects") or {}
    prev_projects = previous.get("projects") or {}
    projects = {}
    for key in sorted(keys):
        lt = light["projects"].get(key) or {}
        if key in deep_projects:
            rec = dict(deep_projects[key])
            rec["deep_fetched_at"] = rec.get("fetched_at")
        elif key in prev_projects:
            rec = dict(prev_projects[key])
            rec.setdefault("deep_fetched_at", rec.get("fetched_at"))
            if lt.get("found") and rec.get("found") and not lt.get("transient"):
                rec.update(stars=lt.get("stars"), forks=lt.get("forks"), pushed_at=lt.get("pushed_at"),
                           fetched_at=lt["fetched_at"])
        else:
            continue
        if lt.get("found") and not lt.get("transient"):
            rec["latest_release_tag"] = (lt.get("latest_release") or {}).get("tag")
        projects[key] = rec
    since = parse_ts(light["as_of"]) - timedelta(days=int(policy["maintenance"]["maintained_days"]))
    return {
        "as_of": light["as_of"],
        "window_start": iso(since),
        "schema_version": common.SCHEMA_VERSION,
        "maintenance_policy_sha256": policy_fingerprint(policy),
        "http_calls": light["http_calls"] + ((deep or {}).get("http_calls") or 0),
        "lookup_failures": sorted((deep or {}).get("lookup_failures") or []),
        "failed_keys": sorted((deep or {}).get("failed_keys") or []),
        "projects": projects,
    }


# ---------------------------------------------------------------- dated history

def _gz_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9) as gz:
        gz.write(text.encode("utf-8"))
    return buf.getvalue()


def history_row(key: str, rec: dict, deep_class: str | None = None) -> dict:
    row = {"key": key, **{f: rec.get(f) for f in LIGHT_FIELDS}, "fetched_at": rec.get("fetched_at")}
    if deep_class:
        row["class"] = deep_class
    return row


def write_history(day: str, light: dict, classes: dict, dataset_version: str | None, run_id: str) -> dict:
    """data/history/daily/<YYYY-MM-DD>.jsonl.gz: one row per corpus key, sorted, gzip with a
    fixed mtime so the same observations always produce the same bytes. INDEX.json lists
    every day with its hash, row count and the run that produced it."""
    history, _ = _paths()
    rows = [history_row(k, v, (classes.get(k) or {}).get("class")) for k, v in sorted(light["projects"].items())]
    text = "".join(common.dumps(r) + "\n" for r in rows)
    path = history / "daily" / f"{day}.jsonl.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_gz_bytes(text))
    index_path = history / "INDEX.json"
    index = common.read_json(index_path) if index_path.exists() else {"schema": HISTORY_SCHEMA, "days": {}}
    index["days"][day] = {"file": f"daily/{day}.jsonl.gz", "rows": len(rows), "as_of": light["as_of"],
                          "sha256": common.sha256_file(path), "run_id": run_id,
                          "dataset_version": dataset_version}
    index["days"] = dict(sorted(index["days"].items()))
    common.write_json(index_path, index)
    return index["days"][day]


def prune_history(retain_days: int, today: str) -> list[str]:
    history, _ = _paths()
    index_path = history / "INDEX.json"
    if not index_path.exists():
        return []
    index = common.read_json(index_path)
    cutoff = (datetime.fromisoformat(today) - timedelta(days=retain_days)).date().isoformat()
    gone = [d for d in index["days"] if d < cutoff]
    for d in gone:
        (history / index["days"][d]["file"]).unlink(missing_ok=True)
        del index["days"][d]
    common.write_json(index_path, index)
    return gone


def load_day(day: str) -> dict:
    history, _ = _paths()
    index = common.read_json(history / "INDEX.json")
    meta = index["days"][day]
    raw = (history / meta["file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta["sha256"]:
        raise ValueError(f"history {day}: sha256 mismatch with INDEX.json")
    rows = [json.loads(line) for line in gzip.decompress(raw).decode("utf-8").splitlines() if line]
    return {r["key"]: r for r in rows}


def history_days() -> list[str]:
    history, _ = _paths()
    p = history / "INDEX.json"
    return sorted(common.read_json(p)["days"]) if p.exists() else []


def delta(days: int, on: str | None = None, keys: list[str] | None = None) -> dict:
    """Change over `days` days, ending on `on` (default: the newest day held). The base is
    the newest snapshot on or before on - days; if none is held the answer says so rather
    than inventing a baseline."""
    held = history_days()
    if not held:
        return {"ok": False, "reason": "no history held"}
    on = on or held[-1]
    if on not in held:
        return {"ok": False, "reason": f"no snapshot for {on}"}
    target = (datetime.fromisoformat(on) - timedelta(days=days)).date().isoformat()
    base_day = max((d for d in held if d <= target), default=None)
    if base_day is None:
        return {"ok": False, "reason": f"insufficient history: need a snapshot on or before {target}, "
                                       f"oldest held is {held[0]}", "on": on, "window_days": days}
    a, b = load_day(base_day), load_day(on)
    out = []
    for key in sorted(set(b) if keys is None else set(keys)):
        cur, old = b.get(key), a.get(key)
        if cur is None:
            continue
        row = {"key": key, "status": "new_since_base" if old is None else "tracked"}
        if old is not None and cur.get("found") and old.get("found"):
            for f in ("stars", "forks", "open_issues"):
                if cur.get(f) is not None and old.get(f) is not None:
                    row[f"{f}_delta"] = cur[f] - old[f]
            row["stars"] = cur.get("stars")
            row["pushed_since_base"] = (cur.get("pushed_at") or "") > (old.get("pushed_at") or "")
            row["changes"] = [f for f in ("archived", "disabled", "licence_spdx", "canonical_id")
                              if cur.get(f) != old.get(f)]
            if ((cur.get("latest_release") or {}).get("tag")) != ((old.get("latest_release") or {}).get("tag")):
                row["changes"].append("latest_release")
        out.append(row)
    return {"ok": True, "on": on, "base": base_day, "window_days": days, "exact": base_day == target,
            "projects": out}


# ---------------------------------------------------------------- discovery pool

def load_pool() -> dict:
    _, discovery = _paths()
    p = discovery / "candidates.jsonl"
    return {r["key"]: r for r in common.read_jsonl(p)} if p.exists() else {}


def reconcile_candidates(found: list[dict], corpus_light: dict, pool: dict, day: str) -> dict:
    """Reconcile discovered candidates against the corpus and the existing pool.

    found         [{"key", "provider", "query", "host_repo_id"?, "canonical_name"?, ...}]
    corpus_light  today's light observations of the corpus (key -> rec)
    A candidate is the same entity as a corpus project when its normalised key, its
    resolved canonical id, or its host repository id matches. Only then is it `known`;
    otherwise it joins (or refreshes) the pool. Nothing here creates a canonical entity:
    entering the canonical corpus still needs a licence-cleared relationship claim.
    """
    by_key, by_id = {}, {}
    for k, rec in corpus_light.items():
        by_key[k] = k
        if rec.get("canonical_id"):
            by_key[rec["canonical_id"]] = k
        if rec.get("host_repo_id"):
            by_id[(k.split("/", 1)[0], rec["host_repo_id"])] = k
    pool = {k: dict(v) for k, v in pool.items()}
    known, new, refreshed = [], [], []
    for c in sorted(found, key=lambda c: (c["key"], c["provider"])):
        key = c["key"].lower()
        hid = c.get("host_repo_id")
        match = by_key.get(key) or (by_id.get((key.split("/", 1)[0], hid)) if hid else None)
        if match:
            known.append({"key": key, "matches": match, "provider": c["provider"],
                          "by": "key" if by_key.get(key) else "host_repo_id"})
            pool.pop(key, None)
            continue
        alias = next((k for k, v in pool.items() if hid and v.get("host_repo_id") == hid and k != key), None)
        target = alias or key
        entry = pool.get(target)
        prov = {"provider": c["provider"], "query": c.get("query")}
        if entry is None:
            entry = {"key": target, "first_seen": day, "status": "awaiting_relationship_evidence",
                     "providers": [], "aliases": []}
            new.append(target)
        else:
            refreshed.append(target)
        if alias and key not in entry["aliases"]:
            entry["aliases"] = sorted(set(entry["aliases"]) | {key})
        if prov not in entry["providers"]:
            entry["providers"] = sorted(entry["providers"] + [prov], key=lambda p: (p["provider"], p["query"] or ""))
        for f in ("host_repo_id", "canonical_name", "stars", "created_at", "pushed_at", "licence_spdx",
                  "archived", "description", "topics"):
            if c.get(f) is not None:
                entry[f] = c[f]
        entry["last_seen"] = day
        pool[target] = entry
    return {"pool": dict(sorted(pool.items())), "known": known, "new": sorted(set(new)),
            "refreshed": sorted(set(refreshed) - set(new))}


def write_pool(pool: dict) -> None:
    _, discovery = _paths()
    common.write_jsonl(discovery / "candidates.jsonl", list(pool.values()))
