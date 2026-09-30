"""Stage 4 — classify, decide eligibility, validate edges, select the launch corpus.

Pure: reads reconciled claims and the live snapshot, writes the dataset. The clock is
the snapshot's `as_of`, never the wall clock, so a rebuild is byte-identical.
"""
from __future__ import annotations

from . import common
from .activity import Rules, evaluate, parse_ts
from .licence_detect import detect as detect_licence
from .normalise import choose_name_entry, load_catalogue, merge_renamed, product_group, reconcile

EXECUTORS = {"claude", "claude-heavy", "claude-code", "draper", "jarvis", "archie", "pipeline", ""}

# Fields of a project record, by where the value came from (criterion 14).
IMPORTED_NOTE = "copied from an upstream list at harvest; never refreshed; each value names its source"
LIVE_NOTE = "read from the repository host's API by The Source at live.fetched_at; refreshed on every run"
DERIVED_NOTE = "computed by The Source from live fields and config/policy.yaml"


def load_exceptions() -> dict:
    doc = common.load_yaml(common.CONFIG / "licence_exceptions.yaml") or {}
    out = {}
    for e in doc.get("exceptions") or []:
        approver = str(e.get("approved_by") or "").strip().lower()
        if approver in EXECUTORS or not e.get("approval_ref"):
            raise ValueError(
                f"licence exception for {e.get('oss_id')} is not valid: it needs a human "
                "approver and an approval_ref"
            )
        out[e["oss_id"].lower()] = e
    return out


def licence_verdict(live: dict, policy: dict, exceptions: dict, oss_id: str) -> dict:
    allowed = set(policy["licence"]["recognised_spdx"])
    spdx = live.get("licence_spdx")
    recognised = spdx in allowed
    detection = {"method": "host_api", "recognised": recognised}
    probe = live.get("licence_probe")
    if not recognised and probe is not None:
        second = detect_licence(probe.get("root") or [], probe.get("texts") or {})
        second.pop("per_file_text", None)
        if second["recognised"] and not set(second["spdx_components"]) <= allowed:
            second["recognised"] = False
            second["refusal_reasons"].append("matched_licence_not_in_recognised_list")
        detection = second
        if second["recognised"]:
            recognised = True
            spdx = second["spdx"]
    exc = exceptions.get(oss_id)
    return {
        "spdx": spdx,
        "spdx_reported_by_host": live.get("licence_spdx"),
        "name_as_reported": live.get("licence_name"),
        "detection": detection,
        "recognised": recognised,
        "exception": (
            {"approved_by": exc["approved_by"], "approval_ref": exc["approval_ref"]} if exc else None
        ),
        "usable": bool(recognised or exc),
    }


