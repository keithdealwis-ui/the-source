"""Community Radar (KEI-850, KEI-913): repositories being discussed right now. Methodology: docs/RADAR.md.

Lawful public sources, each through the interface its operator provides for it:

  hn         Hacker News, official API (hacker-news.firebaseio.com/v0): the show, best and
             top story lists, then each story. A story counts if it links a repository.
  forem      Forem's public v1 API on dev.to: the most popular articles per tag over the
             last few days, then each article's body, read only to find repository links.
  bluesky    Bluesky's documented public search (app.bsky.feed.searchPosts, no key) with
             its domain filter: posts of the last week that link github.com.
  x          X API v2 recent search (api.x.com/2/tweets/search/recent) under a bearer token
             X issued; the query itself excludes reposts. Unset token: `unconfigured`.
  instagram  Instagram Platform hashtag search (graph.facebook.com) under a token Meta
             issued for a professional account; captions are read only to find repository
             links. Unset token: `unconfigured`. Lawfulness and limits: docs/RADAR-SOCIAL.md.

Nothing a person wrote is stored: no titles, no text, no captions, no usernames. A mention
keeps the item id, its own address, its public points and comment counts, when it was
published, the repository addresses it links and, where the source exposes an opaque
account id, a truncated hash of it (data/radar/mentions.jsonl). Re-observing an item keeps
its earlier (points, comments) readings, so attention velocity is measurable.

From those mentions the radar ranks repositories, deterministically and with caps:

  extract     repository links normalised to host/owner/name, at most max_repos_per_item
              per item, each item's weight shared between them; site pages are not owners.
  dedupe      one mention per (source, item id), the newest observation winning; one
              entry per repository, however many addresses (old names, case, .git, deep
              links) its mentions used.
  rank        per mention, engagement = ln(1 + points), ln(1 + comments) and ln(1 + points
              gained per day), weighted per source, halved every half_life_days; a later
              mention by an author already counted on that source is cut to
              same_author_factor (reposts, self-promotion); the strongest N mentions summed,
              with a bonus per corroborating source; ties broken by key; at most max_ranked.
  reconcile   each entry is matched to the corpus by key, current name and host repository
              id (GitHub links are resolved against GitHub, capped and cached in
              data/radar/resolved.jsonl), then labelled canonical / corpus / candidate /
              not_found / unresolved. A new, existing, unarchived, non-fork repository above
              a score floor joins the discovery pool; it never becomes canonical here. The
              strongest max_candidates repositories new to The Source are listed separately.

    data/radar/collection.json   what the last collection asked, got and failed at
    data/radar/mentions.jsonl    the mentions (facts and addresses only), retain_days
    data/radar/resolved.jsonl    GitHub's answer for each resolved address
    data/radar/radar.jsonl       the ranked entries
    data/radar/candidates.jsonl  the ranked repositories not already in The Source
    data/radar/MANIFEST.json     methodology version and fingerprint, inputs, counts, hashes

Collection needs the network; everything after it is a pure function of the files above,
the corpus files and config/radar.yaml, so `radar-validate` rebuilds it byte for byte.
It never touches data/canonical/ or api/v1/.
"""
from __future__ import annotations

import hashlib
import html
import math
import os
import re
import shutil
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import common
from .activity import iso, parse_ts

SCHEMA = "the-source.radar/1"
SOURCES = ("hn", "forem", "bluesky", "x", "instagram")
STATUSES = ("canonical", "corpus", "candidate", "not_found", "unresolved")
FILES = ("collection.json", "mentions.jsonl", "resolved.jsonl", "radar.jsonl", "candidates.jsonl")
MENTION_FIELDS = ("source", "item_id", "permalink", "published_at", "observed_at", "points", "comments", "via", "repos",
                  "author", "observations")
LINK = re.compile(r"https?://(?:www\.)?(github\.com|gitlab\.com|codeberg\.org)/([A-Za-z0-9][A-Za-z0-9_.-]*)/"
                  r"([A-Za-z0-9_.-]+)", re.I)


def _dir() -> Path:
    return common.DATA / "radar"


def load_config() -> dict:
    return common.load_yaml(common.CONFIG / "radar.yaml")


def parameters_sha256(cfg: dict) -> str:
    params = {k: cfg[k] for k in ("extract", "ranking", "reconcile")}
    return hashlib.sha256(common.dumps(params).encode()).hexdigest()


def check_version(cfg: dict) -> list[str]:
    """The method's parameters must equal the ones its version was released with."""
    m = cfg["methodology"]
    released = m.get("released") or {}
    got = parameters_sha256(cfg)
    if m["version"] not in released:
        return [f"methodology {m['version']} is not in methodology.released"]
    if released[m["version"]] != got:
        return [f"parameters changed without a version bump: {m['version']} was released as "
                f"{released[m['version']]}, parameters now hash to {got}"]
    return []


def _r(x: float, n: int = 4) -> float:
    return round(x + 0.0, n)


# ---------------------------------------------------------------- extraction

def repo_key(host: str, owner: str, name: str, extract: dict) -> str | None:
    host, owner, name = host.lower(), owner.lower(), name.lower()
    name = re.sub(r"\.git$", "", name).rstrip(".")
    if not name or name in (".", "..") or not owner:
        return None
    if host == "github.com" and owner in set(extract["reserved_owners"]):
        return None
    return f"{host}/{owner}/{name}"


def extract_repos(texts: list[str | None], extract: dict) -> list[str]:
    """Repository keys linked from `texts`, in order of first appearance, de-duplicated,
    at most max_repos_per_item."""
    hosts = {h.lower() for h in extract["hosts"]}
    out: list[str] = []
    for text in texts:
        for m in LINK.finditer(html.unescape(text or "")):
            if m.group(1).lower() not in hosts:
                continue
            k = repo_key(m.group(1), m.group(2), m.group(3), extract)
            if k and k not in out:
                out.append(k)
    return out[:int(extract["max_repos_per_item"])]


# ---------------------------------------------------------------- collection (network)

def _age_days(published_at: str | None, as_of: datetime) -> float | None:
    t = parse_ts(published_at)
    return None if t is None else max(0.0, (as_of - t).total_seconds() / 86400)


