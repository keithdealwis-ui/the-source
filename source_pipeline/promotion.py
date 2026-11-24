"""Promotion queue: strong discovery-tier pairings go through full scoring (stress test 2026-11-24).

Quick scoring (quick_score.py) covers the whole discovery corpus, but before this queue only pairs that were already
in data/dataset and alphabetically inside the new-pair budget reached the full evaluation, so a project like
Vaultwarden (71k stars, quick Fit 80) could sit in the discovery tier indefinitely. Each cycle, before the harvest,
this module selects every quick pairing with quick Fit >= `min_quick_fit` and stars >= `min_stars`
(config/schedule.yaml scoring.promotion) that the canonical layer does not hold, and:

  * a pair whose evidence is the project's own statement (quick evidence `description` / `topic`, which is
    intake.find_evidence, the same rule as manual intake) and that data/dataset does not hold yet is admitted to the
    intake ledger (data/intake/admitted/promotion-<hash>.json). The same cycle harvests it as a self-described
    relationship, so it goes through the same build validation (licence, activity, plausibility, review queue) as
    every other relationship. Promotion admits a candidate; it never validates or recommends one.
  * a pair data/dataset already holds is placed first in the cycle's new-pair scoring (cycle._score_new) when it is
    validated and not yet full-scored, counted (`full_scored_not_canonical`) when the full evaluation has scored it
    and the canonical recommendation gate decided, and reported as `blocked` with the build's own reasons when it is
    not validated (for example an open contradictory-evidence hold in data/dataset/review_queue.jsonl, which only a
    human can resolve: Vaultwarden and cal.diy on 2026-11-24).

No network, no score is changed, no threshold is bypassed.
"""
from __future__ import annotations

import hashlib

from . import common

DEFAULTS = {"min_quick_fit": 75, "min_stars": 5000, "max_admissions_per_cycle": 20}
SELF_DESCRIBED = ("description", "topic")
PREFIX = "promotion-"


def load_cfg(schedule: dict | None = None) -> dict:
    if schedule is None:
        schedule = common.load_yaml(common.CONFIG / "schedule.yaml")
    return {**DEFAULTS, **(((schedule or {}).get("scoring") or {}).get("promotion") or {})}


def _rows(path):
    return common.read_jsonl(path) if path.exists() else []


def _request_id(oss_id: str) -> str:
    return PREFIX + hashlib.sha256(oss_id.encode()).hexdigest()[:16]


def candidates(cfg: dict | None = None) -> list[dict]:
    """Quick pairings over both thresholds that the canonical layer does not hold, strongest first. `full_scored` says
    whether the full evaluation has already scored the pair."""
    cfg = cfg or load_cfg()
    stars = {r["id"]: r.get("stars") or 0 for r in _rows(common.DATA / "corpus" / "live.jsonl")}
    scored = {(r["saas_id"], r["oss_id"]) for r in _rows(common.DATA / "scores" / "fit.jsonl")}
    canon = {(r["saas_id"], r["oss_id"]) for r in _rows(common.DATA / "canonical" / "relationships.jsonl")}
    out = []
    for q in _rows(common.DATA / "quick" / "pairings.jsonl"):
        fit = (q.get("quick_fit") or {}).get("score") or 0
        s = stars.get(q["oss_id"], 0)
        if fit < cfg["min_quick_fit"] or s < cfg["min_stars"] or (q["saas_id"], q["oss_id"]) in canon:
            continue
        if q.get("licence_eligibility") not in (None, "eligible"):
            continue  # a licence the discovery lane already doubts is the build's question, not the queue's
        out.append({"oss_id": q["oss_id"], "saas_id": q["saas_id"], "saas_name": q.get("saas_name"),
                    "category": q.get("category"), "quick_fit": fit, "stars": s,
                    "full_scored": (q["saas_id"], q["oss_id"]) in scored,
                    "evidence_sources": sorted({e["source"] for e in q.get("evidence") or []})})
    out.sort(key=lambda c: (-c["quick_fit"], -c["stars"], c["oss_id"], c["saas_id"]))
    return out


