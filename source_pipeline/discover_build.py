"""Discover lane (KEI-811) — build the graph-ready discovery corpus.

Pure. Reads what discovery and verification recorded (data/discover/frontier.json,
live.json, history/observations.jsonl) and the KEI-805 dataset, and writes
data/discover/corpus/. The clock is live.json's `as_of`; nothing reads the wall clock
or the network, so the same inputs produce the same bytes.

The KEI-805 dataset is read, never written. Its projects are the Replace lane and keep
their KEI-805 verdicts; this stage only references them.

Two lanes, independent of each other:
  replace   the project is in the KEI-805 replacement dataset
  discover  the project was reached by a discovery route other than the Replace lane
A project may be in both. Neither requires, nor implies, a SaaS relationship.

Three outcomes for every candidate, each counted:
  retained                   passed existence, archive, alias and activity checks
  recommendation_eligible    retained, and the licence is recognised (the full KEI-805 bar)
  rejected                   with machine-readable reasons; never silently dropped
Neither retention nor eligibility is publication or endorsement.
"""
from __future__ import annotations

import hashlib

from . import common
from .activity import Rules, evaluate, parse_ts
from .build import eligibility, lane_of, licence_issue, licence_verdict, live_view, load_exceptions
from .discover import FRONTIER, HISTORY, LIVE, DISCOVER, cfg_fingerprint, load_cfg, prefilter
from .discover_universe import all_routes, distinct_routes, order_for_cap

CORPUS = DISCOVER / "corpus"
SCHEMA_ID = "the-source.discover/1"

REASONS = {
    "repository_not_found": "the host has no repository at this address",
    "live_verification_failed": "the host could not be asked; nothing is assumed",
    "verification_incomplete": "a lookup needed to judge activity failed; the gap is not read as inactivity",
    "not_public": "the repository is not public",
    "archived": "archived by its owner",
    "disabled": "disabled by the host",
    "empty_repository": "the repository has no content",
    "fork_alias": "a fork; recorded as an alias of its parent, not as a project of its own",
    "mirror_alias": "a mirror of a repository hosted elsewhere",
    "duplicate_alias": "another address for a repository already in the corpus",
    "stale_no_meaningful_activity_12_months": "no meaningful activity within 12 months (config/policy.yaml)",
    "no_recognised_open_source_licence": "licence not recognised; retained in the licence-exception lane only if nothing else is wrong",
    "host_not_independently_verifiable": "hosted where the pipeline cannot verify it",
    "repository_identity_changed_since_listed": "renamed into something its listings never described (KEI-805)",
    "discovery_budget_exceeded": "discovered past raw_universe_cap; not evaluated",
    "seed_unresolved": "a seed row that names no repository",
    "kei805_rejected": "rejected in the KEI-805 Replace dataset; its reasons are carried over",
}


def _sha(obj) -> str:
    return hashlib.sha256(common.dumps(obj).encode()).hexdigest()


def _entry_reason(routes: list[dict]) -> str:
    r = routes[0]
    what = {
        "keith_seed": "named in Keith's seed corpus",
        "github_starred": "starred by a configured GitHub account",
        "github_watching": "watched by a configured GitHub account",
        "replace_lane": "in the KEI-805 replacement dataset",
        "owner_neighbourhood": "another repository of a seed's owner",
        "readme_links": "linked from a seed's README",
        "topic_search": "among the most-starred repositories on a seed topic",
        "curated_list": "listed in a licence-cleared curated list",
        "trending": "among the most-starred results of a pinned GitHub search",
    }[r["strategy"]]
    more = distinct_routes(routes) - 1
    return f"{what} ({r['source']}, depth {r['depth']})" + (f"; and {more} other route(s)" if more > 0 else "")