def _hn(http, src: dict, extract: dict, as_of: datetime, observed: str) -> tuple[list[dict], dict]:
    api = src["api"].rstrip("/")
    via: dict[int, list[str]] = {}
    order: list[int] = []
    errors = []
    for name in src["lists"]:
        st, ids = http.call(f"{api}/{name}.json")
        if st != 200 or not isinstance(ids, list):
            errors.append(f"hn {name}: http {st}")
            continue
        for i in ids[:int(src["max_items_per_list"])]:
            if i not in via:
                via[i] = []
                order.append(i)
            via[i].append(name)
    order = order[:int(src["max_items"])]

    def item(i):
        return http.call(f"{api}/item/{int(i)}.json")

    with ThreadPoolExecutor(max_workers=int(src.get("workers", 4))) as pool:
        got = list(pool.map(item, order))
    out, failed, gone = [], 0, 0
    for i, (st, it) in zip(order, got):
        if st in (404, 410):  # removed since it was listed: a real answer, not an outage
            gone += 1
            continue
        if st != 200:
            failed += 1
            continue
        if not isinstance(it, dict) or it.get("type") != "story" or it.get("dead") or it.get("deleted"):
            continue
        published = iso(datetime.fromtimestamp(int(it.get("time") or 0), timezone.utc))
        age = _age_days(published, as_of)
        if (it.get("score") or 0) < int(src["min_points"]) or age is None or age > float(src["max_age_days"]):
            continue
        repos = extract_repos([it.get("url"), it.get("text")], extract)
        if repos:
            out.append({"source": "hn", "item_id": str(it["id"]),
                        "permalink": f"https://news.ycombinator.com/item?id={int(it['id'])}",
                        "published_at": published, "observed_at": observed,
                        "points": int(it.get("score") or 0), "comments": int(it.get("descendants") or 0),
                        "via": sorted(via[i]), "repos": repos})
    if failed:
        errors.append(f"hn: {failed} of {len(order)} item reads failed")
    status = "failed" if not order and errors else "degraded" if errors else "ok"
    return out, {"status": status, "listed": len(via), "items_read": len(order), "gone": gone,
                 "mentions": len(out), "errors": errors}


def _forem(http, src: dict, extract: dict, as_of: datetime, observed: str) -> tuple[list[dict], dict]:
    api = src["api"].rstrip("/")
    arts: dict[int, dict] = {}
    errors = []
    for tag in src["tags"]:
        q = urllib.parse.urlencode({"tag": tag, "top": int(src["top_days"]), "per_page": int(src["per_page"])})
        st, rows = http.call(f"{api}/articles?{q}")
        if st != 200 or not isinstance(rows, list):
            errors.append(f"forem tag {tag}: http {st}")
            continue
        for a in rows:
            if not isinstance(a, dict) or not isinstance(a.get("id"), int):
                continue
            age = _age_days(a.get("published_at"), as_of)
            if (a.get("positive_reactions_count") or 0) < int(src["min_reactions"]) or age is None \
                    or age > float(src["max_age_days"]):
                continue
            e = arts.setdefault(a["id"], {"a": a, "via": []})
            e["via"].append(tag)
    chosen = sorted(arts.values(), key=lambda e: (-(e["a"].get("positive_reactions_count") or 0), e["a"]["id"]))
    chosen = chosen[:int(src["max_articles"])]
    out, failed, gone = [], 0, 0
    for e in chosen:
        a = e["a"]
        st, full = http.call(f"{api}/articles/{a['id']}")
        if st in (404, 410):  # listed, then unpublished or removed: a real answer, not an outage
            gone += 1
            continue
        if st != 200 or not isinstance(full, dict):
            failed += 1
            continue
        repos = extract_repos([full.get("body_markdown")], extract)  # read, never stored
        if repos:
            out.append({"source": "forem", "item_id": str(a["id"]), "permalink": a.get("url"),
                        "published_at": iso(parse_ts(a.get("published_at"))), "observed_at": observed,
                        "points": int(a.get("positive_reactions_count") or 0),
                        "comments": int(a.get("comments_count") or 0),
                        "via": sorted(set(e["via"])), "repos": repos})
    if failed:
        errors.append(f"forem: {failed} of {len(chosen)} article reads failed")
    status = "failed" if not arts and errors else "degraded" if errors else "ok"
    return out, {"status": status, "listed": len(arts), "items_read": len(chosen), "gone": gone,
                 "mentions": len(out), "errors": errors}


def author_hash(source: str, account_id) -> str | None:
    """A truncated sha256 of the source's opaque account id: enough to tell two authors
    apart and to notice one author repeating, never a name, a handle or a profile."""
    if account_id in (None, ""):
        return None
    return hashlib.sha256(f"{source}:{account_id}".encode()).hexdigest()[:16]


def _ts(value) -> str | None:
    """A source timestamp as the radar's UTC second: X writes `2026-10-09T12:34:56.000Z`,
    Instagram `2026-10-09T12:34:56+0000`."""
    if not isinstance(value, str) or not value:
        return None
    v = re.sub(r"\.\d+(?=Z$|[+-]\d\d:?\d\d$)", "", value.strip())
    v = re.sub(r"([+-]\d\d)(\d\d)$", r"\1:\2", v)
    t = parse_ts(v)
    return iso(t) if t else None


def _unconfigured(why: str) -> dict:
    """A source whose credential is not set asks nothing. That is a declared state, not an
    outage: it never degrades the strategy, and retrieval says so."""
    return {"status": "unconfigured", "listed": 0, "items_read": 0, "gone": 0, "mentions": 0, "errors": [why]}


