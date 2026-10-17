"""Corpus admission: new members for the public discovery corpus (data/corpus/), every cycle.

Until this step the corpus had no automated way in. `corpus-export` rebuilds it from the
Discover lane build (data/discover/corpus/, internal and local only, last built 2026-09-29),
and `corpus-refresh` only re-reads the live facts of projects already in it. The daily
discovery lanes (discovery_search.py) and Community Radar (radar.py) kept finding repositories,
but they were reconciled against the canonical corpus only and held in the discovery pool
(data/discovery/candidates.jsonl) indefinitely. So membership stood still while everything
around it ran green.

Admission closes that gap. For every pool candidate the corpus does not already hold (by id,
alias or host repository id, before and after the host is asked), it reads the host's facts in
GraphQL batches and applies the Discover lane's own bar, unchanged:

  * prefilter (discover.prefilter): found, public, not archived, disabled, empty, a fork or a
    mirror, and pushed inside config/policy.yaml maintained_days;
  * licence: recognised when the host's SPDX is in policy.yaml recognised_spdx; otherwise the
    row is admitted as `retained` with its licence issue named (the corpus publishes both, and
    never recommends either: recommendations come only from data/canonical).

Admitted rows are written through corpus.write_layer, so identity resolution (mirrors, renames,
siblings) and corpus validation apply to them exactly as to every other row, and nothing is
promoted unless the whole layer validates. Maintenance status is read from `pushed_at` (the
Discover lane reads commits, tags and releases); the manifest says so.

Every admission is counted: what each strategy found, what dedupe and the gate dropped and why,
and the top new repositories by stars. The run record and the MCP `last_run` block carry it.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import common, corpus

# Provenance strategies that only admission writes (a re-export keeps these rows; see corpus.export).
STRATEGIES = ("discovery_search", "community_radar")
METHOD = "the-source.corpus-admit/1"
TOP_N = 10


def manifest_sha(out: Path) -> str | None:
    path = out / "MANIFEST.json"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def strategy_of(prov: dict) -> str:
    """One label per discovery route: the search lane, the awesome-list seed, or the radar source."""
    if prov.get("provider") == "community_radar":
        return f"community_radar:{prov.get('query')}"
    return f"{prov.get('provider')}:{prov.get('query')}"


def _known(projects: list[dict]) -> tuple[dict, dict]:
    by_key, by_hid = {}, {}
    for p in projects:
        for k in [p["id"], *p["aliases"]]:
            by_key[k] = p["id"]
        if p.get("host_repo_id"):
            by_hid[(p["id"].split("/", 1)[0], p["host_repo_id"])] = p["id"]
    return by_key, by_hid


def _match(key: str, aliases, hid, by_key: dict, by_hid: dict):
    for k in [key, *(aliases or [])]:
        if k in by_key:
            return by_key[k], "key_or_alias"
    if hid and (key.split("/", 1)[0], hid) in by_hid:
        return by_hid[(key.split("/", 1)[0], hid)], "host_repo_id"
    return None, None


def _maintenance(pushed_at: str, as_of: datetime, policy: dict) -> dict:
    days = (as_of - datetime.strptime(pushed_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).days
    status = "active" if days <= int(policy["maintenance"]["active_days"]) else "maintained"
    return {"status": status, "last_meaningful_activity_at": pushed_at}


def _licence(spdx, recognised: set) -> dict:
    if spdx in recognised:
        return {"spdx": spdx, "status": "recognised", "issue": None}
    if not spdx:
        return {"spdx": None, "status": "issue",
                "issue": {"category": "missing", "reason": "the host reports no licence for this repository"}}
    return {"spdx": spdx, "status": "issue",
            "issue": {"category": "unrecognised",
                      "reason": f"licence {spdx} is not a recognised open-source licence (config/policy.yaml)"}}


def _rows(meta: dict, routes: list[dict], as_of: datetime, policy: dict, pub: dict) -> tuple[dict, dict]:
    nwo = meta["name_with_owner"]
    rid = f"github.com/{nwo.lower()}"
    stamp = corpus._iso(as_of)
    project = {
        "id": rid, "host_repo_id": meta.get("database_id"), "name": nwo.split("/")[-1], "full_name": nwo,
        "url": f"https://github.com/{nwo}", "aliases": [],
        "description": corpus._text(meta.get("description"), pub["description_max_chars"])
        if pub["include_descriptions"] else None,
        "homepage": meta.get("homepage") or None,
        "topics": list(meta.get("topics") or [])[:pub["max_topics"]],
        "languages": list(meta.get("languages") or [])[:pub["max_languages"]],
        "licence": _licence(meta.get("licence_spdx"), set(policy["licence"]["recognised_spdx"])),
        "maintenance": _maintenance(meta["pushed_at"], as_of, policy),
        "lanes": ["discover"],
        "provenance": routes,
        "identity": None,
    }
    live = {"id": rid, "found": True, "full_name": nwo, "stars": meta.get("stars"), "forks": meta.get("forks"),
            "pushed_at": meta.get("pushed_at"), "archived": bool(meta.get("archived")),
            "latest_release": corpus._release(meta.get("latest_release")), "licence_spdx": meta.get("licence_spdx"),
            "observed_as_of": stamp, "unresolved": None}
    return project, live


def _routes(entry: dict) -> list[dict]:
    out = []
    for prov in entry.get("providers") or []:
        strategy = "community_radar" if prov.get("provider") == "community_radar" else "discovery_search"
        out.append({"strategy": strategy, "source": strategy_of(prov), "commit": None, "locator": entry["key"],
                    "depth": 1, "via": None})
    return sorted(out, key=lambda r: (r["strategy"], r["source"]))


def admit(pool: dict, as_of: datetime, out: Path | None = None, api=None, max_lookups: int | None = None) -> dict:
    """Admit the pool's genuinely new candidates into data/corpus. Returns the counts the run record carries.

    Raises corpus.CorpusError when the layer cannot be read or would not validate; the previous layer is kept."""
    from .discover import Api, prefilter

    out = out or corpus.OUT
    cfg = corpus.load_cfg()
    pub, adm = cfg["publication"], cfg.get("admission") or {}
    max_lookups = int(max_lookups or adm.get("max_lookups_per_cycle", 2000))
    policy = common.load_policy()
    disc_cfg = common.load_yaml(common.CONFIG / "discover.yaml")
    m = common.read_json(out / "MANIFEST.json")
    sha_before = manifest_sha(out)
    projects = common.read_jsonl(out / "projects.jsonl")
    live = common.read_jsonl(out / "live.jsonl")
    before_ids = {p["id"] for p in projects}
    by_key, by_hid = _known(projects)

    found_by = {}
    dropped = {}

    def drop(reason: str, n: int = 1):
        dropped[reason] = dropped.get(reason, 0) + n

    todo = {}
    for key, entry in sorted(pool.items()):
        strategies = sorted({strategy_of(p) for p in entry.get("providers") or []}) or ["unknown"]
        for s in strategies:
            found_by.setdefault(s, {"found": 0, "new_to_corpus": 0, "admitted": 0})["found"] += 1
        hit, _ = _match(key, entry.get("aliases"), entry.get("host_repo_id"), by_key, by_hid)
        if hit:
            drop("already_in_corpus")
            continue
        if not key.startswith("github.com/") or key.count("/") != 2:
            drop("host_not_independently_verifiable")
            continue
        for s in strategies:
            found_by[s]["new_to_corpus"] += 1
        todo[key.split("/", 1)[1]] = entry
    slugs = sorted(todo)
    deferred = slugs[max_lookups:]
    slugs = slugs[:max_lookups]
    if deferred:
        drop("deferred_lookup_budget", len(deferred))

    api = api or Api()
    meta = api.meta_batch(slugs, size=int(cfg["refresh"]["batch_size"])) if slugs else {}
    window_start = corpus._iso(as_of - timedelta(days=int(policy["maintenance"]["maintained_days"])))
    admitted_p, admitted_l, admitted_routes = {}, {}, {}
    for slug in slugs:
        entry, got = todo[slug], meta.get(slug) or {"found": False, "error": "missing_from_response"}
        if not got.get("found"):
            drop("repository_not_found" if got.get("error") == "not_found" else "lookup_failed")
            continue
        hit, _ = _match(f"github.com/{got['canonical_slug']}", [], got.get("database_id"), by_key, by_hid)
        if hit:  # renamed or transferred into a project the corpus already holds
            drop("already_in_corpus_after_rename")
            continue
        reasons = prefilter(got, window_start, disc_cfg)
        if reasons:
            for r in reasons:
                drop(r)
            continue
        p, l = _rows(got, _routes(entry), as_of, policy, pub)
        if p["id"] in admitted_p:  # two pool addresses for one repository
            admitted_p[p["id"]]["provenance"] = sorted(
                {common.dumps(r): r for r in admitted_p[p["id"]]["provenance"] + p["provenance"]}.values(),
                key=lambda r: (r["strategy"], r["source"]))
            drop("duplicate_in_pool")
            continue
        admitted_p[p["id"]], admitted_l[p["id"]] = p, l
        admitted_routes[p["id"]] = sorted({strategy_of(pr) for pr in entry.get("providers") or []})

    result = {"method": METHOD, "at": corpus._iso(as_of), "pool_candidates": len(pool),
              "looked_up": len(slugs), "membership_before": len(before_ids),
              "manifest_sha256_before": sha_before, "corpus_version_before": m.get("corpus_version"),
              "dropped": dict(sorted(dropped.items()))}
    if not admitted_p:
        result.update(membership_after=len(before_ids), corpus_added=0, corpus_removed=0, corpus_collapsed=0,
                      manifest_sha256_after=sha_before, corpus_version_after=m.get("corpus_version"),
                      membership_as_of=(m.get("membership") or {}).get("as_of"),
                      candidates_found_by_strategy=dict(sorted(found_by.items())), top_new_by_stars=[])
        return result

    membership = dict(m.get("membership") or {})
    membership["as_of"] = result["at"]
    membership["admission"] = {
        "method": f"{METHOD}: source_pipeline/corpus_admit.py; the Discover lane prefilter and licence bar, "
                  "maintenance read from pushed_at",
        "last": {"at": result["at"], "admitted": len(admitted_p)}}
    extra = {k: m[k] for k in ("publication", "licence_classification", "refresh") if k in m}
    extra["membership"] = membership
    manifest = corpus.write_layer(out, projects + list(admitted_p.values()), live + list(admitted_l.values()), extra)

    after = common.read_jsonl(out / "projects.jsonl")
    after_ids = {p["id"] for p in after}
    after_alias = {a for p in after for a in p["aliases"]}
    gone = before_ids - after_ids
    collapsed = {g for g in gone if g in after_alias}
    added = sorted(after_ids - before_ids)
    stars = {r["id"]: r.get("stars") or 0 for r in common.read_jsonl(out / "live.jsonl")}
    for pid in admitted_p:
        if pid not in after_ids:
            drop("collapsed_by_identity")
    for pid in added:
        for s in admitted_routes.get(pid, []):
            found_by[s]["admitted"] += 1
    top = sorted(added, key=lambda pid: (-stars.get(pid, 0), pid))[:TOP_N]
    result.update(
        membership_after=len(after_ids), corpus_added=len(added), corpus_removed=len(gone - collapsed),
        corpus_collapsed=len(collapsed), manifest_sha256_after=manifest_sha(out),
        corpus_version_after=manifest["corpus_version"], membership_as_of=manifest["membership"]["as_of"],
        dropped=dict(sorted(dropped.items())), candidates_found_by_strategy=dict(sorted(found_by.items())),
        top_new_by_stars=[{"id": pid, "stars": stars.get(pid), "licence_status":
                           next(p["licence"]["status"] for p in after if p["id"] == pid),
                           "strategies": admitted_routes.get(pid, [])} for pid in top])
    return result


# ---------------------------------------------------------------- anomalies


def anomalies(record: dict, previous: list[dict], schedule: dict, cycle_key_now: str | None = None) -> list[dict]:
    """Corpus intake anomalies for one run record, judged against the earlier records in runs/.

    zero_corpus_additions_consecutive  this and the previous scheduled run both admitted nothing
    corpus_manifest_unchanged          this run left data/corpus/MANIFEST.json byte-identical
    scheduled_run_missing              a cycle key inside the cadence has no scheduled run record
    A record without a corpus block (before this step existed, or a failed step) proves nothing either way."""
    out = []
    corp = record.get("corpus") or {}
    sched = sorted((r for r in previous if r.get("trigger") == "schedule" and r.get("run_id") != record.get("run_id")),
                   key=lambda r: (r.get("cycle_key") or "", r.get("started_at") or ""))
    if record.get("trigger") == "schedule" and corp.get("corpus_added") == 0:
        prior = [r for r in sched if r.get("status") == "succeeded" and (r.get("corpus") or {}).get("corpus_added")
                 is not None]
        if prior and prior[-1]["corpus"]["corpus_added"] == 0:
            out.append({"code": "zero_corpus_additions_consecutive",
                        "detail": f"no corpus additions in {prior[-1]['run_id']} or this run"})
    if corp.get("manifest_sha256_before") and corp.get("manifest_sha256_before") == corp.get("manifest_sha256_after"):
        out.append({"code": "corpus_manifest_unchanged",
                    "detail": f"data/corpus/MANIFEST.json sha256 {corp['manifest_sha256_after'][:12]} unchanged"})
    key_now = cycle_key_now or (record.get("cycle_key") if record.get("trigger") == "schedule" else None)
    keys = sorted({r["cycle_key"] for r in sched if r.get("cycle_key")})
    if key_now and keys:
        step = timedelta(days=1 if schedule.get("cadence", "weekly") == "daily" else 7)
        last = max(k for k in keys if k < key_now) if any(k < key_now for k in keys) else None
        if last:
            missing, d = [], datetime.fromisoformat(last) + step
            while d.date().isoformat() < key_now:
                if d.date().isoformat() not in keys:
                    missing.append(d.date().isoformat())
                d += step
            if missing:
                out.append({"code": "scheduled_run_missing", "detail": f"no scheduled run for {', '.join(missing[:14])}",
                            "missing": missing})
    return out
