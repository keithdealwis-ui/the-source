"""Community Radar (KEI-850): repositories being discussed right now. Methodology: docs/RADAR.md.

Two lawful public sources, each through the interface its operator provides for it:

  hn      Hacker News, official API (hacker-news.firebaseio.com/v0): the show, best and top
          story lists, then each story. A story counts if it links a repository.
  forem   Forem's public v1 API on dev.to: the most popular articles per tag over the last
          few days, then each article's body, read only to find repository links.

Nothing a person wrote is stored: no titles, no text, no usernames. A mention keeps the
item id, its own address, its public points and comment counts, when it was published,
and the repository addresses it links (data/radar/mentions.jsonl).

From those mentions the radar ranks repositories, deterministically and with caps:

  extract     repository links normalised to host/owner/name, at most max_repos_per_item
              per item, each item's weight shared between them; site pages are not owners.
  dedupe      one mention per (source, item id), the newest observation winning; one
              entry per repository, however many addresses (old names, case, .git, deep
              links) its mentions used.
  rank        per mention, engagement = ln(1 + points) and ln(1 + comments), weighted per
              source, halved every half_life_days; the strongest N mentions summed, with a
              bonus when both sources mention it; ties broken by key; at most max_ranked.
  reconcile   each entry is matched to the corpus by key, current name and host repository
              id (GitHub links are resolved against GitHub, capped and cached in
              data/radar/resolved.jsonl), then labelled canonical / corpus / candidate /
              not_found / unresolved. A new, existing, unarchived, non-fork repository above
              a score floor joins the discovery pool; it never becomes canonical here.

    data/radar/collection.json   what the last collection asked, got and failed at
    data/radar/mentions.jsonl    the mentions (facts and addresses only), retain_days
    data/radar/resolved.jsonl    GitHub's answer for each resolved address
    data/radar/radar.jsonl       the ranked entries
    data/radar/MANIFEST.json     methodology version and fingerprint, inputs, counts, hashes

Collection needs the network; everything after it is a pure function of the files above,
the corpus files and config/radar.yaml, so `radar-validate` rebuilds it byte for byte.
It never touches data/canonical/ or api/v1/.
"""
from __future__ import annotations

import hashlib
import html
import math
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
SOURCES = ("hn", "forem")
STATUSES = ("canonical", "corpus", "candidate", "not_found", "unresolved")
FILES = ("collection.json", "mentions.jsonl", "resolved.jsonl", "radar.jsonl")
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


COLLECTORS = {"hn_firebase": _hn, "forem": _forem}


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


def merge_mentions(previous: list[dict], new: list[dict], as_of: datetime, retain_days: int) -> list[dict]:
    """One mention per (source, item id); the newest observation wins. Mentions published
    more than retain_days before as_of are dropped."""
    by = {}
    for m in list(previous) + list(new):
        k = (m["source"], m["item_id"])
        if k not in by or m["observed_at"] >= by[k]["observed_at"]:
            by[k] = m
    keep = [m for m in by.values() if (_age_days(m["published_at"], as_of) or 0) <= retain_days]
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

def contribution(m: dict, as_of: datetime, rk: dict) -> tuple[float, float, float]:
    """(contribution, age in days, engagement) of one mention, before sharing between repos."""
    age = _age_days(m["published_at"], as_of) or 0.0
    eng = rk["points_weight"] * math.log1p(m["points"]) + rk["comments_weight"] * math.log1p(m["comments"])
    w = float(rk["source_weight"][m["source"]]) * eng * 0.5 ** (age / float(rk["half_life_days"]))
    return w / len(m["repos"]), age, eng


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
    rows = []
    for key, g in groups.items():
        ms = sorted(g["mentions"].values(), key=lambda x: (-x["c"], x["m"]["source"], x["m"]["item_id"]))
        top = ms[:int(rk["max_mentions_per_repo"])]
        base = sum(x["c"] for x in top)
        srcs = sorted({x["m"]["source"] for x in ms})
        mult = 1 + float(rk["second_source_bonus"]) * (len(srcs) - 1)
        per = {s: {"mentions": sum(1 for x in ms if x["m"]["source"] == s),
                   "points": sum(x["m"]["points"] for x in ms if x["m"]["source"] == s),
                   "comments": sum(x["m"]["comments"] for x in ms if x["m"]["source"] == s)} for s in srcs}
        ident = g["ident"]
        res = next((cache[ln] for ln in sorted(g["links"]) if ln in cache and cache[ln].get("found")), None)
        rows.append({
            "key": key, "score": _r(base * mult), "status": ident["status"], "status_reason": ident["reason"],
            "oss_id": ident.get("oss_id"), "pool_key": ident.get("pool_key"),
            "host_repo_id": ident.get("host_repo_id"),
            "canonical_name": res.get("canonical_name") if res else None,
            "link_keys": sorted(g["links"]), "sources": per,
            "first_mentioned_at": min(x["m"]["published_at"] for x in ms),
            "last_mentioned_at": max(x["m"]["published_at"] for x in ms),
            "mentions": [{"source": x["m"]["source"], "item_id": x["m"]["item_id"], "permalink": x["m"]["permalink"],
                          "published_at": x["m"]["published_at"], "points": x["m"]["points"],
                          "comments": x["m"]["comments"], "shared_with": len(x["m"]["repos"]) - 1,
                          "age_days": _r(x["age"], 2), "contribution": _r(x["c"]), "counted": i < len(top)}
                         for i, x in enumerate(ms)],
            "explanation": (f"{len(ms)} mention(s) ({', '.join(f'{s} {per[s]['mentions']}' for s in srcs)}); "
                            f"strongest {len(top)} sum {base:.4f}"
                            + (f" x{mult:g} ({len(srcs)} sources)" if mult != 1 else "")
                            + f" = {base * mult:.4f}"),
        })
    rows.sort(key=lambda r: (-r["score"], r["key"]))
    ranked = rows[:int(rk["max_ranked"])]
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
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
                   "resolutions_held": len(cache)},
    }
    return {"manifest": manifest, "rows": ranked}