def _x(http, src: dict, extract: dict, as_of: datetime, observed: str) -> tuple[list[dict], dict]:
    """X API v2 recent search, one page per query. Only the post id, its public metrics,
    its time, the links it carries and a hash of the author id are read; text is not."""
    token = os.environ.get(src["credential_env"]) or None
    if not token:
        return [], _unconfigured(f"{src['credential_env']} is not set: X is not asked")
    api = src["api"].rstrip("/")
    out, errors, seen = [], [], set()
    for q in src["queries"]:
        params = urllib.parse.urlencode({"query": q, "max_results": int(src["max_results"]),
                                         "tweet.fields": "id,author_id,created_at,public_metrics,entities",
                                         "sort_order": "recency"})
        st, doc = http.call(f"{api}/tweets/search/recent?{params}", token=token)
        if st != 200 or not isinstance(doc, dict):
            errors.append(f"x query {q!r}: http {st}")
            continue
        for t in doc.get("data") or []:
            tid = str(t.get("id") or "") if isinstance(t, dict) else ""
            if not tid.isdigit() or tid in seen:
                continue
            seen.add(tid)
            published = _ts(t.get("created_at"))
            age = _age_days(published, as_of)
            pm = t.get("public_metrics") or {}
            points, comments = int(pm.get("like_count") or 0), int(pm.get("reply_count") or 0)
            if points < int(src["min_points"]) or age is None or age > float(src["max_age_days"]):
                continue
            urls = [u.get("unwound_url") or u.get("expanded_url") or u.get("url")
                    for u in (t.get("entities") or {}).get("urls") or [] if isinstance(u, dict)]
            repos = extract_repos(urls, extract)  # the post's links; its text is never read
            if repos:
                m = {"source": "x", "item_id": tid, "permalink": f"https://x.com/i/web/status/{tid}",
                     "published_at": published, "observed_at": observed, "points": points, "comments": comments,
                     "via": [q], "repos": repos}
                if (a := author_hash("x", t.get("author_id"))):
                    m["author"] = a
                out.append(m)
    status = "failed" if not seen and errors else "degraded" if errors else "ok"
    return out, {"status": status, "listed": len(seen), "items_read": len(seen), "gone": 0,
                 "mentions": len(out), "errors": errors}


def _instagram(http, src: dict, extract: dict, as_of: datetime, observed: str) -> tuple[list[dict], dict]:
    """Instagram Platform hashtag search: the hashtag id, then its top or recent media.
    Captions are read in memory to find repository links and then discarded. Hashtag media
    carries no owner, so Instagram mentions have no author hash."""
    token, user = os.environ.get(src["credential_env"]) or None, os.environ.get(src["user_env"]) or None
    if not token or not user:
        return [], _unconfigured(f"{src['credential_env']} or {src['user_env']} is not set: Instagram is not asked")
    api = src["api"].rstrip("/")
    out, errors, seen, listed = [], [], set(), 0
    for tag in src["hashtags"]:
        st, doc = http.call(f"{api}/ig_hashtag_search?{urllib.parse.urlencode({'user_id': user, 'q': tag})}", token=token)
        hid = ((doc.get("data") or [{}])[0].get("id") if st == 200 and isinstance(doc, dict) else None)
        if not hid:
            errors.append(f"instagram #{tag}: http {st}")
            continue
        q = urllib.parse.urlencode({"user_id": user, "limit": int(src["max_items_per_hashtag"]),
                                    "fields": "id,permalink,timestamp,like_count,comments_count,caption,media_type"})
        st, media = http.call(f"{api}/{hid}/{src['edge']}?{q}", token=token)
        if st != 200 or not isinstance(media, dict):
            errors.append(f"instagram #{tag} {src['edge']}: http {st}")
            continue
        for item in media.get("data") or []:
            mid = str(item.get("id") or "") if isinstance(item, dict) else ""
            if not mid.isdigit() or mid in seen:
                continue
            seen.add(mid)
            listed += 1
            published = _ts(item.get("timestamp"))
            age = _age_days(published, as_of)
            points, comments = int(item.get("like_count") or 0), int(item.get("comments_count") or 0)
            link = item.get("permalink") or ""
            if (points < int(src["min_points"]) or age is None or age > float(src["max_age_days"])
                    or not re.fullmatch(r"https://www\.instagram\.com/(p|reel|tv)/[A-Za-z0-9_-]+/?", link)):
                continue
            repos = extract_repos([item.get("caption")], extract)  # read, never stored
            if repos:
                out.append({"source": "instagram", "item_id": mid, "permalink": link, "published_at": published,
                            "observed_at": observed, "points": points, "comments": comments, "via": [tag],
                            "repos": repos})
    status = "failed" if not listed and errors else "degraded" if errors else "ok"
    return out, {"status": status, "listed": listed, "items_read": listed, "gone": 0,
                 "mentions": len(out), "errors": errors}


AT_POST = re.compile(r"at://(did:[a-z]+:[A-Za-z0-9._:%-]+)/app\.bsky\.feed\.post/([A-Za-z0-9._~-]+)")


def _bluesky(http, src: dict, extract: dict, as_of: datetime, observed: str) -> tuple[list[dict], dict]:
    """Bluesky's documented public search (app.bsky.feed.searchPosts) with its `domain`
    filter: posts of the last max_age_days that link the domain, one page per query, no
    key. The post's own link facets and external embed are read; its text is not. The
    permalink names the account by its opaque DID, never its handle."""
    from datetime import timedelta

    since = iso(as_of - timedelta(days=float(src["max_age_days"])))
    out, errors, seen, host_used = [], [], set(), None
    for q in src["queries"]:
        params = urllib.parse.urlencode({"q": q, "domain": src["domain"], "sort": src.get("sort", "top"),
                                         "since": since, "limit": int(src["limit"])})
        doc, st = None, 0
        for host in src["hosts"]:  # the documented public AppView first; the next host if it refuses the method
            st, got = http.call(f"{host.rstrip('/')}/xrpc/app.bsky.feed.searchPosts?{params}")
            if st == 200 and isinstance(got, dict):
                doc, host_used = got, host
                break
        if doc is None:
            errors.append(f"bluesky {q!r}: http {st}")
            continue
        for p in doc.get("posts") or []:
            m = AT_POST.fullmatch(str((p or {}).get("uri") or "")) if isinstance(p, dict) else None
            if not m:
                continue
            did, rkey = m.groups()
            item_id = f"{did}/{rkey}"
            if item_id in seen:
                continue
            seen.add(item_id)
            rec = p.get("record") or {}
            published = _ts(rec.get("createdAt"))
            age = _age_days(published, as_of)
            points, comments = int(p.get("likeCount") or 0), int(p.get("replyCount") or 0)
            if points < int(src["min_points"]) or age is None or age > float(src["max_age_days"]):
                continue
            links = [f.get("uri") for fc in rec.get("facets") or [] if isinstance(fc, dict)
                     for f in fc.get("features") or [] if isinstance(f, dict) and str(f.get("$type", "")).endswith("#link")]
            emb = rec.get("embed") or {}
            links += [((emb.get("external") or {}).get("uri")), (((emb.get("media") or {}).get("external") or {}).get("uri"))]
            repos = extract_repos(links, extract)  # the post's links; its text is never read
            if repos:
                out.append({"source": "bluesky", "item_id": item_id, "permalink": f"https://bsky.app/profile/{did}/post/{rkey}",
                            "published_at": published, "observed_at": observed, "points": points, "comments": comments,
                            "via": [q], "repos": repos, "author": author_hash("bluesky", did)})
    status = "failed" if not seen and errors else "degraded" if errors else "ok"
    return out, {"status": status, "listed": len(seen), "items_read": len(seen), "gone": 0,
                 "mentions": len(out), "errors": errors, "host": host_used}


