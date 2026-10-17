"""Quick scoring of the whole discovery corpus (KEI-921, The Source V2.1 WP4).

    python -m source_pipeline quick-score [--full]   incremental by default; no network
    python -m source_pipeline quick-validate         labels, coverage, recompute, no-recommendation rule

Quick Health is a programmatic score over the repository facts data/corpus/ already
publishes: last push, stars, forks, latest release, archived, and the Discover lane's
maintenance evaluation. Quick pairings are SaaS products a project's own description or
topics name as the thing it replaces ("alternative to X", "<x>-alternative"), plus the
Replace lane's upstream-listed pairings. Quick Fit is a lightweight score on each such
pairing: how it was detected, category overlap, quick Health and licence eligibility.

Every quick row is labelled `quick`. It carries `recommendation_eligible: false` and
`publish: false`, and it is uncalibrated. Nothing here can make a project recommended.
Recommendation and publication come only from the full evaluation (data/scores/, the
canonical layer). quick-validate enforces that.

Incremental: each project and each pairing has a fingerprint over exactly the inputs that
can move its score: the facts, the recency buckets at run time, the catalogue and the
config. A repeat run rescores only rows whose fingerprint changed and keeps the rest
byte-identical. Every run records its runtime, API calls (always 0), rescored, unchanged,
deferred and failed counts in MANIFEST.json and data/quick/runs.jsonl.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import common
from .intake import find_evidence
from .normalise import load_catalogue

SCHEMA = "the-source.quick/1"
HEALTH_METHOD = "the-source.quick-health/1"
FIT_METHOD = "the-source.quick-fit/1"
LABEL = "quick"
OUT = common.DATA / "quick"
CORPUS = common.DATA / "corpus"
FILES = ("health.jsonl", "pairings.jsonl")
COVERAGE_TARGET = 95.0
NOT_A_RECOMMENDATION = ("quick score: programmatic and uncalibrated; never a recommendation. Only the full "
                        "evaluation (Project Health and Replacement Fit in the canonical layer) recommends")
STOP = {"and", "the", "of", "for", "a", "an", "to", "management", "platform", "software", "tools", "tool",
        "service", "services", "app", "apps"}


class QuickError(RuntimeError):
    pass


def load_cfg() -> dict:
    return common.load_yaml(common.CONFIG / "quick_score.yaml")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse(ts):
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def _digest(obj) -> str:
    return hashlib.sha256(common.dumps(obj).encode()).hexdigest()[:16]


def licence_eligibility(p: dict) -> str:
    """Same rule the read surface serves (mcp_server.corpus_licence_eligibility)."""
    lic = p["licence"]
    if lic["status"] == "recognised":
        return "eligible"
    status = (lic.get("eligibility") or {}).get("status")
    return status if status in ("eligible", "conditional") else "not_eligible"


def _bucket(age_days, buckets):
    """Index of the first bucket whose limit is >= age, len(buckets) past the last, None if unknown."""
    if age_days is None:
        return None
    for i, (limit, _) in enumerate(buckets):
        if age_days <= limit:
            return i
    return len(buckets)


def _bucket_value(i, buckets):
    return 0.0 if i is None or i >= len(buckets) else float(buckets[i][1])


def _band(score, bands):
    for floor, name in bands:
        if score >= floor:
            return name
    return bands[-1][1]


def _log_share(n, full):
    if n is None:
        return None
    return min(1.0, math.log10(max(0, n) + 1) / math.log10(full + 1))


# -- Quick Health ---------------------------------------------------------------------------------

def health_facts(p: dict, live: dict) -> dict:
    rel = live.get("latest_release")
    return {"pushed_at": live.get("pushed_at"), "stars": live.get("stars"), "forks": live.get("forks"),
            "latest_release_at": (rel or {}).get("published_at"), "has_release": rel is not None,
            "archived": bool(live.get("archived")), "found": bool(live.get("found")),
            "maintenance": (p.get("maintenance") or {}).get("status"), "observed_as_of": live.get("observed_as_of")}


def _ages(facts: dict, at: datetime) -> dict:
    def age(ts):
        d = _parse(ts)
        return None if d is None else max(0.0, (at - d).total_seconds() / 86400)
    return {"push": age(facts["pushed_at"]), "release": age(facts["latest_release_at"]),
            "observed": age(facts["observed_as_of"])}


def health_buckets(facts: dict, at: datetime, cfg: dict) -> dict:
    """The time-dependent part of a health score, as bucket indices: the fingerprint carries these."""
    h, a = cfg["health"], _ages(facts, at)
    return {"push": _bucket(a["push"], h["activity"]["buckets"]),
            "release": _bucket(a["release"], h["releases"]["buckets"]),
            "stale": a["observed"] is not None and a["observed"] > h["stale_facts_days"]}


def score_health(oss_id: str, facts: dict, at: datetime, cfg: dict) -> dict:
    """Pure function of (facts, at, cfg): quick-validate recomputes every row from its own evaluated_at."""
    h = cfg["health"]
    b = health_buckets(facts, at, cfg)
    parts = {
        "activity": (_bucket_value(b["push"], h["activity"]["buckets"]) if facts["pushed_at"] else None),
        "adoption": _log_share(facts["stars"], h["adoption"]["full"]),
        "community": _log_share(facts["forks"], h["community"]["full"]),
        "releases": (_bucket_value(b["release"], h["releases"]["buckets"]) if facts["has_release"] else 0.0),
        "maintenance": (h["maintenance"]["status"].get(facts["maintenance"], 0.0)
                        if facts["maintenance"] else None),
    }
    components = {k: (None if v is None else round(v * h[k]["weight"], 1)) for k, v in parts.items()}
    known = [k for k, v in parts.items() if v is not None]
    weight_known = sum(h[k]["weight"] for k in known)
    weight_all = sum(h[k]["weight"] for k in parts)
    raw = sum(components[k] for k in known)
    # missing facts are not scored as zero: the score is over what is known, and confidence says how much that was
    score = round(raw * weight_all / weight_known, 1) if weight_known else 0.0
    watch_outs = []
    if facts["archived"]:
        score = min(score, float(h["archived_cap"]))
        watch_outs.append("archived")
    if not facts["has_release"]:
        watch_outs.append("no_release")
    if b["push"] is not None and b["push"] >= len(h["activity"]["buckets"]):
        watch_outs.append("no_push_in_2_years")
    confidence = 100.0 * weight_known / weight_all
    if b["stale"]:
        confidence *= 0.8
        watch_outs.append("facts_stale")
    return {"oss_id": oss_id, "label": LABEL, "method": HEALTH_METHOD, "score": score,
            "band": _band(score, h["bands"]), "confidence": round(confidence, 1),
            "confidence_meaning": "data completeness (share of the score's facts known), not calibrated",
            "components": components, "facts": facts, "watch_outs": watch_outs, "evaluated_at": _iso(at),
            "recommendation_eligible": False, "publish": False}


# -- Quick pairings and quick Fit -----------------------------------------------------------------

def _words(text) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOP and len(w) > 2}


def detect_pairings(p: dict, catalogue: list, replace_edges: dict) -> list:
    """SaaS products a project is detectably paired with: its own description and topics, then the Replace lane."""
    found = {}
    for ev in find_evidence(p.get("description"), p.get("topics") or [], "", catalogue):
        found[ev["saas_id"]] = {"saas_id": ev["saas_id"], "saas_name": ev["saas_name"], "category": ev["category"],
                                "evidence": [{"source": "topic" if ev["field"] == "topics" else "description",
                                              "matched": ev["matched"], "excerpt": ev["excerpt"]}]}
    by_id = {s["saas_id"]: s for s in catalogue}
    for sid in replace_edges.get(p["id"], ()):
        s = by_id.get(sid)
        if s is None:
            continue
        row = found.setdefault(sid, {"saas_id": sid, "saas_name": s["name"], "category": s["category"],
                                     "evidence": []})
        row["evidence"].append({"source": "replace_lane", "matched": "upstream-listed pairing (data/dataset)",
                                "excerpt": None})
    return [found[k] for k in sorted(found)]


def score_fit(p: dict, pairing: dict, health: dict | None, eligibility: str, cfg: dict) -> dict:
    f = cfg["fit"]
    evidence = max(f["evidence"][e["source"]] for e in pairing["evidence"])
    cat = _words(pairing["category"])
    text = _words(" ".join([p.get("name") or "", p.get("description") or "", " ".join(p.get("topics") or [])]))
    category = (len(cat & text) / len(cat)) if cat else 0.0
    hscore = (health["score"] / 100.0) if health else 0.0
    lic = f["licence"].get(eligibility, 0.0)
    w = f["weights"]
    components = {"evidence": round(evidence * w["evidence"], 1), "category": round(category * w["category"], 1),
                  "health": round(hscore * w["health"], 1), "licence": round(lic * w["licence"], 1)}
    score = round(sum(components.values()), 1)
    confidence = round(min(evidence * 100.0, health["confidence"] if health else 0.0), 1)
    return {"score": score, "band": _band(score, f["bands"]), "confidence": confidence, "components": components,
            "confidence_meaning": "the weaker of pairing evidence strength and quick Health completeness; "
                                  "not calibrated"}


# -- Inputs, state, run ---------------------------------------------------------------------------

def _load_corpus() -> tuple[list, dict, str]:
    m = common.read_json(CORPUS / "MANIFEST.json")
    projects = common.read_jsonl(CORPUS / "projects.jsonl")
    live = {r["id"]: r for r in common.read_jsonl(CORPUS / "live.jsonl")}
    if set(live) != {p["id"] for p in projects}:
        raise QuickError("data/corpus live.jsonl and projects.jsonl name different projects")
    return projects, live, m["corpus_version"]


def _replace_edges() -> dict:
    out = {}
    path = common.DATASET / "relationships.jsonl"
    if path.exists():
        for e in common.read_jsonl(path):
            out.setdefault(e["oss_id"], set()).add(e["saas_id"])
    return out


def _full_scored() -> tuple[set, set]:
    """(projects, (saas, oss) pairs) that the full evaluation has scored."""
    base = common.DATA / "scores"
    projects = {r["oss_id"] for r in common.read_jsonl(base / "health.jsonl")} if (base / "health.jsonl").exists() else set()
    pairs = {(r["saas_id"], r["oss_id"]) for r in common.read_jsonl(base / "fit.jsonl")} if (base / "fit.jsonl").exists() else set()
    return projects, pairs


def _canonical_ids() -> set:
    path = common.DATA / "canonical" / "oss_projects.jsonl"
    return {p["oss_id"] for p in common.read_jsonl(path)} if path.exists() else set()


def _previous(out: Path) -> tuple[dict, dict]:
    h = {r["oss_id"]: r for r in common.read_jsonl(out / "health.jsonl")} if (out / "health.jsonl").exists() else {}
    pr = {(r["oss_id"], r["saas_id"]): r for r in common.read_jsonl(out / "pairings.jsonl")} \
        if (out / "pairings.jsonl").exists() else {}
    return h, pr


def run(now: datetime | None = None, full: bool = False, out: Path = OUT) -> dict:
    t0 = time.monotonic()
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    cfg = load_cfg()
    caps = cfg["caps"]
    products, _ = load_catalogue()
    catalogue = [products[k] for k in sorted(products)]
    config_digest = _digest({"cfg": {k: cfg[k] for k in ("health", "fit")}, "health": HEALTH_METHOD,
                             "fit": FIT_METHOD})
    catalogue_digest = _digest(catalogue)
    projects, live, corpus_version = _load_corpus()
    replace = _replace_edges()
    full_projects, full_pairs = _full_scored()
    canonical = _canonical_ids()
    prev_h, prev_p = ({}, {}) if full else _previous(out)

    stats = {"projects": len(projects), "health_rescored": 0, "health_unchanged": 0, "not_scored": {},
             "pairings_rescored": 0, "pairings_unchanged": 0, "pairings_removed": 0, "deferred": 0, "failures": []}
    budget = int(caps["max_rescore_per_run"])
    health_rows, pair_rows = [], []

    def over_budget():
        return budget <= 0 or (time.monotonic() - t0) > float(caps["max_runtime_s"])

    for p in projects:
        pid = p["id"]
        lv = live[pid]
        elig = licence_eligibility(p)
        facts = health_facts(p, lv)
        h = None
        if not facts["found"]:
            stats["not_scored"]["host_not_found"] = stats["not_scored"].get("host_not_found", 0) + 1
        else:
            fp = _digest({"facts": {k: v for k, v in facts.items() if k != "observed_as_of"},
                          "buckets": health_buckets(facts, now, cfg), "config": config_digest})
            old = prev_h.get(pid)
            if old and old.get("fingerprint") == fp:
                h = old
                stats["health_unchanged"] += 1
            elif old and over_budget():
                h = {**old, "deferred": True}
                stats["deferred"] += 1
            else:
                try:
                    h = {**score_health(pid, facts, now, cfg), "fingerprint": fp}
                    budget -= 1
                    stats["health_rescored"] += 1
                except Exception as exc:  # recorded, never swallowed; the previous score (if any) is kept
                    stats["failures"].append({"oss_id": pid, "stage": "health", "error": f"{type(exc).__name__}: {exc}"})
                    h = old
            if h is not None:
                h = {**h, "licence_eligibility": elig, "in_canonical_layer": pid in canonical,
                     "full_evaluation": pid in full_projects}
                health_rows.append(h)
        for pairing in detect_pairings(p, catalogue, replace):
            key = (pid, pairing["saas_id"])
            fp = _digest({"pairing": pairing, "health": h and h["score"], "hconf": h and h["confidence"],
                          "elig": elig, "text": [p.get("name"), p.get("description"), p.get("topics")],
                          "catalogue": catalogue_digest, "config": config_digest})
            old = prev_p.get(key)
            if old and old.get("fingerprint") == fp:
                row = old
                stats["pairings_unchanged"] += 1
            elif old and over_budget():
                row = {**old, "deferred": True}
                stats["deferred"] += 1
            else:
                try:
                    row = {"oss_id": pid, **pairing, "label": LABEL, "method": FIT_METHOD,
                           "quick_fit": score_fit(p, pairing, h, elig, cfg), "quick_health": h and h["score"],
                           "evaluated_at": _iso(now), "fingerprint": fp, "recommendation_eligible": False,
                           "publish": False}
                    budget -= 1
                    stats["pairings_rescored"] += 1
                except Exception as exc:
                    stats["failures"].append({"oss_id": pid, "saas_id": pairing["saas_id"], "stage": "fit",
                                              "error": f"{type(exc).__name__}: {exc}"})
                    row = old
            if row is not None:
                row = {**row, "licence_eligibility": elig, "in_canonical_layer": pid in canonical,
                       "full_evaluation": key[::-1] in full_pairs}
                pair_rows.append(row)
    stats["pairings_removed"] = len(set(prev_p) - {(r["oss_id"], r["saas_id"]) for r in pair_rows})

    out.mkdir(parents=True, exist_ok=True)
    common.write_jsonl(out / "health.jsonl", health_rows)
    common.write_jsonl(out / "pairings.jsonl", pair_rows)
    elig_ids = {p["id"] for p in projects if licence_eligibility(p) != "not_eligible"}
    scored_elig = {r["oss_id"] for r in health_rows} & elig_ids
    paired = {r["oss_id"] for r in pair_rows}
    run_rec = {"at": _iso(now), "mode": "full" if full or not prev_h else "incremental",
               "runtime_s": round(time.monotonic() - t0, 2), "api_calls": 0,
               "api_calls_allowed": caps["api_calls_allowed"], **stats,
               "failures": stats["failures"][:50], "failure_count": len(stats["failures"]),
               "caps": {k: caps[k] for k in ("max_rescore_per_run", "max_runtime_s")}}
    files = {f: common.sha256_file(out / f) for f in FILES}
    manifest = {
        "schema": SCHEMA, "as_of": _iso(now), "label": LABEL, "rule": NOT_A_RECOMMENDATION,
        "methods": {"health": HEALTH_METHOD, "fit": FIT_METHOD, "pairing": "source_pipeline/intake.find_evidence "
                    "on description and topics, plus Replace lane pairings (data/dataset/relationships.jsonl)"},
        "methodology": "docs/QUICK-SCORING.md", "calibration": "none (V3)",
        "config_digest": config_digest, "catalogue_digest": catalogue_digest, "corpus_version": corpus_version,
        "files": files, "quick_version": _digest(files),
        "counts": {"projects": len(projects), "eligible": len(elig_ids), "health_rows": len(health_rows),
                   "eligible_with_health": len(scored_elig), "pairings": len(pair_rows),
                   "paired_projects": len(paired),
                   "paired_projects_with_fit": len({r["oss_id"] for r in pair_rows if r.get("quick_fit")}),
                   "health_bands": _tally(r["band"] for r in health_rows),
                   "fit_bands": _tally(r["quick_fit"]["band"] for r in pair_rows),
                   "pairings_by_evidence": _tally(e["source"] for r in pair_rows for e in r["evidence"]),
                   "recommendation_eligible": sum(bool(r.get("recommendation_eligible")) for r in health_rows + pair_rows)},
        "coverage": {"eligible_health_pct": round(100.0 * len(scored_elig) / len(elig_ids), 2) if elig_ids else 0.0,
                     "target_pct": COVERAGE_TARGET},
        "last_run": run_rec,
    }
    common.write_json(out / "MANIFEST.json", manifest)
    hist = common.read_jsonl(out / "runs.jsonl") if (out / "runs.jsonl").exists() else []
    common.write_jsonl(out / "runs.jsonl", (hist + [run_rec])[-int(caps["history_runs_kept"]):])
    return manifest


def _tally(values) -> dict:
    out = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


# -- Validation -----------------------------------------------------------------------------------

def validate(out: Path = OUT) -> dict:
    checks = []

    def check(cid, ok, detail=""):
        checks.append({"id": cid, "ok": bool(ok), "detail": detail})

    m = common.read_json(out / "MANIFEST.json")
    check("schema", m.get("schema") == SCHEMA, m.get("schema"))
    for f in FILES:
        check(f"sha256:{f}", common.sha256_file(out / f) == m["files"][f])
    check("quick_version", m["quick_version"] == _digest(m["files"]))
    cfg = load_cfg()
    health = common.read_jsonl(out / "health.jsonl")
    pairs = common.read_jsonl(out / "pairings.jsonl")
    projects, live, corpus_version = _load_corpus()
    check("corpus_version", m["corpus_version"] == corpus_version,
          f"scored against {m['corpus_version']}, corpus is {corpus_version}")
    by_id = {p["id"]: p for p in projects}
    bad_label = [r["oss_id"] for r in health + pairs
                 if r.get("label") != LABEL or r.get("recommendation_eligible") is not False or r.get("publish") is not False]
    check("every_row_labelled_quick_and_never_recommendation_eligible", not bad_label, bad_label[:10])
    mismatch = []
    for r in health:
        p = by_id.get(r["oss_id"])
        if p is None:
            mismatch.append((r["oss_id"], "not in corpus"))
            continue
        want = score_health(r["oss_id"], r["facts"], _parse(r["evaluated_at"]), cfg)
        if (want["score"], want["band"], want["confidence"]) != (r["score"], r["band"], r["confidence"]):
            mismatch.append((r["oss_id"], "recompute differs"))
    check("health_recomputes_from_its_facts", not mismatch, mismatch[:10])
    elig = {pid for pid, p in by_id.items() if licence_eligibility(p) != "not_eligible"}
    covered = elig & {r["oss_id"] for r in health}
    pct = 100.0 * len(covered) / len(elig) if elig else 0.0
    check("eligible_health_coverage_ge_95pct", pct >= COVERAGE_TARGET, f"{len(covered)}/{len(elig)} = {pct:.2f}%")
    products, _ = load_catalogue()
    catalogue = [products[k] for k in sorted(products)]
    replace = _replace_edges()
    want_pairs = {(p["id"], d["saas_id"]) for p in projects for d in detect_pairings(p, catalogue, replace)}
    have = {(r["oss_id"], r["saas_id"]) for r in pairs if r.get("quick_fit") and r["quick_fit"].get("score") is not None}
    check("every_detectable_pairing_has_quick_fit", want_pairs <= have,
          f"{len(want_pairs - have)} detectable pairings without quick Fit; {len(want_pairs)} detectable")
    # zero quick-score-only recommendations: every recommended relationship in the canonical layer and the
    # read surface is backed by a full Replacement Fit evaluation, never a quick one
    _, full_pairs = _full_scored()
    rel_path = common.DATA / "canonical" / "relationships.jsonl"
    recs = [(e["saas_id"], e["oss_id"]) for e in common.read_jsonl(rel_path) if e.get("recommended")] \
        if rel_path.exists() else []
    quick_only = [r for r in recs if r not in full_pairs]
    check("zero_quick_only_recommendations_canonical", not quick_only, quick_only[:10])
    api = []
    for path in sorted((common.ROOT / "api" / "v1").rglob("*.json")) if (common.ROOT / "api" / "v1").exists() else []:
        api.append(path.read_text(encoding="utf-8"))
    leaked = [s for s in ("the-source.quick", HEALTH_METHOD, FIT_METHOD) if any(s in t for t in api)]
    check("read_api_v1_carries_no_quick_scores", not leaked, leaked)
    lr = m.get("last_run") or {}
    check("last_run_cost_visible", lr.get("api_calls") == 0 and "runtime_s" in lr and "failure_count" in lr,
          {k: lr.get(k) for k in ("api_calls", "runtime_s", "failure_count", "health_rescored", "pairings_rescored")})
    failed = [c for c in checks if not c["ok"]]
    return {"schema": SCHEMA, "checks": checks, "failed_checks": len(failed), "coverage_pct": round(pct, 2)}


def main(argv: list) -> int:
    try:
        if argv[:1] == ["quick-score"]:
            m = run(full="--full" in argv[1:])
            print(json.dumps({k: m[k] for k in ("quick_version", "counts", "coverage", "last_run")}, indent=2))
            return 0
        report = validate()
        print(json.dumps(report, indent=2))
        return 1 if report["failed_checks"] else 0
    except (QuickError, OSError, KeyError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1
