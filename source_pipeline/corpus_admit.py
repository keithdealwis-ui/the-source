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
STRATEGIES = ("discovery_search", "community_radar", "corpus_expansion")
HOSTS = ("github.com", "gitlab.com", "codeberg.org")
# Prefilter reasons that, alone, mark a notable project as historical rather than unwanted (KEI-923 S4.2).
HISTORICAL_REASONS = {"archived", "stale_no_meaningful_activity_12_months"}
METHOD = "the-source.corpus-admit/1"
TOP_N = 10


def manifest_sha(out: Path) -> str | None:
    path = out / "MANIFEST.json"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def strategy_of(prov: dict) -> str:
    """One label per discovery route: the search lane, the awesome-list seed, the radar source, or an expansion
    source (`corpus_expansion:<family>:<query>`, KEI-923)."""
    if prov.get("provider") == "community_radar":
        return f"community_radar:{prov.get('query')}"
    return f"{prov.get('provider')}:{prov.get('query')}"


def _verifiable(key: str) -> bool:
    host, _, path = key.partition("/")
    depth = path.count("/") + 1 if path else 0
    return host in HOSTS and (depth == 2 or (host == "gitlab.com" and depth >= 2))


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
    host = meta.get("host") or "github.com"
    rid = f"{host}/{nwo.lower()}"
    stamp = corpus._iso(as_of)
    project = {
        "id": rid, "host_repo_id": meta.get("database_id"), "name": nwo.split("/")[-1], "full_name": nwo,
        "url": f"https://{host}/{nwo}", "aliases": [],
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
        strategy = prov.get("provider") if prov.get("provider") in ("community_radar", "corpus_expansion") \
            else "discovery_search"
        out.append({"strategy": strategy, "source": strategy_of(prov), "commit": None, "locator": entry["key"],
                    "depth": 1, "via": None})
    return sorted(out, key=lambda r: (r["strategy"], r["source"]))


def _lookup(keys: list[str], api, http, batch: int) -> dict:
    """Host facts for `host/path` keys: GitHub in GraphQL batches, GitLab and Codeberg through hosts.py."""
    from . import hosts

    out, by_host = {}, {}
    for k in keys:
        host, _, path = k.partition("/")
        by_host.setdefault(host, []).append(path)
    for host, paths in sorted(by_host.items()):
        got = api.meta_batch(paths, size=batch) if host == "github.com" else hosts.meta_batch(http, host, paths)
        for path in paths:
            m = got.get(path) or {"found": False, "error": "missing_from_response"}
            out[f"{host}/{path}"] = {**m, "host": host} if m.get("found") else m
    return out


def _probe_licence(http, got: dict) -> None:
    """Codeberg reports no licence (hosts.py): read the licence files at the root, as enrich.codeberg does."""
    from .enrich import _gitea_probe_licence
    from .licence_detect import detect

    probe = _gitea_probe_licence(http, "https://codeberg.org/api/v1/repos/" + got["name_with_owner"])
    if not probe.get("error"):
        d = detect(probe["root"], probe["texts"])
        if d["recognised"] and d["spdx"] and " AND " not in d["spdx"]:
            got["licence_spdx"] = d["spdx"]


def _quota(cfg: dict | None):
    """Admission quotas (config/expansion.yaml `quotas`): a route is chargeable while its query and its source
    family are both under their caps. Returns (pick, charge, counts)."""
    if not cfg:
        return (lambda routes: routes[0] if routes else None), (lambda route: None), {}
    per_query, per_family = {}, {}
    q_cap = int(cfg["max_per_query"])
    f_cap = int(float(cfg["max_source_share"]) * int(cfg["max_admit_per_run"]))

    def family(route: str) -> str:
        parts = route.split(":")
        return parts[1] if parts[0] == "corpus_expansion" and len(parts) > 2 else parts[0]

    def pick(routes: list[str]):
        ok = [r for r in routes if per_query.get(r, 0) < q_cap and per_family.get(family(r), 0) < f_cap]
        return min(ok, key=lambda r: (per_query.get(r, 0), r)) if ok else None

    def charge(route: str):
        per_query[route] = per_query.get(route, 0) + 1
        per_family[family(route)] = per_family.get(family(route), 0) + 1

    return pick, charge, {"per_family": per_family, "per_query": per_query}


def admit(pool: dict, as_of: datetime, out: Path | None = None, api=None, max_lookups: int | None = None,
          expansion: dict | None = None, historical_out: Path | None = None, http=None) -> dict:
    """Admit the pool's genuinely new candidates into data/corpus. Returns the counts the run record carries.

    `expansion` (config/expansion.yaml, KEI-923) turns on, for a bulk run: lookups ordered by stars, the
    quotas, the milestone ceiling and the historical tier (written to `historical_out`). The intake noise gate
    (noise.intake_reasons) applies whenever config/expansion.yaml `noise.gate` is on, daily admissions included.

    Raises corpus.CorpusError when the layer cannot be read or would not validate; the previous layer is kept."""
    from . import noise
    from .discover import Api, prefilter

    out = out or corpus.OUT
    cfg = corpus.load_cfg()
    pub, adm = cfg["publication"], cfg.get("admission") or {}
    exp_path = common.CONFIG / "expansion.yaml"
    exp_cfg = common.load_yaml(exp_path) if exp_path.is_file() else {}
    gate = bool((exp_cfg.get("noise") or {}).get("gate"))
    quotas = (expansion or {}).get("quotas")
    max_lookups = int(max_lookups or (quotas or {}).get("max_lookups_per_run") or adm.get("max_lookups_per_cycle", 2000))
    target = int(((expansion or {}).get("milestones") or {}).get("target") or 0)
    hist_min = int(((expansion or {}).get("historical") or {}).get("min_stars") or 0)
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
        if not _verifiable(key):
            drop("host_not_independently_verifiable")
            continue
        for s in strategies:
            found_by[s]["new_to_corpus"] += 1
        todo[key] = entry
    # A bulk run spends its lookups on the most-starred candidates first; a daily run keeps key order.
    keys = sorted(todo, key=(lambda k: (-(todo[k].get("stars") or 0), k)) if expansion else None)
    deferred = keys[max_lookups:]
    keys = keys[:max_lookups]
    if deferred:
        drop("deferred_lookup_budget", len(deferred))

    api = api or Api()
    http = http or api.http
    meta = _lookup(keys, api, http, int(cfg["refresh"]["batch_size"])) if keys else {}
    window_start = corpus._iso(as_of - timedelta(days=int(policy["maintenance"]["maintained_days"])))
    pick, charge, quota_counts = _quota(quotas)
    max_admit = int((quotas or {}).get("max_admit_per_run") or 0)
    admitted_p, admitted_l, admitted_routes, historical = {}, {}, {}, {}
    for key in keys:
        entry, got = todo[key], meta.get(key) or {"found": False, "error": "missing_from_response"}
        host = key.split("/", 1)[0]
        if not got.get("found"):
            drop("repository_not_found" if got.get("error") == "not_found" else "lookup_failed")
            continue
        new_key = f"{host}/{got['canonical_slug']}"
        hit, _ = _match(new_key, [], got.get("database_id"), by_key, by_hid)
        if hit:  # renamed or transferred into a project the corpus already holds
            drop("already_in_corpus_after_rename")
            continue
        noisy = noise.intake_reasons(got["name_with_owner"].split("/")[-1], got["name_with_owner"],
                                     got.get("description"), got.get("topics")) if gate else []
        reasons = prefilter(got, window_start, disc_cfg)
        if reasons:
            if (hist_min and not noisy and set(reasons) <= HISTORICAL_REASONS and (got.get("stars") or 0) >= hist_min
                    and new_key not in historical):
                historical[new_key] = {"id": new_key, "full_name": got["name_with_owner"],
                                       "url": f"https://{host}/{got['name_with_owner']}",
                                       "host_repo_id": got.get("database_id"), "stars": got.get("stars"),
                                       "archived": bool(got.get("archived")), "pushed_at": got.get("pushed_at"),
                                       "licence_spdx": got.get("licence_spdx"),
                                       "description": corpus._text(got.get("description"), pub["description_max_chars"]),
                                       "topics": list(got.get("topics") or [])[:pub["max_topics"]],
                                       "reasons": sorted(reasons), "observed_as_of": corpus._iso(as_of),
                                       "routes": sorted({strategy_of(pr) for pr in entry.get("providers") or []})}
                drop("historical_tier")
                continue
            for r in reasons:
                drop(r)
            continue
        if noisy:
            drop(f"intake_noise:{noisy[0]}")
            continue
        routes = sorted({strategy_of(pr) for pr in entry.get("providers") or []})
        if expansion and target and len(before_ids) + len(admitted_p) >= target:
            drop("milestone_reached")
            continue
        if max_admit and len(admitted_p) >= max_admit:
            drop("deferred_admission_quota")
            continue
        route = pick(routes)
        if quotas and route is None:
            drop("deferred_source_quota")
            continue
        if host == "codeberg.org" and not got.get("licence_spdx"):
            _probe_licence(http, got)
        p, l = _rows(got, _routes(entry), as_of, policy, pub)
        if p["id"] in admitted_p:  # two pool addresses for one repository
            admitted_p[p["id"]]["provenance"] = sorted(
                {common.dumps(r): r for r in admitted_p[p["id"]]["provenance"] + p["provenance"]}.values(),
                key=lambda r: (r["strategy"], r["source"]))
            drop("duplicate_in_pool")
            continue
        if route:
            charge(route)
        admitted_p[p["id"]], admitted_l[p["id"]] = p, l
        admitted_routes[p["id"]] = routes

    if historical_out is not None and historical:
        _write_historical(historical_out, historical, as_of)
    result = {"method": METHOD, "at": corpus._iso(as_of), "pool_candidates": len(pool),
              "looked_up": len(keys), "membership_before": len(before_ids),
              "manifest_sha256_before": sha_before, "corpus_version_before": m.get("corpus_version"),
              "dropped": dict(sorted(dropped.items())), "historical": len(historical),
              "noise_gate": gate}
    if quotas:
        result["quotas"] = {"config": quotas, **{k: dict(sorted(v.items())) for k, v in quota_counts.items()}}
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
    if expansion:
        membership["expansion"] = {
            "method": "the-source.expansion/1: source_pipeline/expansion.py and corpus_admit.py (KEI-923); "
                      "config/expansion.yaml sources, quotas and noise gate",
            "last": {"at": result["at"], "admitted": len(admitted_p), "historical": len(historical)}}
    elif (m.get("membership") or {}).get("expansion"):
        membership["expansion"] = m["membership"]["expansion"]
    extra = {k: m[k] for k in ("publication", "licence_classification", "edition_check", "refresh") if k in m}
    extra["membership"] = membership
    manifest = corpus.write_layer(out, projects + list(admitted_p.values()), live + list(admitted_l.values()), extra)

    after = common.read_jsonl(out / "projects.jsonl")
    after_ids = {p["id"] for p in after}
    after_alias = {a for p in after for a in p["aliases"]}
    gone = before_ids - after_ids
    collapsed = {g for g in gone if g in after_alias}
    added = sorted(after_ids - before_ids)
    stars = {r["id"]: r.get("stars") or 0 for r in common.read_jsonl(out / "live.jsonl")}
    lic = {p["id"]: p["licence"]["status"] for p in after}
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
        top_new_by_stars=[{"id": pid, "stars": stars.get(pid), "licence_status": lic[pid],
                           "strategies": admitted_routes.get(pid, [])} for pid in top])
    return result


def _write_historical(out: Path, rows: dict, as_of: datetime) -> None:
    """The historical tier (KEI-923 S4.2): notable archived or long-inactive projects, kept apart from the active
    corpus. Merged with what is already there; never served as discovery, never recommended."""
    out.mkdir(parents=True, exist_ok=True)
    path = out / "projects.jsonl"
    have = {r["id"]: r for r in common.read_jsonl(path)} if path.exists() else {}
    have.update(rows)
    common.write_jsonl(path, [have[k] for k in sorted(have)])
    by_reason = {}
    for r in have.values():
        for x in r["reasons"]:
            by_reason[x] = by_reason.get(x, 0) + 1
    common.write_json(out / "MANIFEST.json", {
        "schema": "the-source.historical/1", "as_of": corpus._iso(as_of), "projects": len(have),
        "by_reason": dict(sorted(by_reason.items())), "projects_sha256": common.sha256_file(path),
        "note": "Notable projects that no longer clear the active quality floor (archived, or no push inside "
                "config/policy.yaml maintained_days). Not members of data/corpus, not discovery, never recommended."})


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
