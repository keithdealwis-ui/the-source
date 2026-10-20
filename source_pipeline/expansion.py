"""Controlled corpus expansion (KEI-923, V2.1 WP6): find candidates at scale, record them, nothing more.

The daily lanes (discovery_search.py) look back a week and add a handful of members a day. Growing the
discovery corpus from ~9.9k toward 25k needs the whole of each relevant population, read once and then
kept current. Every source is declared, with its bound, in config/expansion.yaml:

  github_topics    one search per product-domain topic, sliced by star band past the API's 1,000-result cap
  docker_signals   repositories that carry a compose file, image or chart topic: the self-hostable ones
  artifacthub      Helm charts, operators and container images whose source is a repository root
  awesome_lists    curated lists as awesome.ecosyste.ms parses them (addresses only)
  gitlab, codeberg the independent forges, by topic

`harvest` writes data/expansion/frontier.jsonl (one row per repository address, every route that found it)
and data/expansion/MANIFEST.json (every source's queries, status, request count and the clock). Admission
(corpus_admit.admit) reads only that record, so the same frontier always admits the same rows. Searches
ask the host only for repositories that clear the active quality floor; a separate star-gated query per
GitHub topic finds the archived or long-inactive notable ones for the historical tier.
"""
from __future__ import annotations

import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import common

CONFIG = "expansion.yaml"
OUT = common.ROOT / "data" / "expansion"
SCHEMA = "the-source.expansion/1"
SEARCH_PAUSE_S = 2.1  # GitHub search allows 30 requests a minute with a token
# The list and chart sources are extras. Each call gets one try, and a source stops after
# AUX_BREAK failures in a row, rather than sleeping out long rate-limit backoffs (a stall of an
# hour or more, 2026-10-20). What it skipped is recorded as errors.
AUX_TRIES = 1
AUX_BREAK = 10
AUX_PAUSE_S = 0.5
SEARCH_CAP = 1000     # results GitHub search will page through for one query


def load_cfg() -> dict:
    return common.load_yaml(common.CONFIG / CONFIG)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _repo_root(url: str) -> str | None:
    """`host/owner/repo` (lower case) when `url` is exactly a repository root on a supported forge."""
    p = urllib.parse.urlparse((url or "").strip())
    host = p.netloc.lower().removeprefix("www.")
    parts = [s for s in p.path.split("/") if s]
    if parts and parts[-1].endswith(".git"):
        parts[-1] = parts[-1][:-4]
    if host in ("github.com", "codeberg.org") and len(parts) == 2:
        return f"{host}/{parts[0]}/{parts[1]}".lower()
    if host == "gitlab.com" and len(parts) >= 2 and "-" not in parts:
        return f"{host}/{'/'.join(parts)}".lower()
    return None


