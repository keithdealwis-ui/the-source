"""The recommendation gate (KEI-961): when a scored relationship is served as a recommendation.

Before KEI-961 a relationship was recommended when its Replacement Fit was publishable and a credible alternative
(config/scoring.yaml thresholds: fit >= 60, core coverage >= 50, confidence >= 50, core gate passed, no
disqualifier). On the 2026-11-05 canonical layer that passed 1,534 of 1,578 relationships (97.2%). Two reasons,
neither of them a scoring bug:

- the population is pre-filtered. Only relationships that some source claims and that passed validation (licence,
  maintenance, plausibility review) are scored at all; 3,064 of 5,103 candidate relationships were already rejected
  upstream of the gate. Upstream lists only ever name positive pairings, and the job-match source (KEI-930) only
  proposes a pairing when the project's own description carries the category's defining job phrases.
- for job-matched pairs (1,000 of the 1,534) the core gate is circular: job_match.py selects on the very phrases the
  core gate then checks, so core use-case coverage scores 100 for every one and its 25 weight points come free. The
  remaining 75 points then need only a 47% average to reach 60.

The gate therefore asks for more where the evidence is thinner (config/policy.yaml recommendation_gate):

    corroborated (two or more independent listing sources)        replacement_fit_min.corroborated
    listed by one upstream list, or self-described (intake)      replacement_fit_min.single_source
    job-matched only (no list or repository names the pairing)   replacement_fit_min.job_matched

on top of the unchanged publishable-and-credible rule. `enabled: false` restores the pre-KEI-961 rule exactly.
Pure functions; no I/O except loading the policy once per call site.
"""
from __future__ import annotations

from . import common

RULE_ID = "the-source.recommendation-gate/1"


def load(policy: dict | None = None) -> dict:
    """The gate's config, with the pre-KEI-961 rule when the section is absent or disabled."""
    cfg = (policy or common.load_policy()).get("recommendation_gate") or {}
    return {"enabled": bool(cfg.get("enabled")), "replacement_fit_min": dict(cfg.get("replacement_fit_min") or {})}


def evidence_tier(evidence_class: str | None, corroborated: bool) -> str:
    if (evidence_class or "listed") == "job_matched":
        return "job_matched"
    return "corroborated" if corroborated else "single_source"


def verdict(fit: dict | None, evidence_class: str | None, corroborated: bool, cfg: dict) -> dict:
    """{"recommended": bool, "tier": ..., "fails": [reason codes]} for one relationship."""
    tier = evidence_tier(evidence_class, corroborated)
    fails = []
    if fit is None:
        fails.append("not_scored")
    else:
        if not fit.get("publish"):
            fails.append("not_publishable")
        if not fit.get("credible_alternative"):
            fails.append("not_credible")
        floor = cfg["replacement_fit_min"].get(tier) if cfg["enabled"] else None
        if floor is not None and not fails and (fit.get("score") is None or fit["score"] < float(floor)):
            fails.append(f"fit_below_{tier}_floor")
    return {"recommended": not fails, "tier": tier, "fails": fails}


def summarise(verdicts: list[dict], cfg: dict) -> dict:
    """Pass/fail ratio over the relationships evaluated, with the reasons, for the MANIFEST and the run record."""
    scored = [v for v in verdicts if "not_scored" not in v["fails"]]
    passed = sum(v["recommended"] for v in scored)
    reasons: dict[str, int] = {}
    by_tier: dict[str, dict] = {}
    for v in verdicts:
        t = by_tier.setdefault(v["tier"], {"evaluated": 0, "passed": 0})
        if "not_scored" not in v["fails"]:
            t["evaluated"] += 1
            t["passed"] += v["recommended"]
        for f in v["fails"]:
            reasons[f] = reasons.get(f, 0) + 1
    return {"rule": RULE_ID if cfg["enabled"] else "publishable_and_credible",
            "replacement_fit_min": cfg["replacement_fit_min"] if cfg["enabled"] else None,
            "relationships": len(verdicts), "evaluated": len(scored), "passed": passed,
            "failed": len(scored) - passed, "not_scored": len(verdicts) - len(scored),
            "pass_rate": round(passed / len(scored), 4) if scored else None,
            "failed_by_reason": dict(sorted(reasons.items())),
            "by_tier": {k: {**v, "pass_rate": round(v["passed"] / v["evaluated"], 4) if v["evaluated"] else None}
                        for k, v in sorted(by_tier.items())}}
