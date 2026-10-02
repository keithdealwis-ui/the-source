"""Single-project scoring path (KEI-807, criteria 10 and 15).

The full scoring lane (KEI-806) scores the whole knowledge graph from ~0.5 GB of stored
evidence and runs where that evidence lives. A repository that arrives between full runs
(a manual intake submission, or a relationship the daily cycle has just discovered)
is scored here instead, through the SAME rubric functions, the same config/scoring.yaml
and the same score_version, from evidence observed for that one repository:

    GitHub metadata   score_harvest._ask   (the full lane's own GraphQL query)
    README text       score_harvest._fetch_readmes (redacted the same way)
    Project Health    score_build.score_health / health_confidence / disqualifiers / watch_outs
    Replacement Fit   score_build.extract_pair + score_pair against config/saas_jobs.yaml

What the full lane has that this path does not: graph-derived capability/use-case terms and
the KEI-811 deep record. Their absence makes this path conservative (fewer term hits, never
more), and every row it writes says `scoring_path: single_project`. The next full-lane run
supersedes these rows (canonical.extract_scores keeps the newest evaluation).
"""
from __future__ import annotations

from . import canonical, common, enrich
from . import score_build as sb
from . import score_harvest as sh


def _replaces_confidence(source_count: int, reviewed: bool) -> float:
    # graph_build.GraphBuilder.replaces_confidence, applied to one relationship.
    c = 0.7 if source_count <= 1 else 0.8 if source_count == 2 else 0.9
    return round(c - (0 if reviewed else 0.1), 3)


def score(key: str, live: dict, licence: dict, pairs: list[dict], as_of: str, http=None) -> dict:
    """Score one GitHub repository and, optionally, its SaaS pairs.

    live     the project's live observation (enrich), for licence status and repo id
    licence  build.licence_verdict output ({"spdx", "status", ...})
    pairs    [{"saas_id", "status", "source_count", "corroborated", "reviewed"}]
    Returns {"health": row|None, "fit": [rows], "errors": [..]}.
    """
    if not key.startswith("github.com/"):
        return {"health": None, "fit": [], "errors": ["single-project scoring supports github.com only"]}
    http = http or enrich.Http()
    cfg = sb.load_cfg()
    jobs = sb.load_jobs()
    overrides = sb.load_overrides()
    win = sh.windows(as_of)
    meta = sh._ask(http, [key], win, as_of)
    if meta.get(key, {}).get("error"):
        return {"health": None, "fit": [], "errors": [f"github_meta: {meta[key]['error']}"]}
    readmes = sh._fetch_readmes(http, [key], meta, as_of)
    owner, name = key.split("/")[1:3]
    st, rest = http.call(f"https://api.github.com/repos/{owner}/{name}", auth=True)
    owner_is_org = bool(rest and (rest.get("owner") or {}).get("type") == "Organization")

    project_id = f"github:{live.get('host_repo_id') or key}"
    row = {
        "canonical": key,
        "project": project_id,
        "licence": {"spdx": licence.get("spdx"), "status": licence.get("status") or "recognised"},
        "observations": [{"as_of": live.get("fetched_at"), "pushed_at": live.get("pushed_at"),
                          "stars": live.get("stars"), "forks": live.get("forks"),
                          "archived": live.get("archived")}],
        "owner": [{"edge": f"{project_id}|OWNED_BY|{'org' if owner_is_org else 'user'}:github:{owner}",
                   "to": f"{'org' if owner_is_org else 'user'}:github:{owner}"}],
    }
    p = sb.extract_project(row, meta, None, owner_is_org, cfg)
    readme_rec = readmes.get(key)
    readme_text = ((readme_rec or {}).get("text") or "").lower()
    h = sb.score_health(p, cfg)
    dq = sb.disqualifiers(p, cfg, overrides)
    wo = sb.watch_outs(p, cfg, readme_text)
    tr = sb.trajectory(p, [], cfg)
    conf = sb.health_confidence(p, h, as_of, cfg)
    health_full = {"project": project_id, "canonical": key, "score_version": cfg["version"], "evaluated_at": as_of,
                   **h, "trajectory": tr, "confidence": conf, "disqualifiers": dq, "watch_outs": wo}
    health = canonical._health_summary(health_full, "single_project")

    saas = {s["saas_id"]: s for s in common.read_jsonl(common.DATASET / "saas_products.jsonl")}
    fits, errors = [], []
    open_core = any(w["id"] == "open_core_markers" for w in wo)
    for pr in pairs:
        s = saas.get(pr["saas_id"])
        job = jobs.get(s["category"]) if s else None
        if not job:
            errors.append(f"{pr['saas_id']}: no job definition for its category; Replacement Fit not scored")
            continue
        edge = {"id": f"{project_id}|REPLACES|saas:{pr['saas_id']}", "to": f"saas:{pr['saas_id']}",
                "confidence": _replaces_confidence(pr.get("source_count") or 1, pr.get("reviewed", False)),
                "attrs": {"kei805_status": pr.get("status"), "corroborated": pr.get("corroborated", False),
                          "source_count": pr.get("source_count") or 1,
                          "recommendation_eligible": pr.get("status") == "validated"},
                "evidence": None}
        pair = sb.extract_pair(edge, p, row, readme_rec, s, job, [], [], cfg)
        pair["open_core"] = open_core
        fr = sb.score_pair(pair, h, dq, as_of, cfg, job=job)
        fr.update({"score_version": cfg["version"], "evaluated_at": as_of})
        fits.append(canonical._fit_summary(fr, "single_project"))
    return {"health": health, "fit": fits, "errors": errors,
            "readme_available": bool(readme_rec and readme_rec.get("text")),
            "description": (meta[key].get("repo") or {}).get("description"),
            "topics": sorted(n["topic"]["name"] for n in ((meta[key].get("repo") or {}).get("repositoryTopics") or {}).get("nodes") or []),
            "homepage": (meta[key].get("repo") or {}).get("homepageUrl"),
            "readme_text": readme_text}


def store(result: dict) -> int:
    """Merge single-project rows into data/scores/, newest evaluation wins; append history."""
    fit, health = canonical._load_scores()
    if result.get("health"):
        h = result["health"]
        if h["oss_id"] not in health or health[h["oss_id"]]["evaluated_at"] <= h["evaluated_at"]:
            health[h["oss_id"]] = h
    for f in result.get("fit") or []:
        k = (f["saas_id"], f["oss_id"])
        if k not in fit or fit[k]["evaluated_at"] <= f["evaluated_at"]:
            fit[k] = f
    canonical._write_scores(fit, health)
    return canonical.append_score_history(result.get("fit") or [], [result["health"]] if result.get("health") else [])
