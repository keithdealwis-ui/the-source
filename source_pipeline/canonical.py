"""Canonical dataset and consumer read surface (KEI-807).

Turns the validated, export-guarded launch corpus into the one dataset every consumer
reads: the Chrome extension, the de-alwis.com comparison pages and future Source
workflows. Nothing here calls a network; it reads only what earlier stages stored.

    data/canonical/            the canonical dataset (saas, projects, relationships, manifest)
    api/v1/                    the read surface: static, deterministic JSON per SaaS domain

Determinism: every output is a pure function of its inputs. No wall-clock time is
written; the only timestamps are observation times recorded by earlier stages. Running
the build twice on the same inputs produces byte-identical files, which is what lets a
failed or repeated weekly cycle be retried without duplicating or corrupting anything.

Only relationships that pass the public export guard (export.py, KEI-805 criterion 15)
become canonical, so nothing withheld for licence reasons can leak into a read surface.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from . import common

CANONICAL = common.DATA / "canonical"
API = common.ROOT / "api" / "v1"
SCORES = common.DATA / "scores"
SUPPORTED = common.CONFIG / "supported_saas.yaml"

CANONICAL_SCHEMA = "the-source.canonical/1"
API_VERSION = "v1"

DIMENSION_LABELS = {
    "core_use_case_coverage": "core use-case coverage",
    "feature_equivalence": "feature parity",
    "ease_of_adoption": "ease of adoption",
    "deployment_ownership": "self-hosting and data ownership",
    "migration_friction": "migration path",
    "integrations_extensibility": "integrations and extensibility",
    "production_commercial_readiness": "production readiness",
    "economic_advantage": "cost advantage",
}
STRENGTH_MIN = 75.0
GAP_MAX = 40.0


# ---------------------------------------------------------------- score extract

def _fit_summary(r: dict, path: str) -> dict:
    fams = sorted(((r.get("evidence") or {}).get("families") or {}).keys())
    return {
        "saas_id": r["saas"].split(":", 1)[1],
        "oss_id": r["canonical"],
        "score": r["score"],
        "credible_alternative": bool(r["credible_alternative"]),
        "publish": bool(r["publish"]),
        "core_gate": r["core_gate"],
        "confidence": r["confidence"]["score"],
        "dimensions": {k: v["score"] for k, v in sorted(r["dimensions"].items())},
        "evidence_families": fams,
        "refusal_reasons": r.get("refusal_reasons") or [],
        "score_version": r["score_version"],
        "evaluated_at": r["evaluated_at"],
        "scoring_path": path,
    }


def _health_summary(h: dict, path: str) -> dict:
    return {
        "oss_id": h["canonical"],
        "score": h["score"],
        "band": h["band"],
        "evidence_insufficient": h.get("evidence_insufficient", False),
        "trajectory": (h.get("trajectory") or {}).get("label"),
        "confidence": (h.get("confidence") or {}).get("score"),
        "watch_outs": sorted(w["id"] for w in h.get("watch_outs") or []),
        "disqualifiers": sorted(d["id"] for d in h.get("disqualifiers") or []),
        "score_version": h["score_version"],
        "evaluated_at": h["evaluated_at"],
        "scoring_path": path,
    }


def extract_scores() -> dict:
    """Compact the KEI-806 scoring lane's output to the projects in the canonical corpus.

    The full lane (data/score/, ~0.5 GB of evidence) runs where its evidence lives. The
    canonical repository carries only this extract; scores added later by the single-
    project path (score_one.py) are merged in, never overwritten by an older extract.
    """
    score_dir = common.DATA / "score"
    oss = {p["oss_id"] for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl")}
    fit, health = _load_scores()
    n_fit = n_health = 0
    for line in open(score_dir / "replacement_fit.jsonl", encoding="utf-8"):
        r = common.json.loads(line)
        if r["canonical"] in oss:
            s = _fit_summary(r, "full_lane")
            key = (s["saas_id"], s["oss_id"])
            if key not in fit or fit[key]["evaluated_at"] <= s["evaluated_at"]:
                fit[key] = s
                n_fit += 1
    for line in open(score_dir / "project_health.jsonl", encoding="utf-8"):
        h = common.json.loads(line)
        if h["canonical"] in oss:
            s = _health_summary(h, "full_lane")
            if h["canonical"] not in health or health[h["canonical"]]["evaluated_at"] <= s["evaluated_at"]:
                health[h["canonical"]] = s
                n_health += 1
    _write_scores(fit, health)
    return {"fit_rows": len(fit), "health_rows": len(health), "fit_from_full_lane": n_fit,
            "health_from_full_lane": n_health}


def _load_scores() -> tuple[dict, dict]:
    fit, health = {}, {}
    if (SCORES / "fit.jsonl").exists():
        for r in common.read_jsonl(SCORES / "fit.jsonl"):
            fit[(r["saas_id"], r["oss_id"])] = r
    if (SCORES / "health.jsonl").exists():
        for h in common.read_jsonl(SCORES / "health.jsonl"):
            health[h["oss_id"]] = h
    return fit, health


def _write_scores(fit: dict, health: dict) -> None:
    common.write_jsonl(SCORES / "fit.jsonl", [fit[k] for k in sorted(fit)])
    common.write_jsonl(SCORES / "health.jsonl", [health[k] for k in sorted(health)])


def append_score_history(fit_rows: list, health_rows: list) -> int:
    """Append-only score history, keyed (kind, project, pair, score_version, evaluated_at).
    Shared by every scoring path so manual intake and weekly discovery have one history."""
    path = SCORES / "history.jsonl"
    seen = set()
    if path.exists():
        for h in common.read_jsonl(path):
            seen.add((h["kind"], h["oss_id"], h.get("saas_id"), h["score_version"], h["evaluated_at"]))
    added = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for kind, rows in (("health", health_rows), ("fit", fit_rows)):
            for r in rows:
                k = (kind, r["oss_id"], r.get("saas_id"), r["score_version"], r["evaluated_at"])
                if k in seen:
                    continue
                seen.add(k)
                fh.write(common.dumps({"kind": kind, "oss_id": r["oss_id"], "saas_id": r.get("saas_id"),
                                       "score": r["score"], "confidence": r["confidence"],
                                       "score_version": r["score_version"], "evaluated_at": r["evaluated_at"],
                                       "scoring_path": r["scoring_path"]}) + "\n")
                added += 1
    return added


# ---------------------------------------------------------------- helpers

def load_supported() -> dict:
    return common.load_yaml(SUPPORTED)


def _latest_release(project: dict) -> dict | None:
    m = project["derived"].get("maintenance") or {}
    rel = [x for x in (m.get("evidence") or []) + (m.get("disregarded") or []) if x.get("kind") == "release"]
    if not rel:
        return None
    top = max(rel, key=lambda x: (_utc(x["at"]), x["ref"]))
    return {"tag": top["ref"], "published_at": _utc(top["at"])}


def _note(fit: dict | None) -> dict:
    """A short machine-derived best-fit / major-gap note. Labelled as derived, never editorial."""
    if not fit:
        return {"strengths": [], "gaps": [], "text": None, "origin": "derived"}
    dims = fit["dimensions"]
    strong = sorted((k for k, v in dims.items() if v is not None and v >= STRENGTH_MIN),
                    key=lambda k: (-dims[k], k))[:2]
    weak = sorted((k for k, v in dims.items() if v is not None and v < GAP_MAX), key=lambda k: (dims[k], k))[:2]
    parts = []
    if strong:
        parts.append("Strongest on " + " and ".join(DIMENSION_LABELS[k] for k in strong))
    if weak:
        parts.append("weakest on " + " and ".join(DIMENSION_LABELS[k] for k in weak))
    text = (("; ".join(parts)) + ".") if parts else None
    if text:
        text = text[0].upper() + text[1:]
    return {"strengths": [DIMENSION_LABELS[k] for k in strong], "gaps": [DIMENSION_LABELS[k] for k in weak],
            "text": text, "origin": "derived"}


def _self_hosting(fit: dict | None) -> dict:
    if not fit:
        return {"self_hostable": None, "basis": "not_scored"}
    if "self_host" in fit["evidence_families"]:
        return {"self_hostable": True, "basis": "self-hosting documented in the project's README"}
    if (fit["dimensions"].get("deployment_ownership") or 0) >= 100:
        return {"self_hostable": True, "basis": "container image or self-hosting evidence found"}
    return {"self_hostable": None, "basis": "no self-hosting evidence found; not evidence of absence"}


def _rank_key(rec: dict):
    fit = rec["replacement_fit"] or {}
    health = rec["project_health"] or {}
    return (-(fit.get("score") or 0), -(health.get("score") or 0), -(rec["stars"] or 0), rec["oss_id"])


def _utc(ts: str | None) -> str | None:
    """Every canonical timestamp is UTC with a Z suffix; hosts report some with offsets."""
    if not ts:
        return ts
    from .activity import iso, parse_ts

    return iso(parse_ts(ts))


def _sha(path: Path) -> str:
    return common.sha256_file(path)


# ---------------------------------------------------------------- build

def build(out_root: Path | None = None) -> dict:
    """Build data/canonical/ and api/v1/ under out_root (default: the repository root).

    Callers that must not disturb the live copy (the weekly cycle) build into a staging
    root and swap it in only after validation passes.
    """
    from . import export

    root = Path(out_root) if out_root else common.ROOT
    canon_dir = root / "data" / "canonical"
    api_dir = root / "api" / "v1"
    for d in (canon_dir, api_dir):
        if d.exists():
            shutil.rmtree(d)

    with tempfile.TemporaryDirectory() as tmp:
        guard = export.run(out=tmp)
        exported = common.read_jsonl(Path(tmp) / "relationships.jsonl")
        notice = (Path(tmp) / "NOTICE.md").read_text(encoding="utf-8")

    summary = common.read_json(common.DATASET / "summary.json")
    sources = {s["source_id"]: s for s in common.read_json(common.DATASET / "sources.json")}
    oss = {p["oss_id"]: p for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl")}
    saas = {s["saas_id"]: s for s in common.read_jsonl(common.DATASET / "saas_products.jsonl")}
    edges = {(e["saas_id"], e["oss_id"]): e for e in common.read_jsonl(common.DATASET / "relationships.jsonl")}
    fit_all, health_all = _load_scores()
    supported = load_supported()
    supported_ids = [p["saas_id"] for p in supported["products"]]
    unknown = [s for s in supported_ids if s not in saas]
    if unknown:
        raise ValueError(f"supported_saas.yaml names SaaS ids not in the catalogue: {unknown}")

    # --- relationships and the projects they reference
    rel_rows, project_ids = [], set()
    for row in exported:
        sid, oid = row["saas"]["id"], row["oss"]["id"]
        e = edges[(sid, oid)]
        fit = fit_all.get((sid, oid))
        rel_rows.append({
            "edge_id": e["edge_id"],
            "saas_id": sid,
            "oss_id": oid,
            "relation": e["relation"],
            "corroborated": e["corroborated"],
            "sources": row["sources"],
            "provenance": [p for p in e["provenance"] if p["source_id"] in row["sources"]],
            "validated_as_of": e["validation"]["validated_as_of"],
            "replacement_fit": None if fit is None else {
                k: fit[k] for k in ("score", "credible_alternative", "publish", "core_gate", "confidence",
                                    "dimensions", "score_version", "evaluated_at", "scoring_path")},
            "recommended": bool(fit and fit["publish"] and fit["credible_alternative"]),
        })
        project_ids.add(oid)
    rel_rows.sort(key=lambda r: (r["saas_id"], r["oss_id"]))

    proj_rows = []
    for oid in sorted(project_ids):
        p = oss[oid]
        live = p["live"]
        m = p["derived"]["maintenance"]
        h = health_all.get(oid)
        proj_rows.append({
            "oss_id": oid,
            "name": p["name"],
            "host": p["host"],
            "repo_url": p["repo_url"],
            "github_url": p["repo_url"] if p["host"] == "github.com" else None,
            "live": {
                "stars": live.get("stars"),
                "forks": live.get("forks"),
                "archived": live.get("archived"),
                "licence_spdx": live.get("licence_spdx"),
                "pushed_at": _utc(live.get("pushed_at")),
                "latest_release": _latest_release(p),
                "checked_at": live["fetched_at"],
            },
            "maintenance": {
                "status": m["status"],
                "last_meaningful_activity_at": _utc(m["last_meaningful_activity_at"]),
                "days_since_meaningful_activity": m["days_since_meaningful_activity"],
                "basis": m["basis"],
            },
            "licence": {"spdx": p["derived"]["licence"]["spdx"], "status": p["derived"]["licence"]["status"]},
            "project_health": None if h is None else {
                k: h[k] for k in ("score", "band", "trajectory", "confidence", "watch_outs", "evidence_insufficient",
                                  "score_version", "evaluated_at", "scoring_path")},
        })

    saas_rows = []
    for sid in sorted({r["saas_id"] for r in rel_rows} | set(supported_ids)):
        s = saas[sid]
        saas_rows.append({"saas_id": sid, "name": s["name"], "domain": s["domain"], "category": s["category"],
                          "aliases": s["aliases"], "supported": sid in supported_ids})

    common.write_jsonl(canon_dir / "saas_products.jsonl", saas_rows)
    common.write_jsonl(canon_dir / "oss_projects.jsonl", proj_rows)
    common.write_jsonl(canon_dir / "relationships.jsonl", rel_rows)
    (canon_dir / "NOTICE.md").write_text(notice, encoding="utf-8")

    checked = sorted(p["live"]["checked_at"] for p in proj_rows)
    live_checked_at = {"oldest": checked[0] if checked else None, "newest": checked[-1] if checked else None}
    files = {f.name: _sha(f) for f in sorted(canon_dir.iterdir()) if f.is_file()}
    dataset_version = hashlib.sha256(common.dumps(files).encode()).hexdigest()[:16]

    # --- read surface
    projects_by_id = {p["oss_id"]: p for p in proj_rows}
    cfg = supported
    lo, hi = cfg["recommendations"]["min"], cfg["recommendations"]["max"]
    index_rows, domain_rules = [], []
    for entry in cfg["products"]:
        sid = entry["saas_id"]
        s = saas[sid]
        recs = []
        for r in rel_rows:
            if r["saas_id"] != sid or not r["recommended"]:
                continue
            p = projects_by_id[r["oss_id"]]
            fit = fit_all[(sid, r["oss_id"])]
            recs.append({
                "oss_id": p["oss_id"], "name": p["name"], "repo_url": p["repo_url"], "github_url": p["github_url"],
                "stars": p["live"]["stars"], "licence_spdx": p["licence"]["spdx"],
                "maintenance_status": p["maintenance"]["status"],
                "last_meaningful_activity_at": p["maintenance"]["last_meaningful_activity_at"],
                "latest_release": p["live"]["latest_release"],
                "replacement_fit": {"score": r["replacement_fit"]["score"],
                                    "confidence": r["replacement_fit"]["confidence"],
                                    "score_version": r["replacement_fit"]["score_version"],
                                    "evaluated_at": r["replacement_fit"]["evaluated_at"]},
                "project_health": None if p["project_health"] is None else {
                    k: p["project_health"][k] for k in ("score", "band", "trajectory", "confidence")},
                "self_hosting": _self_hosting(fit),
                "note": _note(fit),
                "sources": r["sources"],
                "live_checked_at": p["live"]["checked_at"],
            })
        recs.sort(key=_rank_key)
        for i, rec in enumerate(recs, 1):
            rec["rank"] = i
        shown = recs[:hi]
        rec_checked = sorted(r["live_checked_at"] for r in shown)
        doc = {
            "api_version": API_VERSION,
            "dataset_version": dataset_version,
            "saas": {"saas_id": sid, "name": s["name"], "domain": s["domain"], "category": s["category"],
                     "slug": entry["slug"], "compare_path": entry["compare_path"]},
            "status": "ok" if len(shown) >= lo else "insufficient_recommendations",
            "recommendations": shown,
            "more_available": max(0, len(recs) - hi),
            "last_checked": {"oldest": rec_checked[0] if rec_checked else None,
                             "newest": rec_checked[-1] if rec_checked else None},
            "scoring": {"score_version": sorted({r["replacement_fit"]["score_version"] for r in shown}),
                        "methodology": "docs/METHODOLOGY.md"},
            "provenance": {"sources": sorted({x for r in shown for x in r["sources"]}), "notice": "data/canonical/NOTICE.md"},
        }
        common.write_json(api_dir / "saas" / f"{sid}.json", doc)
        index_rows.append({"saas_id": sid, "name": s["name"], "slug": entry["slug"], "path": f"saas/{sid}.json",
                           "compare_path": entry["compare_path"], "recommendations": len(shown),
                           "status": doc["status"]})
        for m in entry["match"]:
            domain_rules.append({"saas_id": sid, **m})

    domain_rules.sort(key=lambda d: (d.get("host") or "", d.get("host_suffix") or "", d.get("path_prefix") or "", d["saas_id"]))
    common.write_json(api_dir / "domains.json", {"api_version": API_VERSION, "dataset_version": dataset_version,
                                                  "match_semantics": cfg["match_semantics"], "rules": domain_rules})
    common.write_json(api_dir / "index.json", {
        "api_version": API_VERSION,
        "dataset_version": dataset_version,
        "canonical_schema": CANONICAL_SCHEMA,
        "data_as_of": summary["as_of"],
        "live_checked_at": live_checked_at,
        "supported_saas": index_rows,
        "domains": "domains.json",
    })

    manifest = {
        "canonical_schema": CANONICAL_SCHEMA,
        "dataset_version": dataset_version,
        "data_as_of": summary["as_of"],
        "live_checked_at": live_checked_at,
        "counts": {"saas_products": len(saas_rows), "oss_projects": len(proj_rows), "relationships": len(rel_rows),
                   "recommended_relationships": sum(r["recommended"] for r in rel_rows),
                   "supported_saas": len(index_rows),
                   "supported_saas_ok": sum(1 for r in index_rows if r["status"] == "ok")},
        "export_guard": {k: guard[k] for k in ("relationships_exported", "relationships_withheld_share_alike_only",
                                                "export_licence_constraint")},
        "sources": {sid: {"pinned_commit": sources[sid]["pinned_commit"], "decision": sources[sid]["decision"]}
                    for sid in sorted({x for r in rel_rows for x in r["sources"]})},
        "files": files,
    }
    common.write_json(canon_dir / "MANIFEST.json", manifest)
    return manifest
