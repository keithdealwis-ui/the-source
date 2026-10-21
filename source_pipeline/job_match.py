"""Job-matched candidate relationships (KEI-930, decision D1 recorded on KEI-930, orch-KEI-930-05ad0e7f).

    python -m source_pipeline job-match             # rewrite the ledger from the corpus; no network
    python -m source_pipeline job-match-observe     # live-verify ledger projects the snapshot lacks (network)

A corpus project whose OWN description or topics carry a catalogue category's defining job phrases (the first
`core` group in config/saas_jobs.yaml, the group the full evaluation's core gate requires) is recorded as a
CANDIDATE alternative to the catalogue products in that category. No list or repository names the pairing; the
project only says it does the job. The ledger (data/job_match/ledger.json) is harvested as its own source
(`job_match`), so every relationship it yields carries `evidence_class: job_matched` and is never presented as
an upstream-listed alternative. A candidate counts toward a product's status only after it passes the unchanged
full evaluation (core gate, publishable and credible Replacement Fit). Nothing here recommends anything.

Who is a candidate (all must hold, so the ledger is reproducible from committed inputs alone):
- on github.com (the full evaluation's host), maintained or active, licence eligible, quick Health >= HEALTH_MIN;
- the category's defining phrase appears in the description (a topic tag alone is too loose), AND one of its
  second-group terms in the description or topics;
- not an integration with another product (KEI-919 integration_kind / not_a_product, plus a client/front-end rule);
- not already a relationship of that product from any other source (job matching never adds a second source to a
  listed pair, so it never corroborates one);
- at most PER_PRODUCT per product, strongest quick Health first, then oss_id; a pairing the full evaluation has
  already scored (data/scores/fit.jsonl) always keeps its place, and new candidates fill what is left.
"""
from __future__ import annotations

import hashlib
import re

from . import common

LEDGER = common.DATA / "job_match" / "ledger.json"
METHOD = "the-source.job-match/1"
SOURCE_ID = "job_match"
HEALTH_MIN = 55.0
PER_PRODUCT = 10
EXCERPT = 80

# A client, front end, downloader or companion for a named catalogue product works with that product (the KEI-919
# product-connector rule, widened to front ends and companions), compiled once over every catalogue name.
_COMPANION = r"\b(?:api client|api wrapper|front ?end|frontend|client|downloader|player|app|companion|viewer|reader|" \
             r"wrapper|connector|integration|sync|bridge|exporter|importer|plugin|extension|add ?on|skills?|tools?|" \
             r"second brain|cli|bot|server) (?:for|to|of|with) (?:the )?(?:{names})\b"


def _text(p: dict) -> str:
    return " ".join([(p.get("description") or ""), *(p.get("topics") or [])]).lower().replace("-", " ")


def _phrase_re(phrases):
    alts = sorted({re.escape(x.lower().replace("-", " ").strip()) for x in phrases if x.strip()}, key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(alts) + r")(?![a-z0-9])") if alts else None


