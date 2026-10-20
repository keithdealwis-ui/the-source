"""Demand-led catalogue: candidates, ranking, selection and honest coverage states (KEI-922, V2.1 WP5).

    python -m source_pipeline catalogue-rank                 rank the candidates, select `target`; no network
    python -m source_pipeline catalogue-demand --kwi FILE    import an aiq-kwi run as demand evidence
    python -m source_pipeline catalogue-validate             exit 1 on any failed check

Candidates are every product in config/saas_catalogue.yaml, with an audience and detection
rules in config/catalogue.yaml. Ranking uses two kinds of evidence, both recorded per row:

* demand: monthly alternative-intent search volume ("<product> alternative", "open source
  <product>") from aiq-kwi runs imported into data/catalogue/demand/. Where none has been
  imported, demand is `unavailable` and the rank is supply alone. KEI-918 MCP telemetry counts
  calls per tool and outcome only; it records no product argument by design, so it carries no
  per-product demand and is reported as such rather than guessed at.
* supply: fully evaluated recommendations, validated relationships, upstream lists that name the
  product, corpus projects that call themselves its alternative, and corpus projects doing the
  job of its category.

Selection keeps every product already served, the products named for evaluation and every
product with a fully evaluated recommendation, then fills to `target` by rank.

The catalogue status of a selected product (ok / partial / none_yet) is not decided here.
canonical-build derives it from full evaluations alone, every time it runs (status()).

Determinism: outputs are a pure function of the committed inputs; no wall-clock time is written.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from pathlib import Path

from . import common, detect

CONFIG = common.CONFIG / "catalogue.yaml"
OUT = common.DATA / "catalogue"
DEMAND = OUT / "demand"
SELECTION_SCHEMA = "the-source.catalogue-selection/1"
DEMAND_SCHEMA = "the-source.catalogue-demand/1"
STATUSES = ("ok", "partial", "none_yet")
AUDIENCES = ("b2b", "b2c", "both")
FULL_PATHS = ("full_lane", "single_project")  # how a Replacement Fit counts as a full evaluation

TELEMETRY = {"status": "no_product_dimension",
             "basis": "KEI-918 MCP telemetry counts calls per tool and outcome; it records no product argument by "
                      "design (docs/OBSERVABILITY.md), so it cannot rank products and is not used as demand."}


# ---------------------------------------------------------------- config

def load_config(path: Path | None = None) -> dict:
    return common.load_yaml(path or CONFIG)


def candidates(cfg: dict | None = None) -> list[dict]:
    """Every candidate, in catalogue order, joined with saas_catalogue.yaml. Raises on any mismatch."""
    from . import normalise

    cfg = cfg or load_config()
    products, _ = normalise.load_catalogue()
    out, seen = [], set()
    for row in cfg["candidates"]:
        sid, audience, rules = row
        if sid in seen:
            raise ValueError(f"catalogue.yaml lists {sid} twice")
        seen.add(sid)
        if sid not in products:
            raise ValueError(f"catalogue.yaml names {sid}, which is not in saas_catalogue.yaml")
        if audience not in AUDIENCES:
            raise ValueError(f"{sid}: audience {audience!r} is not one of {AUDIENCES}")
        if not rules:
            raise ValueError(f"{sid}: no detection rules")
        p = products[sid]
        out.append({**p, "audience": audience, "match": [detect.parse_rule(r) for r in rules]})
    missing = sorted(set(products) - seen)
    if missing:
        raise ValueError(f"saas_catalogue.yaml products without a catalogue.yaml row: {missing[:10]}")
    return out


def rules(cands: list[dict], ids=None) -> list[dict]:
    keep = None if ids is None else set(ids)
    return [{"saas_id": c["saas_id"], **r} for c in cands if keep is None or c["saas_id"] in keep for r in c["match"]]


# ---------------------------------------------------------------- supply signals

def _recommended_counts() -> dict:
    """Fully evaluated, recommended relationships per product, from the canonical layer."""
    path = common.DATA / "canonical" / "relationships.jsonl"
    out = {}
    for r in common.read_jsonl(path) if path.exists() else []:
        fit = r.get("replacement_fit") or {}
        if r.get("recommended") and fit.get("scoring_path") in FULL_PATHS:
            out[r["saas_id"]] = out.get(r["saas_id"], 0) + 1
    return out


def _dataset_signals() -> tuple[dict, dict]:
    sources, validated = {}, {}
    for s in common.read_jsonl(common.DATASET / "saas_products.jsonl"):
        sources[s["saas_id"]] = len((s.get("imported") or {}).get("named_by_sources") or [])
    for e in common.read_jsonl(common.DATASET / "relationships.jsonl"):
        if e["lane"] == "recommendable":
            validated[e["saas_id"]] = validated.get(e["saas_id"], 0) + 1
    return sources, validated


def _corpus_projects() -> list[dict]:
    path = common.DATA / "corpus" / "projects.jsonl"
    return common.read_jsonl(path) if path.exists() else []


def _corpus_pairings(cands: list[dict], projects: list[dict]) -> dict:
    """Corpus projects whose own description or topics call them an alternative to the product
    (the quick-scoring pairing rule, without the upstream lists, which validated_relationships counts)."""
    from .quick_score import detect_pairings

    cat = [{k: c[k] for k in ("saas_id", "name", "category", "aliases")} for c in cands]
    out = {}
    for p in projects:
        for pr in detect_pairings(p, cat, {}):
            out[pr["saas_id"]] = out.get(pr["saas_id"], 0) + 1
    return out


def _phrase_re(phrases: list[str]):
    alts = sorted({re.escape(p.lower().replace("-", " ").strip()) for p in phrases if p.strip()}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])") if alts else None


def _category_supply(projects: list[dict]) -> dict:
    """Corpus projects doing a category's job: the category's defining phrases (config/saas_jobs.yaml, the
    group the full evaluation's core gate requires) found as whole words in the project's description or
    topics. A category without a job definition has no measured supply (None), and cannot be fully evaluated."""
    from .score_build import load_jobs

    texts = [" ".join([(p.get("description") or ""), *(p.get("topics") or [])]).lower().replace("-", " ")
             for p in projects if (p.get("maintenance") or {}).get("status") in ("active", "maintained")]
    out = {}
    for cat, job in sorted(load_jobs().items()):
        rx = _phrase_re(job["core"][0])
        out[cat] = sum(1 for t in texts if rx and rx.search(t))
    return out


# ---------------------------------------------------------------- demand

def _norm(text: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", (text or "").lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.+&!' -]", " ", text)).strip()


def _name_index(cands: list[dict]) -> dict:
    names = {}
    for c in sorted(cands, key=lambda c: c["saas_id"]):
        for n in {c["name"], *c.get("aliases", [])}:
            k = _norm(n)
            if len(re.sub(r"[^a-z0-9]", "", k)) >= 3:
                names.setdefault(k, set()).add(c["saas_id"])
    return names


def match_keyword(keyword: str, names: dict, modifiers: list[str]) -> str | None:
    """The one candidate an alternative-intent keyword is about, or None.

    The keyword must carry an alternative-intent modifier and contain a candidate name or alias as
    whole words; the longest name wins ("google docs alternative" is Google Docs, not Google Search),
    and a name shared by two candidates is ambiguous, so the keyword is not counted."""
    k = " " + _norm(keyword).replace("-", " ") + " "
    if not any(f" {_norm(m).replace('-', ' ')} " in k for m in modifiers):
        return None
    best, best_len = None, 0
    for name, sids in names.items():
        n = name.replace("-", " ")
        if f" {n} " in k and len(n) > best_len:
            best, best_len = sids, len(n)
    if not best or len(best) != 1:
        return None
    return next(iter(best))


def import_kwi(report_path: Path, cfg: dict | None = None, out_dir: Path | None = None) -> dict:
    """aiq-kwi report.json (or a KeywordIdea list) -> data/catalogue/demand/kwi-<sha12>.json.

    Only keywords with an alternative-intent modifier that name exactly one candidate count. A candidate is
    `covered` when the run could have found it: it was a seed of the run, or one of its keywords matched.
    Uncovered candidates have no demand figure (null), never zero."""
    cfg = cfg or load_config()
    cands = candidates(cfg)
    raw = Path(report_path).read_bytes()
    doc = json.loads(raw)
    if isinstance(doc, list):
        rows, run = [{"keyword": r.get("text") or r.get("keyword"), "volume": r.get("volume")} for r in doc], {}
    else:
        rows, run = doc.get("keywords") or [], doc.get("run") or {}
    names = _name_index(cands)
    per, unmatched = {}, 0
    for r in rows:
        kw, vol = r.get("keyword"), r.get("volume")
        sid = match_keyword(kw or "", names, cfg["alternative_intent"]) if kw else None
        if sid is None or not isinstance(vol, int) or vol < 0:
            unmatched += 1
            continue
        d = per.setdefault(sid, {})
        d[_norm(kw)] = max(d.get(_norm(kw), 0), vol)
    covered = set(per)
    for seed in run.get("seeds") or []:
        for name, sids in names.items():
            if len(sids) == 1 and f" {name} " in f" {_norm(seed)} ":
                covered |= sids
    sha = hashlib.sha256(raw).hexdigest()
    out = {
        "schema": DEMAND_SCHEMA,
        "source": "aiq-kwi",
        "imported_from": {"sha256": sha, "file": Path(report_path).name},
        "market": {k: run.get(k) for k in ("location", "language") if run.get(k) is not None},
        "covered": sorted(covered),
        "rows": [{"saas_id": sid, "volume": sum(per[sid].values()),
                  "keywords": [{"keyword": k, "volume": v} for k, v in sorted(per[sid].items())]}
                 for sid in sorted(per)],
        "keywords_unmatched": unmatched,
    }
    path = (out_dir or DEMAND) / f"kwi-{sha[:12]}.json"
    common.write_json(path, out)
    return {"path": str(path), "covered": len(covered), "with_demand": len(per), "keywords_unmatched": unmatched}


def load_demand(demand_dir: Path | None = None) -> dict:
    """{"status", "sources", "volume": {saas_id: int}, "covered": set}. Files of different markets add up."""
    d = demand_dir or DEMAND
    files = sorted(d.glob("*.json")) if d.exists() else []
    vol, covered, sources = {}, set(), []
    for f in files:
        doc = common.read_json(f)
        if doc.get("schema") != DEMAND_SCHEMA:
            raise ValueError(f"{f}: not a {DEMAND_SCHEMA} file")
        sources.append({"file": f.name, "source": doc["source"], "market": doc.get("market") or {},
                        "sha256": (doc.get("imported_from") or {}).get("sha256")})
        covered |= set(doc.get("covered") or [])
        for r in doc["rows"]:
            vol[r["saas_id"]] = vol.get(r["saas_id"], 0) + int(r["volume"])
            covered.add(r["saas_id"])
    return {"status": "available" if files else "unavailable", "sources": sources, "volume": vol, "covered": covered}


# ---------------------------------------------------------------- ranking

def _scaled(value, sig: dict) -> float:
    if value is None:
        return 0.0
    cap = sig["cap"]
    if sig.get("scale") == "log":
        share = math.log1p(min(value, cap)) / math.log1p(cap)
    else:
        share = min(value, cap) / cap
    return sig["weight"] * share


def rank(cfg: dict | None = None, demand_dir: Path | None = None) -> dict:
    from .score_build import load_jobs

    cfg = cfg or load_config()
    cands = candidates(cfg)
    supported = [p["saas_id"] for p in common.load_yaml(common.CONFIG / "supported_saas.yaml")["products"]]
    rec = _recommended_counts()
    sources, validated = _dataset_signals()
    projects = _corpus_projects()
    pairings = _corpus_pairings(cands, projects)
    cat_supply = _category_supply(projects)
    jobs = load_jobs()
    demand = load_demand(demand_dir)
    w = cfg["ranking"]
    rows = []
    for c in cands:
        sid = c["saas_id"]
        signals = {"recommended": rec.get(sid, 0), "validated_relationships": validated.get(sid, 0),
                   "upstream_sources": sources.get(sid, 0), "corpus_pairings": pairings.get(sid, 0),
                   "category_supply": cat_supply.get(c["category"]) if c["category"] in jobs else None}
        supply = round(sum(_scaled(signals[k], s) for k, s in w["supply"].items()), 2)
        if sid in demand["covered"]:
            v = demand["volume"].get(sid, 0)
            dscore = round(100.0 * math.log1p(min(v, w["demand_cap_volume"])) / math.log1p(w["demand_cap_volume"]), 2)
            score = round(w["demand_weight"] * dscore + (1 - w["demand_weight"]) * supply, 2)
            dem = {"status": "measured", "monthly_volume": v, "score": dscore}
        else:
            score = supply
            dem = {"status": "unavailable" if demand["status"] == "unavailable" else "not_covered",
                   "monthly_volume": None, "score": None}
        rows.append({"saas_id": sid, "name": c["name"], "category": c["category"], "audience": c["audience"],
                     "job_defined": c["category"] in jobs, "signals": signals, "supply_score": supply,
                     "demand": dem, "score": score})
    rows.sort(key=lambda r: (-r["score"], -r["signals"]["upstream_sources"], r["saas_id"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i

    must = {}
    for sid in supported:
        must.setdefault(sid, "already_served")
    for sid in cfg.get("must_include") or []:
        must.setdefault(sid, "named_for_evaluation")
    for r in rows:
        if r["signals"]["recommended"]:
            must.setdefault(r["saas_id"], "has_full_evaluation")
    known = {r["saas_id"] for r in rows}
    if set(must) - known:
        raise ValueError(f"must-include products are not candidates: {sorted(set(must) - known)}")
    target = cfg["target"]
    if len(must) > target:
        raise ValueError(f"{len(must)} must-include products exceed the target of {target}")
    room = target - len(must)
    for r in rows:
        if r["saas_id"] in must:
            r["selected"], r["selected_by"] = True, must[r["saas_id"]]
        elif room > 0:
            r["selected"], r["selected_by"] = True, "rank"
            room -= 1
        else:
            r["selected"], r["selected_by"] = False, None
    return {"rows": rows, "demand": demand, "category_supply": cat_supply}


def _digest(path: Path) -> str | None:
    return common.sha256_file(path)[:16] if path.exists() else None


def ranked_from(root: Path | None = None) -> dict:
    """Fingerprints of everything the ranking reads. The selection is a deliberate snapshot: the daily cycle
    moves these inputs, and the catalogue keeps its 500 until catalogue-rank is run again."""
    root = root or common.ROOT
    canon = root / "data" / "canonical" / "MANIFEST.json"
    corpus = root / "data" / "corpus" / "MANIFEST.json"
    demand = root / "data" / "catalogue" / "demand"
    return {
        "canonical_dataset_version": common.read_json(canon)["dataset_version"] if canon.exists() else None,
        "corpus_version": common.read_json(corpus).get("corpus_version") if corpus.exists() else None,
        "dataset_relationships": _digest(root / "data" / "dataset" / "relationships.jsonl"),
        "dataset_saas_products": _digest(root / "data" / "dataset" / "saas_products.jsonl"),
        "catalogue_config": _digest(root / "config" / "catalogue.yaml"),
        "saas_catalogue": _digest(root / "config" / "saas_catalogue.yaml"),
        "saas_jobs": _digest(root / "config" / "saas_jobs.yaml"),
        "supported_saas": _digest(root / "config" / "supported_saas.yaml"),
        "demand": sorted(f"{f.name}:{_digest(f)}" for f in demand.glob("*.json")) if demand.exists() else [],
    }


# Raw keyword volumes are Google Ads Keyword Planner figures. They stay in data/catalogue/demand/, which is not
# committed (KEI-922 f2, Keith 2026-10-20); the public candidates file carries a coarse band only. The combined
# rank score is withheld too, since with the published supply score it would give the volume back.
DEMAND_BANDS = ((1000, "high"), (100, "medium"), (1, "low"), (0, "none"))


def demand_band(volume: int | None) -> str | None:
    if volume is None:
        return None
    return next(name for floor, name in DEMAND_BANDS if volume >= floor)


def public_row(r: dict) -> dict:
    out = {k: v for k, v in r.items() if k != "score"}
    out["demand"] = {"status": r["demand"]["status"], "band": demand_band(r["demand"]["monthly_volume"])}
    return out


def write(result: dict, out: Path | None = None) -> dict:
    out = out or OUT
    rows = result["rows"]
    selected = [r for r in rows if r["selected"]]
    by = lambda key, rs: {k: sum(1 for r in rs if r[key] == k) for k in sorted({r[key] for r in rs})}
    summary = {
        "schema": SELECTION_SCHEMA,
        "ticket": "KEI-922",
        "target": len(selected),
        "ranked_from": ranked_from(),
        "demand": {k: result["demand"][k] for k in ("status", "sources")}
                  | {"covered": len(result["demand"]["covered"])},
        "telemetry": TELEMETRY,
        "counts": {
            "candidates": len(rows),
            "selected": len(selected),
            "selected_by": by("selected_by", selected),
            "candidates_by_audience": by("audience", rows),
            "selected_by_audience": by("audience", selected),
            "selected_with_job_definition": sum(r["job_defined"] for r in selected),
            "selected_with_full_evaluation": sum(1 for r in selected if r["signals"]["recommended"]),
        },
        "selected": [r["saas_id"] for r in selected],
    }
    common.write_jsonl(out / "candidates.jsonl", [public_row(r) for r in rows])
    common.write_json(out / "selection.json", summary)
    return summary


def load_selection(root: Path | None = None) -> dict | None:
    path = (root or common.ROOT) / "data" / "catalogue" / "selection.json"
    return common.read_json(path) if path.exists() else None


# ---------------------------------------------------------------- status (used by canonical-build)

REASON_TEXT = {
    "full_evaluation": "{recommended} recommendations passed the full evaluation",
    "fewer_than_minimum": "{recommended} of the {minimum} recommendations needed have passed the full evaluation",
    "awaiting_full_evaluation": "{awaiting} validated relationships have not been fully evaluated yet",
    "evaluated_not_recommended": "{not_recommended} fully evaluated relationships did not qualify as recommendations",
    "outside_launch_selection": "{outside_launch} validated relationships are outside the published launch corpus "
                                "(config/policy.yaml launch, edges_max {edges_max}); publishing them is a launch-policy decision",
    "share_alike_only": "{share_alike_only} validated relationships rest only on a share-alike source and are withheld "
                        "from the public layer",
    "licence_review": "{licence_review} candidate relationships are waiting on a licence decision",
    "no_job_definition": "the category has no job definition yet, so its alternatives cannot be fully evaluated",
    "corpus_candidates_only": "{corpus_pairings} corpus projects describe themselves as alternatives; none is fully evaluated",
    "no_relationship_evidence": "no open-source alternative has been found with relationship evidence yet",
}


def status(recommended: int, minimum: int, evidence: dict, edges_max: int | None = None) -> dict:
    """ok / partial / none_yet and why. `recommended` counts only fully evaluated recommendations.

    evidence: {"awaiting", "not_recommended", "outside_launch", "share_alike_only", "licence_review",
               "corpus_pairings", "job_defined"}"""
    if recommended >= minimum:
        st, reasons = "ok", ["full_evaluation"]
    else:
        st = "partial" if recommended else "none_yet"
        reasons = ["fewer_than_minimum"] if recommended else []
        for key, code in (("awaiting", "awaiting_full_evaluation"), ("not_recommended", "evaluated_not_recommended"),
                          ("outside_launch", "outside_launch_selection"), ("share_alike_only", "share_alike_only"),
                          ("licence_review", "licence_review")):
            if evidence.get(key):
                reasons.append(code)
        if not evidence.get("job_defined"):
            reasons.append("no_job_definition")
        if not recommended and evidence.get("corpus_pairings") and not any(
                evidence.get(k) for k in ("awaiting", "not_recommended", "outside_launch", "share_alike_only",
                                          "licence_review")):
            reasons.append("corpus_candidates_only")
        if not reasons:
            reasons.append("no_relationship_evidence")
    fields = {"recommended": recommended, "minimum": minimum, "edges_max": edges_max, **evidence}
    return {"status": st, "reasons": reasons, "reason": "; ".join(REASON_TEXT[r].format(**fields) for r in reasons)}


# ---------------------------------------------------------------- validation

def validate(root: Path | None = None) -> dict:
    """Configuration, detection and selection checks. The published statuses are checked by canonical-validate."""
    root = root or common.ROOT
    checks = []

    def check(cid, ok, detail=""):
        checks.append({"id": cid, "ok": bool(ok), "detail": detail})

    cfg = load_config(root / "config" / "catalogue.yaml")
    try:
        cands = candidates(cfg)
    except (ValueError, detect.RuleError) as exc:
        check("candidates_well_formed", False, str(exc))
        return {"ok": False, "checks": checks}
    check("candidates_well_formed", True, f"{len(cands)} candidates, each with an audience and detection rules")
    check("candidate_count_about_600", 550 <= len(cands) <= 650, f"{len(cands)} candidates")
    all_rules = rules(cands)
    dup, seen = [], {}
    for r in all_rules:
        k = detect.format_rule(r)
        if k in seen and seen[k] != r["saas_id"]:
            dup.append(f"{k}: {seen[k]} and {r['saas_id']}")
        seen.setdefault(k, r["saas_id"])
    check("rules_unambiguous", not dup, "; ".join(dup[:5]) or f"{len(all_rules)} rules, none shared")
    wrong = [(u, r["saas_id"], detect.match_url(u, all_rules)) for r in all_rules for u in detect.example_urls(r)
             if detect.match_url(u, all_rules) != r["saas_id"]]
    check("every_rule_detects_its_product", not wrong, f"{len(wrong)} wrong, e.g. {wrong[:3]}" if wrong
          else f"{2 * len(all_rules)} rule URLs resolve to their own product")
    bad_ex = [(u, exp, detect.match_url(u, all_rules)) for u, exp in cfg.get("examples") or []
              if detect.match_url(u, all_rules) != exp]
    check("detection_examples_hold", not bad_ex, f"{len(bad_ex)} wrong, e.g. {bad_ex[:3]}" if bad_ex
          else f"{len(cfg.get('examples') or [])} examples")
    undetected = sorted({c["saas_id"] for c in cands} - {detect.match_url(u, all_rules) for r in all_rules
                                                         for u in detect.example_urls(r)})
    check("every_candidate_detectable", not undetected, f"undetectable: {undetected[:5]}")

    sel = load_selection(root)
    if sel is None:
        check("selection_present", False, "data/catalogue/selection.json missing; run catalogue-rank")
        return {"ok": all(c["ok"] for c in checks), "checks": checks}
    ids = sel["selected"]
    check("selection_meets_target", len(ids) == cfg["target"] == len(set(ids)),
          f"{len(ids)} selected, target {cfg['target']}")
    known = {c["saas_id"] for c in cands}
    check("selection_names_candidates", set(ids) <= known, f"unknown: {sorted(set(ids) - known)[:5]}")
    supported = [p["saas_id"] for p in common.load_yaml(root / "config" / "supported_saas.yaml")["products"]]
    must = set(supported) | set(cfg.get("must_include") or [])
    check("selection_keeps_must_include", must <= set(ids), f"missing: {sorted(must - set(ids))}")
    rows = common.read_jsonl(root / "data" / "catalogue" / "candidates.jsonl")
    check("candidates_file_matches_selection", [r["saas_id"] for r in rows if r["selected"]] == ids
          and len(rows) == len(cands), f"{len(rows)} rows")
    current = ranked_from(root)
    if sel["ranked_from"] == current:
        fresh = rank(cfg, demand_dir=root / "data" / "catalogue" / "demand")["rows"]
        same = [r["saas_id"] for r in fresh if r["selected"]] == ids
        check("selection_reproduces", same, "catalogue-rank on the inputs it was ranked from selects exactly the "
              "committed products" if same else "re-run catalogue-rank")
    else:
        # The cycle has moved the inputs since the ranking. The selection stands until it is deliberately re-ranked;
        # saying so is the check, failing CI every day would not be.
        moved = sorted(k for k in current if current[k] != sel["ranked_from"].get(k))
        check("selection_reproduces", True, f"not re-checked: inputs moved since ranking ({', '.join(moved)}); "
              "run catalogue-rank to refresh the selection")
    report_stale = sel["ranked_from"] != current
    no_demand_claim = [r["saas_id"] for r in rows
                       if r["demand"]["status"] != "measured" and r["demand"].get("band") is not None]
    check("no_demand_figure_without_evidence", not no_demand_claim, f"{no_demand_claim[:5]}")
    raw = [r["saas_id"] for r in rows if "score" in r or set(r["demand"]) != {"status", "band"}]
    check("no_raw_demand_published", not raw, f"rows carrying a volume or rank score: {raw[:5]}")
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "selection_inputs_moved": report_stale}


def main(argv: list[str]) -> int:
    cmd = argv[0]
    if cmd == "catalogue-rank":
        print(json.dumps(write(rank()), indent=2, sort_keys=True))
        return 0
    if cmd == "catalogue-demand":
        if len(argv) != 3 or argv[1] != "--kwi":
            print("usage: catalogue-demand --kwi REPORT.json", file=sys.stderr)
            return 2
        print(json.dumps(import_kwi(Path(argv[2])), indent=2, sort_keys=True))
        return 0
    if cmd == "catalogue-validate":
        rep = validate()
        print(json.dumps(rep, indent=2, sort_keys=True))
        return 0 if rep["ok"] else 1
    print(f"unknown command {cmd}", file=sys.stderr)
    return 2
