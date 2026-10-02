"""Upstream corroboration (KEI-848 criteria 10-12).

The light refresh asks each repository's own host. This stage asks independent upstreams
the same questions for a rotating daily sample and records whether they agree. It is
deliberately one-way:

  * a provider can flag a disagreement; it can never change a canonical value. The
    canonical record is always the host's own answer (GitHub, GitLab, Codeberg API);
  * a provider that is down, slow, or disagrees too often is marked degraded in the run
    record, and the cycle carries on: no upstream is a silent single point of truth, and
    none can silently redefine one;
  * only agreement statistics, identity checks and the keys that disagree are stored. No
    provider value is copied into the dataset, which also keeps ecosyste.ms's CC BY-SA 4.0
    data out of the CC BY 4.0 canonical files.

Providers and why each was adopted for this and nothing more: docs/BUILD-VS-INGEST.md.
"""
from __future__ import annotations

import hashlib
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import common

CONFIG = "corroboration.yaml"


def load_cfg() -> dict:
    return common.load_yaml(common.CONFIG / CONFIG)


def sample(keys: list[str], size: int, day: str) -> list[str]:
    """A deterministic rotating sample: every key is covered over len(keys)/size days."""
    ranked = sorted(keys, key=lambda k: hashlib.sha256(f"{day}|{k}".encode()).hexdigest())
    return sorted(ranked[:size])


def _ecosystems(http, key: str, mailto: str | None) -> dict | None:
    _, owner, name = key.split("/", 2)
    q = "?" + urllib.parse.urlencode({"mailto": mailto}) if mailto else ""
    st, d = http.call(f"https://repos.ecosyste.ms/api/v1/hosts/GitHub/repositories/{owner}/{name}{q}", tries=2)
    if st == 404:
        return {"found": False}
    if st != 200 or not isinstance(d, dict):
        return None
    lic = d.get("license")
    return {"found": True, "stars": d.get("stargazers_count"), "archived": d.get("archived"),
            "licence": lic.upper() if isinstance(lic, str) else None,
            "host_repo_id": int(d["uuid"]) if str(d.get("uuid") or "").isdigit() else None,
            "synced_at": d.get("last_synced_at")}


def _depsdev(http, key: str, mailto: str | None) -> dict | None:
    st, d = http.call("https://api.deps.dev/v3/projects/" + urllib.parse.quote(key, safe=""), tries=2)
    if st == 404:
        return {"found": False}
    if st != 200 or not isinstance(d, dict):
        return None
    return {"found": True, "stars": d.get("starsCount"), "archived": None,
            "licence": (d.get("license") or None), "host_repo_id": None, "synced_at": None}


PROVIDERS = {"ecosystems": _ecosystems, "depsdev": _depsdev}


def compare(host: dict, up: dict, tol: dict) -> list[str]:
    """What the upstream disagrees with the host about. Stars move between syncs, so they
    disagree only beyond max(absolute, relative x host value)."""
    out = []
    hs, us = host.get("stars"), up.get("stars")
    if hs is not None and us is not None and abs(hs - us) > max(int(tol["stars_absolute"]), float(tol["stars_relative"]) * hs):
        out.append("stars")
    if up.get("archived") is not None and bool(up["archived"]) != bool(host.get("archived")):
        out.append("archived")
    hl, ul = (host.get("licence_spdx") or "").upper(), (up.get("licence") or "").upper()
    if hl and ul and hl not in ("NOASSERTION", "OTHER") and ul not in ("NOASSERTION", "OTHER") and hl != ul:
        out.append("licence")
    if up.get("host_repo_id") and host.get("host_repo_id") and up["host_repo_id"] != host["host_repo_id"]:
        out.append("identity")
    return out


def run(light_projects: dict, as_of: datetime, http=None) -> dict:
    """Corroborate today's sample. Returns {"summary": {...}} for the run record."""
    from .enrich import Http

    cfg = load_cfg()
    http = http or Http()
    day = as_of.date().isoformat()
    gh = [k for k, v in light_projects.items() if k.startswith("github.com/") and v.get("found")]
    keys = sample(gh, int(cfg["sample_per_day"]), day)
    providers = {}
    for pid, pcfg in cfg["providers"].items():
        if not pcfg.get("enabled", True):
            continue
        fn = PROVIDERS[pid]
        # Ask under the repository's current name: a renamed repository is the same entity.
        ask = {k: light_projects[k].get("canonical_id") or k for k in keys}
        with ThreadPoolExecutor(max_workers=4) as pool:
            answers = dict(zip(keys, pool.map(lambda k: fn(http, ask[k], cfg.get("mailto")), keys)))
        errors = sorted(k for k, a in answers.items() if a is None)
        disagree, missing = {}, []
        for k, a in answers.items():
            if a is None:
                continue
            if not a.get("found"):
                missing.append(k)  # coverage, not disagreement: the provider simply lacks it
                continue
            host = dict(light_projects[k])
            diff = compare(host, a, cfg["tolerance"])
            if diff:
                disagree[k] = diff
        answered = len(keys) - len(errors)
        covered = answered - len(missing)
        agreement = round(1 - len(disagree) / covered, 4) if covered else None
        coverage = round(covered / answered, 4) if answered else None
        error_rate = round(len(errors) / len(keys), 4) if keys else 0.0
        if not keys:
            status = "skipped"
        elif error_rate > float(cfg["degraded_if"]["error_rate_above"]):
            status = "degraded"
            why = f"error rate {error_rate} above {cfg['degraded_if']['error_rate_above']}"
        elif agreement is not None and agreement < float(cfg["degraded_if"]["agreement_below"]):
            status = "degraded"
            why = f"agreement {agreement} below {cfg['degraded_if']['agreement_below']}"
        else:
            status, why = "ok", None
        by_field = {}
        for diff in disagree.values():
            for f in diff:
                by_field[f] = by_field.get(f, 0) + 1
        providers[pid] = {"status": status, "reason": why, "trusted_for": pcfg["trusted_for"],
                          "asked": len(keys), "answered": answered, "errors": len(errors),
                          "coverage": coverage, "not_covered": missing[:50], "agreement": agreement,
                          "disagreements_by_field": dict(sorted(by_field.items())),
                          "disagreeing_keys": {k: v for k, v in sorted(disagree.items())[:50]},
                          "canonical_effect": "none: flags only; the host's own answer stays canonical"}
    degraded = sorted(p for p, v in providers.items() if v["status"] == "degraded")
    return {"summary": {"status": "ok" if not degraded else "degraded", "sample": len(keys), "day": day,
                        "degraded_providers": degraded, "providers": providers}}
