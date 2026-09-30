"""Discover lane (KEI-811) — validate the corpus against the ticket's acceptance boundary.

Each check names the criterion it evidences. The report is written to
data/discover/corpus/reports/validation_report.json, then MANIFEST.json is written
over every corpus file.
"""
from __future__ import annotations

from . import common
from .discover import FRONTIER, LIVE, load_cfg
from .discover_build import CORPUS, REASONS, SCHEMA_ID, write_manifest
from .discover_universe import all_routes, parse_seed_file


def run() -> dict:
    cfg = load_cfg()
    policy = common.load_policy()
    frontier = common.read_json(FRONTIER)
    live = common.read_json(LIVE)
    projects = common.read_jsonl(CORPUS / "projects.jsonl")
    rejections = common.read_jsonl(CORPUS / "rejections.jsonl")
    edges = common.read_jsonl(CORPUS / "relationship_candidates.jsonl")
    nodes = common.read_jsonl(CORPUS / "nodes.jsonl")
    coverage = common.read_json(CORPUS / "seed_coverage.json")
    summary = common.read_json(CORPUS / "summary.json")
    counts = summary["counts"]
    checks = []

    def check(cid, criterion, ok, detail):
        checks.append({"id": cid, "criterion": criterion, "ok": bool(ok), "detail": detail})

    # AC1 — Replace lane intact and identifiable.
    baseline = {}
    for line in (common.DATA / "discover" / "kei805_baseline.sha256").read_text().splitlines():
        digest, path = line.split()
        baseline[path] = digest
    now = {p: common.sha256_file(common.ROOT / p) for p in baseline}
    check("kei805_dataset_unchanged", "AC1", now == baseline,
          {"files": len(baseline), "changed": sorted(p for p in baseline if now[p] != baseline[p])})
    kei805 = common.read_jsonl(common.DATASET / "oss_projects.jsonl")
    kept = {r["oss_id"] for r in kei805 if r["derived"]["lane"] != "rejected"}
    replace_ids = {p["replace"]["kei805_oss_id"] for p in projects if p["lanes"]["replace"]}
    check("replace_lane_complete", "AC1", kept == replace_ids,
          {"kei805_retained": len(kept), "replace_lane_records": len(replace_ids),
           "missing": sorted(kept - replace_ids)[:10], "extra": sorted(replace_ids - kept)[:10]})
    bad = [p["id"] for p in projects if p["lanes"]["replace"] and (
        p["quality"]["basis"] != "kei805" or p["replace"]["kei805_lane"] != p["quality"]["quality_lane"])]
    check("replace_lane_verdicts_carried_not_recomputed", "AC1", not bad, {"violations": bad[:10]})

    # AC2 — Discover lane accepts projects without a SaaS edge.
    no_edge = [p for p in projects if p["lanes"]["discover"] and not p["lanes"]["replace"]]
    check("discover_lane_accepts_without_saas_edge", "AC2", len(no_edge) > 0,
          {"discover_only_retained": len(no_edge),
           "with_saas_edge": sum(1 for p in no_edge if (p["replace"] or {}).get("saas_edge_ids"))})

    # AC3 — Keith's seeds ingested from the pasted file, every row accounted for.
    rows = parse_seed_file((common.ROOT / cfg["seeds"]["keith_seed_file"]).read_text(encoding="utf-8"))
    missing = [c for c in coverage if c["outcome"] == "missing"]
    check("every_seed_row_accounted_for", "AC3", len(coverage) == len(rows) and not missing,
          {"rows": len(rows), "coverage": len(coverage), "missing": missing[:5]})
    check("seed_file_is_the_input", "AC3", frontier["seeds"]["keith"] == rows,
          "frontier seeds equal a fresh parse of the seed file")

    # AC4 — bounded, reproducible recursion.
    routes = all_routes(frontier, cfg)
    depths = {min(r["depth"] for r in rs) for rs in routes.values()}
    check("recursion_bounded", "AC4", max(depths) <= int(cfg["recursion"]["max_depth"])
          and len(frontier["strategies"]["recursion"]["frontier"]) <= int(cfg["recursion"]["frontier_max"]),
          {"depths": sorted(depths), "frontier": len(frontier["strategies"]["recursion"]["frontier"])})
    strategies = set(counts["candidates_by_strategy"])
    check("multiple_strategies_used", "AC4", {"owner_neighbourhood", "readme_links", "topic_search",
                                              "curated_list", "trending"} <= strategies, sorted(strategies))
    check("frontier_matches_config", "AC4",
          frontier["config_sha256"] == summary["inputs"]["discover_config_sha256"], "config fingerprint")

    # AC5 — scale without lowering the bar.
    check("raw_universe_order_10k", "AC5", counts["resolved_unique_repositories"] >= 8000,
          {"resolved_unique_repositories_evaluated": counts["resolved_unique_repositories"],
           "raw_unique_repositories": counts["raw_unique_repositories"],
           "raw_candidate_addresses": counts["raw_candidate_addresses"]})
    lax = []
    allowed = set(policy["licence"]["recognised_spdx"])
    for p in projects:
        q = p["quality"]
        if p["recommendation_eligible"]:
            if q["quality_lane"] != "recommendable" or q["exclusion_reasons"]:
                lax.append((p["id"], "eligible_with_reasons"))
            if (q["maintenance"] or {}).get("status") not in ("active", "maintained"):
                lax.append((p["id"], "eligible_not_maintained"))
            lic = q["licence"] or {}
            if not (lic.get("recognised") or lic.get("status") == "exception_approved"):
                lax.append((p["id"], "eligible_licence_not_recognised"))
        if p["live"].get("archived"):
            lax.append((p["id"], "archived_retained"))
        if (q["maintenance"] or {}).get("status") == "stale":
            lax.append((p["id"], "stale_retained"))
        if q["basis"] == "discover" and not q["maintenance"]["evidence"]:
            lax.append((p["id"], "retained_without_activity_evidence"))
    check("quality_bar_not_lowered", "AC5", not lax, {"violations": lax[:10]})
    check("quality_policy_is_kei805_policy", "AC5",
          live["maintenance_policy_sha256"] == __import__("source_pipeline.enrich", fromlist=["x"]).policy_fingerprint(policy),
          "the maintenance policy verified against is config/policy.yaml, shared with KEI-805")
    from .activity import parse_ts as _pt
    clock = _pt(live["as_of"])
    def after_clock(keys):
        return sorted(slug for slug, d in live["deep"].items()
                      if any((_pt(x.get("at")) or clock) > clock for k in keys
                             for x in (d.get("observations") or {}).get(k) or []))
    # Merge and publication times are stamped by the host, so none may fall after the
    # clock. Commit and tag dates are set by the committer's machine and can lie in the
    # future; those are ignored by the evaluator and only counted here.
    late = after_clock(("merged_prs", "releases"))
    check("no_observation_after_clock", "AC5", not late,
          {"as_of": live["as_of"], "started_at": live.get("started_at"), "violations": late[:10],
           "count": len(late), "future_dated_commits_or_tags_ignored": len(after_clock(("commits", "tags")))})
    check("no_lookup_left_unexplained", "AC5",
          all(r["reasons"] for r in rejections), "every rejection carries at least one reason")

    # AC6 — provenance for every retained project.
    noprov = [p["id"] for p in projects if not p["provenance"] or not p["entry_reason"]]
    check("provenance_for_every_retained", "AC6", not noprov, {"violations": noprov[:10]})

    # AC7 — deterministic de-duplication.
    ids = [p["id"] for p in projects]
    aliases = [a for p in projects for a in p["aliases"]]
    check("unique_ids", "AC7", len(ids) == len(set(ids)), len(ids))
    check("each_address_one_record", "AC7", len(aliases) == len(set(aliases)), len(aliases))
    rej_ids = {r.get("entity_id") for r in rejections if r.get("entity_id")}
    check("retained_and_rejected_disjoint", "AC7", not (set(ids) & rej_ids), sorted(set(ids) & rej_ids)[:5])
    forks = [r for r in rejections if "fork_alias" in r["reasons"]]
    check("forks_point_to_parent", "AC7", all(r["evidence"].get("parent_id") for r in forks), len(forks))

    # AC8 — machine-readable rejection reasons.
    unknown = sorted({x for r in rejections for x in r["reasons"]} - set(REASONS)
                     - set(sum((r["reasons"] for r in rejections if r["stage"] == "replace_lane"), [])))
    check("rejection_reasons_known", "AC8", not unknown, unknown)
    accounted = len(projects) + len({r.get("entity_id") for r in rejections if r.get("entity_id")}) \
        + sum(1 for r in rejections if not r.get("entity_id") and r["stage"] in ("metadata", "universe_cap"))
    check("every_candidate_accounted_for", "AC8",
          accounted == counts["raw_unique_repositories"],
          {"retained": len(projects), "accounted": accounted, "raw": counts["raw_unique_repositories"]})

    # AC9 — graph-ready records.
    node_ids = {n["id"] for n in nodes}
    project_ids = set(ids)
    dangling = [e for e in edges if e["to"] not in node_ids | project_ids
                or e["from"] not in project_ids | node_ids | rej_ids and not e["from"].startswith("unresolved-slug:")]
    check("relationship_endpoints_resolve", "AC9", not dangling, dangling[:5])
    import jsonschema

    schema = common.read_json(common.SCHEMA / "the-source.discover.schema.json")

    def invalid(rows, name):
        v = jsonschema.Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": schema["$defs"]})
        out = []
        for r in rows:
            errs = list(v.iter_errors(r))
            if errs:
                out.append({"record": r.get("id") or r.get("candidate") or r.get("from"), "error": errs[0].message[:200]})
        return out

    for name, rows in (("project", projects), ("rejection", rejections), ("relationship", edges)):
        bad_rows = invalid(rows, name)
        check(f"schema_valid_{name}s", "AC9", not bad_rows, {"records": len(rows), "invalid": bad_rows[:5]})
    shape = [p["id"] for p in projects if p["schema"] != SCHEMA_ID or not p["entities"] or "id" not in p]
    check("records_have_stable_ids_and_entities", "AC9", not shape, shape[:5])

    # AC10 — history is appendable.
    nohist = [p["id"] for p in projects if p["id"].startswith("github:") and p["live"].get("found") is not False
              and not p["freshness"]["history"] and p["lanes"]["discover"]]
    check("freshness_history_present", "AC10", not nohist, {"without_history": nohist[:10], "count": len(nohist)})

    # AC12 — nothing published or endorsed.
    pub = [p["id"] for p in projects if p["endorsed"] or p["publication_status"] != "internal_not_published"]
    check("no_endorsement_or_publication", "AC12", not pub, pub[:5])

    report = {"as_of": summary["as_of"], "ok": all(c["ok"] for c in checks), "checks": checks}
    common.write_json(CORPUS / "reports" / "validation_report.json", report)
    write_manifest(summary["as_of"])
    return report