COLLECTORS = {"hn_firebase": _hn, "forem": _forem, "bluesky_search": _bluesky, "x_api_v2": _x,
              "instagram_graph": _instagram}


def collect(as_of: datetime, http=None, cfg: dict | None = None) -> dict:
    """Ask each source once. A source that fails is recorded and the others carry on: the
    radar is additive, so an outage costs a day's freshness, never what is already held."""
    from .enrich import Http

    cfg = cfg or load_config()
    http = http or Http()
    observed = iso(as_of)
    mentions, sources = [], {}
    for name in SOURCES:
        src = cfg["sources"][name]
        before = http.calls
        try:
            got, stats = COLLECTORS[src["provider"]](http, src, cfg["extract"], as_of, observed)
        except Exception as exc:  # a source failure is recorded, never fatal
            got, stats = [], {"status": "failed", "listed": 0, "items_read": 0, "gone": 0, "mentions": 0,
                              "errors": [f"{name}: {type(exc).__name__}: {exc}"[:300]]}
        stats["http_calls"] = http.calls - before
        sources[name] = stats
        mentions += got
    return {"as_of": observed, "mentions": mentions, "sources": sources}


def merge_mentions(previous: list[dict], new: list[dict], as_of: datetime, retain_days: int,
                   max_observations: int = 8) -> list[dict]:
    """One mention per (source, item id); the newest observation wins, and the earlier
    readings of its points and comments are kept as `observations` (oldest first, at most
    max_observations, the first always kept) so velocity can be measured. The result does
    not depend on the order the records arrive in. Mentions published more than
    retain_days before as_of are dropped."""
    by: dict[tuple, list[dict]] = {}
    for m in list(previous) + list(new):
        by.setdefault((m["source"], m["item_id"]), []).append(m)
    out = []
    for ms in by.values():
        base = max(ms, key=lambda m: (m["observed_at"], m["points"], m["comments"]))
        obs = {}
        for m in ms:
            for o in m.get("observations") or []:
                obs[o["at"]] = {"at": o["at"], "points": int(o["points"]), "comments": int(o["comments"])}
            obs[m["observed_at"]] = {"at": m["observed_at"], "points": m["points"], "comments": m["comments"]}
        hist = [obs[a] for a in sorted(obs)]
        if len(hist) > max_observations:
            hist = [hist[0]] + hist[-(max_observations - 1):]
        rec = {k: v for k, v in base.items() if k != "observations"}
        if len(hist) > 1:
            rec["observations"] = hist
        out.append(rec)
    keep = [m for m in out if (_age_days(m["published_at"], as_of) or 0) <= retain_days]
    return sorted(keep, key=lambda m: (m["source"], int(m["item_id"]) if m["item_id"].isdigit() else 0, m["item_id"]))


def _resolve_one(http, key: str, at: str) -> dict | None:
    _, owner, name = key.split("/", 2)
    st, doc = http.call(f"https://api.github.com/repos/{owner}/{name}", auth=True)
    if st in (404, 410, 451):
        return {"key": key, "found": False, "resolved_at": at, "http_status": st}
    if st != 200 or not isinstance(doc, dict):
        return None  # transient: asked again next cycle
    return {"key": key, "found": True, "resolved_at": at, "host_repo_id": doc.get("id"),
            "canonical_name": doc.get("full_name"), "archived": bool(doc.get("archived")),
            "fork": bool(doc.get("fork")), "stars": doc.get("stargazers_count"),
            "created_at": doc.get("created_at"), "pushed_at": doc.get("pushed_at"),
            "licence_spdx": (doc.get("license") or {}).get("spdx_id")}


def resolve(keys: list[str], cache: dict, as_of: datetime, http, cfg: dict) -> tuple[dict, dict]:
    """Ask GitHub about github.com addresses not resolved within resolve.max_age_days, in
    the order given (highest-ranked first), at most resolve.max_lookups."""
    rc = cfg["resolve"]
    at = iso(as_of)
    due = [k for k in keys if k.startswith("github.com/")
           and (k not in cache or (_age_days(cache[k]["resolved_at"], as_of) or 0) > float(rc["max_age_days"]))]
    ask, deferred = due[:int(rc["max_lookups"])], due[int(rc["max_lookups"]):]
    cache = dict(cache)
    errors = []
    for k in ask:
        r = _resolve_one(http, k, at)
        if r is None:
            errors.append(k)
        else:
            cache[k] = r
    return cache, {"asked": len(ask), "resolved": len(ask) - len(errors), "transient_failures": errors[:20],
                   "deferred": len(deferred)}


# ---------------------------------------------------------------- identity (corpus, pool)

def identity_index() -> dict:
    """Every name the corpus and the pool know a repository by, and its host repository id."""
    key_to_oss, id_to_oss = {}, {}
    ds = common.DATASET / "oss_projects.jsonl"
    for p in common.read_jsonl(ds) if ds.exists() else []:
        for k in [p["oss_id"], *p.get("harvested_as", [])]:
            key_to_oss[k.lower()] = p["oss_id"]
    snap = common.LIVE / "snapshot.json"
    for k, v in (common.read_json(snap)["projects"] if snap.exists() else {}).items():
        oid = key_to_oss.get(k.lower())
        if oid is None:
            continue
        if v.get("canonical_id"):
            key_to_oss.setdefault(v["canonical_id"].lower(), oid)
        if v.get("host_repo_id"):
            id_to_oss.setdefault((k.split("/", 1)[0], v["host_repo_id"]), oid)
    cp = common.DATA / "canonical" / "oss_projects.jsonl"
    canonical = {p["oss_id"] for p in common.read_jsonl(cp)} if cp.exists() else set()
    pool_keys, pool_ids = {}, {}
    pp = common.DATA / "discovery" / "candidates.jsonl"
    for c in common.read_jsonl(pp) if pp.exists() else []:
        for k in [c["key"], *c.get("aliases", [])]:
            pool_keys[k.lower()] = c["key"]
        if c.get("host_repo_id"):
            pool_ids[(c["key"].split("/", 1)[0], c["host_repo_id"])] = c["key"]
    return {"key_to_oss": key_to_oss, "id_to_oss": id_to_oss, "canonical": canonical,
            "pool_keys": pool_keys, "pool_ids": pool_ids}


