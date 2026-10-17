"""Rejection reason codes and the contradictory-evidence review queue (KEI-919 S1.5).

Every record in data/dataset/rejections.jsonl carries exactly one `code` from CODES and a
human `detail`. The machine `reason` written before KEI-919 is kept unchanged beside it, so
existing readers keep working. The set is closed: a reason the build produces that has no
code here fails the build (code_for raises) and fails validation (the JSON schema enum).

The review queue holds claims that three or more independent sources contradict. Nothing
in it is silently accepted or rejected: the build holds every relationship it touches out
of recommendations (fail closed) until a human resolution is recorded in
config/evidence_review.yaml. No score, confidence or gate threshold is changed by any of it.
"""
from __future__ import annotations

import re

from . import common

# code -> what it means. The JSON schema's rejection.code enum must equal this key set.
CODES: dict[str, str] = {
    "not_a_repository": "the upstream URL does not name a source repository (missing, a website, or an organisation page)",
    "saas_not_in_catalogue": "the SaaS product the upstream names is not in the reviewed catalogue",
    "not_found": "the repository host reports the repository does not exist",
    "host_unverifiable": "the repository lives on a host The Source cannot independently verify",
    "verification_failed": "live verification against the host did not complete",
    "archived": "the repository is archived (read-only) on its host",
    "disabled": "the repository is disabled on its host",
    "empty_repository": "the repository has no content",
    "identity_unverified": "the repository's identity changed since the upstream listed it and no reviewed rename confirms it",
    "licence_not_open": "no recognised open-source licence (and no approved licence exception)",
    "unmaintained": "no meaningful activity in the last 12 months",
    "not_a_replacement": "desk plausibility review: the project is a different kind of software, not a replacement for the product",
    "contradictory_evidence": "three or more independent sources contradict each other; held in the review queue until a human resolves it",
}

# Raw machine reasons the pipeline emits -> code. Eligibility reasons are listed in
# precedence order: a project rejected for several reasons is coded by the first.
_ELIGIBILITY = [
    ("repository_not_found", "not_found", "the host reports the repository does not exist"),
    ("host_not_independently_verifiable", "host_unverifiable", "the repository's host cannot be independently verified"),
    ("live_verification_failed", "verification_failed", "live verification against the host failed"),
    ("archived", "archived", "archived on its host"),
    ("disabled", "disabled", "disabled on its host"),
    ("empty_repository", "empty_repository", "the repository is empty"),
    ("repository_identity_changed_since_listed", "identity_unverified", "the repository's identity changed since it was listed"),
    ("no_recognised_open_source_licence", "licence_not_open", "no recognised open-source licence"),
    ("stale_no_meaningful_activity_12_months", "unmaintained", "no meaningful activity in 12 months"),
]
ELIGIBILITY_CODE = {raw: code for raw, code, _ in _ELIGIBILITY}
ELIGIBILITY_TEXT = {raw: text for raw, _, text in _ELIGIBILITY}
_RANK = {raw: i for i, (raw, _, _) in enumerate(_ELIGIBILITY)}

NORMALISE_CODE = {
    "repo_url_missing": "not_a_repository",
    "repo_url_not_a_repository": "not_a_repository",
    "saas_not_in_catalogue": "saas_not_in_catalogue",
}


class UnknownReason(ValueError):
    """A rejection reason with no code: the build stops rather than publish an uncoded rejection."""


def code_for(record: dict) -> tuple[str, str]:
    """(code, detail) for a rejection record. Raises UnknownReason for anything unmapped."""
    stage, reason = record["stage"], record["reason"]
    if stage == "eligibility":
        raws = reason.split(";")
        unknown = [r for r in raws if r not in ELIGIBILITY_CODE]
        if unknown or not raws:
            raise UnknownReason(f"eligibility reason(s) with no code: {unknown or reason!r}")
        raws.sort(key=_RANK.__getitem__)
        detail = "; ".join(ELIGIBILITY_TEXT[r] for r in raws)
        return ELIGIBILITY_CODE[raws[0]], detail
    if stage == "normalise":
        if reason not in NORMALISE_CODE:
            raise UnknownReason(f"normalise reason with no code: {reason!r}")
        code = NORMALISE_CODE[reason]
        if code == "saas_not_in_catalogue":
            return code, f"'{record.get('saas_name')}' is not a product in the reviewed SaaS catalogue"
        return code, f"'{record.get('repo_url') or ''}' does not name a source repository"
    if stage == "plausibility_review":
        return "not_a_replacement", f"plausibility review ({record.get('reviewed_by')}, {record.get('reviewed_at')}): {reason}"
    raise UnknownReason(f"rejection stage with no codes: {stage!r}")


