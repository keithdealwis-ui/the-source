"""Stage 5 — validate the built dataset against the schema and the ticket's rules.

Each check is tied to the KEI-805 acceptance criterion it gives evidence for. The
report states what was checked and what was found; it does not decide acceptance.
"""
from __future__ import annotations

import jsonschema

from . import build, common
from .activity import Rules, evaluate, parse_ts


def run() -> dict:
    schema = common.read_json(common.SCHEMA / "the-source.schema.json")
    policy = common.load_policy()
    rules = Rules(policy)
    sources = common.read_json(common.DATASET / "sources.json")
    saas = common.read_jsonl(common.DATASET / "saas_products.jsonl")
    oss = common.read_jsonl(common.DATASET / "oss_projects.jsonl")
    edges = common.read_jsonl(common.DATASET / "relationships.jsonl")
    summary = common.read_json(common.DATASET / "summary.json")
    as_of = parse_ts(summary["as_of"])

    checks = []

    def check(cid, criterion, description, failures, observed=None):
        failures = list(failures)
        checks.append(
            {"id": cid, "criterion": criterion, "kind": "assertion", "description": description,
             "ok": not failures, "failures": len(failures), "examples": failures[:5], "observed": observed}
        )

    def observe(cid, criterion, description, observed):
        """A recorded figure. It asserts nothing and is not counted as a passing check."""
        observations.append({"id": cid, "criterion": criterion, "kind": "observation",
                             "description": description, "observed": observed})

    observations = []
    snapshot = common.read_json(common.LIVE / "snapshot.json")
    raw_live = {}
    for key, lv in snapshot["projects"].items():
        raw_live.setdefault(lv.get("canonical_id") or key, lv)
    exceptions = build.load_exceptions()
    staged = common.read_jsonl(common.STAGING / "claims.jsonl")
    cfg = common.load_sources()

    def schema_errors(rows, name, ident):
        v = jsonschema.Draft202012Validator(
            {"$ref": f"#/$defs/{name}", "$defs": schema["$defs"]},
            format_checker=jsonschema.FormatChecker(),
        )
        out = []
        for row in rows:
            for err in v.iter_errors(row):
                out.append(f"{row.get(ident)}: {err.message[:160]}")
        return out

    check("schema.source", 1, "every source record conforms to the schema",
          schema_errors(sources, "source", "source_id"), len(sources))
    check("schema.saas", 1, "every SaaS product record conforms to the schema",
          schema_errors(saas, "saas_product", "saas_id"), len(saas))
    check("schema.oss", 1, "every OSS project record conforms to the schema",
          schema_errors(oss, "oss_project", "oss_id"), len(oss))
    check("schema.edge", 1, "every relationship record conforms to the schema",
          schema_errors(edges, "relationship", "edge_id"), len(edges))

    src_by_id = {s["source_id"]: s for s in sources}
    oss_by_id = {p["oss_id"]: p for p in oss}
    saas_by_id = {s["saas_id"]: s for s in saas}

    bad = []
    for e in edges:
        for p in e["provenance"]:
            s = src_by_id.get(p["source_id"])
            if not s or p["source_commit"] != s["pinned_commit"] or not p["locator"]:
                bad.append(e["edge_id"])
        if not e["provenance"]:
            bad.append(e["edge_id"])
    check("provenance.edge", 2, "every relationship carries provenance naming a registered source, its pinned commit and a locator", bad, len(edges))
    bad = [p["oss_id"] for p in oss if not p["provenance"]
           or any(x["source_id"] not in src_by_id for x in p["provenance"])]
    check("provenance.oss", 2, "every OSS project carries provenance to a registered source", bad, len(oss))
    bad = [p["oss_id"] for p in oss for f in p["imported"].values() for v in f if v["source_id"] not in src_by_id]
    check("provenance.imported_values", 2, "every imported value names the source it was copied from", bad)

    bad = [s["source_id"] for s in sources
           if s["decision"] not in ("allow", "allow_with_conditions", "refuse") or len(s["rationale"]) < 40
           or not s["licence_file_sha256"]]
    check("licence.sources_decided", 3, "every upstream has a licence decision with rationale, pinned to a licence-file hash", bad, len(sources))
    bad = [s["source_id"] for s in sources
           if s["harvest"].get("harvested") and s["harvest"].get("licence_file_sha256") != s["licence_file_sha256"]]
    check("licence.hash_verified_at_harvest", 3, "the licence file read at harvest is the one the decision was made about", bad)
    allowed_ids = {x["source_id"] for x in cfg["sources"] if x["decision"] in ("allow", "allow_with_conditions")}
    refused_repos = {x["repo"].lower() for x in cfg["sources"] if x["decision"] == "refuse"}
    refused_repos |= {x["repo"].lower() for x in cfg.get("investigated_not_harvested") or []
                      if x["decision"] == "refuse"}
    bad = sorted({c["source_id"] for c in staged if c["source_id"] not in allowed_ids})
    bad += sorted(x["repo"] for x in sources if x["repo"].lower() in refused_repos)
    bad += sorted(str(d.relative_to(common.RAW)) for d in common.RAW.glob("*/*/.complete")
                  if d.parent.parent.name.replace("_", "/", 1).lower() in refused_repos)
    check("licence.refused_not_harvested", 3,
          "no staged claim comes from a source that is not licence-cleared, and no refused repository was fetched or registered",
          bad, {"staged_claims": len(staged), "refused_repositories": sorted(refused_repos)})

    for name, rows, ident in (("saas", saas, "saas_id"), ("oss", oss, "oss_id"), ("edge", edges, "edge_id")):
        ids = [r[ident] for r in rows]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        check(f"dedupe.{name}_ids_unique", 5, f"no duplicate {ident}", dupes, len(ids))
    repo_ids = [(p["host"], p["live"].get("host_repo_id")) for p in oss if p["live"].get("host_repo_id")]
    dupes = sorted({str(i) for i in repo_ids if repo_ids.count(i) > 1})
    check("dedupe.one_record_per_repository", 5, "no two project records point at the same repository on the host", dupes, len(repo_ids))
    pairs = [(e["saas_id"], e["oss_id"]) for e in edges]
    check("dedupe.one_edge_per_pair", 5, "one relationship per (SaaS, OSS) pair", sorted({str(p) for p in pairs if pairs.count(p) > 1}))
    bad = [e["edge_id"] for e in edges if e["saas_id"] not in saas_by_id or e["oss_id"] not in oss_by_id]
    check("integrity.edge_endpoints", 5, "every relationship resolves to a SaaS record and an OSS record", bad)

    rec = [p for p in oss if p["derived"]["eligibility"]["recommended"]]
    launch_oss = [p for p in oss if p["derived"]["launch"]]
    launch_edges = [e for e in edges if e["launch"]]
    launch_saas = [s for s in saas if s["derived"]["launch"]]

    def days(p):
        return (as_of - parse_ts(p["derived"]["maintenance"]["last_meaningful_activity_at"])).days

    bad = [p["oss_id"] for p in rec if p["derived"]["maintenance"]["status"] not in ("active", "maintained")
           or days(p) > rules.maintained_days]
    check("activity.recommended_within_12_months", 7, "every recommended project has meaningful activity within 12 months", bad, len(rec))
    # Re-derived from the RAW observations in the live snapshot, not from the evidence
    # summary the build wrote: the change or release cited must exist in the snapshot,
    # carry the cited date, and pass the rules when judged from its full path list.
    bad = []
    for p in rec:
        obs = (raw_live.get(p["oss_id"]) or {}).get("observations") or {}
        m = p["derived"]["maintenance"]
        if not m["evidence"]:
            bad.append(f"{p['oss_id']}: no evidence")
        for ev in m["evidence"]:
            pool = {"code_commit": "commits", "merged_pr": "merged_prs", "release": "releases", "tag": "tags"}[ev["kind"]]
            raw = [o for o in obs.get(pool) or [] if o.get("ref") == ev["ref"]]
            if not raw:
                bad.append(f"{p['oss_id']}: {ev['kind']} {ev['ref']} is not in the snapshot")
                continue
            raw = raw[0]
            if parse_ts(raw["at"]).replace(microsecond=0) != parse_ts(ev["at"]):
                bad.append(f"{p['oss_id']}: {ev['kind']} {ev['ref']} date differs from the snapshot")
            paths = raw.get("paths") or []
            if ev["kind"] in ("code_commit", "merged_pr"):
                if rules.is_bot(raw.get("author")):
                    bad.append(f"{p['oss_id']}: {ev['ref']} authored by automation {raw.get('author')}")
                if not any(rules.is_code_path(x) for x in paths):
                    bad.append(f"{p['oss_id']}: {ev['ref']} touches no code path")
            else:
                if not rules.is_version(raw.get("ref")):
                    bad.append(f"{p['oss_id']}: {ev['kind']} {raw.get('ref')} is not a version")
                if raw.get("draft"):
                    bad.append(f"{p['oss_id']}: {raw.get('ref')} is a draft")
                if not paths or all(rules.is_documentation(x) for x in paths):
                    bad.append(f"{p['oss_id']}: {ev['kind']} {raw.get('ref')} ships documentation only")
    check("activity.evidence_is_not_docs_only", 8,
          "every piece of evidence relied on exists in the raw snapshot and, judged from its full path list, is human-authored code or a version release that ships more than documentation",
          bad, sum(len(p["derived"]["maintenance"]["evidence"]) for p in rec))
    bad = [p["oss_id"] for p in rec if p["live"].get("archived")]
    check("archived.excluded", 9, "no archived repository is recommended", bad,
          sum(1 for p in oss if p["live"].get("archived")))
    bad = [p["oss_id"] for p in oss if p["live"].get("archived")
           and "archived" not in p["derived"]["eligibility"]["exclusion_reasons"]]
    check("archived.reason_recorded", 9, "every archived repository carries the exclusion reason", bad)
    recognised = set(policy["licence"]["recognised_spdx"])
    bad = [p["oss_id"] for p in rec
           if not p["derived"]["licence"]["exception"]
           and not set(str(p["derived"]["licence"]["spdx"]).split(" AND ")) <= recognised]
    check("licence.recommended_have_recognised_licence", 10, "every recommended project has a recognised open-source licence or an approved exception", bad, len(rec))
    # Licence-exception lane (Keith direction, Linear comment 258b38dd).
    lane = [p for p in oss if p["derived"]["lane"] == "licence_exception"]
    lane_ids = {p["oss_id"] for p in lane}
    bad = [p["oss_id"] for p in oss
           if p["live"].get("found") and not p["derived"]["licence"]["recognised"]
           and not p["derived"]["licence"]["issue"]]
    check("licence_lane.every_licence_issue_is_described", 10,
          "every verified project without a recognised licence carries an issue record: category, exact reason, what was detected, upstream claims, evidence and review status",
          bad, sum(1 for p in oss if p["derived"]["licence"] and p["derived"]["licence"]["issue"]))
    bad = [p["oss_id"] for p in oss
           if p["derived"]["eligibility"]["exclusion_reasons"] == ["no_recognised_open_source_licence"]
           and p["derived"]["lane"] != "licence_exception"]
    bad += [p["oss_id"] for p in lane
            if p["derived"]["eligibility"]["exclusion_reasons"] != ["no_recognised_open_source_licence"]]
    check("licence_lane.retained_not_discarded", 10,
          "every project whose only shortcoming is its licence is retained in the licence-exception lane, and nothing else is in that lane",
          bad, len(lane))
    bad = [p["oss_id"] for p in lane
           if p["derived"]["recommendation_eligible"] or p["derived"]["eligibility"]["recommended"]
           or p["derived"]["launch"] or p["derived"]["licence"]["issue"]["reuse_rights_established"] is not False]
    bad += [e["edge_id"] for e in edges if e["oss_id"] in lane_ids
            and (e["recommendation_eligible"] or e["launch"] or e["validation"]["status"] == "validated")]
    check("licence_lane.never_recommended", 10,
          "no licence-exception record, and no relationship pointing at one, is recommendation-eligible, validated, or in the launch set",
          bad, {"projects": len(lane), "relationships": sum(1 for e in edges if e["lane"] == "licence_exception")})
    export_dir = common.DATA / "export-public"
    export_current = ((export_dir / "EXPORT.json").exists()
                      and common.read_json(export_dir / "EXPORT.json").get("dataset_as_of") == summary["as_of"])
    export_rows = common.read_jsonl(export_dir / "relationships.jsonl") if export_current else []
    observe("export.local_dry_run", 15, "whether a local export built from THIS dataset was present to be checked",
            {"present": export_current, "rows": len(export_rows)})
    not_ok = {p["oss_id"] for p in oss if not p["derived"]["recommendation_eligible"]}
    check("licence_lane.never_exported", 15,
          "the local export contains no project that is not recommendation-eligible",
          sorted({r["oss"]["id"] for r in export_rows if r["oss"]["id"] in not_ok}), len(export_rows))
    bad = [p["oss_id"] for p in oss if p["derived"]["recommendation_eligible"] != p["derived"]["eligibility"]["recommended"]
           or (p["derived"]["lane"] == "recommendable") != p["derived"]["eligibility"]["recommended"]]
    bad += [e["edge_id"] for e in edges if e["recommendation_eligible"] != (e["validation"]["status"] == "validated")
            or (e["lane"] == "recommendable") != (e["validation"]["status"] == "validated")]
    check("lanes.consistent", 10, "lane, recommendation_eligible and the underlying verdict agree on every record", bad,
          summary["lanes"])

    bad = [p["oss_id"] for p in oss if p["derived"]["licence"] and p["derived"]["licence"]["exception"]
           and str(p["derived"]["licence"]["exception"]["approved_by"]).lower() != "keith"]
    check("licence.exceptions_are_approved", 10, "every licence exception in use names its approver", bad,
          sum(1 for p in oss if p["derived"]["licence"] and p["derived"]["licence"]["exception"]))

    bad = []
    for p in oss:
        m = p["derived"]["maintenance"]
        if not m:
            continue
        dates = [parse_ts(e["at"]) for e in m["evidence"]]
        newest = max(dates) if dates else None
        d = (as_of - newest).days if newest else None
        want = "stale" if d is None or d > rules.maintained_days else ("active" if d <= rules.active_days else "maintained")
        stated = parse_ts(m["last_meaningful_activity_at"]) if m["last_meaningful_activity_at"] else None
        if m["status"] != want or stated != newest or m["days_since_meaningful_activity"] != d:
            bad.append(p["oss_id"])
    check("maintenance.classification_matches_definition", 11,
          "status, recomputed from the dates of the evidence itself: active <= 90 days, maintained <= 365 days, otherwise stale",
          bad, summary["verified"]["maintenance_status"])
    bad = [p["oss_id"] for p in rec if p["derived"]["maintenance"]["status"] == "stale"]
    check("maintenance.stale_excluded", 11, "no stale project is recommended", bad)

    # Every verdict is recomputed from the raw snapshot three times: as observed, with
    # stars forced to zero, and with stars forced to ten million. All three must equal
    # the verdict in the dataset.
    bad = []
    for p in oss:
        lv = raw_live.get(p["oss_id"])
        if not lv or not lv.get("found"):
            continue
        for stars_value in (lv.get("stars"), 0, 10_000_000):
            trial = dict(lv, stars=stars_value, forks=stars_value)
            m = evaluate(trial, rules, as_of)
            lic = build.licence_verdict(trial, policy, exceptions, p["oss_id"])
            drift = "repository_identity_changed_since_listed" in p["derived"]["eligibility"]["exclusion_reasons"]
            if build.eligibility(trial, m, lic, drift) != p["derived"]["eligibility"]:
                bad.append(f"{p['oss_id']} with stars={stars_value}")
    check("stars.do_not_change_any_verdict", 12,
          "every eligibility verdict is unchanged when stars are set to 0 or to 10,000,000", bad, len(oss))
    high_star_rejected = sorted(
        (p for p in oss if not p["derived"]["eligibility"]["recommended"] and (p["live"].get("stars") or 0) >= 10000),
        key=lambda p: -p["live"]["stars"],
    )
    low_star_accepted = [p for p in oss if p["derived"]["launch"] and (p["live"].get("stars") or 0) < 500]
    observe("stars.rejected_despite_stars", 12, "projects with 10,000+ stars that were rejected, and launch projects with fewer than 500",
            {"rejected_with_10k_plus_stars": len(high_star_rejected),
             "launch_projects_under_500_stars": len(low_star_accepted),
             "examples": [f"{p['oss_id']} ({p['live']['stars']} stars: {','.join(p['derived']['eligibility']['exclusion_reasons'])})" for p in high_star_rejected[:5]]})

    observe("strategy.harvest_broad_reject_aggressive", 13, "harvested volume against accepted volume",
            {"entries_read": summary["harvest"]["entries_read"],
             "relationship_claims_read": summary["harvest"]["relationship_claims_read"],
             "oss_candidates": len(oss), "oss_recommendable": len(rec),
             "relationships_validated": summary["verified"]["relationships_validated"],
             "rejections_recorded": summary["rejections"]["total"]})
    rejected = [p for p in oss if not p["derived"]["eligibility"]["recommended"]]
    logged = {r.get("oss_id") for r in common.read_jsonl(common.DATASET / "rejections.jsonl") if r["stage"] == "eligibility"}
    check("strategy.every_rejection_is_recorded", 13, "every rejected project has a rejection record stating why",
          [p["oss_id"] for p in rejected if p["oss_id"] not in logged
           or not p["derived"]["eligibility"]["exclusion_reasons"]], len(rejected))

    allowed_keys = {"schema_version", "oss_id", "name", "name_origin", "product_group", "host", "repo_url",
                    "harvested_as", "imported", "live", "derived", "provenance"}
    bad = [f"{p['oss_id']}: {sorted(set(p) - allowed_keys)}" for p in oss if set(p) - allowed_keys]
    bad += [p["oss_id"] for p in oss if set(p["imported"]) & set(p["live"])]
    check("origin.no_unlabelled_fields", 14,
          "a project has no top-level field outside the identity fields and the imported, live and derived blocks, and no field name appears in both imported and live", bad, len(oss))
    bad = []
    for p in oss:
        lv = raw_live.get(p["oss_id"]) or {}
        for field in ("archived", "licence_spdx", "stars", "pushed_at"):
            if p["live"].get(field) != lv.get(field):
                bad.append(f"{p['oss_id']}.live.{field}")
    check("origin.live_matches_snapshot", 14, "every live field equals what the host returned in the snapshot", bad, len(oss))
    bad = [f"{p['oss_id']}: {v['value'][:40]}" for p in oss for f in p["imported"].values() for v in f if len(v["value"]) > 80]
    check("origin.imported_values_are_facts_not_prose", 14, "no imported value is longer than 80 characters", bad)
    bad = [p["oss_id"] for p in oss if not p["live"].get("fetched_at")]
    check("origin.live_is_dated", 14, "every live block is dated", bad)

    # Criterion 15. What can be checked from here: the dataset is marked internal, the
    # working directory has no git remote to push to, and the local export (if one was
    # built) withholds share-alike-only material and is marked unpublished.
    share = [e["edge_id"] for e in edges if e["share_alike_only"]]
    bad = []
    # KEI-807: the canonical repository is the approved home of this data. A remote is
    # allowed only if it is that repository; any status but private needs Keith's approval.
    pub = common.load_yaml(common.CONFIG / "publication.yaml") if (common.CONFIG / "publication.yaml").exists() \
        else {"status": "internal_not_published", "canonical_remote": None, "approval": None}
    if summary["publication_status"] != pub["status"]:
        bad.append("summary.publication_status disagrees with config/publication.yaml")
    if pub["status"] not in ("internal_not_published", "private_repository") and not pub.get("approval"):
        bad.append(f"publication status {pub['status']!r} has no recorded approval")
    git_config = common.ROOT / ".git" / "config"
    if git_config.exists():
        import re as _re
        for url in _re.findall(r"^\s*url\s*=\s*(\S+)", git_config.read_text(), _re.M):
            norm = _re.sub(r"^(https?://|git@|ssh://git@)", "", url).replace(":", "/").removesuffix(".git").lower()
            if not pub.get("canonical_remote") or norm != pub["canonical_remote"].lower():
                bad.append(f"a git remote other than the canonical repository is configured: {url}")
    if export_current:
        exp = common.read_json(export_dir / "EXPORT.json")
        if exp.get("published") is not False:
            bad.append("export is not marked unpublished")
        if exp["export_licence_constraint"] != "CC-BY-SA-3.0":
            exported = {(r["saas"]["id"], r["oss"]["id"]) for r in common.read_jsonl(export_dir / "relationships.jsonl")}
            leaked = [e["edge_id"] for e in edges if e["share_alike_only"] and (e["saas_id"], e["oss_id"]) in exported]
            bad += [f"share-alike-only edge exported: {x}" for x in leaked]
    check("publication.guard", 15,
          "publication state matches config/publication.yaml (public only with a recorded approval); the only git remote is the canonical repository; any local export is unpublished and withholds share-alike-only relationships",
          bad, {"publication_status": summary["publication_status"],
                # Three different populations; each is named so they cannot be confused.
                "share_alike_only_relationships_all_lanes": len(share),
                "share_alike_only_relationships_validated": sum(
                    1 for e in edges if e["share_alike_only"] and e["validation"]["status"] == "validated"),
                "share_alike_only_relationships_in_launch": sum(
                    1 for e in edges if e["share_alike_only"] and e["launch"]),
                "withheld_from_default_export": summary["launch"]["relationships_share_alike_only"]})

    seen, bad = set(), []
    for e in sorted(launch_edges, key=lambda e: e["edge_id"]):
        k = (e["saas_id"], oss_by_id[e["oss_id"]]["product_group"])
        if k in seen:
            bad.append(e["edge_id"])
        seen.add(k)
    check("dedupe.one_repository_per_product_per_saas", 5, "within the launch set a SaaS lists each product once, even when the product has several repositories", bad, len(launch_edges))

    bad = [e["edge_id"] for e in launch_edges if e["validation"]["status"] != "validated"]
    check("launch.only_validated_edges", 6, "every launch relationship is validated", bad, len(launch_edges))
    bad = [e["edge_id"] for e in launch_edges if not oss_by_id[e["oss_id"]]["derived"]["eligibility"]["recommended"]]
    check("launch.only_recommendable_projects", 6, "every launch relationship points at a recommendable project", bad)
    targets = policy["launch"]
    counts = {"saas_products": len(launch_saas), "oss_projects": len(launch_oss),
              "oss_distinct_products": len({p["product_group"] for p in launch_oss}),
              "relationships": len(launch_edges)}
    miss = []
    if targets.get("saas_target_is", "fixed") == "fixed" and counts["saas_products"] != targets["saas_target"]:
        miss.append(f"saas_products {counts['saas_products']} != {targets['saas_target']} (fixed)")
    if counts["saas_products"] < targets["saas_target"]:
        miss.append(f"saas_products {counts['saas_products']} < {targets['saas_target']}")
    if min(counts["oss_projects"], counts["oss_distinct_products"]) < targets["oss_minimum"]:
        miss.append(f"oss_projects {counts['oss_projects']} / distinct products {counts['oss_distinct_products']} < {targets['oss_minimum']}")
    if not targets["edges_min"] <= counts["relationships"] <= targets["edges_max"]:
        miss.append(f"relationships {counts['relationships']} outside {targets['edges_min']}-{targets['edges_max']}")
    ranks = sorted(x["derived"]["launch_rank"] for x in launch_saas)
    bad = [] if ranks == list(range(1, len(ranks) + 1)) else ["launch ranks are not the unbroken top of the ranking"]
    by_rank = sorted(saas, key=lambda x: (-x["derived"]["source_count"], -x["derived"]["validated_alternatives"], x["name"].lower()))
    eligible = [x["saas_id"] for x in by_rank if x["derived"]["validated_alternatives"] > 0][: len(ranks)]
    chosen_ids = [x["saas_id"] for x in sorted(launch_saas, key=lambda x: x["derived"]["launch_rank"])]
    if eligible != chosen_ids:
        bad.append("the launch SaaS are not the first N of the declared ranking")
    check("launch.ranking_unchanged", 6,
          "the launch SaaS products are exactly the first N of the declared ranking (independent sources, then validated alternatives, then name): nothing was skipped or promoted",
          bad, {"launch_saas": len(ranks), "floor": targets["saas_target"], "mode": targets.get("saas_target_is", "fixed")})
    check("launch.targets", 6, "launch corpus size against the ticket's targets (100 SaaS read as a floor per dr-KEI-805-507e4f70)", miss, counts)

    report = {"as_of": summary["as_of"], "ok": all(c["ok"] for c in checks), "checks": checks,
              "observations": observations}
    common.write_json(common.REPORTS / "validation_report.json", report)
    common.write_manifest(summary["as_of"])
    return report