def identify(link: str, cache: dict, idx: dict) -> dict:
    """The one entity behind a linked address: a corpus project (by address, current name or
    host repository id), else a pool candidate, else the address GitHub resolved it to."""
    host = link.split("/", 1)[0]
    res = cache.get(link)
    names = [link] + ([f"{host}/{res['canonical_name'].lower()}"] if res and res.get("canonical_name") else [])
    hid = res.get("host_repo_id") if res else None
    oid = next((idx["key_to_oss"][n] for n in names if n in idx["key_to_oss"]), None) \
        or (idx["id_to_oss"].get((host, hid)) if hid else None)
    if oid:
        return {"key": oid, "oss_id": oid, "status": "canonical" if oid in idx["canonical"] else "corpus",
                "reason": "in the canonical dataset" if oid in idx["canonical"]
                else "in the corpus, not in the canonical dataset", "host_repo_id": hid}
    pk = next((idx["pool_keys"][n] for n in names if n in idx["pool_keys"]), None) \
        or (idx["pool_ids"].get((host, hid)) if hid else None)
    if pk:
        return {"key": pk, "pool_key": pk, "status": "candidate", "reason": "in the discovery pool",
                "host_repo_id": hid}
    if res and not res["found"]:
        return {"key": link, "status": "not_found", "reason": f"GitHub answered {res.get('http_status')}",
                "host_repo_id": None}
    if res:
        return {"key": names[-1], "status": "unresolved", "host_repo_id": hid,
                "reason": "resolved, not admitted to the pool (archived, fork, below reconcile.pool_min_score "
                          "or over reconcile.max_pool_additions)"}
    return {"key": link, "status": "unresolved", "host_repo_id": None,
            "reason": "not yet resolved (resolve budget)" if host == "github.com"
            else f"{host} addresses are matched by address only"}


# ---------------------------------------------------------------- ranking (pure)

def points_per_day(m: dict) -> float:
    """Attention velocity of one mention: points gained between its first and its latest
    observation, per day, with a one-day floor on the interval. One observation: 0 (unknown
    is not fast)."""
    obs = m.get("observations") or []
    if len(obs) < 2:
        return 0.0
    first, last = parse_ts(obs[0]["at"]), parse_ts(obs[-1]["at"])
    days = max(1.0, (last - first).total_seconds() / 86400) if first and last else 1.0
    return max(0.0, (int(obs[-1]["points"]) - int(obs[0]["points"])) / days)


def contribution(m: dict, as_of: datetime, rk: dict) -> tuple[float, float, float]:
    """(contribution, age in days, engagement) of one mention, before sharing between repos."""
    age = _age_days(m["published_at"], as_of) or 0.0
    eng = (rk["points_weight"] * math.log1p(m["points"]) + rk["comments_weight"] * math.log1p(m["comments"])
           + float(rk.get("velocity_weight", 0)) * math.log1p(points_per_day(m)))
    w = float(rk["source_weight"][m["source"]]) * eng * 0.5 ** (age / float(rk["half_life_days"]))
    return w / len(m["repos"]), age, eng


def _candidates(rows: list[dict], rc: dict) -> list[dict]:
    """The strongest repositories not already in The Source, in score order, from every
    repository mentioned (not only the ranked ones): the daily list worth looking at."""
    picks = [r for r in rows if r["status"] in ("candidate", "unresolved")][:int(rc["max_candidates"])]
    return [{"rank": i, "radar_rank": r.get("rank"), "key": r["key"], "score": r["score"], "status": r["status"],
             "status_reason": r["status_reason"], "pool_key": r["pool_key"], "host_repo_id": r["host_repo_id"],
             "canonical_name": r["canonical_name"], "sources": {s: v["mentions"] for s, v in r["sources"].items()},
             "signals": r["signals"], "first_mentioned_at": r["first_mentioned_at"],
             "last_mentioned_at": r["last_mentioned_at"], "explanation": r["explanation"]}
            for i, r in enumerate(picks, 1)]