def code_all(rejections: list[dict]) -> None:
    """Attach code + detail to every record in place (fails on the first unmapped reason)."""
    for r in rejections:
        r["code"], r["detail"] = code_for(r)


# --- contradictory evidence ---------------------------------------------------------------

MIN_SOURCES = 3
# Values that assert nothing about which licence applies.
_NON_ASSERTIONS = {"", "NOASSERTION", "OTHER", "UNKNOWN", "NONE", "N/A"}


def norm_licence(value: str | None) -> str | None:
    """Comparable licence identifier: case, 'License' suffix and -only/-or-later folded."""
    if not value:
        return None
    v = re.sub(r"\s+LICEN[SC]E$", "", value.strip().upper())
    v = re.sub(r"(-ONLY|-OR-LATER|\+)$", "", v)
    return None if v in _NON_ASSERTIONS else v


def licence_evidence(project: dict) -> list[dict]:
    """Every independent assertion about a project's licence: each upstream list once per
    value, the host API, and The Source's own reading of the licence files."""
    out = []
    for d in project["imported"]["declared_licences"]:
        n = norm_licence(d["value"])
        if n:
            out.append({"source_id": d["source_id"], "kind": "upstream_list", "value": d["value"], "normalised": n})
    host = project["live"].get("licence_spdx")
    if norm_licence(host):
        out.append({"source_id": "host_api", "kind": "repository_host", "value": host, "normalised": norm_licence(host)})
    det = ((project["derived"].get("licence") or {}).get("detection")) or {}
    if det.get("method") != "host_api" and norm_licence(det.get("spdx")):
        out.append({"source_id": "licence_files", "kind": "licence_text_detection", "value": det["spdx"],
                    "normalised": norm_licence(det["spdx"])})
    return out


def load_resolutions() -> dict:
    """Human resolutions of queue items, keyed by queue_id. An executor cannot resolve one."""
    from .build import EXECUTORS

    path = common.CONFIG / "evidence_review.yaml"
    doc = (common.load_yaml(path) if path.exists() else None) or {}
    out = {}
    for r in doc.get("resolved") or []:
        who = str(r.get("reviewed_by") or "").strip().lower()
        if who in EXECUTORS or not r.get("approval_ref") or not r.get("resolution"):
            raise ValueError(f"evidence review resolution for {r.get('queue_id')} needs a human reviewer, "
                             "an approval_ref and a resolution")
        out[r["queue_id"]] = r
    return out


def review_queue(projects: dict, edges: list[dict], as_of: str, resolutions: dict | None = None) -> list[dict]:
    """Licence claims contradicted across >= MIN_SOURCES independent sources."""
    resolutions = load_resolutions() if resolutions is None else resolutions
    by_oss: dict[str, list[str]] = {}
    for e in edges:
        by_oss.setdefault(e["oss_id"], []).append(e.get("edge_id") or f"{e['saas_id']}--{e['oss_id']}")
    queue = []
    for oss_id in sorted(projects):
        ev = licence_evidence(projects[oss_id])
        sources = sorted({x["source_id"] for x in ev})
        values = sorted({x["normalised"] for x in ev})
        if len(sources) < MIN_SOURCES or len(values) < 2:
            continue
        qid = f"licence--{oss_id}"
        res = resolutions.get(qid)
        queue.append({
            "queue_id": qid,
            "code": "contradictory_evidence",
            "claim": {"kind": "licence", "oss_id": oss_id,
                      "question": "which licence governs this repository"},
            "evidence": ev,
            "distinct_values": values,
            "source_count": len(sources),
            "affected_edges": sorted(by_oss.get(oss_id, [])),
            "status": "resolved" if res else "open",
            "resolution": ({k: res[k] for k in ("resolution", "reviewed_by", "reviewed_at", "approval_ref") if k in res}
                           if res else None),
            "effect": ("none: resolved by a human review" if res else
                       "every affected relationship is held out of recommendations until reviewed"),
            "as_of": as_of,
        })
    return queue
