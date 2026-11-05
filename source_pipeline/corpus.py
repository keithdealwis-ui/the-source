"""Public discovery corpus (KEI-912): export, refresh and validate data/corpus/.

    corpus-export     build data/corpus/ from the Discover lane corpus (data/discover/corpus/,
                      internal, KEI-811) under the publication rules in config/corpus.yaml; no network
    corpus-refresh    re-read every published project's live facts from the host, in GraphQL batches,
                      and promote the result only if it validates (network; runs weekly in CI)
    corpus-validate   check data/corpus/ against its MANIFEST and the publication rules
    corpus-due        is a refresh due (config/corpus.yaml refresh.due_after_days)?
    corpus-identity   re-resolve renames, forks, mirrors and sibling repositories (source_pipeline/identity.py);
                      with --refresh, first re-read GitHub's identity facts in bounded GraphQL batches (network)

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
                "languages", "licence", "maintenance", "lanes", "provenance", "identity"}
LICENCE_KEYS = {"spdx", "status", "issue"}
# Added by licence-classify (KEI-919) to flagged rows; optional so an export of new rows still validates.
LICENCE_CLASSIFIED_KEYS = LICENCE_KEYS | {"classification", "eligibility"}
# Added by licence-edition-check (edition_check.py) to open_core rows; listed_alternative_to only where the
# KEI-805 dataset lists the project as replacing a product.
LICENCE_EDITION_KEYS = ("edition_check", "listed_alternative_to")
EDITION_RESULTS = ("pass", "fail", "ambiguous")
LICENCE_CLASSES = ("open", "open_core", "source_available", "restricted", "missing", "unclear")
ELIGIBILITY_BY_CLASS = {"open": "eligible", "open_core": "conditional"}
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
    from . import identity

    lanes = {}
    for p in projects:
        for lane in p["lanes"]:
            lanes[lane] = lanes.get(lane, 0) + 1
    classified = _tally(p["licence"]["classification"]["class"] for p in projects
                        if p["licence"].get("classification"))
    eligible = _tally(p["licence"]["eligibility"]["status"] for p in projects if p["licence"].get("eligibility"))
    extra = {"licence_classification": classified, "licence_eligibility": eligible} if classified else {}
    return {"projects": len(projects),
            "licence_issue": sum(1 for p in projects if p["licence"]["status"] != "recognised"),
            "maintainer_seeded": sum(1 for p in projects
                                     if any(r["strategy"] == "maintainer_seed" for r in p["provenance"])),
            "by_lane": dict(sorted(lanes.items())),
            "host_not_found": sum(1 for r in live if not r["found"]),
            "archived": sum(1 for r in live if r["archived"]),
            "with_description": sum(1 for p in projects if p["description"]),
            "identity": identity.counts(projects), **extra}


def _tally(values) -> dict:
    out = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def write_layer(out: Path, projects: list, live: list, manifest_extra: dict) -> dict:
    from . import identity

    facts_path = identity.data_dir(out) / "identity" / identity.FACTS_NAME
    facts = identity.load_facts(facts_path)
    canonical = identity.load_canonical(identity.data_dir(out) / "canonical" / "oss_projects.jsonl")
    projects, live, _ = identity.resolve(projects, live, facts, canonical)
    observed = sorted({f["observed_at"] for f in facts.values() if f.get("observed_at")})
    manifest_extra = {**manifest_extra, "identity": {
        "method": "source_pipeline/identity.py: renames, forks, mirrors, sibling repositories (docs/CORPUS.md)",
        "facts": {"path": "data/identity/" + identity.FACTS_NAME,
                  "sha256": common.sha256_file(facts_path) if facts_path.is_file() else None,
                  "repositories": len(facts), "observed_as_of": observed[-1] if observed else None}}}
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
    # Licence classifications (KEI-919) are made by licence-classify against this layer, not by the
    # Discover lane; keep them across a re-export while the row's licence evaluation is unchanged.
    carried = {}
    if (out / "projects.jsonl").is_file():
        for p in common.read_jsonl(out / "projects.jsonl"):
            if p["licence"].get("classification"):
                carried[p["id"]] = p["licence"]
    for rec in common.read_jsonl(src / "projects.jsonl"):
        if rec.get("outcome") not in pub["outcomes"]:
            skipped[rec.get("outcome")] = skipped.get(rec.get("outcome"), 0) + 1
            continue
        p, l = public_record(rec, cfg)
        prev = carried.get(p["id"])
        if prev and p["licence"]["status"] == "issue" and all(prev[k] == p["licence"][k] for k in LICENCE_KEYS):
            p["licence"].update(classification=prev["classification"], eligibility=prev["eligibility"],
                                **{k: prev[k] for k in LICENCE_EDITION_KEYS if k in prev})
        projects.append(p)
        live.append(l)
    # Rows admitted by corpus-admit (corpus_admit.py) since the Discover lane build stay members: a
    # re-export from an older build must not undo admissions. The build's own row wins when it has one.
    admitted = 0
    if (out / "projects.jsonl").is_file():
        from .corpus_admit import STRATEGIES

        have = {p["id"] for p in projects} | {a for p in projects for a in p["aliases"]}
        prev_live = {r["id"]: r for r in common.read_jsonl(out / "live.jsonl")}
        for p in common.read_jsonl(out / "projects.jsonl"):
            if (p["provenance"] and all(r["strategy"] in STRATEGIES for r in p["provenance"])
                    and p["id"] not in have and p["id"] in prev_live):
                projects.append(p)
                live.append(prev_live[p["id"]])
                admitted += 1
    extra = {
        "membership": {"as_of": src_manifest.get("as_of"), "maintenance_evaluated_as_of": "per the Discover lane "
                       "build at membership.as_of (docs/DISCOVER.md); live facts are refreshed, evaluations are not", "source_schema": src_manifest.get("schema"),
                       "source_manifest_sha256": hashlib.sha256(src_manifest_bytes).hexdigest(),
                       "outcomes_published": pub["outcomes"], "skipped": skipped,
                       "rebuilt_by": "corpus-export from the Discover lane corpus (KEI-811)",
                       "admitted_rows_kept": admitted},
        "publication": {"rules": "config/corpus.yaml publication", "gate": pub.get("gate"),
                        "descriptions": "host-reported, truncated" if pub["include_descriptions"] else "omitted",
                        "private_seed_routes": f"published as {pub['private_seed_label']!r} without file or line"},
        "refresh": {"cadence": cfg["refresh"]["cadence"], "stale_after_days": cfg["refresh"]["stale_after_days"],
                    "last": None},
    }
    if carried:
        try:
            prev_m = common.read_json(out / "MANIFEST.json")
        except (OSError, ValueError):
            prev_m = {}
        for k in ("licence_classification", "edition_check"):
            if prev_m.get(k):
                extra[k] = prev_m[k]
    return write_layer(out, projects, live, extra)


# -- refresh


def due(now: datetime | None = None, out: Path = OUT) -> dict:
    """Is a refresh due, and is the layer already stale? A pass that a previous run left part-way
    (`refresh.in_progress`, KEI-961) is always due: the next wake-up carries it on."""
    now = now or datetime.now(timezone.utc)
    cfg = load_cfg()["refresh"]
    try:
        m = common.read_json(out / "MANIFEST.json")
    except (OSError, ValueError):
        return {"due": False, "stale": False, "reason": "no data/corpus layer to refresh; run corpus-export first"}
    r = m.get("refresh") or {}
    last = r.get("last") or {}
    at = last.get("at") or m.get("as_of")
    if not at:
        return {"due": True, "stale": True, "reason": "never refreshed"}
    age = (now - _parse(at)).total_seconds() / 86400
    out_ = {"due": age >= cfg["due_after_days"], "last_refresh": at, "age_days": round(age, 2),
            "due_after_days": cfg["due_after_days"], "stale": age > cfg["stale_after_days"],
            "stale_after_days": cfg["stale_after_days"]}
    prog = r.get("in_progress")
    if prog:
        out_.update(due=True, reason="a refresh pass is in progress; resume it",
                    in_progress={k: prog.get(k) for k in ("started_at", "done", "total", "runs", "updated_at")})
    return out_


def _refresh_rows(pids: list, projects: dict, live: dict, api, cfg: dict, pub: dict, stamp: str) -> tuple:
    """Re-read one chunk of rows. Returns (new project rows, new live rows, stats); applies nothing."""
    from . import hosts

    slugs, forge = {}, {}
    for pid in pids:
        host = pid.split("/", 1)[0]
        slug = (live[pid].get("full_name") or projects[pid]["full_name"]).lower()
        if host == "github.com":
            slugs[pid] = slug
        elif host in hosts.HOSTS:
            forge.setdefault(host, {})[pid] = slug
    meta = {}
    if slugs:
        got = api.meta_batch(sorted(set(slugs.values())), size=cfg["batch_size"])
        meta.update({f"github.com/{k}": v for k, v in got.items()})
    keyed = {pid: f"github.com/{slug}" for pid, slug in slugs.items()}
    # GitLab and Codeberg members (KEI-923) are re-read through hosts.py, one REST call (and GitLab GraphQL) each.
    for host, rows in sorted(forge.items()):
        got = hosts.meta_batch(api.http, host, sorted(set(rows.values())))
        meta.update({f"{host}/{k}": {**v, "host": host} for k, v in got.items()})
        keyed.update({pid: f"{host}/{slug}" for pid, slug in rows.items()})
    stats = {"resolved": 0, "not_found": 0, "unresolved": 0, "renamed": 0, "changed_metadata": 0,
             "not_on_github": len(pids) - len(keyed)}
    new_p, new_l = {}, {}
    for pid, slug in keyed.items():
        host = slug.split("/", 1)[0]
        got = meta.get(slug) or {"found": False, "error": "missing_from_response"}
        row = dict(live[pid])
        if got.get("found"):
            stats["resolved"] += 1
            row.update(found=True, full_name=got["name_with_owner"], stars=got["stars"], forks=got["forks"],
                       pushed_at=got["pushed_at"], archived=got["archived"],
                       latest_release=_release(got.get("latest_release")),
                       # Codeberg's API names no licence (hosts.py); keep the one admission read from the files
                       licence_spdx=got["licence_spdx"] if host != "codeberg.org" else
                       (got["licence_spdx"] or row.get("licence_spdx")),
                       observed_as_of=stamp, unresolved=None)
            p = dict(projects[pid])
            if f"{host}/{got['canonical_slug']}" != slug:
                stats["renamed"] += 1
            new_key = f"{host}/{got['canonical_slug']}"
            aliases = sorted(set(p["aliases"]) | ({new_key} - {pid}))
            upd = {"aliases": aliases, "full_name": got["name_with_owner"],
                   "name": got["name_with_owner"].split("/")[-1],
                   "url": f"https://{host}/{got['name_with_owner']}",
                   "description": _text(got.get("description"), pub["description_max_chars"])
                   if pub["include_descriptions"] else None,
                   "homepage": got.get("homepage") or None,
                   "topics": list(got.get("topics") or [])[:pub["max_topics"]],
                   "languages": list(got.get("languages") or [])[:pub["max_languages"]]}
            if any(p.get(k) != v for k, v in upd.items()):
                stats["changed_metadata"] += 1
                p.update(upd)
                new_p[pid] = p
        elif got.get("error") == "not_found":
            stats["not_found"] += 1
            row.update(found=False, observed_as_of=stamp, unresolved="not_found")
        else:
            # Not read: the row keeps its last known facts and observed_as_of, and says why.
            stats["unresolved"] += 1
            row["unresolved"] = got.get("error") or "unknown"
        new_l[pid] = row
    return new_p, new_l, stats


STAT_KEYS = ("resolved", "not_found", "unresolved", "renamed", "changed_metadata", "not_on_github",
             "requests", "batches", "waited_seconds")


def refresh(now: datetime | None = None, out: Path = OUT, api=None, budget_seconds: float | None = None) -> dict:
    """Re-read every published row's live facts, in chunks, inside a wall-clock budget (KEI-961).

    A full pass over ~25k rows does not reliably fit one job, so a pass is resumable: rows are taken
    in id order, `chunk_size` at a time, and every chunk read in full is promoted. A run that reaches
    its budget writes what it read and records the pass in MANIFEST `refresh.in_progress` (started_at,
    cursor, running totals); the next wake-up carries on from the cursor. Each row's observed_as_of is
    the moment it was actually read. `refresh.last` (what `corpus-due` and the MCP server's staleness
    read) advances only when a pass has covered the whole corpus, and then records the pass's start,
    so the layer is never presented as fresher than its oldest row in that pass.
    """
    import time

    from .discover import Api, log

    now = now or datetime.now(timezone.utc)
    full_cfg = load_cfg()
    cfg, pub = full_cfg["refresh"], full_cfg["publication"]
    budget = float(budget_seconds if budget_seconds is not None else cfg.get("max_runtime_minutes", 40) * 60)
    t0 = time.monotonic()
    m = common.read_json(out / "MANIFEST.json")
    pre = validate(out)
    if pre["failed_checks"]:
        raise CorpusError(f"data/corpus does not validate before refresh: {pre['failed_checks']}")
    projects = {p["id"]: p for p in common.read_jsonl(out / "projects.jsonl")}
    live = {r["id"]: r for r in common.read_jsonl(out / "live.jsonl")}
    on_github = sum(1 for pid in projects if pid.startswith("github.com/"))
    batches = -(-on_github // cfg["batch_size"])
    if batches > cfg["max_graphql_points"]:
        raise CorpusError(f"{batches} batches would exceed max_graphql_points {cfg['max_graphql_points']}")
    stamp = _iso(now)
    prog = dict((m.get("refresh") or {}).get("in_progress") or {})
    if prog.get("cursor") and (now - _parse(prog["started_at"])).total_seconds() / 86400 >= cfg["due_after_days"]:
        # A pass that has not finished within a refresh cycle is restarted rather than completed:
        # finishing it would date a "full refresh" from rows a week old or more.
        log(f"corpus-refresh: the pass started {prog['started_at']} is older than {cfg['due_after_days']} days; "
            "starting a new pass")
        prog = {}
    if prog.get("cursor"):
        log(f"corpus-refresh: resuming the pass started {prog['started_at']} after {prog['cursor']} "
            f"({prog.get('done')}/{prog.get('total')} rows read)")
    else:
        prog = {"started_at": stamp, "cursor": None, "runs": 0, "stats": {k: 0 for k in STAT_KEYS}}
    order = sorted(projects)
    pending = [pid for pid in order if prog["cursor"] is None or pid > prog["cursor"]]
    api = api or Api()
    http = api.http
    http.deadline = time.monotonic() + budget
    http.max_sleep = float(cfg.get("max_wait_seconds", 120))

    def expired() -> bool:
        return time.monotonic() >= http.deadline

    def waited() -> float:
        return float(getattr(http, "waited", 0.0))
    size = int(cfg.get("chunk_size", 2500))
    log(f"corpus-refresh: {len(order)} rows ({on_github} on GitHub, {batches} GraphQL batches a pass); "
        f"{len(pending)} to read; chunks of {size}; budget {budget / 60:.0f} min")
    run = {k: 0 for k in STAT_KEYS}
    slowest, stopped, chunks_done = 0.0, None, 0
    for i in range(0, len(pending), size):
        chunk = pending[i:i + size]
        left = http.deadline - time.monotonic()
        if chunks_done and left < 1.5 * slowest:
            stopped = f"budget: {left / 60:.1f} min left, a chunk takes up to {slowest / 60:.1f} min"
            break
        t, calls0, waited0 = time.monotonic(), http.calls, waited()
        new_p, new_l, st = _refresh_rows(chunk, projects, live, api, cfg, pub, stamp)
        took = time.monotonic() - t
        if expired():
            # Lookups past the deadline were refused, not answered: this chunk is not promoted.
            stopped = f"deadline reached during chunk {chunk[0]} .. {chunk[-1]}; it is read again next run"
            break
        # The bar is the whole pass's, as before chunking: at most max_unresolved_share of the corpus
        # unread. Promoting a chunk with a few unread rows is harmless (they keep their last facts and
        # say why); a chunk that would take the pass over the bar is not promoted.
        unread = prog["stats"].get("unresolved", 0) + st["unresolved"]
        share = unread / max(len(order), 1)
        if share > cfg["max_unresolved_share"]:
            msg = (f"{unread} of {len(order)} repositories unresolved in this pass ({share:.1%}), "
                   f"{st['unresolved']} of {len(chunk)} in chunk {chunk[0]} .. {chunk[-1]}")
            if not chunks_done:
                raise CorpusError(f"{msg}; the previous layer is kept")
            stopped = f"unresolved: {msg}; not promoted"
            break
        projects.update(new_p)
        live.update(new_l)
        st.update(requests=http.calls - calls0, batches=-(-sum(1 for p in chunk if p.startswith("github.com/"))
                                                          // cfg["batch_size"]),
                  waited_seconds=round(waited() - waited0))
        for k in STAT_KEYS:
            run[k] += st.get(k, 0)
            prog["stats"][k] = prog["stats"].get(k, 0) + st.get(k, 0)
        prog["cursor"] = chunk[-1]
        chunks_done += 1
        slowest = max(slowest, took)
        done = sum(1 for pid in order if pid <= prog["cursor"])
        log(f"corpus-refresh: chunk {chunks_done} done in {took:.0f}s, {done}/{len(order)} rows "
            f"(unresolved {st['unresolved']}, waited {st['waited_seconds']}s, http calls {http.calls}, "
            f"elapsed {(time.monotonic() - t0) / 60:.1f} min)")
    if http.failures:
        log(f"corpus-refresh: {len(http.failures)} lookups failed after retries")
    complete = stopped is None
    prog["runs"] = prog.get("runs", 0) + 1
    method = (f"GitHub GraphQL, {cfg['batch_size']} repositories per request; REST only to follow a rename; "
              "GitLab and Codeberg through hosts.py; resumable chunks of "
              f"{size} rows inside a {budget / 60:.0f} min budget per run")
    extra = {k: m[k] for k in ("membership", "publication", "licence_classification", "edition_check") if k in m}
    refresh_block = {"cadence": cfg["cadence"], "stale_after_days": cfg["stale_after_days"],
                     "last": (m.get("refresh") or {}).get("last")}
    if complete:
        totals = prog["stats"]
        refresh_block["last"] = {"at": prog["started_at"], "completed_at": stamp, "method": method,
                                 "runs": prog["runs"], **{k: totals[k] for k in STAT_KEYS}}
        status = "complete"
    else:
        done = sum(1 for pid in order if prog["cursor"] and pid <= prog["cursor"])
        refresh_block["in_progress"] = {"started_at": prog["started_at"], "updated_at": stamp,
                                        "cursor": prog["cursor"], "done": done, "total": len(order),
                                        "runs": prog["runs"], "method": method, "stopped": stopped,
                                        "stats": prog["stats"]}
        status = "partial"
        log(f"corpus-refresh: stopped ({stopped}); {done}/{len(order)} rows of this pass read")
    extra["refresh"] = refresh_block
    manifest = write_layer(out, list(projects.values()), list(live.values()), extra)
    return {"status": status, "this_run": {**run, "chunks": chunks_done, "stopped": stopped,
                                           "elapsed_minutes": round((time.monotonic() - t0) / 60, 1)},
            "refresh": refresh_block["last"] if complete else refresh_block["in_progress"],
            "corpus_version": manifest["corpus_version"]}


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
        check(set(p["licence"]) in (LICENCE_KEYS, LICENCE_CLASSIFIED_KEYS)
              or (set(p["licence"]) - set(LICENCE_EDITION_KEYS)) == LICENCE_CLASSIFIED_KEYS,
              f"{p['id']}: licence fields {sorted(p['licence'])}")
        check(p["licence"]["status"] in ("recognised", "issue"), f"{p['id']}: licence status")
        if "classification" in p["licence"]:
            _check_classification(p, check)
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
    from jsonschema import Draft202012Validator

    from . import identity

    schema = common.read_json(common.SCHEMA / "the-source.corpus.schema.json")
    v = Draft202012Validator({**schema, "$ref": "#/$defs/identity"})
    bad = [f"{p.get('id')}: identity {e.message}" for p in projects if isinstance(p, dict) and "identity" in p
           for e in v.iter_errors(p["identity"])]
    check(not bad, "; ".join(bad[:5]))
    for msg in (identity.check([p for p in projects if isinstance(p, dict) and "identity" in p]) if not bad else []):
        check(False, msg)
    for r in live:
        check(set(r) == LIVE_KEYS, f"{r.get('id')}: live fields {sorted(set(r) ^ LIVE_KEYS)}")
    counts = _counts(projects, live) if not failed else {}
    if counts:
        check(m.get("counts") == counts, f"MANIFEST counts {m.get('counts')} != {counts}")
    return {"checks": checks, "failed_checks": failed[:50], "failed_total": len(failed),
            "projects": len(projects), "corpus_version": m.get("corpus_version")}


def _check_classification(p: dict, check) -> None:
    """A licence classification (KEI-919) is well formed, carries its provenance (S1.7), and its
    eligibility follows from its class and nothing else. It never applies to a recognised row."""
    pid, c, e = p["id"], p["licence"]["classification"] or {}, p["licence"]["eligibility"] or {}
    check(p["licence"]["status"] == "issue", f"{pid}: classification on a row without a licence issue")
    check(c.get("class") in LICENCE_CLASSES, f"{pid}: licence class {c.get('class')!r}")
    check(bool(c.get("reason")), f"{pid}: classification without a reason")
    prov = c.get("provenance") or {}
    check(set(prov) == {"source", "commit", "checked_at"}, f"{pid}: classification provenance {sorted(prov)}")
    check(bool(prov.get("source")) and bool(prov.get("checked_at")), f"{pid}: provenance source/checked_at")
    check(prov.get("commit") is not None or c.get("class") == "unclear", f"{pid}: a class other than unclear "
                                                                         "without the commit it was read at")
    want = ELIGIBILITY_BY_CLASS.get(c.get("class"), "not_eligible")
    ec = p["licence"].get("edition_check")
    passed = c.get("class") == "open_core" and isinstance(ec, dict) and ec.get("result") == "pass"
    check(e.get("status") == ("eligible" if passed else want),
          f"{pid}: eligibility {e.get('status')!r} for class {c.get('class')!r}")
    if ec is not None:
        _check_edition(pid, c, e, ec, check)
    if c.get("class") == "open_core":
        ex = c.get("excluded") or {}
        check(bool(c.get("core_spdx")) and bool(ex.get("paths") or ex.get("markers")),
              f"{pid}: open_core without a core SPDX and the excluded paths")
        check(bool(e.get("condition")) and (passed or bool(e.get("follow_up"))),
              f"{pid}: open_core without its condition")
    else:
        check(e.get("condition") is None and e.get("follow_up") is None, f"{pid}: condition on {c.get('class')}")
    check(e.get("self_hostable") in (True, None), f"{pid}: self_hostable must be true or null")


def _check_edition(pid: str, c: dict, e: dict, ec: dict, check) -> None:
    """An edition check (edition_check.py) is well formed and fails closed: only `pass` makes an open-core row
    eligible, and only with the disclosure as its condition and the provenance of the classification's commit."""
    check(c.get("class") == "open_core", f"{pid}: edition check on a row that is not open_core")
    check(ec.get("result") in EDITION_RESULTS and bool(ec.get("reason_code")) and bool(ec.get("reason")),
          f"{pid}: edition check without a result and reason code")
    prov = ec.get("provenance") or {}
    check(set(prov) == {"source", "commit", "checked_at"} and bool(prov.get("checked_at")),
          f"{pid}: edition check provenance {sorted(prov)}")
    if ec.get("result") == "pass":
        check(prov.get("commit") == (c.get("provenance") or {}).get("commit") and bool(prov.get("commit")),
              f"{pid}: edition check passed at a commit other than the classification's")
        check(bool(ec.get("disclosure")) and e.get("condition") == ec.get("disclosure") and not e.get("follow_up"),
              f"{pid}: a passed edition check without its disclosure")
        check(bool(ec.get("excluded_scope")), f"{pid}: a passed edition check that does not say what is excluded")
    else:
        check(e.get("status") == "conditional" and e.get("reason_code") == ec.get("reason_code"),
              f"{pid}: a {ec.get('result')} edition check must stay conditional with its reason code")


def main(command: str) -> int:
    try:
        if command == "corpus-export":
            out = export()
            print(json.dumps({"corpus_version": out["corpus_version"], "counts": out["counts"]}, indent=2))
        elif command == "corpus-refresh":
            print(json.dumps(refresh(), indent=2))
        elif command == "corpus-licence-classify":
            from . import licence_classify

            return licence_classify.main()
        elif command == "corpus-admit":
            from . import corpus_admit, daily

            print(json.dumps(corpus_admit.admit(daily.load_pool(), datetime.now(timezone.utc).replace(microsecond=0)),
                             indent=2))
        elif command == "corpus-edition-check":
            from . import edition_check

            return edition_check.main()
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