def compute(as_of: str, cfg: dict | None = None) -> dict:
    """Rank and reconcile from the stored mentions and resolutions. No network, no clock."""
    cfg = cfg or load_config()
    rk = cfg["ranking"]
    at = parse_ts(as_of)
    d = _dir()
    mentions = common.read_jsonl(d / "mentions.jsonl") if (d / "mentions.jsonl").exists() else []
    cache = {r["key"]: r for r in common.read_jsonl(d / "resolved.jsonl")} if (d / "resolved.jsonl").exists() else {}
    idx = identity_index()
    window = [m for m in mentions if (_age_days(m["published_at"], at) or 0) <= float(rk["window_days"])]
    groups: dict[str, dict] = {}
    for m in window:
        c, age, eng = contribution(m, at, rk)
        for link in m["repos"]:
            ident = identify(link, cache, idx)
            g = groups.setdefault(ident["key"], {"ident": ident, "links": set(), "mentions": {}})
            g["links"].add(link)
            k = (m["source"], m["item_id"])
            if k not in g["mentions"] or c > g["mentions"][k]["c"]:  # one item linking two old names counts once
                g["mentions"][k] = {"m": m, "c": c, "age": age}
    same_author = float(rk.get("same_author_factor", 1.0))
    max_corr = int(rk.get("max_corroborating_sources", 2))
    rows = []
    for key, g in groups.items():
        ms = sorted(g["mentions"].values(), key=lambda x: (-x["c"], x["m"]["source"], x["m"]["item_id"]))
        # an author's strongest mention counts in full; the same author again, on the same
        # source, is a repost or self-promotion and counts a fraction
        seen_authors: set[tuple[str, str]] = set()
        for x in ms:
            a = x["m"].get("author")
            x["repeat"] = bool(a) and (x["m"]["source"], a) in seen_authors
            if a:
                seen_authors.add((x["m"]["source"], a))
            if x["repeat"]:
                x["c"] *= same_author
        ms.sort(key=lambda x: (-x["c"], x["m"]["source"], x["m"]["item_id"]))
        top = ms[:int(rk["max_mentions_per_repo"])]
        base = sum(x["c"] for x in top)
        srcs = sorted({x["m"]["source"] for x in ms})
        corr = min(len(srcs), max_corr)
        mult = 1 + float(rk["second_source_bonus"]) * (corr - 1)
        per = {s: {"mentions": sum(1 for x in ms if x["m"]["source"] == s),
                   "points": sum(x["m"]["points"] for x in ms if x["m"]["source"] == s),
                   "comments": sum(x["m"]["comments"] for x in ms if x["m"]["source"] == s)} for s in srcs}
        authors = {(x["m"]["source"], x["m"]["author"]) for x in ms if x["m"].get("author")}
        velocity = max((points_per_day(x["m"]) for x in top), default=0.0)
        signals = {"authors": len(authors) if authors else None, "points_per_day": _r(velocity, 2),
                   "corroborating_sources": len(srcs)}
        ident = g["ident"]
        res = next((cache[ln] for ln in sorted(g["links"]) if ln in cache and cache[ln].get("found")), None)
        repeats = sum(1 for x in ms if x["repeat"])
        rows.append({
            "key": key, "score": _r(base * mult), "status": ident["status"], "status_reason": ident["reason"],
            "oss_id": ident.get("oss_id"), "pool_key": ident.get("pool_key"),
            "host_repo_id": ident.get("host_repo_id"),
            "canonical_name": res.get("canonical_name") if res else None,
            "link_keys": sorted(g["links"]), "sources": per, "signals": signals,
            "first_mentioned_at": min(x["m"]["published_at"] for x in ms),
            "last_mentioned_at": max(x["m"]["published_at"] for x in ms),
            "mentions": [{"source": x["m"]["source"], "item_id": x["m"]["item_id"], "permalink": x["m"]["permalink"],
                          "published_at": x["m"]["published_at"], "points": x["m"]["points"],
                          "comments": x["m"]["comments"], "shared_with": len(x["m"]["repos"]) - 1,
                          "age_days": _r(x["age"], 2), "contribution": _r(x["c"]), "counted": i < len(top),
                          **({"points_per_day": _r(points_per_day(x["m"]), 2)} if x["m"].get("observations") else {}),
                          **({"repeat_author": True} if x["repeat"] else {})}
                         for i, x in enumerate(ms)],
            "explanation": (f"{len(ms)} mention(s) ({', '.join(f'{s} {per[s]['mentions']}' for s in srcs)}"
                            + (f"; {len(authors)} author(s)" if authors else "")
                            + (f", {repeats} repeat(s) x{same_author:g}" if repeats else "")
                            + (f"; +{velocity:.2f} points/day" if velocity else "") + "); "
                            f"strongest {len(top)} sum {base:.4f}"
                            + (f" x{mult:g} ({corr} sources)" if mult != 1 else "")
                            + f" = {base * mult:.4f}"),
        })
    rows.sort(key=lambda r: (-r["score"], r["key"]))
    ranked = rows[:int(rk["max_ranked"])]
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    candidates = _candidates(rows, cfg["reconcile"])
    by_status = {s: sum(1 for r in ranked if r["status"] == s) for s in STATUSES}
    by_source = {s: sum(1 for m in window if m["source"] == s) for s in SOURCES}
    collection = common.read_json(d / "collection.json") if (d / "collection.json").exists() else None
    manifest = {
        "schema": SCHEMA, "as_of": as_of,
        "methodology": {"id": cfg["methodology"]["id"], "version": cfg["methodology"]["version"],
                        "parameters_sha256": parameters_sha256(cfg), "doc": cfg["methodology"]["doc"]},
        "window_days": int(rk["window_days"]),
        "collection": collection,
        "inputs": {"data/" + str(p.relative_to(common.DATA)): common.sha256_file(p) for p in (
            common.DATASET / "oss_projects.jsonl", common.LIVE / "snapshot.json",
            common.DATA / "canonical" / "oss_projects.jsonl", common.DATA / "discovery" / "candidates.jsonl")
            if p.exists()},
        "counts": {"mentions_held": len(mentions), "mentions_in_window": len(window),
                   "mentions_in_window_by_source": by_source, "repositories_mentioned": len(rows),
                   "ranked": len(ranked), "truncated": len(rows) - len(ranked), "ranked_by_status": by_status,
                   "resolutions_held": len(cache), "candidates": len(candidates)},
    }
    return {"manifest": manifest, "rows": ranked, "candidates": candidates}


def write(result: dict, out: Path | None = None) -> dict:
    """Write radar.jsonl, candidates.jsonl and the manifest into `out` (the radar directory
    by default), copying the inputs alongside when `out` is elsewhere, so a rebuild can be
    compared."""
    out = out or _dir()
    out.mkdir(parents=True, exist_ok=True)
    src = _dir()
    for f in ("collection.json", "mentions.jsonl", "resolved.jsonl"):
        if out != src and (src / f).exists():
            shutil.copyfile(src / f, out / f)
    common.write_jsonl(out / "radar.jsonl", result["rows"])
    common.write_jsonl(out / "candidates.jsonl", result["candidates"])
    manifest = {**result["manifest"], "files": {f: common.sha256_file(out / f) for f in FILES if (out / f).exists()}}
    common.write_json(out / "MANIFEST.json", manifest)
    return manifest


def build(as_of: str | None = None) -> dict:
    """Rebuild data/radar/radar.jsonl from what is held. Default as_of: the last collection's."""
    d = _dir()
    if as_of is None:
        if not (d / "collection.json").exists():
            raise ValueError("no collection held: pass --as-of")
        as_of = common.read_json(d / "collection.json")["as_of"]
    return write(compute(as_of))


# ---------------------------------------------------------------- validation

