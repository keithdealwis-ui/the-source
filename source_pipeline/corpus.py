"""Public discovery corpus (KEI-912): export, refresh and validate data/corpus/.

    corpus-export     build data/corpus/ from the Discover lane corpus (data/discover/corpus/,
                      internal, KEI-811) under the publication rules in config/corpus.yaml; no network
    corpus-refresh    re-read every published project's live facts from the host, in GraphQL batches,
                      and promote the result only if it validates (network; runs weekly in CI)
    corpus-validate   check data/corpus/ against its MANIFEST and the publication rules
    corpus-due        is a refresh due (config/corpus.yaml refresh.due_after_days)?

The export carries facts only: identity, host-reported metadata, licence and maintenance status as
the Discover lane evaluated them, and where each project was found (list + pinned commit + line).
It carries no tier: the MCP server derives `tier` at load time from the canonical layer it is
serving beside, so a project's tier can never disagree with the recommendations being served.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import common

SCHEMA = "the-source.corpus/1"
CORPUS_SRC = common.DATA / "discover" / "corpus"
OUT = common.DATA / "corpus"
FILES = ("live.jsonl", "projects.jsonl")
ATTRIBUTION = "The Source (github.com/keithdealwis-ui/the-source), by Keith de Alwis"
# Never in a published route: the private seed corpus or an internal lane's paths.
FORBIDDEN_IN_PROVENANCE = ("discover_seeds", "config/discover", "data/discover", "keith_seed")
PROJECT_KEYS = {"id", "host_repo_id", "name", "full_name", "url", "aliases", "description", "homepage", "topics",
                "languages", "licence", "maintenance", "lanes", "provenance"}
LIVE_KEYS = {"id", "found", "full_name", "stars", "forks", "pushed_at", "archived", "latest_release",
             "licence_spdx", "observed_as_of", "unresolved"}


class CorpusError(Exception):
    pass


def load_cfg() -> dict:
    return common.load_yaml(common.CONFIG / "corpus.yaml")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _strip(prefix: str, value: str) -> str:
    return value[len(prefix):] if value.startswith(prefix) else value


def _text(value, limit: int):
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    if not value:
        return None
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def _provenance(routes: list, pub: dict) -> list:
    out, seen = [], set()
    for r in routes:
        strategy = r.get("strategy")
        if strategy in pub["private_seed_strategies"]:
            row = {"strategy": pub["private_seed_label"], "source": None, "commit": None, "locator": None,
                   "depth": r.get("depth"), "via": None}
        else:
            source, commit = r.get("source"), None
            if isinstance(source, str) and source.startswith("list:") and "@" in source:
                source, commit = source.split("@", 1)
            row = {"strategy": strategy, "source": source, "commit": commit, "locator": r.get("locator"),
                   "depth": r.get("depth"), "via": r.get("via")}
        key = common.dumps(row)
        if key not in seen:
            seen.add(key)
            out.append(row)
    out.sort(key=lambda x: (x["depth"] if x["depth"] is not None else 9, x["strategy"] or "", x["source"] or "",
                            x["locator"] or ""))
    return out


def public_record(rec: dict, cfg: dict) -> tuple[dict, dict]:
    """One corpus record -> (project row, live row) under the publication rules."""
    pub = cfg["publication"]
    live = rec.get("live") or {}
    desc = rec.get("descriptive") or {}
    ent = rec.get("entities") or {}
    lic = (rec.get("quality") or {}).get("licence") or {}
    maint = (rec.get("quality") or {}).get("maintenance") or {}
    hist = ((rec.get("freshness") or {}).get("history") or [{}])[-1]
    rid = rec["canonical"].lower()
    full = live.get("canonical_name") or rid.split("/", 1)[1]
    project = {
        "id": rid,
        "host_repo_id": live.get("host_repo_id"),
        "name": full.split("/")[-1],
        "full_name": full,
        "url": f"https://{rid.split('/', 1)[0]}/{full}",
        "aliases": sorted({a.lower() for a in rec.get("aliases") or []} - {rid}),
        "description": _text(desc.get("description"), pub["description_max_chars"])
        if pub["include_descriptions"] else None,
        "homepage": desc.get("homepage") or None,
        "topics": [_strip("github-topic:", t) for t in ent.get("topics") or []][:pub["max_topics"]],
        "languages": [_strip("language:", t) for t in ent.get("languages") or []][:pub["max_languages"]],
        "licence": {"spdx": lic.get("spdx"), "status": lic.get("status"), "issue": _licence_issue(rec, lic)},
        "maintenance": {"status": maint.get("status"),
                        "last_meaningful_activity_at": maint.get("last_meaningful_activity_at")},
        "lanes": sorted(k for k, v in (rec.get("lanes") or {}).items() if v),
        "provenance": _provenance(rec.get("provenance") or [], pub),
    }
    rel = hist.get("latest_release") or live.get("latest_release")
    live_row = {
        "id": rid, "found": bool(live.get("found", True)), "full_name": full,
        "stars": live.get("stars"), "forks": live.get("forks"), "pushed_at": live.get("pushed_at"),
        "archived": bool(live.get("archived")),
        "latest_release": _release(rel),
        "licence_spdx": live.get("licence_spdx"),
        "observed_as_of": (rec.get("freshness") or {}).get("observed_as_of"),
        "unresolved": None,
    }
    return project, live_row


def _licence_issue(rec: dict, lic: dict):
    if lic.get("status") == "recognised":
        return None
    issue = lic.get("issue")
    if isinstance(issue, dict) and issue.get("reason"):
        return {"category": issue.get("category"), "reason": issue["reason"]}
    reasons = [r for r in (rec.get("quality") or {}).get("exclusion_reasons") or [] if "licence" in r]
    return {"category": "unrecognised",
            "reason": reasons[0] if reasons else
            f"licence {lic.get('spdx') or 'unknown'} is not a recognised open-source licence"}


def _release(rel):
    if not rel:
        return None
    return {"tag": rel.get("tag") or rel.get("tagName"),
            "published_at": rel.get("published_at") or rel.get("publishedAt")}


def _counts(projects: list, live: list) -> dict:
    lanes = {}
    for p in projects:
        for lane in p["lanes"]:
            lanes[lane] = lanes.get(lane, 0) + 1
    return {"projects": len(projects),
            "licence_issue": sum(1 for p in projects if p["licence"]["status"] != "recognised"),
            "maintainer_seeded": sum(1 for p in projects
                                     if any(r["strategy"] == "maintainer_seed" for r in p["provenance"])),
            "by_lane": dict(sorted(lanes.items())),
            "host_not_found": sum(1 for r in live if not r["found"]),
            "archived": sum(1 for r in live if r["archived"]),
            "with_description": sum(1 for p in projects if p["description"])}


def write_layer(out: Path, projects: list, live: list, manifest_extra: dict) -> dict:
    projects = sorted(projects, key=lambda p: p["id"])
    live = sorted(live, key=lambda r: r["id"])
    tmp = out.with_name(out.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    common.write_jsonl(tmp / "projects.jsonl", projects)
    common.write_jsonl(tmp / "live.jsonl", live)
    files = {name: common.sha256_file(tmp / name) for name in FILES}
    observed = sorted(r["observed_as_of"] for r in live if r["observed_as_of"])
    manifest = {
        "schema": SCHEMA,
        "as_of": observed[-1] if observed else None,
        "files": files,
        "corpus_version": hashlib.sha256(common.dumps(files).encode()).hexdigest()[:16],
        "counts": _counts(projects, live),
        "live_observed_as_of": {"oldest": observed[0] if observed else None,
                                "newest": observed[-1] if observed else None},
        "licence": "CC BY 4.0",
        "attribution": ATTRIBUTION,
        **manifest_extra,
    }
    common.write_json(tmp / "MANIFEST.json", manifest)
    report = validate(tmp)
    if report["failed_checks"]:
        raise CorpusError(f"refusing to promote data/corpus: {report['failed_checks']}")
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    return manifest


def export(src: Path = CORPUS_SRC, out: Path = OUT) -> dict:
    cfg = load_cfg()
    pub = cfg["publication"]
    try:
        src_manifest_bytes = (src / "MANIFEST.json").read_bytes()
    except OSError as exc:
        raise CorpusError(f"{src}/MANIFEST.json unreadable: {exc}") from exc
    src_manifest = json.loads(src_manifest_bytes)
    for name, digest in (src_manifest.get("sha256") or {}).items():
        if name == "projects.jsonl" and common.sha256_file(src / name) != digest:
            raise CorpusError(f"{src}/{name} does not match its MANIFEST sha256")
    projects, live, skipped = [], [], {}
    for rec in common.read_jsonl(src / "projects.jsonl"):
        if rec.get("outcome") not in pub["outcomes"]:
            skipped[rec.get("outcome")] = skipped.get(rec.get("outcome"), 0) + 1
            continue
        p, l = public_record(rec, cfg)
        projects.append(p)
        live.append(l)
    extra = {
        "membership": {"as_of": src_manifest.get("as_of"), "maintenance_evaluated_as_of": "per the Discover lane "
                       "build at membership.as_of (docs/DISCOVER.md); live facts are refreshed, evaluations are not", "source_schema": src_manifest.get("schema"),
                       "source_manifest_sha256": hashlib.sha256(src_manifest_bytes).hexdigest(),
                       "outcomes_published": pub["outcomes"], "skipped": skipped,
                       "rebuilt_by": "corpus-export from the Discover lane corpus (KEI-811)"},
        "publication": {"rules": "config/corpus.yaml publication", "gate": pub.get("gate"),
                        "descriptions": "host-reported, truncated" if pub["include_descriptions"] else "omitted",
                        "private_seed_routes": f"published as {pub['private_seed_label']!r} without file or line"},
        "refresh": {"cadence": cfg["refresh"]["cadence"], "stale_after_days": cfg["refresh"]["stale_after_days"],
                    "last": None},
    }
    return write_layer(out, projects, live, extra)


# -- refresh


def due(now: datetime | None = None, out: Path = OUT) -> dict:
    now = now or datetime.now(timezone.utc)
    cfg = load_cfg()["refresh"]
    try:
        m = common.read_json(out / "MANIFEST.json")
    except (OSError, ValueError):
        return {"due": False, "reason": "no data/corpus layer to refresh; run corpus-export first"}
    last = (m.get("refresh") or {}).get("last") or {}
    at = last.get("at") or m.get("as_of")
    if not at:
        return {"due": True, "reason": "never refreshed"}
    age = (now - _parse(at)).total_seconds() / 86400
    return {"due": age >= cfg["due_after_days"], "last_refresh": at, "age_days": round(age, 2),
            "due_after_days": cfg["due_after_days"]}


def refresh(now: datetime | None = None, out: Path = OUT, api=None) -> dict:
    from .discover import Api, log

    now = now or datetime.now(timezone.utc)
    cfg = load_cfg()["refresh"]
    m = common.read_json(out / "MANIFEST.json")
    pre = validate(out)
    if pre["failed_checks"]:
        raise CorpusError(f"data/corpus does not validate before refresh: {pre['failed_checks']}")
    projects = {p["id"]: p for p in common.read_jsonl(out / "projects.jsonl")}
    live = {r["id"]: r for r in common.read_jsonl(out / "live.jsonl")}
    slugs = {pid: (live[pid].get("full_name") or projects[pid]["full_name"]).lower()
             for pid in projects if pid.startswith("github.com/")}
    batches = -(-len(slugs) // cfg["batch_size"])
    if batches > cfg["max_graphql_points"]:
        raise CorpusError(f"{batches} batches would exceed max_graphql_points {cfg['max_graphql_points']}")
    api = api or Api()
    log(f"corpus-refresh: {len(slugs)} repositories in {batches} GraphQL batches")
    meta = api.meta_batch(sorted(set(slugs.values())), size=cfg["batch_size"])
    stamp = _iso(now)
    stats = {"resolved": 0, "not_found": 0, "unresolved": 0, "renamed": 0, "changed_metadata": 0,
             "not_on_github": len(projects) - len(slugs)}
    pub = load_cfg()["publication"]
    for pid, slug in slugs.items():
        got = meta.get(slug) or {"found": False, "error": "missing_from_response"}
        row = dict(live[pid])
        if got.get("found"):
            stats["resolved"] += 1
            row.update(found=True, full_name=got["name_with_owner"], stars=got["stars"], forks=got["forks"],
                       pushed_at=got["pushed_at"], archived=got["archived"],
                       latest_release=_release(got.get("latest_release")), licence_spdx=got["licence_spdx"],
                       observed_as_of=stamp, unresolved=None)
            p = dict(projects[pid])
            if got["canonical_slug"] != slug:
                stats["renamed"] += 1
            new_key = f"github.com/{got['canonical_slug']}"
            aliases = sorted(set(p["aliases"]) | ({new_key} - {pid}))
            upd = {"aliases": aliases, "full_name": got["name_with_owner"],
                   "name": got["name_with_owner"].split("/")[-1],
                   "url": f"https://github.com/{got['name_with_owner']}",
                   "description": _text(got.get("description"), pub["description_max_chars"])
                   if pub["include_descriptions"] else None,
                   "homepage": got.get("homepage") or None,
                   "topics": list(got.get("topics") or [])[:pub["max_topics"]],
                   "languages": list(got.get("languages") or [])[:pub["max_languages"]]}
            if any(p.get(k) != v for k, v in upd.items()):
                stats["changed_metadata"] += 1
                p.update(upd)
                projects[pid] = p
        elif got.get("error") == "not_found":
            stats["not_found"] += 1
            row.update(found=False, observed_as_of=stamp, unresolved="not_found")
        else:
            stats["unresolved"] += 1
            row["unresolved"] = got.get("error") or "unknown"
        live[pid] = row
    share = stats["unresolved"] / max(len(slugs), 1)
    if share > cfg["max_unresolved_share"]:
        raise CorpusError(f"{stats['unresolved']} of {len(slugs)} repositories unresolved ({share:.1%}); "
                          f"the previous layer is kept")
    if api.http.failures:
        log(f"corpus-refresh: {len(api.http.failures)} transient failures were retried")
    extra = {k: m[k] for k in ("membership", "publication") if k in m}
    extra["refresh"] = {"cadence": cfg["cadence"], "stale_after_days": cfg["stale_after_days"],
                        "last": {"at": stamp, "method": f"GitHub GraphQL, {cfg['batch_size']} repositories per "
                                                        "request; REST only to follow a rename",
                                 "requests": api.http.calls, "batches": batches, **stats}}
    manifest = write_layer(out, list(projects.values()), list(live.values()), extra)
    return {"refresh": extra["refresh"]["last"], "corpus_version": manifest["corpus_version"]}


# -- validate


def validate(root: Path = OUT) -> dict:
    failed, checks = [], 0

    def check(ok: bool, what: str):
        nonlocal checks
        checks += 1
        if not ok:
            failed.append(what)
        return ok

    try:
        m = common.read_json(root / "MANIFEST.json")
    except (OSError, ValueError) as exc:
        return {"checks": 1, "failed_checks": [f"MANIFEST.json unreadable: {exc}"]}
    check(m.get("schema") == SCHEMA, f"schema is {m.get('schema')!r}")
    files = m.get("files") or {}
    check(sorted(files) == sorted(FILES), f"MANIFEST files {sorted(files)}")
    for name in FILES:
        path = root / name
        if not check(path.is_file() and common.sha256_file(path) == files.get(name), f"{name} sha256 mismatch"):
            return {"checks": checks, "failed_checks": failed}
    check(m.get("corpus_version") == hashlib.sha256(common.dumps(files).encode()).hexdigest()[:16],
          "corpus_version is not the hash of the files")
    projects = common.read_jsonl(root / "projects.jsonl")
    live = common.read_jsonl(root / "live.jsonl")
    ids = [p.get("id") for p in projects]
    check(ids == sorted(ids) and len(set(ids)) == len(ids), "project ids not unique and sorted")
    check([r.get("id") for r in live] == ids, "live.jsonl ids differ from projects.jsonl")
    keys = {}
    for p in projects:
        if not check(set(p) == PROJECT_KEYS, f"{p.get('id')}: fields {sorted(set(p) ^ PROJECT_KEYS)}"):
            continue
        for k in [p["id"], *p["aliases"]]:
            check(keys.setdefault(k, p["id"]) == p["id"], f"{k} names two projects")
        check(p["licence"]["status"] in ("recognised", "issue"), f"{p['id']}: licence status")
        issue = p["licence"]["issue"]
        check((p["licence"]["status"] == "recognised") == (issue is None), f"{p['id']}: licence issue mismatch")
        check(issue is None or (isinstance(issue, dict) and bool(issue.get("reason"))), f"{p['id']}: issue unnamed")
        check(bool(p["provenance"]), f"{p['id']}: no provenance")
        for r in p["provenance"]:
            if r["strategy"] == "curated_list":
                check(bool(r["commit"]) and bool(r["locator"]), f"{p['id']}: curated route without commit/line")
        routes = common.dumps(p["provenance"]).lower()
        for word in FORBIDDEN_IN_PROVENANCE:
            check(word not in routes, f"{p['id']}: provenance carries {word!r}")
        check("discover_seeds" not in common.dumps(p), f"{p['id']}: names the private seed file")
    for r in live:
        check(set(r) == LIVE_KEYS, f"{r.get('id')}: live fields {sorted(set(r) ^ LIVE_KEYS)}")
    counts = _counts(projects, live) if not failed else {}
    if counts:
        check(m.get("counts") == counts, f"MANIFEST counts {m.get('counts')} != {counts}")
    return {"checks": checks, "failed_checks": failed[:50], "failed_total": len(failed),
            "projects": len(projects), "corpus_version": m.get("corpus_version")}


def main(command: str) -> int:
    try:
        if command == "corpus-export":
            out = export()
            print(json.dumps({"corpus_version": out["corpus_version"], "counts": out["counts"]}, indent=2))
        elif command == "corpus-refresh":
            print(json.dumps(refresh(), indent=2))
        elif command == "corpus-due":
            print(json.dumps(due()))
        else:
            report = validate()
            print(json.dumps(report, indent=2))
            return 1 if report["failed_checks"] else 0
    except CorpusError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    return 0