def licence_issue(live: dict, licence: dict, imported: dict, policy: dict, lane: dict) -> dict | None:
    """Describe what is wrong with a project's licence, for the licence-exception lane.

    Returns None when the licence is recognised. Otherwise a record that states the
    category, the exact reason, what was detected, what upstream lists claimed, and the
    evidence, so the issue cannot be missed or mistaken for a recommendation.
    """
    if licence["recognised"]:
        return None
    allowed = set(policy["licence"]["recognised_spdx"])
    det = licence["detection"]
    probe = live.get("licence_probe")
    per_file = det.get("per_file") or {}
    markers = sorted({m for r in per_file.values() for m in r.get("restriction_markers") or []})
    enterprise_dirs = det.get("enterprise_directories") or []
    files = det.get("licence_files") or []
    host_spdx = live.get("licence_spdx")
    claims = sorted({c["value"] for c in imported["declared_licences"]})
    claimed_open = sorted(c for c in claims if c in allowed)
    sa = [m for m in markers if m in lane["source_available_markers"]]
    oc = [m for m in markers if m in lane["open_core_markers"]]
    matched = sorted({r["spdx"] for r in per_file.values() if r.get("spdx") and r.get("coverage", 0) >= 0.9})

    if probe is None or probe.get("error"):
        category = "undetermined"
        reason = "the repository's licence files could not be read" + (
            f" ({probe['error']})" if probe and probe.get("error") else " on this host")
    elif not files and host_spdx in (None, "NOASSERTION"):
        category = "missing"
        reason = "no licence file at the repository root and the host reports no licence"
    elif sa:
        category = "source_available"
        reason = "licence text contains source-available or use-restricted terms: " + ", ".join(sa)
    elif enterprise_dirs or oc:
        category = "open_core"
        parts = []
        if enterprise_dirs:
            parts.append("enterprise directory at the repository root: " + ", ".join(enterprise_dirs))
        if oc:
            parts.append("licence text scopes or splits the licence: " + ", ".join(oc))
        reason = "; ".join(parts)
    elif markers:
        category = "restricted"
        reason = "licence text adds or removes terms: " + ", ".join(markers)
    elif host_spdx not in (None, "NOASSERTION"):
        category = "non_osi"
        reason = f"the host identifies the licence as {host_spdx}, which is not on the recognised open-source list"
    elif claimed_open:
        category = "conflicting"
        reason = ("upstream lists declare " + ", ".join(claimed_open)
                  + "; the repository's licence files do not match that licence as a plain standard text")
    else:
        category = "unrecognised"
        reason = "licence text is present and matches no reference licence closely enough"
    exc = licence["exception"]
    return {
        "category": category,
        "category_meaning": lane["categories"][category],
        "reason": reason,
        "detected": {
            "host_spdx": host_spdx,
            "host_licence_name": live.get("licence_name"),
            "licence_files": files,
            "closest_standard_licences": matched,
            "enterprise_directories": enterprise_dirs,
            "restriction_markers": markers,
        },
        "upstream_claims": [dict(c) for c in imported["declared_licences"]],
        "upstream_claims_disagree_with_repository": bool(claimed_open),
        "evidence": {
            "basis": "licence files read from the repository host at live.fetched_at",
            "fetched_at": live.get("fetched_at"),
            "per_file": {
                name: {k: r.get(k) for k in ("spdx", "coverage", "leftover_chars", "restriction_markers", "matched", "reason")
                       if k in r}
                for name, r in sorted(per_file.items())
            },
        },
        "review_status": "exception_approved" if exc else "flagged_retained_not_reviewed_individually",
        "retained_under": lane["retained_under"],
        "named_exception": exc,
        "reuse_rights_established": False,
    }


def identity_changed(oss_id: str, project: dict) -> bool:
    """True if a repository was renamed into something its listings never described.

    Lists point at a repository by URL. If that URL now redirects to a repository whose
    name and owner share nothing with any name it was listed under, the project at the
    other end may no longer be the product the list described.
    """
    from .normalise import name_key

    harvested = [h for h in project["harvested_as"] if h != oss_id]
    if not harvested:
        return False
    confirmed = (common.load_yaml(common.CONFIG / "repo_overrides.yaml") or {}).get("confirmed_renames") or {}
    if oss_id in {k.lower() for k in confirmed}:
        return False
    owner, repo = (name_key(x) for x in oss_id.split("/")[1:3])
    known = {name_key(n["value"]) for n in project["imported"]["names"]}
    known |= {name_key(h.split("/")[-1]) for h in harvested}
    known = {k for k in known if len(k) >= 3}
    for mine in (repo, owner):
        if len(mine) >= 3 and any(mine in k or k in mine for k in known):
            return False
    return True


def eligibility(live: dict, maintenance: dict | None, licence: dict | None, drifted: bool = False) -> dict:
    """Decide whether a project may be recommended. Stars are not consulted."""
    reasons = []
    if not live.get("found"):
        err = live.get("error") or "unknown"
        if err == "unsupported_host":
            reasons.append("host_not_independently_verifiable")
        elif err == "not_found":
            reasons.append("repository_not_found")
        else:
            reasons.append("live_verification_failed")
        return {"recommended": False, "exclusion_reasons": reasons}
    if live.get("archived"):
        reasons.append("archived")
    if live.get("disabled"):
        reasons.append("disabled")
    if live.get("empty"):
        reasons.append("empty_repository")
    if not licence["usable"]:
        reasons.append("no_recognised_open_source_licence")
    if maintenance["status"] == "stale":
        reasons.append("stale_no_meaningful_activity_12_months")
    if drifted:
        reasons.append("repository_identity_changed_since_listed")
    return {"recommended": not reasons, "exclusion_reasons": reasons}