def validate() -> dict:
    """Committed radar equals a rebuild from what is committed, conforms to its schema,
    was produced under the released methodology, and every entry reconciles."""
    from jsonschema import Draft202012Validator

    checks = []

    def check(cid, ok, detail=None):
        checks.append({"id": cid, "ok": bool(ok), "detail": detail})

    cfg = load_config()
    errs = check_version(cfg)
    check("parameters_match_released_version", not errs, errs or cfg["methodology"]["version"])
    d = _dir()
    if not (d / "MANIFEST.json").exists():
        # Absent is honest only until a cycle has produced it; after that, absence is a loss.
        runs = common.ROOT / "runs"
        produced = sorted(p.name for p in runs.glob("*.json") if (r := common.read_json(p))["status"] == "succeeded"
                          and any(s["id"] == "community_radar" for s in r["strategies"])) if runs.exists() else []
        check("radar_present_once_a_cycle_produced_it", not produced,
              f"{d} missing; produced by {produced[-1]}" if produced else "not collected yet: the next cycle creates it")
        return {"ok": all(c["ok"] for c in checks), "checks": checks}
    manifest = common.read_json(d / "MANIFEST.json")
    rows = common.read_jsonl(d / "radar.jsonl")
    cands = common.read_jsonl(d / "candidates.jsonl") if (d / "candidates.jsonl").exists() else []
    mentions = common.read_jsonl(d / "mentions.jsonl") if (d / "mentions.jsonl").exists() else []
    resolved = common.read_jsonl(d / "resolved.jsonl") if (d / "resolved.jsonl").exists() else []
    check("manifest_version_is_config_version",
          manifest["methodology"]["version"] == cfg["methodology"]["version"]
          and manifest["methodology"]["parameters_sha256"] == parameters_sha256(cfg), manifest["methodology"])
    check("files_hash", all(common.sha256_file(d / f) == h for f, h in manifest["files"].items()), manifest["files"])
    schema = common.read_json(common.SCHEMA / "the-source.radar.schema.json")
    bad = []
    for kind, objs in (("manifest", [manifest]), ("entry", rows), ("candidate", cands), ("mention", mentions),
                       ("resolution", resolved)):
        sub = {**schema, "$ref": f"#/$defs/{kind}"}
        v = Draft202012Validator(sub)
        for o in objs:
            bad += [f"{kind} {o.get('key') or o.get('item_id') or ''}: {e.message}" for e in v.iter_errors(o)][:3]
    check("schema_valid", not bad, bad[:10])
    check("one_mention_per_item", len({(m["source"], m["item_id"]) for m in mentions}) == len(mentions))
    check("one_entry_per_repository", len({r["key"] for r in rows}) == len(rows)
          and len({r["host_repo_id"] for r in rows if r["host_repo_id"]})
          == sum(1 for r in rows if r["host_repo_id"]))
    check("ranks_contiguous_and_capped", [r["rank"] for r in rows] == list(range(1, len(rows) + 1))
          and len(rows) <= int(cfg["ranking"]["max_ranked"])
          and all(a["score"] >= b["score"] for a, b in zip(rows, rows[1:])))
    by_key = {r["key"]: r for r in rows}
    check("candidates_are_new_to_the_source_ranked_and_capped",
          [c["rank"] for c in cands] == list(range(1, len(cands) + 1))
          and len(cands) <= int(cfg["reconcile"]["max_candidates"])
          and all(a["score"] >= b["score"] for a, b in zip(cands, cands[1:]))
          and all(c["status"] in ("candidate", "unresolved") for c in cands)
          and all(c["radar_rank"] == (by_key[c["key"]]["rank"] if c["key"] in by_key else None) for c in cands),
          {"candidates": len(cands)})
    idx = identity_index()
    wrong = [r["key"] for r in rows if
             (r["status"] == "canonical" and r["oss_id"] not in idx["canonical"])
             or (r["status"] == "corpus" and (r["oss_id"] not in set(idx["key_to_oss"].values())
                                              or r["oss_id"] in idx["canonical"]))
             or (r["status"] == "candidate" and r["pool_key"] not in set(idx["pool_keys"].values()))]
    check("statuses_reconcile_with_corpus_and_pool", not wrong, wrong[:10])
    stored = sorted({k for m in mentions for k in m} - set(MENTION_FIELDS))
    check("mentions_hold_no_personal_or_authored_content", not stored, stored)
    if not errs:
        with tempfile.TemporaryDirectory() as tmp:
            again = write(compute(manifest["as_of"], cfg), Path(tmp) / "radar")
            same = all((Path(tmp) / "radar" / f).read_bytes() == (d / f).read_bytes()
                       for f in ("MANIFEST.json", "radar.jsonl", "candidates.jsonl"))
        check("rebuild_is_byte_identical", same, {"as_of": manifest["as_of"], "files": again["files"]})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


# ---------------------------------------------------------------- the daily cycle

def guarded_cycle_step(as_of: datetime, day: str, corpus_light: dict, http=None) -> dict:
    """cycle_step, but a radar that crashes or fails its own validation puts the previous
    data/radar/ and discovery pool back, so it can never block or corrupt a cycle."""
    d, pool = _dir(), common.DATA / "discovery" / "candidates.jsonl"
    with tempfile.TemporaryDirectory() as tmp:
        keep = Path(tmp)
        if d.exists():
            shutil.copytree(d, keep / "radar")
        if pool.exists():
            shutil.copyfile(pool, keep / "candidates.jsonl")
        try:
            out = cycle_step(as_of, day, corpus_light, http)
        except Exception as exc:  # recorded in the run record, never swallowed
            out = {"ok": False, "failed_checks": [f"{type(exc).__name__}: {exc}"[:300]], "ranked": 0,
                   "pool_added": 0, "strategy": {"id": "community_radar", "status": "failed"}}
        if not out["ok"]:
            shutil.rmtree(d, ignore_errors=True)
            if (keep / "radar").exists():
                shutil.copytree(keep / "radar", d)
            if (keep / "candidates.jsonl").exists():
                shutil.copyfile(keep / "candidates.jsonl", pool)
            out["strategy"] = {**out["strategy"], "status": "failed", "failed_checks": out["failed_checks"],
                               "kept_previous": True}
            out["ranked"], out["pool_added"] = 0, 0
    return out