def run(cfg: dict | None = None) -> dict:
    """Admit the self-described candidates to the intake ledger; report the rest. Returns the cycle strategy record
    plus `priority`, the oss_ids cycle._score_new asks first."""
    cfg = cfg or load_cfg()
    from . import intake

    rel = {(e["saas_id"], e["oss_id"]): e for e in _rows(common.DATASET / "relationships.jsonl")}
    ds = {p["oss_id"]: p for p in _rows(common.DATASET / "oss_projects.jsonl")}
    corpus = {p["id"]: p for p in _rows(common.DATA / "corpus" / "projects.jsonl")}
    ledger = {r["oss_id"]: r for r in intake.ledger_rows()}
    admit: dict[str, list] = {}
    priority, blocked, queued, gated = [], [], 0, 0
    for c in candidates(cfg):
        queued += 1
        e = rel.get((c["saas_id"], c["oss_id"]))
        if e is not None:
            if e["validation"]["status"] == "validated":
                if c["full_scored"]:
                    gated += 1  # through full scoring already; the canonical recommendation gate decided
                elif c["oss_id"] not in priority:
                    priority.append(c["oss_id"])
            else:
                blocked.append({**{k: c[k] for k in ("oss_id", "saas_id", "quick_fit", "stars")},
                                "status": e["validation"]["status"], "reasons": e["validation"]["reasons"][:5]})
            continue
        if not set(c["evidence_sources"]) & set(SELF_DESCRIBED):
            continue  # a replace_lane pairing is always in data/dataset; nothing to admit
        if c["oss_id"] in ledger and c["saas_id"] in ledger[c["oss_id"]].get("saas_ids", []):
            if c["oss_id"] in ds and c["oss_id"] not in priority:
                priority.append(c["oss_id"])
            continue
        admit.setdefault(c["oss_id"], []).append(c)
    admitted = []
    for oid in sorted(admit, key=lambda o: (-max(c["quick_fit"] for c in admit[o]), o)):
        if len(admitted) >= int(cfg["max_admissions_per_cycle"]):
            break
        p = corpus.get(oid) or {}
        cs = sorted(admit[oid], key=lambda c: c["saas_id"])
        prev = ledger.get(oid)
        if prev and not prev["request_id"].startswith(PREFIX):
            continue  # a human intake row is never rewritten; its saas_ids are the submitter's evidence
        ids = sorted(set((prev or {}).get("saas_ids", [])) | {c["saas_id"] for c in cs})
        names = {**dict(zip((prev or {}).get("saas_ids", []), (prev or {}).get("saas_names", []))),
                 **{c["saas_id"]: c["saas_name"] for c in cs}}
        lic = (p.get("licence") or {}).get("spdx")
        row = {"request_id": _request_id(oid), "oss_id": oid,
               "oss_name": p.get("name") or oid.rsplit("/", 1)[-1],
               "repo_url": "https://" + (f"{oid.split('/', 1)[0]}/{p['full_name']}" if p.get("full_name") else oid),
               "saas_ids": ids, "saas_names": [names[s] for s in ids], "category": cs[0]["category"],
               "declared_licence": lic, "admitted_at": (prev or {}).get("admitted_at") or intake._now(),
               "admitted_by": "promotion_queue",
               "basis": [{"saas_id": c["saas_id"], "quick_fit": c["quick_fit"], "stars": c["stars"],
                          "evidence": c["evidence_sources"]} for c in cs]}
        if prev != row:
            common.write_json(intake.LEDGER / f"{row['request_id']}.json", row)
        admitted.append({"oss_id": oid, "saas_ids": ids})
    return {"id": "promotion_queue", "status": "ok", "thresholds": {k: cfg[k] for k in DEFAULTS},
            "candidates": queued, "admitted_to_intake_ledger": admitted,
            "deferred_admissions": max(0, len(admit) - len(admitted)),
            "scoring_priority": priority, "full_scored_not_canonical": gated, "blocked": blocked[:50], "blocked_count": len(blocked)}