class _Search:
    """GitHub repository search, paced, with star-band slicing past the 1,000-result cap."""

    def __init__(self, http):
        self.http = http
        self.requests = 0
        self.errors: list[str] = []

    def page(self, q: str, page: int) -> dict | None:
        time.sleep(SEARCH_PAUSE_S)
        self.requests += 1
        url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
            {"q": q, "sort": "stars", "order": "desc", "per_page": 100, "page": page})
        st, doc = self.http.call(url, auth=True)
        if st != 200 or not isinstance(doc, dict):
            self.errors.append(f"github search http {st}: {q}")
            return None
        return doc

    def all(self, base: str, lo: int, hi: int | None) -> tuple[list[dict], list[str]]:
        """Every result of `base` with stars in [lo, hi], splitting the band until each slice fits."""
        band = f"stars:{lo}..{hi}" if hi is not None else f"stars:>={lo}"
        q = f"{base} {band}"
        first = self.page(q, 1)
        if first is None:
            return [], [q]
        total = int(first.get("total_count") or 0)
        if total > SEARCH_CAP and (hi is None or hi - lo >= 1):
            mid = max(lo + 1, lo * 3 if hi is None else (lo + hi) // 2)
            if hi is not None and mid > hi:
                mid = hi
            a, qa = self.all(base, lo, mid - 1)
            b, qb = self.all(base, mid, hi)
            return a + b, qa + qb
        items = list(first.get("items") or [])
        page = 1
        while len(items) < min(total, SEARCH_CAP) and len(first.get("items") or []) == 100:
            page += 1
            doc = self.page(q, page)
            if doc is None or not doc.get("items"):
                break
            items += doc["items"]
            if len(doc["items"]) < 100:
                break
        return items, [q]


def _gh_row(r: dict) -> dict:
    return {"key": "github.com/" + r["full_name"].lower(), "host_repo_id": r.get("id"),
            "canonical_name": r.get("full_name"), "stars": r.get("stargazers_count"),
            "pushed_at": r.get("pushed_at"), "archived": bool(r.get("archived"))}


def _github_topics(http, src: dict, floor: dict, hist: dict, as_of: datetime, family: str, search: _Search):
    window = (as_of - timedelta(days=int(src.get("pushed_within_days", 365)))).date().isoformat()
    lo = int(floor["min_stars"]["github.com"])
    rows, queries = [], []
    for topic in src["topics"]:
        items, qs = search.all(f"topic:{topic} pushed:>={window} archived:false", lo, None)
        queries += qs
        rows += [{**_gh_row(r), "source": f"{family}:{topic}", "historical_candidate": False} for r in items]
        # Notable projects that no longer clear the active floor: the historical tier.
        items, qs = search.all(f"topic:{topic} pushed:<{window}", int(hist["min_stars"]), None)
        queries += qs
        rows += [{**_gh_row(r), "source": f"{family}:{topic}", "historical_candidate": True} for r in items]
        items, qs = search.all(f"topic:{topic} archived:true pushed:>={window}", int(hist["min_stars"]), None)
        queries += qs
        rows += [{**_gh_row(r), "source": f"{family}:{topic}", "historical_candidate": True} for r in items]
    return rows, queries


def _artifacthub(http, src: dict, family: str) -> tuple[list[dict], list[str], list[str]]:
    rows, queries, errors = [], [], []
    for kind in src["kinds"]:
        ids, offset, size = [], 0, int(src.get("page_size", 60))
        while len(ids) < int(src["max_packages_per_kind"]):
            url = "https://artifacthub.io/api/v1/packages/search?" + urllib.parse.urlencode(
                {"kind": kind, "sort": "stars", "limit": size, "offset": offset})
            queries.append(url)
            st, doc = http.call(url, tries=AUX_TRIES)
            if st != 200 or not isinstance(doc, dict):
                errors.append(f"artifacthub kind {kind} offset {offset}: http {st}")
                break
            pkgs = doc.get("packages") or []
            ids += [p["package_id"] for p in pkgs if p.get("package_id")]
            if len(pkgs) < size:
                break
            offset += size
        failed = 0
        for pid in ids[: int(src["max_packages_per_kind"])]:
            time.sleep(AUX_PAUSE_S)
            st, d = http.call(f"https://artifacthub.io/api/v1/packages/{pid}", tries=AUX_TRIES)
            if st != 200 or not isinstance(d, dict):
                errors.append(f"artifacthub package {pid}: http {st}")
                failed += 1
                if failed >= AUX_BREAK:
                    errors.append(f"artifacthub kind {kind}: stopped after {failed} failures in a row")
                    break
                continue
            failed = 0
            urls = [l.get("url") for l in d.get("links") or []] + [d.get("home_url")]
            keys = sorted({k for k in map(_repo_root, filter(None, urls)) if k})
            for k in keys:
                rows.append({"key": k, "source": f"{family}:artifacthub-kind-{kind}", "host_repo_id": None,
                             "stars": None, "historical_candidate": False})
    return rows, queries, errors


def _awesome(http, src: dict, family: str) -> tuple[list[dict], list[str], list[str]]:
    rows, queries, errors = [], [], []
    for list_url in src["lists"]:
        name = "/".join(urllib.parse.urlparse(list_url).path.strip("/").split("/")[:2]).lower()
        st, meta = http.call("https://awesome.ecosyste.ms/api/v1/lists/lookup?" + urllib.parse.urlencode({"url": list_url}), tries=AUX_TRIES)
        if st != 200 or not isinstance(meta, dict) or not meta.get("projects_url"):
            errors.append(f"awesome {name}: lookup http {st}")
            continue
        queries.append(meta["projects_url"])
        for page in range(1, int(src.get("max_pages", 40)) + 1):
            st, got = http.call(meta["projects_url"] + f"?per_page=100&page={page}", tries=AUX_TRIES)
            if st != 200 or not isinstance(got, list):
                errors.append(f"awesome {name}: page {page} http {st}")
                break
            for r in got:
                k = _repo_root(r.get("url") or "")
                if k:
                    repo = r.get("repository") or {}
                    hid = int(repo["uuid"]) if k.startswith("github.com/") and str(repo.get("uuid") or "").isdigit() else None
                    rows.append({"key": k, "source": f"{family}:{name}", "host_repo_id": hid, "stars": None,
                                 "historical_candidate": False})
            if len(got) < 100:
                break
    return rows, queries, errors


def _forge(http, src: dict, floor: dict, family: str) -> tuple[list[dict], list[str], list[str]]:
    from . import hosts

    host = src["host"]
    rows, queries, errors = [], [], []
    for topic in src["topics"]:
        queries.append(f"{host} topic:{topic}")
        try:
            got = hosts.search(http, host, topic, int(floor["min_stars"][host]), int(src["per_topic"]))
        except Exception as exc:  # one topic failing degrades the source, never the run
            errors.append(f"{host} {topic}: {type(exc).__name__}: {exc}")
            continue
        for c in got:
            rows.append({"key": c["key"], "source": f"{family}:{topic}", "host_repo_id": c.get("host_repo_id"),
                         "canonical_name": c.get("canonical_name"), "stars": c.get("stars"),
                         "pushed_at": c.get("pushed_at"), "archived": bool(c.get("archived")),
                         "historical_candidate": bool(c.get("archived"))})
    return rows, queries, errors


def merge(rows: list[dict]) -> list[dict]:
    """One frontier row per address, carrying every route that found it, sorted by key."""
    by = {}
    for r in rows:
        e = by.setdefault(r["key"], {"key": r["key"], "host": r["key"].split("/", 1)[0], "sources": set(),
                                      "host_repo_id": None, "stars": None, "pushed_at": None,
                                      "archived": None, "historical_candidate": True})
        e["sources"].add(r["source"])
        for f in ("host_repo_id", "pushed_at"):
            e[f] = e[f] or r.get(f)
        if r.get("stars") is not None:
            e["stars"] = max(e["stars"] or 0, r["stars"])
        if r.get("archived") is not None:
            e["archived"] = bool(e["archived"]) or bool(r["archived"])
        # a repository is a historical candidate only if no active-floor route reached it
        e["historical_candidate"] = e["historical_candidate"] and bool(r.get("historical_candidate"))
    out = []
    for k in sorted(by):
        e = by[k]
        e["sources"] = sorted(e["sources"])
        out.append(e)
    return out


def harvest(as_of: datetime | None = None, http=None, out: Path = OUT, only: list[str] | None = None,
            resume: bool = False) -> dict:
    """Run every declared source (network) and record the frontier. Returns the manifest."""
    from .discover import log
    from .enrich import Http

    as_of = as_of or datetime.now(timezone.utc)
    cfg = load_cfg()
    http = http or Http()
    search = _Search(http)
    started = time.time()
    rows, sources = [], {}
    part = out / "sources"
    part.mkdir(parents=True, exist_ok=True)
    done = common.read_json(out / "sources.json") if (out / "sources.json").exists() else {}
    for name, src in cfg["sources"].items():
        if only and name not in only:
            continue
        if resume and (part / f"{name}.jsonl").exists():
            got = common.read_jsonl(part / f"{name}.jsonl")
            rows += got
            sources[name] = {**done.get(name, {"family": src["family"], "status": "ok"}), "resumed": True}
            log(f"expansion: {name} resumed {len(got)} rows from the last harvest")
            continue
        t0, calls0, family = time.time(), http.calls, src["family"]
        errors: list[str] = []
        try:
            if name in ("github_topics", "docker_signals"):
                n_err = len(search.errors)
                got, queries = _github_topics(http, src, cfg["floor"], cfg["historical"], as_of, family, search)
                errors = search.errors[n_err:]
            elif name == "artifacthub":
                got, queries, errors = _artifacthub(http, src, family)
            elif name == "awesome_lists":
                got, queries, errors = _awesome(http, src, family)
            elif name in ("gitlab", "codeberg"):
                got, queries, errors = _forge(http, src, cfg["floor"], family)
            else:
                got, queries, errors = [], [], [f"unknown source {name}"]
        except Exception as exc:  # a source failure is recorded, never fatal
            got, queries, errors = [], [], [f"{name}: {type(exc).__name__}: {exc}"]
        rows += got
        # Each source's rows are kept as soon as it ends, so a later stall cannot cost them;
        # `resume` reuses them instead of asking the host again.
        common.write_jsonl(part / f"{name}.jsonl", got)
        sources[name] = {"family": family, "status": "failed" if errors and not got else "degraded" if errors else "ok",
                         "returned": len(got), "unique": len({r["key"] for r in got}), "queries": len(queries),
                         "requests": http.calls - calls0, "runtime_s": round(time.time() - t0, 1),
                         "errors": errors[:50], "error_count": len(errors)}
        log(f"expansion: {name} {sources[name]['status']} {len(got)} rows, {sources[name]['requests']} requests")
        common.write_json(out / "sources.json", {**done, **sources})
    frontier = merge(rows)
    if only and (out / "frontier.jsonl").exists():  # a partial harvest updates its own sources only
        keep = [r for r in common.read_jsonl(out / "frontier.jsonl")
                if not any(s.split(":", 1)[0] in {cfg["sources"][n]["family"] for n in only} for s in r["sources"])]
        frontier = merge([{**r, "source": s} for r in keep for s in r["sources"]] +
                         [{**r, "source": s} for r in frontier for s in r["sources"]])
        prev = common.read_json(out / "MANIFEST.json").get("sources") or {}
        sources = {**prev, **sources}
    out.mkdir(parents=True, exist_ok=True)
    common.write_jsonl(out / "frontier.jsonl", frontier)
    by_host = {}
    for r in frontier:
        by_host[r["host"]] = by_host.get(r["host"], 0) + 1
    manifest = {
        "schema": SCHEMA, "as_of": _iso(as_of), "config_sha256": common.sha256_file(common.CONFIG / CONFIG),
        "frontier_sha256": common.sha256_file(out / "frontier.jsonl"),
        "counts": {"addresses": len(frontier), "by_host": dict(sorted(by_host.items())),
                   "historical_candidates": sum(1 for r in frontier if r["historical_candidate"])},
        "sources": dict(sorted(sources.items())),
        "cost": {"requests": http.calls, "github_search_requests": search.requests,
                 "runtime_s": round(time.time() - started, 1), "transient_failures": len(http.failures)},
        "note": "Addresses and host-reported light facts only. Nothing here is a member, a score or a recommendation.",
    }
    common.write_json(out / "MANIFEST.json", manifest)
    return manifest


def pool(out: Path = OUT) -> dict:
    """The frontier as an admission pool (corpus_admit.admit's shape), keyed by address."""
    rows = common.read_jsonl(out / "frontier.jsonl") if (out / "frontier.jsonl").exists() else []
    return {r["key"]: {"key": r["key"], "host_repo_id": r.get("host_repo_id"), "aliases": [],
                       "stars": r.get("stars"), "historical_candidate": r.get("historical_candidate", False),
                       "providers": [{"provider": "corpus_expansion", "query": s} for s in r["sources"]]}
            for r in rows}


HISTORICAL = common.ROOT / "data" / "historical"


def admit(as_of: datetime | None = None, max_admit: int | None = None) -> dict:
    """Admit from the recorded frontier under the expansion quotas, then quick-score every new member (S4.4).

    Both or neither: if quick scoring cannot follow the new corpus, the previous corpus and quick layer are put
    back, exactly as the daily cycle step does (cycle.py)."""
    import shutil
    import tempfile

    from . import corpus, corpus_admit, quick_score

    as_of = (as_of or datetime.now(timezone.utc)).replace(microsecond=0)
    cfg = load_cfg()
    if max_admit:
        cfg["quotas"] = {**cfg["quotas"], "max_admit_per_run": int(max_admit)}
    with tempfile.TemporaryDirectory() as tmp:
        saved = {}
        for name, d in (("corpus", corpus.OUT), ("quick", quick_score.OUT)):
            if d.exists():
                saved[name] = Path(tmp) / name
                shutil.copytree(d, saved[name])
        t0 = time.time()
        try:
            res = corpus_admit.admit(pool(), as_of, expansion=cfg, historical_out=HISTORICAL)
            res["runtime_s"] = round(time.time() - t0, 1)
            if res["corpus_version_after"] != res["corpus_version_before"] and quick_score.OUT.exists():
                t1 = time.time()
                q = quick_score.run(as_of)
                check = quick_score.validate()
                if check.get("failed_checks"):
                    raise RuntimeError(f"quick scores do not validate: {check['failed_checks'][:3]}")
                res["quick_score"] = {"runtime_s": round(time.time() - t1, 1), "counts": q.get("counts"),
                                      "coverage": q.get("coverage"),
                                      "last_run": {k: v for k, v in (q.get("last_run") or {}).items()
                                                   if k != "failures"}}
        except Exception:
            for name, d in (("corpus", corpus.OUT), ("quick", quick_score.OUT)):
                if name in saved:
                    shutil.rmtree(d, ignore_errors=True)
                    shutil.copytree(saved[name], d)
            raise
    # The run's own report is the lineage between the frontier and the corpus: what each route admitted, what
    # was refused and why, and the caps it ran under.
    if res.get("corpus_added"):
        common.write_json(OUT / "runs" / (res["membership_as_of"].replace(":", "") + ".json"), res)
    return res


def main(argv: list[str]) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="source_pipeline")
    ap.add_argument("command", choices=["corpus-expand-harvest", "corpus-expand-admit"])
    ap.add_argument("--only", action="append", help="corpus-expand-harvest: run only this source (repeatable)")
    ap.add_argument("--resume", action="store_true", help="corpus-expand-harvest: reuse sources the last harvest finished")
    ap.add_argument("--max-admit", type=int, help="corpus-expand-admit: admit at most this many in this run")
    a = ap.parse_args(argv)
    if a.command == "corpus-expand-harvest":
        m = harvest(only=a.only, resume=a.resume)
        print(json.dumps({k: m[k] for k in ("as_of", "counts", "cost")}, indent=2))
        return 0
    res = admit(max_admit=a.max_admit)
    print(json.dumps(res, indent=2))
    return 0