def cycle_step(as_of: datetime, day: str, corpus_light: dict, http=None) -> dict:
    """Collect, merge, resolve, admit new repositories to the discovery pool, rank, validate.

    Runs after the canonical build, inside the cycle's scratch copy. A source outage
    degrades the strategy; a validation failure fails it (see guarded_cycle_step)."""
    from . import daily
    from .enrich import Http

    cfg = load_config()
    if check_version(cfg):
        return {"ok": False, "failed_checks": ["parameters_match_released_version"], "strategy": {
            "id": "community_radar", "status": "failed"}, "ranked": 0, "pool_added": 0}
    http = http or Http()
    d = _dir()
    as_of_s = iso(as_of)
    got = collect(as_of, http, cfg)
    prev = common.read_jsonl(d / "mentions.jsonl") if (d / "mentions.jsonl").exists() else []
    common.write_jsonl(d / "mentions.jsonl", merge_mentions(prev, got["mentions"], as_of, int(cfg["retain_days"]),
                                                            int(cfg["ranking"].get("max_observations", 8))))
    common.write_json(d / "collection.json", {"as_of": got["as_of"], "sources": got["sources"]})

    # resolve the addresses that could rank, strongest first, then admit what is new
    pre = compute(as_of_s, cfg)
    order = [k for r in pre["rows"] if r["status"] == "unresolved" for k in r["link_keys"]]
    cache = {r["key"]: r for r in common.read_jsonl(d / "resolved.jsonl")} if (d / "resolved.jsonl").exists() else {}
    cache, rstats = resolve(order, cache, as_of, http, cfg)
    common.write_jsonl(d / "resolved.jsonl", [cache[k] for k in sorted(cache)])
    mid = compute(as_of_s, cfg)
    rc = cfg["reconcile"]
    admit = []
    for r in mid["rows"]:
        res = next((cache[k] for k in r["link_keys"] if k in cache and cache[k].get("found")), None)
        if (r["status"] == "unresolved" and res and not res["archived"] and not res["fork"]
                and r["score"] >= float(rc["pool_min_score"])):
            admit.append({"key": f"github.com/{res['canonical_name'].lower()}", "provider": "community_radar",
                          "query": "+".join(sorted(r["sources"])), "host_repo_id": res["host_repo_id"],
                          "canonical_name": res["canonical_name"], "stars": res["stars"],
                          "created_at": res["created_at"], "pushed_at": res["pushed_at"],
                          "licence_spdx": res["licence_spdx"], "archived": res["archived"]})
    admit = admit[:int(rc["max_pool_additions"])]
    rec = daily.reconcile_candidates(admit, corpus_light, daily.load_pool(), day) if admit else \
        {"pool": daily.load_pool(), "new": [], "known": [], "refreshed": []}
    if admit:
        daily.write_pool(rec["pool"])
    rstats["pool_added"] = rec["new"]
    common.write_json(d / "collection.json", {"as_of": got["as_of"], "sources": got["sources"], "resolve": rstats})

    manifest = write(compute(as_of_s, cfg))
    report = validate()
    srcs = got["sources"]
    # an unconfigured source asked nothing by declaration: it is not an outage
    status = "failed" if not report["ok"] else \
        "ok" if all(s["status"] in ("ok", "unconfigured") for s in srcs.values()) and not rstats["transient_failures"] \
        else "degraded"
    rows = common.read_jsonl(d / "radar.jsonl")
    cands = common.read_jsonl(d / "candidates.jsonl")
    return {"ok": report["ok"], "failed_checks": [c["id"] for c in report["checks"] if not c["ok"]],
            "ranked": len(rows), "pool_added": len(rec["new"]),
            "strategy": {"id": "community_radar", "status": status,
                         "methodology_version": manifest["methodology"]["version"],
                         "sources": {k: {f: v[f] for f in ("status", "http_calls", "items_read", "gone", "mentions", "errors")}
                                     for k, v in srcs.items()},
                         "resolve": {k: v for k, v in rstats.items() if k != "pool_added"},
                         "pool_added": rec["new"], "counts": manifest["counts"],
                         "top": [{"rank": r["rank"], "key": r["key"], "score": r["score"], "status": r["status"]}
                                 for r in rows[:10]],
                         "candidates": [{"rank": c["rank"], "key": c["key"], "score": c["score"], "status": c["status"]}
                                        for c in cands[:10]]}}


# ---------------------------------------------------------------- retrieval

def retrieve(keys: list[str] | None = None, source: str | None = None, status: str | None = None,
             limit: int | None = None, candidates: bool = False) -> dict:
    """Read the committed radar. With `keys`, those repositories (by radar key, corpus id,
    pool key, current name or any address a mention used; case-insensitive), ranked or not
    on the radar. With `candidates`, the daily list of repositories new to The Source.
    Otherwise the ranking, optionally filtered by source and status. Always returns what
    the last collection could and could not ask, so an empty answer is never mistaken for
    "nobody is talking about it" when a source was down or is not configured."""
    if source and source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    if status and status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    d = _dir()
    manifest = common.read_json(d / "MANIFEST.json")
    rows = common.read_jsonl(d / "radar.jsonl")
    coll = manifest.get("collection") or {}
    out = {"methodology": manifest["methodology"], "as_of": manifest["as_of"], "window_days": manifest["window_days"],
           "sources": {k: {f: v.get(f) for f in ("status", "errors")} for k, v in (coll.get("sources") or {}).items()}}
    if candidates:
        cands = common.read_jsonl(d / "candidates.jsonl") if (d / "candidates.jsonl").exists() else []
        sel = [c for c in cands if not source or source in c["sources"]]
        out["matched"] = len(sel)
        out["candidates"] = sel[:limit] if limit else sel
        return out
    if keys:
        want = {k.lower() for k in keys}

        def names(r):
            return {n.lower() for n in [r["key"], r.get("oss_id"), r.get("pool_key"),
                                        f"github.com/{r['canonical_name']}" if r.get("canonical_name") else None,
                                        *r["link_keys"]] if n}
        hits = [r for r in rows if names(r) & want]
        out["entries"] = hits
        out["unknown_keys"] = sorted(want - set().union(*(names(r) for r in hits))) if hits else sorted(want)
        return out
    sel = [r for r in rows if (not source or source in r["sources"]) and (not status or r["status"] == status)]
    out["matched"] = len(sel)
    out["entries"] = sel[:limit] if limit else sel
    return out
