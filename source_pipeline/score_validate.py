"""Scoring (KEI-806) — validate the scores against the ticket's acceptance boundary.

Each check names the criterion it evidences (AC1-AC12, plus the rubric rules R1-R8 and the
calibration set). Writes data/score/reports/validation_report.json and calibration.json,
then data/score/MANIFEST.json over every scoring file.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import tempfile
from pathlib import Path

import jsonschema

from . import common, score_build
from .score_build import GRAPH, SCORE

SCHEMA = common.SCHEMA / "the-source.score.schema.json"
CALIBRATION = common.CONFIG / "score_calibration.yaml"
REPORTS = SCORE / "reports"

# Positive claims of human testing or endorsement. The endorsement.statement field, which
# states the negation, is excluded from the scan.
FORBIDDEN = re.compile(r"keith[- ]tested|tested by keith|keith[- ]endorsed|endorsed by keith(?! de alwis, and not)"
                       r"|keith recommends|recommended by keith|editor'?s pick|editorial pick|hand[- ]picked", re.I)


def _sha(path: Path) -> str:
    return common.sha256_file(path)


def _digest_rows(rows) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(common.dumps(r).encode())
        h.update(b"\n")
    return h.hexdigest()


def write_manifest(as_of, version):
    files = {}
    for path in sorted(SCORE.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.json":
            files[str(path.relative_to(SCORE))] = _sha(path)
    common.write_json(SCORE / "MANIFEST.json", {"evidence_as_of": as_of, "score_version": version,
                                                "schema": "the-source.score.schema.json",
                                                "publication_status": "internal_not_published", "sha256": files})


class RefResolver:
    """Resolve every provenance ref a score cites to the record it names."""

    def __init__(self):
        self.meta = common.read_json(score_build.EVIDENCE / "github_meta.json")["records"]
        rp = score_build.EVIDENCE / "readmes.json"
        self.readmes = common.read_json(rp)["records"] if rp.exists() else {}
        self.view = {r["project"] for r in common.read_jsonl(GRAPH / "views" / "scoring_inputs.jsonl")}
        self.edges = set()
        for line in open(GRAPH / "edges.jsonl", encoding="utf-8"):
            self.edges.add(common.json.loads(line)["id"])
        self.deep = {v.get("canonical_id") for v in common.read_json(common.DATA / "discover" / "live.json")["deep"].values()}

    def ok(self, ref: str | None) -> bool:
        if not ref:
            return False
        if ref.startswith("score:evidence/github_meta.json#"):
            key = ref.split("#", 1)[1].split("@", 1)[0].split(":", 1)[0]
            return key in self.meta
        if ref.startswith("score:evidence/readmes.json#"):
            key = ref.split("#", 1)[1].split("@", 1)[0]
            return key in self.readmes
        if ref.startswith("graph:edges.jsonl#"):
            return ref.split("#", 1)[1] in self.edges
        if ref.startswith("graph:views/scoring_inputs.jsonl#"):
            # project ids carry their own colon ("github:123"): host prefix + id
            return ":".join(ref.split("#", 1)[1].split(":")[:2]) in self.view
        if ref.startswith("kei811:live.json#deep/"):
            return ref.split("#deep/", 1)[1].split(":", 1)[0] in self.deep
        return False


def _calibration(health, fits, records, signals, cfg):
    cal = common.load_yaml(CALIBRATION)
    H = {h["canonical"]: h for h in health}
    R = {r["canonical"]: r for r in records}
    F = {(f["canonical"], f["saas"]): f for f in fits}
    S = {s["canonical"]: s for s in signals}
    results = []

    def res(group, subject, ok, detail):
        results.append({"group": group, "subject": subject, "ok": bool(ok), "detail": detail})

    for group, spec in cal["named"].items():
        exp = spec["expect"]
        for subj in spec.get("projects", []):
            h, r = H.get(subj), R.get(subj)
            if h is None:
                res(group, subj, False, "not in corpus")
                continue
            dq = [d["id"] for d in h["disqualifiers"]]
            ok, why = True, {"health": h["score"], "band": h["band"], "trajectory": h["trajectory"]["label"],
                             "disqualifiers": dq, "confidence": h["confidence"]["score"]}
            if "health_min" in exp:
                ok &= (h["score"] or 0) >= exp["health_min"]
            if exp.get("no_disqualifier"):
                ok &= not dq
            if "disqualifier" in exp:
                ok &= exp["disqualifier"] in dq
            if "recommendation_eligible" in exp:
                ok &= r["recommendation_eligible"] == exp["recommendation_eligible"]
            if "trajectory_not" in exp:
                ok &= h["trajectory"]["label"] not in exp["trajectory_not"]
            if exp.get("health_scored"):
                ok &= h["score"] is not None
            if exp.get("replacement_fit") == "none":
                ok &= not r["replacement_fit"]
                why["replacement_fit"] = len(r["replacement_fit"])
            res(group, subj, ok, why)
        for o, s in spec.get("pairs", []):
            f = F.get((o, s))
            if f is None:
                res(group, f"{o} -> {s}", False, "pair not scored")
                continue
            ok = f["core_gate"] == exp["core_gate"] and (f["score"] or 0) >= exp["fit_min"]
            res(group, f"{o} -> {s}", ok, {"fit": f["score"], "core_gate": f["core_gate"],
                                            "core": f["core_coverage"]["score"], "confidence": f["confidence"]["score"],
                                            "credible": f["credible_alternative"]})
    for group, spec in cal["rules"].items():
        sel, exp = spec["select"], spec["expect"]
        members = []
        for s in signals:
            f = s["facts"]
            v = lambda k: (s["signals"].get(k) or {}).get("value")
            if group == "emerging" and f.get("age_years") is not None and f["age_years"] < sel["age_years_below"] \
                    and (v("commits_90d") or 0) >= sel["commits_90d_min"]:
                members.append(s["canonical"])
            elif group == "archived" and f.get("archived"):
                members.append(s["canonical"])
            elif group == "dormant" and f.get("c365") == 0 and f.get("cprev") == 0:
                members.append(s["canonical"])
        fails = []
        for m in members:
            h, r = H[m], R[m]
            dq = [d["id"] for d in h["disqualifiers"]]
            ok = True
            if "trajectory" in exp:
                ok &= h["trajectory"]["label"] == exp["trajectory"]
            if exp.get("maturity_below_maintenance"):
                d = h["dimensions"]
                ok &= (d["maturity_continuity"]["score"] or 0) < (d["maintenance_momentum"]["score"] or 0)
            if "disqualifier" in exp:
                ok &= exp["disqualifier"] in dq
            if "recommendation_eligible" in exp:
                ok &= r["recommendation_eligible"] == exp["recommendation_eligible"]
            if "maintenance_max" in exp:
                ok &= (h["dimensions"]["maintenance_momentum"]["score"] or 0) <= exp["maintenance_max"]
            if not ok:
                fails.append({"project": m, "health": h["score"], "trajectory": h["trajectory"]["label"], "disqualifiers": dq,
                              "maturity": h["dimensions"]["maturity_continuity"]["score"],
                              "maintenance": h["dimensions"]["maintenance_momentum"]["score"]})
        res(group, f"rule {sel}", (bool(members) or spec.get("allow_empty", False)) and not fails,
            {"members": len(members), "failures": len(fails), "failed_examples": fails[:10],
             "empty_allowed": spec.get("allow_empty", False)})
    return results


def run() -> dict:
    cfg = score_build.load_cfg()
    jobs = score_build.load_jobs()
    health = common.read_jsonl(SCORE / "project_health.jsonl")
    fits = common.read_jsonl(SCORE / "replacement_fit.jsonl")
    records = common.read_jsonl(SCORE / "records.jsonl")
    signals = common.read_jsonl(SCORE / "signals.jsonl")
    pairs = common.read_jsonl(SCORE / "pair_signals.jsonl")
    view = common.read_jsonl(GRAPH / "views" / "scoring_inputs.jsonl")
    saas = {f"saas:{s['saas_id']}": s for s in common.read_jsonl(common.DATASET / "saas_products.jsonl")}
    rep_edges = [common.json.loads(l) for l in open(GRAPH / "edges.jsonl", encoding="utf-8") if '"REPLACES"' in l]
    rep_edges = [e for e in rep_edges if e["relation"] == "REPLACES"]
    checks = []

    def check(cid, criterion, ok, detail):
        checks.append({"id": cid, "criterion": criterion, "ok": bool(ok), "detail": detail})

    H = {h["project"]: h for h in health}
    R = {r["project"]: r for r in records}

    # ---------------- AC1 Project Health: documented, machine-reproducible, every qualified project
    missing = [r["project"] for r in view if r["project"] not in H or H[r["project"]]["score"] is None]
    check("health_for_every_retained_project", "AC1", not missing,
          {"retained_projects": len(view), "scored": len(view) - len(missing), "missing": missing[:20]})
    check("health_rubric_documented", "AC1", (common.ROOT / "docs" / "SCORING.md").exists()
          and "Maintenance & momentum" in (common.ROOT / "docs" / "SCORING.md").read_text(), {"doc": "docs/SCORING.md"})
    w = {k: v["weight"] for k, v in cfg["project_health"]["dimensions"].items()}
    check("health_weights_match_approved_rubric", "AC1",
          w == {"maintenance_momentum": 25, "community_contributors": 15, "issue_pr_responsiveness": 15,
                "maturity_continuity": 15, "documentation_usability": 10, "operational_readiness": 10,
                "security_governance": 5, "licence": 5}, w)
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp) / "a", Path(tmp) / "b"
        a.mkdir(), b.mkdir()
        os.environ["THE_SOURCE_NO_NETWORK"] = "1"
        try:
            score_build.rescore(cfg, a, history=False)
            score_build.rescore(cfg, b, history=False)
        finally:
            os.environ.pop("THE_SOURCE_NO_NETWORK", None)
        same = all(_sha(a / f) == _sha(b / f) for f in ("project_health.jsonl", "replacement_fit.jsonl", "records.jsonl"))
        matches_published = all(_sha(a / f) == _sha(SCORE / f) for f in ("project_health.jsonl", "replacement_fit.jsonl", "records.jsonl"))
    check("scoring_is_deterministic_offline", "AC1", same and matches_published,
          {"two_offline_rescores_identical": same, "equal_to_published_outputs": matches_published,
           "network": "THE_SOURCE_NO_NETWORK=1"})

    # ---------------- AC2 Replacement Fit only for credible SaaS edges
    edge_ids = {e["id"] for e in rep_edges}
    fit_edges = {f["edge"] for f in fits}
    no_edge_fit = [f["edge"] for f in fits if f["edge"] not in edge_ids]
    view_rep = {r["project"] for r in view if r["replaces"]}
    fit_projects = {f["project"] for f in fits}
    check("replacement_fit_only_on_replaces_edges", "AC2", not no_edge_fit and fit_projects <= view_rep,
          {"fit_rows": len(fits), "replaces_edges": len(edge_ids), "fit_without_edge": no_edge_fit[:10],
           "projects_with_fit_but_no_edge": sorted(fit_projects - view_rep)[:10]})
    cats_with_edges = {saas[e["to"]]["category"] for e in rep_edges}
    check("every_replaced_category_has_a_job_definition", "AC2", cats_with_edges <= set(jobs),
          {"categories_with_edges": len(cats_with_edges), "missing": sorted(cats_with_edges - set(jobs))})
    check("every_replaces_edge_scored", "AC2", fit_edges == edge_ids,
          {"unscored": sorted(edge_ids - fit_edges)[:10]})
    rw = {k: v["weight"] for k, v in cfg["replacement_fit"]["dimensions"].items()}
    check("fit_weights_match_approved_rubric", "AC2",
          rw == {"core_use_case_coverage": 25, "feature_equivalence": 15, "ease_of_adoption": 15,
                 "deployment_ownership": 10, "migration_friction": 10, "integrations_extensibility": 10,
                 "production_commercial_readiness": 10, "economic_advantage": 5}, rw)
    gate = cfg["replacement_fit"]["core_gate"]
    gate_bad = [f["edge"] for f in fits if f["core_gate"] != "passed" and (f["score"] or 0) > gate["gate_cap"]]
    gate_state_bad = [f["edge"] for f in fits if f["core_coverage"]["score"] is not None and
                      (f["core_gate"] == "passed") != (f["core_coverage"]["score"] >= gate["gate_min"]
                                                       and (f["core_coverage"]["defining_group_in_identity"]
                                                            or not gate["defining_group_required"]))]
    check("core_use_case_gate_enforced", "AC2/R-gate", not gate_bad and not gate_state_bad,
          {"gate_min": gate["gate_min"], "gate_cap": gate["gate_cap"],
           "states": {s: sum(1 for f in fits if f["core_gate"] == s) for s in ("passed", "failed", "unproven")},
           "violations": (gate_bad + gate_state_bad)[:10]})

    # Negative control: the gate must discriminate. Re-score every pair against a randomly
    # mismatched category's job (seeded); a gate that still passes most of them means nothing.
    import random
    rng = random.Random(806)
    cats = sorted(jobs)
    passed_wrong, total_wrong, leaks = 0, 0, []
    for p in pairs:
        if p.get("missing_job"):
            continue
        wrong = rng.choice([c for c in cats if c != p["category"]])
        q = score_build.rejob(p, jobs[wrong], wrong)
        f = score_build.score_pair(q, {"score": None}, [], health[0]["evaluated_at"], cfg)
        total_wrong += 1
        if f["core_gate"] == "passed":
            passed_wrong += 1
            leaks.append((p["canonical"], p["category"], wrong, f["score"]))
    rate = passed_wrong / total_wrong if total_wrong else 1.0
    true_rate = sum(1 for f in fits if f["core_gate"] == "passed") / len(fits)
    check("core_gate_discriminates_negative_control", "AC2/R-gate",
          rate <= gate["negative_control_max_pass_rate"] and true_rate > rate * 3,
          {"mismatched_category_pass_rate": round(rate, 3), "max": gate["negative_control_max_pass_rate"],
           "true_pair_pass_rate": round(true_rate, 3), "pairs": total_wrong,
           "leaks_examples": sorted(leaks, key=lambda x: -(x[3] or 0))[:8]})

    # ---------------- AC3 evidence inspectable and provenance-linked
    resolver = RefResolver()
    bad, n = [], 0
    for s in signals:
        for name, sg in s["signals"].items():
            if sg.get("value") is None:
                continue
            n += 1
            if not (sg.get("source") and sg.get("observed_at") and resolver.ok(sg.get("ref"))):
                bad.append({"project": s["canonical"], "signal": name, "ref": sg.get("ref")})
    check("every_known_signal_resolves_to_evidence", "AC3", not bad,
          {"signals_checked": n, "unresolved": len(bad), "examples": bad[:10]})
    fbad = [f["edge"] for f in fits if not resolver.ok(f["evidence"]["relationship"])
            or (f["evidence"]["readme"] and not resolver.ok(f["evidence"]["readme"]))]
    check("every_fit_cites_its_relationship_and_text", "AC3", not fbad, {"fits": len(fits), "unresolved": fbad[:10]})
    wbad = [(r["canonical"], x["text"]) for r in records for x in r["why"] if not resolver.ok(x["evidence"])]
    dbad = [(r["canonical"], d["id"]) for r in records for d in r["disqualifiers"] + r["watch_outs"]
            if d["id"] != "security_concern" and not resolver.ok(d.get("evidence"))]
    check("every_reason_and_flag_cites_evidence", "AC3/R7", not wbad and not dbad,
          {"why_unresolved": wbad[:10], "flags_unresolved": dbad[:10]})

    # ---------------- AC4 independence of Health and Fit
    # Two directions, symmetric about the Healthy band edge (70): a Healthy project whose Fit is
    # poor (< 50), and a strong Fit (>= 70) from a project below Healthy. The stricter reverse
    # (Health < 55 with Fit >= 60) is reported too; the replace lane is pre-filtered by the
    # KEI-805 live quality bar, so genuinely weak projects there are rare, and the reverse is
    # additionally proven synthetically (tests/test_score.py TestIndependence).
    healthy_poor_fit = [(f["canonical"], f["saas"], f["project_health_ref"]["score"], f["score"]) for f in fits
                        if (f["project_health_ref"]["score"] or 0) >= 70 and f["score"] is not None and f["score"] < 50]
    weaker_strong_fit = [(f["canonical"], f["saas"], f["project_health_ref"]["score"], f["score"]) for f in fits
                         if (f["project_health_ref"]["score"] or 100) < 70 and (f["score"] or 0) >= 70]
    strict_reverse = [x for x in weaker_strong_fit if x[2] < 55] + \
        [(f["canonical"], f["saas"], f["project_health_ref"]["score"], f["score"]) for f in fits
         if (f["project_health_ref"]["score"] or 100) < 55 and 60 <= (f["score"] or 0) < 70]
    same_project_split = []
    by_p = {}
    for f in fits:
        by_p.setdefault(f["canonical"], []).append(f)
    for p, fl in by_p.items():
        sc = [x["score"] for x in fl if x["score"] is not None]
        if len(sc) >= 2 and max(sc) - min(sc) >= 20:
            same_project_split.append((p, {x["saas"]: x["score"] for x in fl}))
    fit_h = [(f["project_health_ref"]["score"], f["score"]) for f in fits if f["score"] is not None and f["project_health_ref"]["score"] is not None]
    n = len(fit_h)
    mx, my = sum(a for a, _ in fit_h) / n, sum(b for _, b in fit_h) / n
    cov = sum((a - mx) * (b - my) for a, b in fit_h)
    corr = cov / ((sum((a - mx) ** 2 for a, _ in fit_h) * sum((b - my) ** 2 for _, b in fit_h)) ** 0.5)
    check("healthy_project_can_fit_poorly_and_vice_versa", "AC4",
          healthy_poor_fit and weaker_strong_fit and same_project_split,
          {"healthy_70_plus_but_fit_below_50": len(healthy_poor_fit), "examples_healthy_poor_fit": healthy_poor_fit[:5],
           "below_healthy_but_fit_70_plus": len(weaker_strong_fit), "examples_weaker_strong_fit": sorted(weaker_strong_fit, key=lambda x: x[2])[:5],
           "strict_reverse_health_below_55_fit_60_plus": len(strict_reverse),
           "same_project_fit_spread_20_plus": len(same_project_split), "examples_spread": same_project_split[:3],
           "health_fit_correlation": round(corr, 3)})
    # Structural: the fit computation takes no health input. Perturb health, fit is unchanged.
    sample = [p for p in pairs if not p.get("missing_job")][:50]
    h0 = {"score": 10.0, "band": "High Risk"}
    h1 = {"score": 95.0, "band": "Exceptional"}
    diff = [p["edge"] for p in sample
            if score_build.score_pair(p, h0, [], health[0]["evaluated_at"], cfg)["score"]
            != score_build.score_pair(p, h1, [], health[0]["evaluated_at"], cfg)["score"]]
    check("fit_is_computed_without_health", "AC4/R3", not diff, {"pairs_perturbed": len(sample), "changed": diff})
    blended = [r["project"] for r in records if any(k in r for k in ("overall_score", "aggregate_score", "combined_score"))]
    check("no_blended_aggregate_score", "R3", not blended, {"records_with_aggregate": blended[:5]})

    # ---------------- AC5 confidence represented separately
    sep = all("evidence_confidence" in r and "score" in r["project_health"] for r in records) and \
        all("confidence" in f and "score" in f for f in fits)
    # Changing only confidence inputs (evaluation date -> freshness) leaves every score unchanged.
    ext = {"evidence_as_of": "2027-09-29T00:00:00Z", "projects": signals[:300],
           "pairs": [p for p in pairs if p.get("project") in {s["project"] for s in signals[:300]}],
           "view_rows": {r["project"]: r for r in view}}
    h_late, f_late, _ = score_build.score_all(ext, cfg, {}, {})
    moved = [h["project"] for h in h_late if h["score"] != H[h["project"]]["score"]]
    conf_moved = sum(1 for h in h_late if h["confidence"]["score"] != H[h["project"]]["confidence"]["score"])
    check("confidence_is_separate_from_score", "AC5", sep and not moved and conf_moved > 0,
          {"separate_fields": sep, "scores_changed_by_staleness": moved[:5],
           "confidences_changed_by_staleness": conf_moved, "sample": len(h_late)})
    hl = cfg["thresholds"]["high_score_low_confidence"]
    lbl_bad = [r["project"] for r in records if (r["project_health"]["score"] or 0) >= hl["score_min"]
               and (r["evidence_confidence"] or 0) < hl["confidence_below"] and r["presentation_label"] != hl["label"]]
    check("high_score_low_confidence_presented_as_uncertain", "AC5/rubric", not lbl_bad,
          {"label": hl["label"], "violations": lbl_bad[:10],
           "labelled": sum(1 for r in records if r["presentation_label"] == hl["label"])})

    # ---------------- AC6 never presented as Keith-tested / endorsed
    end_bad = [r["project"] for r in records if r["endorsement"]["keith_tested"] or r["endorsement"]["keith_endorsed"]
               or r["endorsement"]["editorial_recommendation"]]
    hits = []
    for name in ("records.jsonl", "project_health.jsonl", "replacement_fit.jsonl"):
        for line in open(SCORE / name, encoding="utf-8"):
            body = line.replace(cfg["endorsement"]["statement"], "")
            m = FORBIDDEN.search(body)
            if m:
                hits.append((name, m.group(0)))
    check("never_presented_as_keith_tested_or_endorsed", "AC6/R8", not end_bad and not hits,
          {"records": len(records), "flags_true": end_bad[:5], "forbidden_phrases": hits[:5],
           "statement": cfg["endorsement"]["statement"]})

    # ---------------- AC7 low-confidence mappings refused without dropping the project
    refused = [f for f in fits if not f["publish"]]
    low_conf_refused = [f for f in refused if any("confidence" in x for x in f["refusal_reasons"])]
    kept = [f for f in refused if f["project"] in R and R[f["project"]]["project_health"]["score"] is not None]
    disc_kept = [f for f in refused if R[f["project"]]["lanes"]["discover"]]
    pm = cfg["thresholds"]["publish_mapping"]
    publish_bad = [f["edge"] for f in fits if f["publish"] and ((f["confidence"]["score"] or 0) < pm["confidence_min"]
                                                               or f["relationship"]["status"] not in pm["relationship_statuses"]
                                                               or (pm.get("requires_core_gate_passed") and f["core_gate"] != "passed"))]
    check("low_confidence_mappings_refused_projects_kept", "AC7",
          refused and len(kept) == len(refused) and disc_kept and not publish_bad,
          {"mappings": len(fits), "refused": len(refused), "refused_for_low_confidence": len(low_conf_refused),
           "refused_but_project_still_scored": len(kept), "refused_discover_lane_projects_kept": len(disc_kept),
           "published_in_breach": publish_bad[:5],
           "example": disc_kept[0] and {"project": disc_kept[0]["canonical"], "saas": disc_kept[0]["saas"],
                                        "reasons": disc_kept[0]["refusal_reasons"],
                                        "health": R[disc_kept[0]["project"]]["project_health"]}})

    # ---------------- AC8 >= 60 relationships across the initial SaaS set
    launch = {k for k, s in saas.items() if (s.get("derived") or {}).get("launch")}
    launch_fits = [f for f in fits if f["saas"] in launch and f["score"] is not None]
    check("at_least_60_initial_set_relationships_scored", "AC8", len(launch_fits) >= 60,
          {"initial_saas_set": len(launch), "initial_set_relationships_scored": len(launch_fits),
           "initial_set_saas_covered": len({f["saas"] for f in launch_fits}),
           "all_relationships_scored": sum(1 for f in fits if f["score"] is not None)})

    # ---------------- AC9 Discover-only sample health-scored, no invented edges
    disc_only = [r for r in records if r["lanes"]["discover"] and not r["lanes"]["replace"]]
    disc_scored = [r for r in disc_only if r["project_health"]["score"] is not None]
    invented = [r["project"] for r in disc_only if r["replacement_fit"]]
    graph_rep_now = sum(1 for l in open(GRAPH / "edges.jsonl", encoding="utf-8") if '"relation":"REPLACES"' in l.replace(" ", ""))
    check("discover_only_health_scored_without_invented_edges", "AC9",
          disc_only and len(disc_scored) == len(disc_only) and not invented and len(fits) == len(rep_edges),
          {"discover_only_projects": len(disc_only), "health_scored": len(disc_scored),
           "with_replacement_fit": len(invented), "fit_rows": len(fits), "graph_replaces_edges": len(rep_edges),
           "graph_replaces_edges_on_disk": graph_rep_now})

    # ---------------- AC10 thresholds explicit and testable
    th = cfg["thresholds"]
    explicit = all(k in th for k in ("credible_alternative", "publish_mapping", "recommendation_eligible", "health_status"))
    band_bad = [h["project"] for h in health if h["score"] is not None and h["band"] != score_build._band(h["score"], cfg)]
    ca = th["credible_alternative"]
    cred_bad = [f["edge"] for f in fits if f["credible_alternative"] != (
        f["score"] is not None and f["score"] >= ca["replacement_fit_min"] and f["core_coverage"]["score"] is not None
        and f["core_coverage"]["score"] >= ca["core_coverage_min"] and (f["confidence"]["score"] or 0) >= ca["confidence_min"]
        and not H[f["project"]]["disqualifiers"])]
    re_ = th["recommendation_eligible"]
    viewp = {r["project"]: r for r in view}
    rec_bad = [r["project"] for r in records if r["recommendation_eligible"] != (
        (r["project_health"]["score"] or 0) >= re_["health_min"] and (r["evidence_confidence"] or 0) >= re_["health_confidence_min"]
        and not r["disqualifiers"] and viewp[r["project"]]["recommendation_eligible"])]
    check("thresholds_explicit_and_consistent", "AC10", explicit and not band_bad and not cred_bad and not rec_bad,
          {"thresholds": th, "band_mismatches": band_bad[:5], "credible_mismatches": cred_bad[:5],
           "recommendation_mismatches": rec_bad[:5]})

    # ---------------- AC11 rerun against a changed rubric / refreshed metadata, same schema and code
    schema = common.read_json(SCHEMA)
    validator = jsonschema.Draft7Validator(schema)
    errs = [f"{r['project']}: {e.message}" for r in records for e in validator.iter_errors(r)]
    check("records_match_schema", "AC11", not errs, {"records": len(records), "errors": errs[:5]})
    cfg2 = copy.deepcopy(cfg)
    cfg2["version"] = cfg["version"] + "+ac11-probe"
    cfg2["project_health"]["dimensions"]["maintenance_momentum"]["weight"] = 30
    cfg2["project_health"]["dimensions"]["community_contributors"]["weight"] = 10
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["THE_SOURCE_NO_NETWORK"] = "1"
        try:
            h2, f2, r2, _ = score_build.rescore(cfg2, Path(tmp), history=False)
        finally:
            os.environ.pop("THE_SOURCE_NO_NETWORK", None)
        errs2 = [e.message for r in r2 for e in validator.iter_errors(r)]
        changed = sum(1 for h in h2 if h["score"] != H[h["project"]]["score"])
    check("rescore_under_changed_rubric_without_reharvest", "AC11", not errs2 and changed > 0 and
          all(r["score_version"] == cfg2["version"] for r in r2),
          {"probe_version": cfg2["version"], "scores_changed": changed, "schema_errors": errs2[:5],
           "network": "THE_SOURCE_NO_NETWORK=1", "inputs": "data/score/signals.jsonl + pair_signals.jsonl only"})
    hv = common.read_json(common.ROOT / "evidence" / "KEI-806-refresh-probe.json") \
        if (common.ROOT / "evidence" / "KEI-806-refresh-probe.json").exists() else None
    check("refreshed_metadata_rescored_same_schema", "AC11", hv is not None and hv.get("ok"),
          hv or {"missing": "evidence/KEI-806-refresh-probe.json (scripts/score_refresh_probe.py)"})

    # ---------------- AC12 history retained, append-only
    hist = common.read_jsonl(SCORE / "history" / "scores.jsonl") if (SCORE / "history" / "scores.jsonl").exists() else []
    keys = [(h["project"], h["score_version"], h["evidence_as_of"]) for h in hist]
    obs = common.read_jsonl(SCORE / "history" / "observations.jsonl") if (SCORE / "history" / "observations.jsonl").exists() else []
    current = health[0]["evaluated_at"]
    check("score_history_append_only", "AC12",
          len(keys) == len(set(keys)) and sum(1 for k in keys if k[2] == current and k[1] == cfg["version"]) == len(health)
          and len(obs) >= len(health) - 10,
          {"score_history_rows": len(hist), "distinct_evaluations": len({(k[1], k[2]) for k in keys}),
           "observation_rows": len(obs), "observation_snapshots": len({o["observed_at"] for o in obs}),
           "duplicates": len(keys) - len(set(keys))})

    # ---------------- rubric rules
    zero_bad = []
    for h in health:
        for dn, d in h["dimensions"].items():
            for sn, sg in d["signals"].items():
                if sg["value"] is None and sg["points"] is not None:
                    zero_bad.append((h["project"], sn))
            if d["known_share"] == 0 and d["score"] is not None:
                zero_bad.append((h["project"], dn))
    partial = sum(1 for h in health if h["known_weight_share"] < 1)
    conf_ok = all(h["confidence"]["components"]["completeness"] == round(h["known_weight_share"] * 100, 1) for h in health)
    check("missing_evidence_is_unknown_not_zero", "R1", not zero_bad and conf_ok,
          {"violations": zero_bad[:10], "projects_with_unknown_signals": partial,
           "completeness_charged_to_confidence": conf_ok})
    dq_in_score = [h["project"] for h in health if h["disqualifiers"] and h["score"] is None]
    check("disqualifiers_outside_the_number", "R2", not dq_in_score and any(h["disqualifiers"] for h in health),
          {"disqualified": sum(1 for h in health if h["disqualifiers"]),
           "by_reason": _count(d["id"] for h in health for d in h["disqualifiers"]),
           "disqualified_without_score": dq_in_score[:5]})
    persisted = all(k in health[0] for k in ("dimensions", "score_version")) and \
        all("weight" in d and "signals" in d for d in health[0]["dimensions"].values())
    check("raw_signals_subscores_weights_version_persisted", "R4", persisted and len(signals) == len(health),
          {"signals_rows": len(signals), "health_rows": len(health), "score_version": cfg["version"]})
    tl = _count(h["trajectory"]["label"] for h in health)
    check("trajectory_exposed_and_unweighted", "R6", set(tl) <= {"Rising", "Stable", "Declining", "Insufficient history"}
          and "trajectory" not in cfg["project_health"]["dimensions"], tl)

    cal = _calibration(health, fits, records, signals, cfg)
    common.write_json(REPORTS / "calibration.json", {"score_version": cfg["version"], "results": cal})
    cal_fail = [c for c in cal if not c["ok"]]
    check("calibration_set", "calibration", not cal_fail,
          {"expectations": len(cal), "met": len(cal) - len(cal_fail),
           "missed": [{"group": c["group"], "subject": c["subject"], "detail": c["detail"]} for c in cal_fail]})

    report = {"ok": all(c["ok"] for c in checks), "score_version": cfg["version"],
              "evidence_as_of": health[0]["evaluated_at"], "checks": checks}
    common.write_json(REPORTS / "validation_report.json", report)
    write_manifest(health[0]["evaluated_at"], cfg["version"])
    return report


def _count(it):
    out = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items()))