def run() -> dict:
    cfg = load_cfg()
    policy = common.load_policy()
    rules = Rules(policy)
    exceptions = load_exceptions()
    lane_cfg = common.load_yaml(common.CONFIG / "licence_lane.yaml")
    frontier = common.read_json(FRONTIER)
    live = common.read_json(LIVE)
    from .enrich import policy_fingerprint

    if not live.get("complete"):
        raise RuntimeError("live.json is a checkpoint of an interrupted run; run discover-verify --only-failed")
    if live["maintenance_policy_sha256"] != policy_fingerprint(policy):
        raise RuntimeError("the maintenance policy changed since discover-verify; run it again")
    if live["frontier_as_of"] != frontier["as_of"]:
        raise RuntimeError("live.json was verified against a different frontier; run discover-verify again")
    as_of = parse_ts(live["as_of"])
    stamp = live["as_of"]
    window_start = live["window_start"]

    routes = all_routes(frontier, cfg)
    ordered = order_for_cap(routes)
    cap = int(cfg["raw_universe_cap"])
    evaluated, over_cap = ordered[:cap], ordered[cap:]

    # KEI-805 Replace lane, read-only.
    replace_rows = {r["oss_id"]: r for r in common.read_jsonl(common.DATASET / "oss_projects.jsonl")}
    replace_by_slug = {k.split("/", 1)[1]: r for k, r in replace_rows.items() if k.startswith("github.com/")}
    replace_by_hostid = {r["live"].get("host_repo_id"): r for r in replace_by_slug.values()
                         if (r.get("live") or {}).get("host_repo_id")}
    saas_edges: dict[str, list] = {}
    for e in common.read_jsonl(common.DATASET / "relationships.jsonl"):
        saas_edges.setdefault(e["oss_id"], []).append(e)

    history: dict[str, list] = {}
    if HISTORY.exists():
        for row in common.read_jsonl(HISTORY):
            if row["as_of"] <= stamp:
                history.setdefault(row["id"], []).append(row)

    # 1. Resolve every evaluated slug to one entity. The host's numeric id is the key, so a
    #    renamed, moved or differently-cased address lands on the same record.
    entities: dict[str, dict] = {}
    rejections: list[dict] = []

    def reject(slug, reasons, stage, **extra):
        rejections.append({
            "candidate": slug, "reasons": reasons, "stage": stage, "as_of": stamp,
            "routes": routes.get(slug, []), **extra})

    for slug in evaluated:
        rs = routes[slug]
        rep = replace_by_slug.get(slug)
        meta = live["meta"].get(slug)
        if rep is not None:
            hid = (rep.get("live") or {}).get("host_repo_id")
            eid = f"github:{hid}" if hid else f"kei805:{rep['oss_id']}"
            ent = entities.setdefault(eid, {"id": eid, "slugs": set(), "routes": [], "replace": rep, "meta": None})
            ent["slugs"].add(slug)
            ent["routes"] += rs
            if meta and meta.get("found"):
                ent["meta"] = ent["meta"] or meta
            continue
        if meta is None:
            reject(slug, ["live_verification_failed"], "metadata", evidence={"error": "no_metadata_recorded"})
            continue
        if not meta.get("found"):
            reason = "repository_not_found" if meta.get("error") == "not_found" else "live_verification_failed"
            reject(slug, [reason], "metadata", evidence={"error": meta.get("error")})
            continue
        eid = f"github:{meta['database_id']}"
        ent = entities.setdefault(eid, {"id": eid, "slugs": set(), "routes": [], "replace": None, "meta": None})
        ent["slugs"].add(slug)
        ent["routes"] += rs
        ent["meta"] = ent["meta"] or meta
        if ent["replace"] is None and meta["database_id"] in replace_by_hostid:
            ent["replace"] = replace_by_hostid[meta["database_id"]]

    # Replace-lane projects hosted outside GitHub are not discovery candidates, but they
    # are part of the Replace lane and are carried as such.
    for oss_id, rep in sorted(replace_rows.items()):
        if not oss_id.startswith("github.com/"):
            eid = f"kei805:{oss_id}"
            entities[eid] = {"id": eid, "slugs": set(), "replace": rep, "meta": None,
                             "routes": [{"strategy": "replace_lane", "source": "kei805-dataset", "depth": 0,
                                         "via": None, "locator": oss_id}]}

    for slug in over_cap:
        reject(slug, ["discovery_budget_exceeded"], "universe_cap")
    for row in frontier["seeds"]["keith"]:
        if not row.get("slug"):
            rejections.append({"candidate": None, "seed_row": row, "reasons": ["seed_unresolved"],
                               "stage": "seed", "as_of": stamp, "routes": [],
                               "evidence": {"detail": row.get("unresolved_reason")}})

    # 2. Judge each entity once.
    projects: list[dict] = []
    edges: list[dict] = []
    nodes: dict[str, dict] = {}

    def node(nid, kind, **attrs):
        nodes.setdefault(nid, {"id": nid, "kind": kind, **attrs})
        return nid

    def edge(src, rel, dst, evidence):
        edges.append({"from": src, "relation": rel, "to": dst, "evidence": evidence})

    for eid in sorted(entities):
        ent = entities[eid]
        ent["routes"].sort(key=lambda r: (r["depth"], r["strategy"], r["source"], r["locator"] or ""))
        rs = ent["routes"]
        rep, meta = ent["replace"], ent["meta"]
        in_discover = any(r["strategy"] != "replace_lane" for r in rs)
        slugs = sorted(ent["slugs"])

        if rep is not None:
            # Replace lane: the KEI-805 verdict stands. It is referenced, not recomputed.
            d = rep["derived"]
            host = rep["oss_id"].split("/", 1)[0]
            canonical = f"{host}/" + (rep["live"].get("canonical_name") or rep["oss_id"].split("/", 1)[1]).lower()
            quality = {
                "basis": "kei805",
                "evaluated_as_of": d["evaluated_as_of"],
                "quality_lane": d["lane"],
                "exclusion_reasons": d["eligibility"]["exclusion_reasons"],
                "maintenance": d["maintenance"],
                "licence": ({k: d["licence"].get(k) for k in ("spdx", "recognised", "status", "usable")}
                            if d.get("licence") else None),
            }
            live_block = {k: rep["live"].get(k) for k in ("provider", "fetched_at", "found", "canonical_name",
                          "host_repo_id", "archived", "stars", "forks", "pushed_at", "created_at", "licence_spdx")}
            descriptive = {"description": (meta or {}).get("description"), "homepage": (meta or {}).get("homepage")}
            topics = (meta or {}).get("topics") or []
            languages = (meta or {}).get("languages") or []
            owner = (meta or {}).get("owner") or ({"login": rep["live"]["canonical_name"].split("/")[0], "type": None}
                                                  if rep["live"].get("canonical_name") else None)
            if d["lane"] == "rejected":
                reject(canonical.split("/", 1)[1] if canonical.startswith("github.com/") else canonical,
                       ["kei805_rejected"] + d["eligibility"]["exclusion_reasons"], "replace_lane",
                       entity_id=eid, aliases=slugs, evidence={"kei805_oss_id": rep["oss_id"]})
                continue
        else:
            canonical = "github.com/" + meta["canonical_slug"]
            reasons = prefilter(meta, window_start, cfg)
            dl = live["deep"].get(meta["canonical_slug"])
            if not reasons and (dl is None or not dl.get("found")):
                reasons = ["live_verification_failed"]
            if not reasons and dl.get("incomplete"):
                reasons = ["verification_incomplete"]
            if reasons:
                evidence = {k: meta.get(k) for k in ("archived", "disabled", "empty", "is_fork", "is_mirror",
                                                     "pushed_at", "private")}
                evidence["window_start"] = window_start
                if meta.get("parent"):
                    evidence["parent"] = meta["parent"]
                    evidence["parent_id"] = f"github:{meta['parent']['database_id']}"
                reject(meta["canonical_slug"], reasons, "prefilter", entity_id=eid, aliases=slugs, evidence=evidence)
                continue
            maintenance = evaluate(dl, rules, as_of)
            licence = licence_verdict(dl, policy, exceptions, canonical)
            verdict = eligibility(dl, maintenance, licence)
            licence["issue"] = licence_issue(dl, licence, {"declared_licences": []}, policy, lane_cfg)
            licence["status"] = ("exception_approved" if licence["exception"] and not licence["recognised"]
                                 else "recognised" if licence["recognised"] else "issue")
            qlane = lane_of(verdict)
            if qlane == "rejected":
                reject(meta["canonical_slug"], verdict["exclusion_reasons"], "quality_bar", entity_id=eid,
                       aliases=slugs, evidence={"maintenance": maintenance["status"],
                                                "last_meaningful_activity_at": maintenance["last_meaningful_activity_at"],
                                                "basis": maintenance["basis"], "licence_spdx": licence["spdx"]})
                continue
            quality = {"basis": "discover", "evaluated_as_of": stamp, "quality_lane": qlane,
                       "exclusion_reasons": verdict["exclusion_reasons"], "maintenance": maintenance,
                       "licence": licence}
            live_block = live_view(dl)
            descriptive = {"description": meta.get("description"), "homepage": meta.get("homepage")}
            topics, languages, owner = meta.get("topics") or [], meta.get("languages") or [], meta.get("owner")

        eligible = quality["quality_lane"] == "recommendable"
        aliases = sorted({"github.com/" + a for a in slugs} | {canonical})
        hist = sorted(history.get(eid, []), key=lambda r: r["as_of"])
        rec = {
            "schema": SCHEMA_ID,
            "id": eid,
            "canonical": canonical,
            "url": "https://" + canonical,
            "aliases": aliases,
            "lanes": {"replace": rep is not None, "discover": in_discover},
            "outcome": "recommendation_eligible" if eligible else "retained",
            "recommendation_eligible": eligible,
            "endorsed": False,
            "publication_status": "internal_not_published",
            "entry_reason": _entry_reason(rs),
            "provenance": rs,
            "discovery": {"depth": min(r["depth"] for r in rs), "distinct_routes": distinct_routes(rs),
                          "strategies": sorted({r["strategy"] for r in rs})},
            "quality": quality,
            "live": live_block,
            "descriptive": descriptive,
            "entities": {
                "owner": ({"id": f"github-owner:{owner['login'].lower()}", **owner} if owner else None),
                "topics": [f"github-topic:{t}" for t in topics],
                "languages": [f"language:{l}" for l in languages],
            },
            "replace": ({"kei805_oss_id": rep["oss_id"], "kei805_lane": rep["derived"]["lane"],
                         "saas_edge_ids": sorted(e["edge_id"] for e in saas_edges.get(rep["oss_id"], []))}
                        if rep is not None else None),
            "freshness": {
                "observed_as_of": stamp,
                "history": hist,
                "history_source": "data/discover/history/observations.jsonl (append-only; one row per verify run)",
            },
        }
        projects.append(rec)

        # Relationship candidates. Evidence, not conclusions: KEI-844 resolves them.
        if owner:
            oid = node(f"github-owner:{owner['login'].lower()}", "owner", login=owner["login"], owner_type=owner.get("type"))
            edge(eid, "owned_by", oid, {"basis": "host_api", "as_of": stamp})
        for t in topics:
            edge(eid, "has_topic", node(f"github-topic:{t}", "topic", name=t), {"basis": "host_api", "as_of": stamp})
        for i, l in enumerate(languages):
            edge(eid, "written_in", node(f"language:{l}", "language", name=l),
                 {"basis": "host_api", "rank_by_size": i + 1, "as_of": stamp})
        for r in rs:
            if r["strategy"] == "curated_list":
                lid = node(r["source"], "curated_list", name=r["source"].split(":", 1)[1])
                edge(eid, "listed_in", lid, {"locator": r["locator"]})
            elif r["strategy"] == "readme_links":
                edge(f"slug:{r['via'][0]}", "readme_links_to", eid, {"locator": r["locator"], "depth": r["depth"]})
            elif r["strategy"] == "topic_search":
                edge(eid, "discovered_via_topic", node(r["source"], "topic", name=r["source"].split(":", 1)[1]),
                     {"basis": "github_search"})
        if rep is not None:
            for e in saas_edges.get(rep["oss_id"], []):
                edge(eid, "saas_replacement_candidate", node(f"saas:{e['saas_id']}", "saas_product", kei805_id=e["saas_id"]),
                     {"kei805_edge_id": e["edge_id"], "kei805_lane": e["lane"],
                      "kei805_recommendation_eligible": e["recommendation_eligible"]})

    # README edges name the linking seed by address; point them at its entity where known.
    slug_to_id = {a.split("/", 1)[1]: p["id"] for p in projects for a in p["aliases"] if a.startswith("github.com/")}
    for r in rejections:
        for a in r.get("aliases") or []:
            slug_to_id.setdefault(a, r.get("entity_id"))
    for e in edges:
        if e["from"].startswith("slug:"):
            s = e["from"][5:]
            e["from"] = slug_to_id.get(s) or f"unresolved-slug:{s}"
    edges.sort(key=lambda e: (e["from"], e["relation"], e["to"], common.dumps(e["evidence"])))
    dedup, prev = [], None
    for e in edges:
        k = (e["from"], e["relation"], e["to"], common.dumps(e["evidence"]))
        if k != prev:
            dedup.append(e)
        prev = k
    edges = dedup

    rejections.sort(key=lambda r: (r["stage"], str(r.get("candidate")), common.dumps(r["reasons"])))
    projects.sort(key=lambda p: p["id"])

    # 3. Seed coverage: every row of Keith's corpus is accounted for.
    by_alias = {a.split("/", 1)[1]: p for p in projects for a in p["aliases"] if a.startswith("github.com/")}
    rej_by = {}
    for r in rejections:
        for a in [r.get("candidate")] + (r.get("aliases") or []):
            if a:
                rej_by.setdefault(a, r)
    coverage = []
    for row in frontier["seeds"]["keith"]:
        s = row.get("slug")
        if not s:
            coverage.append({**row, "outcome": "rejected", "reasons": ["seed_unresolved"]})
        elif s in by_alias:
            p = by_alias[s]
            coverage.append({**row, "outcome": p["outcome"], "id": p["id"], "canonical": p["canonical"],
                             "quality_lane": p["quality"]["quality_lane"],
                             "maintenance": (p["quality"]["maintenance"] or {}).get("status")})
        elif s in rej_by:
            coverage.append({**row, "outcome": "rejected", "reasons": rej_by[s]["reasons"],
                             "id": rej_by[s].get("entity_id")})
        else:
            coverage.append({**row, "outcome": "missing"})

    # 4. Counts.
    reason_counts: dict[str, int] = {}
    for r in rejections:
        for x in r["reasons"]:
            reason_counts[x] = reason_counts.get(x, 0) + 1
    strategy_counts: dict[str, int] = {}
    for rs in routes.values():
        for s in {r["strategy"] for r in rs}:
            strategy_counts[s] = strategy_counts.get(s, 0) + 1
    rejected_entities = {r.get("entity_id") for r in rejections if r.get("entity_id")}
    counts = {
        "raw_candidate_addresses": len(routes),
        "evaluated_addresses": len(evaluated),
        "raw_unique_repositories": len(entities) + sum(1 for r in rejections if not r.get("entity_id")
                                                       and r["stage"] in ("metadata", "universe_cap")),
        "resolved_unique_repositories": len(entities),
        "retained": len(projects),
        "recommendation_eligible": sum(p["recommendation_eligible"] for p in projects),
        "retained_licence_exception_lane": sum(p["quality"]["quality_lane"] == "licence_exception" for p in projects),
        "rejected_repositories": len(rejected_entities),
        "rejected_records": len(rejections),
        "rejection_reasons": dict(sorted(reason_counts.items())),
        "lanes": {
            "replace_only": sum(p["lanes"]["replace"] and not p["lanes"]["discover"] for p in projects),
            "discover_only": sum(p["lanes"]["discover"] and not p["lanes"]["replace"] for p in projects),
            "both": sum(p["lanes"]["replace"] and p["lanes"]["discover"] for p in projects),
            "discover_retained_without_saas_edge": sum(p["lanes"]["discover"] and not (p["replace"] or {}).get("saas_edge_ids")
                                                       for p in projects),
        },
        "candidates_by_strategy": dict(sorted(strategy_counts.items())),
        "candidates_by_depth": {str(d): sum(1 for rs in routes.values() if min(r["depth"] for r in rs) == d)
                                for d in (0, 1, 2)},
        "maintenance": {s: sum(1 for p in projects if (p["quality"]["maintenance"] or {}).get("status") == s)
                        for s in ("active", "maintained")},
        "keith_seed_rows": len(coverage),
        "keith_seed_outcomes": {o: sum(1 for c in coverage if c["outcome"] == o)
                                for o in sorted({c["outcome"] for c in coverage})},
        "relationship_candidates": len(edges),
        "relationship_candidates_by_relation": {rel: sum(1 for e in edges if e["relation"] == rel)
                                                for rel in sorted({e["relation"] for e in edges})},
        "nodes_by_kind": {k: sum(1 for n in nodes.values() if n["kind"] == k)
                          for k in sorted({n["kind"] for n in nodes.values()})},
    }

    sources = {
        "curated_lists": {k: {x: v.get(x) for x in ("decision", "reason", "licence_spdx", "commit", "readme_sha256")}
                          | {"links": len(v.get("links") or [])}
                          for k, v in frontier["strategies"]["curated_lists"].items()},
        "topic_search": {"ranked_topics": frontier["strategies"]["topic_search"]["ranked_topics"],
                         "queries": {t: {k: r[k] for k in ("query", "status", "total_count")}
                                     for t, r in frontier["strategies"]["topic_search"]["results"].items()}},
        "trending": {k: {x: v[x] for x in ("query", "status")} | {"results": len(v["repos"])}
                     for k, v in frontier["strategies"]["trending"].items()},
        "github_accounts": {k: {x: v.get(x) for x in ("starred_status", "watching_status")}
                            | {"starred": len(v["starred"]), "watching": len(v["watching"])}
                            for k, v in frontier["seeds"]["accounts"].items()},
        "owner_neighbourhood": {k: {"status": v["status"], "repos": len(v["repos"]), "via": v["via"]}
                                for k, v in frontier["strategies"]["owner_neighbourhood"].items()},
        "recursion": {"frontier": frontier["strategies"]["recursion"]["frontier"],
                      "owners": len(frontier["strategies"]["recursion"]["owner_neighbourhood"]),
                      "readmes": len(frontier["strategies"]["recursion"]["readme_links"])},
    }

    summary = {
        "schema": SCHEMA_ID,
        "as_of": stamp,
        "frontier_as_of": frontier["as_of"],
        "window_start": window_start,
        "publication_status": "internal_not_published",
        "endorsement": "none: discovery, retention and eligibility imply no publication, recommendation or endorsement",
        "inputs": {
            "discover_config_sha256": frontier["config_sha256"],
            "seed_file_sha256": frontier["seed_file_sha256"],
            "frontier_sha256": common.sha256_file(FRONTIER),
            "live_sha256": common.sha256_file(LIVE),
            "policy_sha256": _sha(policy),
            "kei805_manifest_sha256": common.sha256_file(common.DATASET / "MANIFEST.json"),
        },
        "counts": counts,
        "reason_meanings": REASONS,
        "field_origin": {
            "provenance": "the discovery routes that reached the repository, recorded at harvest",
            "live": "read from the host's API at live.fetched_at",
            "quality": "computed from live fields and config/policy.yaml (basis=discover) or carried from KEI-805 (basis=kei805)",
            "descriptive": "read from the host's API; free text written by the repository owner",
            "freshness.history": "append-only observations, one per verify run",
        },
    }

    import shutil
    if CORPUS.exists():
        shutil.rmtree(CORPUS)
    common.write_jsonl(CORPUS / "projects.jsonl", projects)
    common.write_jsonl(CORPUS / "rejections.jsonl", rejections)
    common.write_jsonl(CORPUS / "relationship_candidates.jsonl", edges)
    common.write_jsonl(CORPUS / "nodes.jsonl", [nodes[k] for k in sorted(nodes)])
    common.write_json(CORPUS / "seed_coverage.json", coverage)
    common.write_json(CORPUS / "sources.json", sources)
    common.write_json(CORPUS / "summary.json", summary)
    return summary


def write_manifest(stamp: str) -> dict:
    files = {str(p.relative_to(CORPUS)): common.sha256_file(p)
             for p in sorted(CORPUS.rglob("*")) if p.is_file() and p.name != "MANIFEST.json"}
    manifest = {"as_of": stamp, "schema": SCHEMA_ID, "publication_status": "internal_not_published", "sha256": files}
    common.write_json(CORPUS / "MANIFEST.json", manifest)
    return manifest