def write(result: dict, out: Path | None = None) -> dict:
    """Write radar.jsonl and the manifest into `out` (the radar directory by default),
    copying the inputs alongside when `out` is elsewhere, so a rebuild can be compared."""
    out = out or _dir()
    out.mkdir(parents=True, exist_ok=True)
    src = _dir()
    for f in ("collection.json", "mentions.jsonl", "resolved.jsonl"):
        if out != src and (src / f).exists():
            shutil.copyfile(src / f, out / f)
    common.write_jsonl(out / "radar.jsonl", result["rows"])
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
    mentions = common.read_jsonl(d / "mentions.jsonl") if (d / "mentions.jsonl").exists() else []
    resolved = common.read_jsonl(d / "resolved.jsonl") if (d / "resolved.jsonl").exists() else []
    check("manifest_version_is_config_version",
          manifest["methodology"]["version"] == cfg["methodology"]["version"]
          and manifest["methodology"]["parameters_sha256"] == parameters_sha256(cfg), manifest["methodology"])
    check("files_hash", all(common.sha256_file(d / f) == h for f, h in manifest["files"].items()), manifest["files"])
    schema = common.read_json(common.SCHEMA / "the-source.radar.schema.json")
    bad = []
    for kind, objs in (("manifest", [manifest]), ("entry", rows), ("mention", mentions), ("resolution", resolved)):
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
    idx = identity_index()
    wrong = [r["key"] for r in rows if
             (r["status"] == "canonical" and r["oss_id"] not in idx["canonical"])
             or (r["status"] == "corpus" and (r["oss_id"] not in set(idx["key_to_oss"].values())
                                              or r["oss_id"] in idx["canonical"]))
             or (r["status"] == "candidate" and r["pool_key"] not in set(idx["pool_keys"].values()))]
    check("statuses_reconcile_with_corpus_and_pool", not wrong, wrong[:10])
    stored = sorted({k for m in mentions for k in m} - {"source", "item_id", "permalink", "published_at",
                                                       "observed_at", "points", "comments", "via", "repos"})
    check("mentions_hold_no_personal_or_authored_content", not stored, stored)
    if not errs:
        with tempfile.TemporaryDirectory() as tmp:
            again = write(compute(manifest["as_of"], cfg), Path(tmp) / "radar")
            same = all((Path(tmp) / "radar" / f).read_bytes() == (d / f).read_bytes()
                       for f in ("MANIFEST.json", "radar.jsonl"))
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
    common.write_jsonl(d / "mentions.jsonl", merge_mentions(prev, got["mentions"], as_of, int(cfg["retain_days"])))
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
    status = "failed" if not report["ok"] else \
        "ok" if all(s["status"] == "ok" for s in srcs.values()) and not rstats["transient_failures"] else "degraded"
    rows = common.read_jsonl(d / "radar.jsonl")
    return {"ok": report["ok"], "failed_checks": [c["id"] for c in report["checks"] if not c["ok"]],
            "ranked": len(rows), "pool_added": len(rec["new"]),
            "strategy": {"id": "community_radar", "status": status,
                         "methodology_version": manifest["methodology"]["version"],
                         "sources": {k: {f: v[f] for f in ("status", "http_calls", "items_read", "gone", "mentions", "errors")}
                                     for k, v in srcs.items()},
                         "resolve": {k: v for k, v in rstats.items() if k != "pool_added"},
                         "pool_added": rec["new"], "counts": manifest["counts"],
                         "top": [{"rank": r["rank"], "key": r["key"], "score": r["score"], "status": r["status"]}
                                 for r in rows[:10]]}}


# ---------------------------------------------------------------- retrieval

def retrieve(keys: list[str] | None = None, source: str | None = None, status: str | None = None,
             limit: int | None = None) -> dict:
    """Read the committed radar. With `keys`, those repositories (by radar key, corpus id,
    pool key, current name or any address a mention used; case-insensitive), ranked or not
    on the radar. Otherwise the ranking, optionally filtered by source and status. Always
    returns what the last collection could and could not ask, so an empty answer is never
    mistaken for "nobody is talking about it" when a source was down."""
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
