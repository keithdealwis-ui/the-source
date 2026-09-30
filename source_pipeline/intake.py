"""Manual / conversational repository intake (KEI-807 criteria 13-18).

    python -m source_pipeline intake-assess --url <repo url> --request-id <id>
                                            [--saas-hint notion] [--submitted-by keith]

Assesses one submitted repository with the same rules as scheduled discovery, records
exactly one durable outcome under data/intake/outcomes/<request_id>.json, and, when the
repository qualifies, admits it to the intake ledger (data/intake/admitted.jsonl), which
the next cycle harvests like any other source. See docs/INTAKE-POLICY.md.

Outcomes:
  accepted             passes every rule and at least one SaaS relationship is evidenced by
                       the project's own repository; admitted, pending promotion
  already_known        already in the corpus or the ledger; its current standing is reported
  needs_more_evidence  a qualifying project, but no SaaS relationship is evidenced
  rejected             fails a rule (licence, activity, archive, identity, not found, ...)
  failed               the assessment could not complete (for example a host lookup failed);
                       retry with a new request id
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import build, common, enrich, score_one
from .activity import Rules, evaluate, parse_ts
from .normalise import repo_key

OUTCOMES = common.INTAKE / "outcomes"
LEDGER = common.INTAKE / "admitted.jsonl"
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{6,80}$")
README_SCAN_CHARS = 20000
EXCERPT = 80


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def outcome_path(request_id: str):
    return OUTCOMES / f"{request_id}.json"


def _catalogue() -> list[dict]:
    return common.read_jsonl(common.DATASET / "saas_products.jsonl")


def _norm(text: str) -> str:
    text = re.sub(r"\([^)]*\)", " ", text.lower())
    text = re.sub(r"^(?:the|a|an)\s+", "", text.strip())
    text = re.sub(r"\s+(?:etc\.?|and more|and others|and co\.?)$", "", text)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.+ -]", " ", text)).strip(" .-")


LIST_AFTER = re.compile(r"\b(?:alternatives?|replacements?) (?:to|for) ([^.;:!?\n]{1,120})")
NAME_BEFORE = re.compile(r"([a-z0-9][a-z0-9.+ -]{0,40}?)[ -]alternatives?\b")


def _items(segment: str) -> list[str]:
    parts = re.split(r",|/|&|\bor\b|\band\b|\bvs\.?\b", segment)
    return [x for x in (_norm(p) for p in parts) if x]


def find_evidence(description: str | None, topics: list[str], readme: str, catalogue: list[dict]) -> list[dict]:
    """SaaS relationships the project's own repository states. Deterministic; case-folded.

    Counted: "alternative(s)/replacement(s) to|for A, B and C" where an item EQUALS a
    catalogue name or alias, "<name> alternative", and a topic "<name>-alternative".
    Item equality is what keeps "alternative to GitHub Gist" from matching GitHub. A bare
    mention of a product is not a relationship.
    """
    names = {}
    for s in sorted(catalogue, key=lambda s: s["saas_id"]):
        for n in {s["name"], *s.get("aliases", [])}:
            k = _norm(n)
            if len(re.sub(r"[^a-z0-9]", "", k)) >= 3:
                names.setdefault(k, s)
    fields = {"description": (description or "").lower(), "readme": (readme or "")[:README_SCAN_CHARS].lower()}
    found = {}

    def add(s, field, matched, text, m):
        if s["saas_id"] in found:
            return
        a, b = max(0, m.start() - EXCERPT // 2), min(len(text), m.end() + EXCERPT // 2)
        found[s["saas_id"]] = {"saas_id": s["saas_id"], "saas_name": s["name"], "category": s["category"],
                               "field": field, "matched": matched,
                               "excerpt": re.sub(r"\s+", " ", text[a:b]).strip()}

    for field, text in fields.items():
        for m in LIST_AFTER.finditer(text):
            for item in _items(m.group(1)):
                if item in names:
                    add(names[item], field, item, text, m)
        for m in NAME_BEFORE.finditer(text):
            words = _norm(m.group(1)).split(" ")
            # the longest trailing run of words that is a catalogue name ("open-source notion" -> "notion")
            for i in range(len(words)):
                cand = " ".join(words[i:])
                if cand in names:
                    add(names[cand], field, cand + " alternative", text, m)
                    break
    for t in sorted({t.lower() for t in topics or []}):
        mt = re.match(r"^(.+?)-alternatives?$", t)
        if mt:
            for k, s in names.items():
                if re.sub(r"[^a-z0-9]+", "-", k).strip("-") == mt.group(1):
                    found.setdefault(s["saas_id"], {"saas_id": s["saas_id"], "saas_name": s["name"],
                                                    "category": s["category"], "field": "topics",
                                                    "matched": t, "excerpt": f"topic {t}"})
    return [found[k] for k in sorted(found)]


def _known(key: str) -> dict | None:
    for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl"):
        if p["oss_id"] == key or key in p["harvested_as"]:
            rels = [e for e in common.read_jsonl(common.DATASET / "relationships.jsonl") if e["oss_id"] == p["oss_id"]]
            canon = common.DATA / "canonical" / "oss_projects.jsonl"
            in_canon = canon.exists() and any(c["oss_id"] == p["oss_id"] for c in common.read_jsonl(canon))
            return {"where": "corpus", "oss_id": p["oss_id"], "lane": p["derived"]["lane"],
                    "exclusion_reasons": p["derived"]["eligibility"]["exclusion_reasons"],
                    "canonical": in_canon,
                    "relationships": sorted(f"{e['saas_id']}:{e['validation']['status']}" for e in rels)}
    if LEDGER.exists():
        for r in common.read_jsonl(LEDGER):
            if r["oss_id"] == key:
                return {"where": "intake_ledger", "oss_id": key, "admitted_by_request": r["request_id"],
                        "saas": r["saas_ids"]}
    return None


def _write(out: dict) -> dict:
    common.write_json(outcome_path(out["request_id"]), out)
    return out


def assess(url: str, request_id: str, submitted_by: str | None = None, saas_hint: str | None = None,
           note: str | None = None, submitted_at: str | None = None) -> dict:
    if not REQUEST_ID.match(request_id or ""):
        raise ValueError("request_id must be 6-80 characters of letters, digits, '.', '_' or '-'")
    existing = outcome_path(request_id)
    if existing.exists():
        return common.read_json(existing)  # idempotent: a resubmission returns the recorded outcome
    now = _now()
    out = {"request_id": request_id, "repo_url": url, "oss_id": None, "submitted_at": submitted_at or now,
           "submitted_by": submitted_by, "saas_hint": saas_hint, "note": note, "assessed_at": now,
           "outcome": None, "reasons": [], "evidence": {}, "scores": None,
           "advisory_notice": "A preliminary human or conversational assessment is advisory only and was not used.",
           "canonical": {"status": "not_applicable"}}

    key, why = repo_key(url)
    if key is None:
        return _write({**out, "outcome": "rejected", "reasons": [why]})
    out["oss_id"] = key
    policy = common.load_policy()
    if key.split("/", 1)[0] not in set(policy["hosts"]["supported"]):
        return _write({**out, "outcome": "rejected", "reasons": ["host_not_independently_verifiable"]})

    known = _known(key)
    if known:
        return _write({**out, "outcome": "already_known", "reasons": [f"already in the {known['where']}"],
                       "evidence": {"known": known},
                       "canonical": {"status": "canonical" if known.get("canonical") else known.get("lane", "pending_promotion")}})

    try:
        snap = enrich.observe([key], workers=1)
    except Exception as exc:
        return _write({**out, "outcome": "failed", "reasons": [f"live_verification_error:{type(exc).__name__}"]})
    live = snap["projects"][key]
    if snap["failed_keys"]:
        return _write({**out, "outcome": "failed", "reasons": ["host_lookup_failed_retry_later"],
                       "evidence": {"lookup_failures": snap["lookup_failures"][:5]}})
    if live.get("canonical_id") and live["canonical_id"] != key:
        out["oss_id"] = key = live["canonical_id"]
        known = _known(key)
        if known:
            return _write({**out, "outcome": "already_known", "reasons": [f"redirects to {key}, already in the {known['where']}"],
                           "evidence": {"known": known},
                           "canonical": {"status": "canonical" if known.get("canonical") else known.get("lane", "pending_promotion")}})

    as_of = parse_ts(snap["as_of"])
    maintenance = evaluate(live, Rules(policy), as_of) if live.get("found") else None
    licence = build.licence_verdict(live, policy, build.load_exceptions(), key) if live.get("found") else None
    verdict = build.eligibility(live, maintenance, licence)
    if licence is not None:
        licence["status"] = ("exception_approved" if licence["exception"] and not licence["recognised"]
                             else "recognised" if licence["recognised"] else "issue")
    out["evidence"]["live"] = build.live_view(live)
    out["evidence"]["maintenance"] = None if not maintenance else {
        k: maintenance.get(k) for k in ("status", "last_meaningful_activity_at", "days_since_meaningful_activity", "basis")}
    out["evidence"]["licence"] = None if not licence else {k: licence.get(k) for k in ("spdx", "status", "recognised")}
    if not verdict["recommended"]:
        return _write({**out, "outcome": "rejected", "reasons": verdict["exclusion_reasons"],
                       "canonical": {"status": build.lane_of(verdict)}})

    scored = score_one.score(key, live, licence, [], snap["as_of"])
    catalogue = _catalogue()
    evid = find_evidence(scored.get("description"), scored.get("topics") or [], scored.get("readme_text") or "", catalogue)
    out["evidence"]["saas_relationships"] = evid
    out["evidence"]["searched"] = {"description": bool(scored.get("description")), "topics": len(scored.get("topics") or []),
                                   "readme": bool(scored.get("readme_available")), "catalogue_products": len(catalogue)}
    hint_note = []
    if saas_hint and saas_hint not in {e["saas_id"] for e in evid}:
        hint_note = [f"hinted SaaS '{saas_hint}' is not evidenced by the repository itself"]
    if not evid:
        if scored.get("health"):
            score_one.store(scored)
            out["scores"] = {"project_health": scored["health"], "replacement_fit": []}
        return _write({**out, "outcome": "needs_more_evidence",
                       "reasons": ["no_evidenced_saas_relationship"] + hint_note,
                       "canonical": {"status": "not_admitted"}})

    pairs = [{"saas_id": e["saas_id"], "status": "validated", "source_count": 1, "corroborated": False,
              "reviewed": False} for e in evid]
    scored = score_one.score(key, live, licence, pairs, snap["as_of"])
    score_one.store(scored)
    out["scores"] = {"project_health": scored["health"], "replacement_fit": scored["fit"],
                     "preliminary": True, "errors": scored["errors"]}

    name = (live.get("canonical_name") or key.split("/", 1)[1]).split("/")[-1]
    row = {"request_id": request_id, "oss_id": key, "oss_name": name,
           "repo_url": "https://" + (f"{key.split('/', 1)[0]}/{live['canonical_name']}" if live.get("canonical_name") else key),
           "saas_ids": [e["saas_id"] for e in evid], "saas_names": [e["saas_name"] for e in evid],
           "category": evid[0]["category"], "declared_licence": licence["spdx"], "admitted_at": now}
    rows = common.read_jsonl(LEDGER) if LEDGER.exists() else []
    if not any(r["oss_id"] == key for r in rows):
        rows.append(row)
        rows.sort(key=lambda r: r["oss_id"])
        common.write_jsonl(LEDGER, rows)
    return _write({**out, "outcome": "accepted", "reasons": ["passes every rule; relationship evidenced"] + hint_note,
                   "canonical": {"status": "pending_promotion", "promoted_in_run": None}})


def status(request_id: str) -> dict:
    p = outcome_path(request_id)
    if not p.exists():
        return {"request_id": request_id, "status": "unknown"}
    o = common.read_json(p)
    return {"request_id": request_id, "status": "assessed", "outcome": o["outcome"], "canonical": o["canonical"]}


def reconcile_promotions(run_id: str) -> dict:
    """After a cycle: record, on each pending outcome, whether its repository is now canonical."""
    if not OUTCOMES.exists():
        return {"promoted": 0, "pending": 0}
    canon = {p["oss_id"] for p in common.read_jsonl(common.DATA / "canonical" / "oss_projects.jsonl")}
    ds = {p["oss_id"]: p for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl")}
    promoted = pending = 0
    for path in sorted(OUTCOMES.glob("*.json")):
        o = common.read_json(path)
        if o["outcome"] != "accepted" or o["canonical"].get("status") == "canonical":
            continue
        if o["oss_id"] in canon:
            o["canonical"] = {"status": "canonical", "promoted_in_run": run_id}
            promoted += 1
        else:
            p = ds.get(o["oss_id"])
            o["canonical"] = {"status": "not_promoted" if p else "pending_promotion", "promoted_in_run": None,
                              "checked_in_run": run_id,
                              "detail": None if not p else {"lane": p["derived"]["lane"],
                                                            "reasons": p["derived"]["eligibility"]["exclusion_reasons"]}}
            pending += 1
        common.write_json(path, o)
    return {"promoted": promoted, "pending": pending}