def _excerpt(text: str, m: re.Match) -> str:
    a = max(0, m.start() - EXCERPT // 2)
    return text[a:a + EXCERPT].strip()


def _integration(p: dict, companion: re.Pattern) -> str | None:
    from .mcp_server import _norm, integration_kind, not_a_product

    if not_a_product(p.get("name"), p.get("full_name"), p.get("topics")):
        return "not_a_product"
    kind = integration_kind(p.get("name"), p.get("full_name"), p.get("description"), p.get("topics"))
    if kind:
        return kind
    if companion.search(" ".join(_norm(p.get("description") or "").split())):
        return "companion"
    return None


def select() -> dict:
    """The ledger, computed from config/saas_catalogue.yaml, config/saas_jobs.yaml, data/corpus/projects.jsonl,
    data/quick/health.jsonl and the non-job-match relationships in data/dataset/relationships.jsonl."""
    from .catalogue import candidates
    from .score_build import load_jobs

    cands = candidates()
    jobs = load_jobs()
    health = {r["oss_id"]: r for r in common.read_jsonl(common.DATA / "quick" / "health.jsonl")}
    projects = common.read_jsonl(common.DATA / "corpus" / "projects.jsonl")
    listed = {(e["saas_id"], e["oss_id"]) for e in common.read_jsonl(common.DATASET / "relationships.jsonl")
              if set(e["source_ids"]) - {SOURCE_ID}}
    from .mcp_server import _norm

    names = sorted({_norm(n) for c in cands for n in (c["name"], *c.get("aliases", [])) if len(_norm(n)) > 2},
                   key=len, reverse=True)
    companion = re.compile(_COMPANION.format(names="|".join(re.escape(n) for n in names)))

    pool, excluded = [], {}
    for p in projects:
        h = health.get(p["id"]) or {}
        if not p["id"].startswith("github.com/") or (p.get("maintenance") or {}).get("status") not in ("active", "maintained"):
            continue
        if h.get("licence_eligibility") != "eligible" or (h.get("score") or 0) < HEALTH_MIN:
            continue
        why = _integration(p, companion)
        if why:
            excluded[why] = excluded.get(why, 0) + 1
            continue
        pool.append((p, h["score"], _text(p), (p.get("description") or "").lower().replace("-", " ")))

    by_cat = {}
    for cat, job in sorted(jobs.items()):
        r0 = _phrase_re(job["core"][0])
        r1 = _phrase_re(job["core"][1]) if len(job["core"]) > 1 else None
        hits = []
        for p, score, text, desc in pool:
            m = r0.search(desc) if r0 else None  # the job in the project's own words; a tag alone is too loose
            if m and (r1 is None or r1.search(text)):
                hits.append((p, score, m.group(0), _excerpt(desc, m)))
        by_cat[cat] = sorted(hits, key=lambda x: (-x[1], x[0]["id"]))

    # A candidate the full evaluation has already scored keeps its place: a larger corpus must not displace an
    # evaluated pairing (and with it a recommendation) for a stronger, unevaluated one. New candidates take the
    # remaining places, so the cap holds unless the evaluated pairings alone exceed it.
    scored = {(r["saas_id"], r["oss_id"]) for r in common.read_jsonl(common.DATA / "scores" / "fit.jsonl")}
    rows: dict[str, dict] = {}
    for c in sorted(cands, key=lambda c: c["saas_id"]):
        hits = [x for x in by_cat.get(c["category"], []) if (c["saas_id"], x[0]["id"]) not in listed]
        kept = [x for x in hits if (c["saas_id"], x[0]["id"]) in scored]
        picked = kept + [x for x in hits if (c["saas_id"], x[0]["id"]) not in scored][:max(0, PER_PRODUCT - len(kept))]
        picked.sort(key=lambda x: (-x[1], x[0]["id"]))
        for p, score, phrase, excerpt in picked:
            r = rows.setdefault(p["id"], {
                "oss_id": p["id"], "oss_name": p.get("name") or p["id"].split("/")[-1],
                "repo_url": p.get("url") or "https://" + p["id"], "category": c["category"],
                "matched_phrase": phrase, "excerpt": excerpt, "quick_health": score,
                "saas_ids": [], "saas_names": []})
            r["saas_ids"].append(c["saas_id"])
            r["saas_names"].append(c["name"])
    out = [rows[k] for k in sorted(rows)]
    return {"method": METHOD, "inputs": inputs_digest(), "health_min": HEALTH_MIN, "per_product": PER_PRODUCT,
            "excluded_integrations": dict(sorted(excluded.items())),
            "pairs": sum(len(r["saas_ids"]) for r in out), "projects": len(out), "rows": out}


CONFIG_INPUTS = ("config/saas_catalogue.yaml", "config/catalogue.yaml", "config/saas_jobs.yaml", "data/scores/fit.jsonl")
PROJECT_FIELDS = ("id", "name", "full_name", "description", "topics", "url")


def inputs_digest() -> str:
    """The exact facts the ledger is computed from: the three configs, the corpus fields it reads and each project's
    quick Health score and licence eligibility (the listed relationships are the complement of its own output, so
    they are not an input here). The daily cycle refreshes these facts, so a ledger can be older than its inputs:
    it is then stale, not wrong, and `job-match` regenerates it."""
    import json

    h = hashlib.sha256()
    for rel in CONFIG_INPUTS:
        h.update(rel.encode() + b"\0" + (common.ROOT / rel).read_bytes() + b"\0")
    for p in common.read_jsonl(common.DATA / "corpus" / "projects.jsonl"):
        row = {k: p.get(k) for k in PROJECT_FIELDS} | {"status": (p.get("maintenance") or {}).get("status")}
        h.update(json.dumps(row, sort_keys=True).encode())
    for r in common.read_jsonl(common.DATA / "quick" / "health.jsonl"):
        h.update(json.dumps([r["oss_id"], r.get("score"), r.get("licence_eligibility")]).encode())
    return h.hexdigest()[:16]


def write() -> dict:
    doc = select()
    common.write_json(LEDGER, doc)
    return {k: v for k, v in doc.items() if k != "rows"}


def version() -> str:
    """Content hash of the ledger, in the 40-hex shape every source commit has."""
    if not LEDGER.exists():
        return "0" * 40
    return hashlib.sha256(LEDGER.read_bytes()).hexdigest()[:40]


def claims(source: dict) -> list[dict]:
    """The ledger as harvest claims, one per project, in the shape every source produces."""
    if not LEDGER.exists():
        return []
    ver = version()
    return [{"category": r["category"], "declared_licence": None, "locator": f"data/job_match/ledger.json#{r['oss_id']}",
             "oss_name": r["oss_name"], "repo_url": r["repo_url"], "saas_names": list(r["saas_names"]),
             "source_commit": ver, "source_id": source["source_id"]}
            for r in common.read_json(LEDGER)["rows"]]


def observe_missing(workers: int = 8) -> dict:
    """Live-verify the ledger's projects that data/live/snapshot.json lacks, under the snapshot's own clock, and
    merge (the same way enrich.retry_failed does). Projects already observed are not asked again."""
    from datetime import datetime, timezone

    from . import enrich
    from .normalise import repo_key

    snap = common.read_json(common.LIVE / "snapshot.json")
    keys = sorted({repo_key(r["repo_url"])[0] for r in common.read_json(LEDGER)["rows"]} - set(snap["projects"]) - {None})
    if not keys:
        return {"observed": 0, "lookup_failures": 0}
    as_of = datetime.strptime(snap["as_of"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    new = enrich.observe(keys, as_of=as_of, workers=workers)
    merged = dict(snap)
    merged["projects"] = dict(sorted({**snap["projects"], **new["projects"]}.items()))
    merged["http_calls"] = snap["http_calls"] + new["http_calls"]
    merged["lookup_failures"] = sorted(set(snap.get("lookup_failures") or []) | set(new["lookup_failures"]))
    merged["failed_keys"] = sorted(set(snap.get("failed_keys") or []) | set(new["failed_keys"]))
    enrich.write_snapshot(merged)
    return {"observed": len(keys), "http_calls": new["http_calls"], "lookup_failures": len(new["lookup_failures"]),
            "failed_keys": new["failed_keys"][:20]}


def score(budget: int) -> dict:
    """Fully evaluate validated relationships that have no Replacement Fit yet, through the cycle's own new-pair
    step (score_one: the full lane's rubric and GraphQL query, scoring_path single_project)."""
    from . import cycle

    res = {"strategies": []}
    cycle._score_new(budget, res)
    return res["strategies"][-1]


def main(argv) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="source_pipeline " + argv[0])
    ap.add_argument("--budget", type=int, default=2000, help="most projects to evaluate in one run")
    args = ap.parse_args(argv[1:])
    if argv[0] == "job-match":
        out = write()
    elif argv[0] == "job-match-observe":
        out = observe_missing()
    else:
        out = score(args.budget)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0
