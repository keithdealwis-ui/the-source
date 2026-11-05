"""Re-apply the current rubric's evidence caps to stored Replacement Fit rows (KEI-961).

    python -m source_pipeline scores-recap      network: reads each affected project's README (GitHub REST)

The scores in data/scores/fit.jsonl were computed where their evidence lives (the full lane) or by the single-project
path; this repository holds their dimensions, not the evidence. A rubric change that only ADDS caps (v1.3.0: the
out-of-scope feature cap and the corroboration cap, config/scoring.yaml) does not need a full rescore: the caps are
a deterministic function of the stored dimensions, the relationship's corroboration (data/dataset/
relationships.jsonl) and the project's own README. This command applies exactly the functions score_pair applies
(score_build.excluded_features / feature_scope_cap / corroboration / corroboration_cap) to every row whose
score_version is older than the rubric's, and marks it with the rubric version and a `recap` record (from which
version, when the README was read). Caps only ever lower a number. A row scored under the current version is left
alone, so the command is idempotent, and the next full or single-project scoring of a pair supersedes the recap.

Differences from a fresh score, by construction: the README read is today's (its observed_at is recorded); and a
ruled-out feature only caps the stored Feature equivalence, it is not first removed from the stored feature hits
(those are not held here), so a recapped row is never lower than a fresh score would be.
"""
from __future__ import annotations

import base64
import time

from . import canonical, common
from . import score_build as sb

API = "https://api.github.com/repos/{key}/readme"


def _fetch_readme(http, key: str) -> dict:
    """The project's README text as score-harvest records it (redacted, truncated), or an error."""
    from .score_harvest import README_MAX, redact

    owner_name = key.split("/", 1)[1]
    st, doc = http.call(API.format(key=owner_name), auth=True, tries=3)
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if st != 200 or not doc or doc.get("encoding") != "base64":
        return {"observed_at": at, "error": f"readme_{st}"}
    raw = base64.b64decode(doc["content"]).decode("utf-8", "replace")
    text, red = redact(raw[:README_MAX])
    return {"observed_at": at, "text": text, "redactions": red, "truncated": len(raw) > README_MAX}


def _weighted(dims: dict, weights: dict):
    known = {k: v for k, v in dims.items() if v is not None and k in weights}
    den = sum(weights[k] for k in known)
    return sum(weights[k] * v for k, v in known.items()) / den if den else None


def recap_row(row: dict, rel: dict | None, readme: dict | None, description: str | None, saas: dict | None,
              job: dict | None, scope: list, cfg: dict) -> dict:
    """One stored fit row under the current rubric's caps. Pure: no I/O."""
    rf = cfg["replacement_fit"]
    weights = {k: v["weight"] for k, v in rf["dimensions"].items()}
    saas_name = (saas or {}).get("name") or row["saas_id"]
    text = (readme or {}).get("text")
    own = "\n".join([text, description or ""]).lower() if text else None
    checklist = list(dict.fromkeys(list((job or {}).get("features") or []) + list(scope or [])))
    excluded = sb.excluded_features(own, checklist, cfg) if own else []
    dims = dict(row["dimensions"])
    raw = _weighted(dims, weights)
    score = row["score"]
    caps = []
    fcap = sb.feature_scope_cap(excluded, checklist, cfg)
    feat = dims.get("feature_equivalence")
    if fcap is not None and feat is not None and feat > fcap:
        caps.append(sb.feature_cap_entry(feat, fcap, excluded, saas_name))
        dims["feature_equivalence"] = fcap
        raw = _weighted(dims, weights)
        score = raw if row["core_gate"] == "passed" else min(raw, rf["core_gate"]["gate_cap"])
        score = min(score, row["score"])  # a cap only ever lowers
    own_named = sb.names_product_as_alternative(own, [saas_name] + list((saas or {}).get("aliases") or []), cfg) \
        if own else None
    corr = sb.corroboration(rel or {}, own_named, cfg)
    score, ccap = sb.corroboration_cap(score, corr, saas_name, cfg)
    if ccap:
        caps.append(ccap)
    ca = cfg["thresholds"]["credible_alternative"]
    credible = bool(row["credible_alternative"]) and score is not None and score >= ca["replacement_fit_min"]
    out = {**row, "score": sb.r1(score), "uncapped_score": sb.r1(raw), "caps": caps, "corroboration": corr,
           "dimensions": {k: sb.r1(v) for k, v in sorted(dims.items())}, "credible_alternative": credible,
           "score_version": cfg["version"],
           "recap": {"from_version": row["score_version"], "readme_observed_at": (readme or {}).get("observed_at"),
                     "readme_available": bool(text), "features_out_of_scope": excluded}}
    return out


def recap(http=None, readmes: dict | None = None) -> dict:
    """Recap every stored row older than the rubric. `readmes` (canonical key -> record) skips the network."""
    cfg = sb.load_cfg()
    jobs = sb.load_jobs()
    scope = sb.load_product_scope()
    fit, health = canonical._load_scores()
    rels = {(e["saas_id"], e["oss_id"]): e for e in common.read_jsonl(common.DATASET / "relationships.jsonl")}
    saas = {s["saas_id"]: s for s in common.read_jsonl(common.DATASET / "saas_products.jsonl")}
    desc = {}
    corpus = common.DATA / "corpus" / "projects.jsonl"
    if corpus.exists():
        for p in common.read_jsonl(corpus):
            desc[p["id"]] = p.get("description")
    todo = sorted(k for k, r in fit.items() if r["score_version"] != cfg["version"])
    keys = sorted({k[1] for k in todo})
    readmes = dict(readmes or {})
    missing = [k for k in keys if k not in readmes and k.startswith("github.com/")]
    if missing:
        from . import enrich

        http = http or enrich.Http()
        for k in missing:
            readmes[k] = _fetch_readme(http, k)
    before = {k: dict(fit[k]) for k in todo}
    for k in todo:
        sid, oid = k
        s = saas.get(sid)
        fit[k] = recap_row(fit[k], rels.get(k), readmes.get(oid), desc.get(oid), s,
                           jobs.get((s or {}).get("category")), scope.get(sid) or [], cfg)
    canonical._write_scores(fit, health)
    changed = [k for k in todo if fit[k]["score"] != before[k]["score"]]
    # history records numbers that changed; a row the caps left alone keeps its history entry
    added = canonical.append_score_history([fit[k] for k in changed], [])
    return {
        "score_version": cfg["version"], "rows_recapped": len(todo), "projects": len(keys),
        "readme_unavailable": sorted(k for k in keys if not (readmes.get(k) or {}).get("text"))[:50],
        "readme_unavailable_count": sum(1 for k in keys if not (readmes.get(k) or {}).get("text")),
        "score_changed": len(changed),
        "capped_uncorroborated": sum(1 for k in todo if any(c["id"] == "uncorroborated_pairing" for c in fit[k]["caps"])),
        "capped_feature_scope": sum(1 for k in todo if any(c["id"] == "feature_out_of_scope" for c in fit[k]["caps"])),
        "no_longer_above_80": sum(1 for k in todo if (before[k]["score"] or 0) > 80 >= (fit[k]["score"] or 0)),
        "lost_credible": sum(1 for k in todo if before[k]["credible_alternative"] and not fit[k]["credible_alternative"]),
        "history_rows_added": added,
        "http_failures": len(getattr(http, "failures", []) or []),
    }