def _tally(values) -> dict:
    out: dict = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def lane_of(verdict: dict) -> str:
    """recommendable | licence_exception | rejected.

    The licence-exception lane holds projects whose ONLY shortcoming is the licence.
    A project that is also archived, stale, missing or repurposed is rejected.
    """
    if verdict["recommended"]:
        return "recommendable"
    if verdict["exclusion_reasons"] == ["no_recognised_open_source_licence"]:
        return "licence_exception"
    return "rejected"


def live_view(live: dict) -> dict:
    """The live block published on a project record (observations summarised)."""
    keep = (
        "provider", "fetched_at", "found", "error", "canonical_name", "host_repo_id", "archived",
        "disabled", "empty", "is_fork", "is_mirror", "licence_spdx", "licence_name", "stars",
        "forks", "pushed_at", "created_at", "default_branch",
    )
    out = {k: live.get(k) for k in keep if k in live}
    obs = live.get("observations") or {}
    out["observed"] = {
        "commits_in_12m_window": obs.get("commits_in_window"),
        "commits_inspected": len(obs.get("commits") or []),
        "merged_prs_inspected": len(obs.get("merged_prs") or []),
        "releases_seen": len(obs.get("releases") or []),
        "tags_seen": len(obs.get("tags") or []),
        "issues_closed_12m": obs.get("issues_closed_12m"),
    }
    return out


def select_launch(saas: dict, edges: list[dict], projects: dict, policy: dict) -> dict:
    launch = policy["launch"]
    valid = [e for e in edges if e["validation"]["status"] == "validated"]
    per_saas: dict[str, list] = {}
    for e in valid:
        per_saas.setdefault(e["saas_id"], []).append(e)

    def saas_rank(sid):
        return (-len(saas[sid]["source_ids"]), -len(per_saas[sid]), saas[sid]["name"].lower())

    ranked = sorted(per_saas, key=saas_rank)
    floor = int(launch["saas_target"])

    def edge_rank(e):
        last = projects[e["oss_id"]]["derived"]["maintenance"]["last_meaningful_activity_at"]
        # more independent sources first, then most recent meaningful activity, then id
        return (-e["source_count"], -parse_ts(last).timestamp(), e["oss_id"])

    def distinct_products(sid):
        """One repository per product per SaaS: later repositories of the same product are skipped."""
        seen, out = set(), []
        for e in sorted(per_saas[sid], key=edge_rank):
            group = projects[e["oss_id"]]["product_group"]
            if group in seen:
                continue
            seen.add(group)
            out.append(e)
        return out

    for sid in ranked:
        per_saas[sid] = distinct_products(sid)

    def measure(chosen):
        cap = launch.get("max_edges_per_saas")
        if cap is None:
            cap = max((len(per_saas[s]) for s in chosen), default=0)
            while cap > 1 and sum(min(len(per_saas[s]), cap) for s in chosen) > int(launch["edges_max"]):
                cap -= 1
        kept = [e for s in chosen for e in per_saas[s][:cap]]
        products = {projects[e["oss_id"]]["product_group"] for e in kept}
        return cap, len(kept), len(products)

    chosen = ranked[:floor]
    extended_by = 0
    if launch.get("saas_target_is", "fixed") == "floor":
        # Continue down the SAME ranking, one product at a time, until the OSS minimum is
        # reached. Stop early rather than exceed edges_max or shrink the per-SaaS cap.
        base_cap = measure(chosen)[0]
        while len(chosen) < len(ranked) and measure(chosen)[2] < int(launch["oss_minimum"]):
            trial = chosen + [ranked[len(chosen)]]
            cap, n_edges, _ = measure(trial)
            if n_edges > int(launch["edges_max"]) or cap < base_cap:
                break
            chosen = trial
            extended_by += 1
    cap = measure(chosen)[0]
    picked = {}
    for rank, sid in enumerate(chosen, 1):
        for pos, e in enumerate(per_saas[sid][:cap], 1):
            picked[e["edge_id"]] = {"saas_rank": rank, "position": pos}
    return {
        "saas_ranked": chosen,
        "edge_picks": picked,
        "cap_per_saas": cap,
        "saas_floor": floor,
        "saas_added_beyond_floor": extended_by,
        "saas_target_is": launch.get("saas_target_is", "fixed"),
        "saas_with_validated_edges": len(ranked),
    }


def run() -> dict:
    policy = common.load_policy()
    sources_cfg = common.load_sources()
    rules = Rules(policy)
    exceptions = load_exceptions()
    lane_cfg = common.load_yaml(common.CONFIG / "licence_lane.yaml")
    snapshot = common.read_json(common.LIVE / "snapshot.json")
    from .enrich import policy_fingerprint

    if snapshot.get("maintenance_policy_sha256") != policy_fingerprint(policy):
        # Live verification stops inspecting a repository at the first change that
        # qualifies under the policy in force. Observations gathered under one policy
        # cannot be judged under another.
        raise RuntimeError(
            "the maintenance policy has changed since the live snapshot was taken; "
            "run `python -m source_pipeline verify` (or `run`) to observe again"
        )
    if snapshot.get("lookup_failures"):
        # A lookup that failed looks exactly like a repository with nothing to show.
        raise RuntimeError(
            f"{len(snapshot['lookup_failures'])} host lookups failed during live verification "
            f"(e.g. {snapshot['lookup_failures'][:3]}); the snapshot is not trusted. Run verify again."
        )
    as_of = parse_ts(snapshot["as_of"])
    claims = common.read_jsonl(common.STAGING / "claims.jsonl")
    harvest_stats = common.read_json(common.STAGING / "harvest_stats.json")

    rec = reconcile(claims, sources_cfg)
    missing = sorted(set(rec["oss"]) - set(snapshot["projects"]))
    if missing:
        raise RuntimeError(f"{len(missing)} candidate projects have no live observation, e.g. {missing[:3]}")

    canonical = {
        key: live["canonical_id"]
        for key, live in snapshot["projects"].items()
        if live.get("found") and live.get("canonical_id")
    }
    live_by_canonical = {}
    for key in sorted(snapshot["projects"]):
        live_by_canonical.setdefault(canonical.get(key, key), snapshot["projects"][key])
    merged = merge_renamed(rec, canonical)

    rejections = list(rec["rejections"])
    projects = {}
    for oss_id, project in merged["oss"].items():
        live = live_by_canonical[oss_id]
        maintenance = evaluate(live, rules, as_of) if live.get("found") else None
        licence = licence_verdict(live, policy, exceptions, oss_id) if live.get("found") else None
        verdict = eligibility(live, maintenance, licence, identity_changed(oss_id, project))
        if licence is not None:
            issue = licence_issue(live, licence, project["imported"], policy, lane_cfg)
            licence["status"] = (
                "exception_approved" if licence["exception"] and not licence["recognised"]
                else "recognised" if licence["recognised"] else "issue")
            licence["issue"] = issue
        host = oss_id.split("/", 1)[0]
        projects[oss_id] = {
            "schema_version": common.SCHEMA_VERSION,
            "oss_id": oss_id,
            "name": project["name"],
            "name_origin": {"block": "imported",
                            "source_id": choose_name_entry(oss_id, project["imported"]["names"])["source_id"]},
            "product_group": product_group(oss_id, project["name"]),
            "host": host,
            "repo_url": "https://" + (
                f"{host}/{live['canonical_name']}" if live.get("canonical_name") else oss_id
            ),
            "harvested_as": project["harvested_as"],
            "imported": project["imported"],
            "live": live_view(live),
            "derived": {
                "evaluated_as_of": snapshot["as_of"],
                "maintenance": maintenance,
                "licence": licence,
                "eligibility": verdict,
                "recommendation_eligible": verdict["recommended"],
                "lane": lane_of(verdict),
                "launch": False,
            },
            "provenance": project["provenance"],
        }
        if not verdict["recommended"]:
            rejections.append(
                {"stage": "eligibility", "reason": ";".join(verdict["exclusion_reasons"]),
                 "oss_id": oss_id, "as_of": snapshot["as_of"]}
            )

    allowed_sources = {
        s["source_id"] for s in sources_cfg["sources"] if s["decision"] in ("allow", "allow_with_conditions")
    }
    review = common.load_yaml(common.CONFIG / "edge_review.yaml")
    review_rejections = {(r["saas"], r["oss"].lower()): r["reason"] for r in review["rejected"]}
    unknown = sorted(k for k in review_rejections
                     if k[0] not in rec["saas"] and k[0] not in load_catalogue()[0])
    if unknown:
        raise ValueError(f"edge_review.yaml names SaaS ids that do not exist: {unknown}")
    edges = []
    for e in merged["edges"]:
        project = projects[e["oss_id"]]
        refusal = review_rejections.get((e["saas_id"], e["oss_id"]))
        if refusal:
            rejections.append({"stage": "plausibility_review", "reason": refusal, "oss_id": e["oss_id"],
                               "saas_id": e["saas_id"], "reviewed_by": review["reviewed_by"],
                               "reviewed_at": review["reviewed_at"]})
        checks = {
            "passed_plausibility_review": refusal is None,
            "saas_in_reviewed_catalogue": e["saas_id"] in rec["saas"],
            "provenance_present": bool(e["provenance"]),
            "all_sources_licence_allowed": all(p["source_id"] in allowed_sources for p in e["provenance"]),
            "oss_live_verified": bool(project["live"].get("found")),
            "oss_recommendable": project["derived"]["eligibility"]["recommended"],
        }
        ok = all(checks.values())
        failed = sorted(k for k, v in checks.items() if not v)
        # Flagged: nothing is wrong with the relationship except the project's licence.
        flagged = failed == ["oss_recommendable"] and project["derived"]["lane"] == "licence_exception"
        edges.append(
            {
                "schema_version": common.SCHEMA_VERSION,
                "edge_id": f"{e['saas_id']}--{e['oss_id']}",
                "saas_id": e["saas_id"],
                "oss_id": e["oss_id"],
                "relation": e["relation"],
                "provenance": e["provenance"],
                "source_ids": e["source_ids"],
                "source_count": e["source_count"],
                "corroborated": e["source_count"] >= 2,
                "share_alike_only": e["share_alike_only"],
                "lane": "recommendable" if ok else ("licence_exception" if flagged else "rejected"),
                "recommendation_eligible": ok,
                "validation": {
                    "status": "validated" if ok else ("flagged_licence_exception" if flagged else "rejected"),
                    "method": "provenance + reviewed catalogue + independent live repository verification + desk review of plausibility; not a test of fitness",
                    "checks": checks,
                    "reasons": sorted(k for k, v in checks.items() if not v)
                    + ([] if ok else project["derived"]["eligibility"]["exclusion_reasons"])
                    + ([f"review: {refusal}"] if refusal else []),
                    "plausibility_review": {"reviewed_by": review["reviewed_by"],
                                            "reviewed_at": review["reviewed_at"],
                                            "outcome": ("rejected" if refusal else
                                                        "left standing" if ok else "not reviewed")},
                    "validated_as_of": snapshot["as_of"],
                },
                "launch": False,
            }
        )

    sel = select_launch(rec["saas"], edges, projects, policy)
    launch_oss = set()
    for e in edges:
        pick = sel["edge_picks"].get(e["edge_id"])
        if pick:
            e["launch"] = True
            e["launch_position"] = pick
            launch_oss.add(e["oss_id"])
    for oss_id in launch_oss:
        projects[oss_id]["derived"]["launch"] = True

    validated_per_saas: dict[str, int] = {}
    for e in edges:
        if e["validation"]["status"] == "validated":
            validated_per_saas[e["saas_id"]] = validated_per_saas.get(e["saas_id"], 0) + 1
    raw_spellings: dict[str, set] = {}
    for e in edges:
        for p in e["provenance"]:
            raw_spellings.setdefault(e["saas_id"], set()).add(p["claimed_saas_name"])
    saas_rows = []
    for sid, product in rec["saas"].items():
        rank = sel["saas_ranked"].index(sid) + 1 if sid in sel["saas_ranked"] else None
        saas_rows.append(
            {
                "schema_version": common.SCHEMA_VERSION,
                "saas_id": sid,
                "name": product["name"],
                "domain": product["domain"],
                "category": product["category"],
                "aliases": product["aliases"],
                "record_origin": "reviewed_catalogue",
                "imported": {
                    "named_by_sources": product["source_ids"],
                    "spellings_seen": sorted(raw_spellings.get(sid, [])),
                },
                "derived": {
                    "source_count": len(product["source_ids"]),
                    "validated_alternatives": validated_per_saas.get(sid, 0),
                    "launch": rank is not None,
                    "launch_rank": rank,
                },
            }
        )

    source_rows = []
    for s in sorted(sources_cfg["sources"], key=lambda s: s["priority"]):
        row = {k: v for k, v in s.items() if k not in ("parser", "parse_path")}
        row["schema_version"] = common.SCHEMA_VERSION
        row["reviewed_at"] = sources_cfg["reviewed_at"]
        row["reviewed_by"] = sources_cfg["reviewed_by"]
        row["harvest"] = harvest_stats.get(s["source_id"], {})
        if s.get("kind") == "intake":
            # The ledger as harvested, not as it is now: an admission changes the dataset
            # only when a cycle harvests it (KEI-807).
            row["pinned_commit"] = row["harvest"].get("pinned_commit", row["pinned_commit"])
        source_rows.append(row)

    edges.sort(key=lambda e: e["edge_id"])
    project_rows = [projects[k] for k in sorted(projects)]
    rejections.sort(key=lambda r: (r["stage"], r["reason"], r.get("oss_id") or r.get("repo_url") or "", r.get("saas_id", ""),
                                   r.get("source_id", ""), r.get("locator", ""), r.get("saas_name", "")))

    common.write_json(common.DATASET / "sources.json", source_rows)
    common.write_jsonl(common.DATASET / "saas_products.jsonl", saas_rows)
    common.write_jsonl(common.DATASET / "oss_projects.jsonl", project_rows)
    common.write_jsonl(common.DATASET / "relationships.jsonl", edges)
    common.write_jsonl(common.DATASET / "rejections.jsonl", rejections)

    lane_rows = [p for p in project_rows if p["derived"]["lane"] == "licence_exception"]
    candidates = [
        {
            "oss_id": p["oss_id"], "name": p["name"], "repo_url": p["repo_url"],
            "recommendation_eligible": False,
            "issue_category": p["derived"]["licence"]["issue"]["category"],
            "reason": p["derived"]["licence"]["issue"]["reason"],
            "detected": p["derived"]["licence"]["issue"]["detected"],
            "upstream_claims": p["imported"]["declared_licences"],
            "review_status": p["derived"]["licence"]["issue"]["review_status"],
            "maintenance": p["derived"]["maintenance"]["status"],
            "relationships_flagged": sum(1 for e in edges if e["oss_id"] == p["oss_id"]
                                         and e["lane"] == "licence_exception"),
        }
        for p in lane_rows
    ]
    candidates.sort(key=lambda c: (-c["relationships_flagged"], c["oss_id"]))
    common.write_json(common.REPORTS / "licence_exception_candidates.json", candidates)
    common.write_json(common.REPORTS / "uncatalogued_saas_names.json", rec["uncatalogued"])

    def count(rows, pred):
        return sum(1 for r in rows if pred(r))

    reason_counts: dict[str, int] = {}
    for r in rejections:
        k = f"{r['stage']}:{r['reason']}"
        reason_counts[k] = reason_counts.get(k, 0) + 1
    status_counts: dict[str, int] = {}
    for p in project_rows:
        m = p["derived"]["maintenance"]
        k = m["status"] if m else "unverified"
        status_counts[k] = status_counts.get(k, 0) + 1

    launch_edges = [e for e in edges if e["launch"]]
    summary = {
        "schema_version": common.SCHEMA_VERSION,
        "as_of": snapshot["as_of"],
        "publication_status": (common.load_yaml(common.CONFIG / "publication.yaml") or {}).get("status", "internal_not_published"),
        "field_origin": {"imported": IMPORTED_NOTE, "live": LIVE_NOTE, "derived": DERIVED_NOTE},
        "harvest": {
            "entries_read": sum(s.get("entries", 0) for s in harvest_stats.values()),
            "relationship_claims_read": sum(s.get("relationship_claims", 0) for s in harvest_stats.values()),
            "by_source": harvest_stats,
        },
        "reconciled": {
            "oss_candidates_before_identity_merge": len(rec["oss"]),
            "oss_candidates": len(project_rows),
            "renamed_or_duplicate_repositories_merged": len(rec["oss"]) - len(project_rows),
            "saas_products_named": len(saas_rows),
            "relationship_candidates": len(edges),
        },
        "verified": {
            "oss_recommendable": count(project_rows, lambda p: p["derived"]["eligibility"]["recommended"]),
            "oss_rejected": count(project_rows, lambda p: not p["derived"]["eligibility"]["recommended"]),
            "maintenance_status": dict(sorted(status_counts.items())),
            "relationships_validated": count(edges, lambda e: e["validation"]["status"] == "validated"),
            "relationships_flagged_licence_exception": count(
                edges, lambda e: e["validation"]["status"] == "flagged_licence_exception"),
            "relationships_rejected": count(edges, lambda e: e["validation"]["status"] == "rejected"),
            "saas_with_validated_alternatives": sel["saas_with_validated_edges"],
        },
        # Reported apart from the recommendable corpus and from the launch set, so that
        # neither figure is inflated by the other.
        "licence_exception_lane": {
            "note": "retained and flagged; recommendation_eligible is false for every record; "
                    "none is in the launch set or in any export; retention establishes no reuse rights",
            "retained_under": lane_cfg["retained_under"],
            "oss_projects": len(lane_rows),
            "by_issue_category": dict(sorted(_tally(
                p["derived"]["licence"]["issue"]["category"] for p in lane_rows).items())),
            "relationships_flagged": count(edges, lambda e: e["lane"] == "licence_exception"),
            "saas_products_touched": len({e["saas_id"] for e in edges if e["lane"] == "licence_exception"}),
            "named_exceptions_approved": count(project_rows, lambda p: bool(
                p["derived"]["licence"] and p["derived"]["licence"]["exception"])),
            "oss_with_licence_issue_also_rejected_for_other_reasons": count(
                project_rows, lambda p: p["derived"]["lane"] == "rejected" and bool(
                    p["derived"]["licence"] and p["derived"]["licence"]["issue"])),
        },
        "lanes": {
            "oss_projects": dict(sorted(_tally(p["derived"]["lane"] for p in project_rows).items())),
            "relationships": dict(sorted(_tally(e["lane"] for e in edges).items())),
        },
        "launch": {
            "targets": {k: policy["launch"][k] for k in ("saas_target", "oss_minimum", "edges_min", "edges_max")},
            "saas_products": len(sel["saas_ranked"]),
            "oss_projects": len(launch_oss),
            "oss_distinct_products": len({projects[o]["product_group"] for o in launch_oss}),
            "relationships": len(launch_edges),
            "relationships_corroborated_by_2plus_sources": count(launch_edges, lambda e: e["corroborated"]),
            "relationships_share_alike_only": count(launch_edges, lambda e: e["share_alike_only"]),
            "cap_per_saas": sel["cap_per_saas"],
            "saas_target_is": sel["saas_target_is"],
            "saas_target_decision_ref": policy["launch"].get("saas_target_decision_ref"),
            "saas_added_beyond_floor": sel["saas_added_beyond_floor"],
            "saas_named_by_a_single_source": count(
                saas_rows, lambda r: r["derived"]["launch"] and r["derived"]["source_count"] == 1),
        },
        "rejections": {"total": len(rejections), "by_reason": dict(sorted(reason_counts.items()))},
    }
    t = summary["launch"]
    summary["launch"]["targets_met"] = {
        "saas_products": (t["saas_products"] >= policy["launch"]["saas_target"]
                          if sel["saas_target_is"] == "floor"
                          else t["saas_products"] == policy["launch"]["saas_target"]),
        "oss_projects": min(t["oss_projects"], t["oss_distinct_products"]) >= policy["launch"]["oss_minimum"],
        "relationships": policy["launch"]["edges_min"] <= t["relationships"] <= policy["launch"]["edges_max"],
    }
    common.write_json(common.DATASET / "summary.json", summary)

    # A validation report left by an earlier build describes a different dataset.
    stale = common.REPORTS / "validation_report.json"
    if stale.exists():
        stale.unlink()
    common.write_manifest(snapshot["as_of"])
    return summary
