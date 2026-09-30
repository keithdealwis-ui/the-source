"""Meaningful-activity rules (KEI-805 criteria 7, 8, 11, 12).

Pure functions. Given the raw observations recorded for a repository and the policy,
decide what counts as meaningful activity and classify maintenance. Stars are not an
input to anything in this module.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Rules:
    def __init__(self, policy: dict):
        m = policy["maintenance"]
        self.active_days = int(m["active_days"])
        self.maintained_days = int(m["maintained_days"])
        self.qualifying = list(m["qualifying_evidence"])
        self._docs = [re.compile(p, re.I) for p in m["documentation_path_patterns"]]
        self._house = [re.compile(p, re.I) for p in m["housekeeping_path_patterns"]]
        self._always = [re.compile(p, re.I) for p in m["code_path_overrides"]]
        self._bots = [re.compile(p, re.I) for p in m["bot_login_patterns"]]
        self._version = re.compile(m["version_tag_pattern"], re.I)

    def is_bot(self, login: str | None) -> bool:
        return bool(login) and any(p.search(login) for p in self._bots)

    def is_documentation(self, path: str) -> bool:
        if any(p.search(path) for p in self._always):
            return False
        return any(p.search(path) for p in self._docs)

    def is_code_path(self, path: str) -> bool:
        if any(p.search(path) for p in self._always):
            return True
        return not any(p.search(path) for p in self._docs + self._house)

    def code_paths(self, paths: list[str]) -> list[str]:
        return [p for p in paths if self.is_code_path(p)]

    def is_version(self, name: str | None) -> bool:
        return bool(name) and bool(self._version.match(name.strip()))

    def change_qualifies(self, change: dict) -> tuple[bool, str]:
        """A commit or merged PR qualifies only if a human made it and it touches code."""
        if self.is_bot(change.get("author")):
            return False, "automation_author"
        paths = change.get("paths")
        if not paths:
            return False, "no_paths_observed"
        if not self.code_paths(paths):
            return False, "documentation_or_housekeeping_only"
        return True, "touches_code"

    def release_qualifies(self, item: dict) -> tuple[bool, str]:
        """A release or tag qualifies only if it is a version and ships more than prose.

        The tagged commit may be a version bump, so housekeeping paths are accepted
        here; a tagged commit made up purely of documentation is not. If the tagged
        commit's contents could not be read, the release does not qualify.
        """
        if item.get("draft"):
            return False, "draft"
        if not self.is_version(item.get("ref")):
            return False, "not_a_version_tag"
        paths = item.get("paths")
        if not paths:
            return False, "tagged_commit_not_inspected"
        if all(self.is_documentation(p) for p in paths):
            return False, "tagged_commit_documentation_only"
        return True, "version_release"


def evaluate(live: dict, rules: Rules, as_of: datetime) -> dict:
    """Return the maintenance assessment for one repository's live observations."""
    evidence: list[dict] = []
    disregarded: list[dict] = []
    obs = live.get("observations") or {}

    for kind, key in (("code_commit", "commits"), ("merged_pr", "merged_prs")):
        for change in obs.get(key) or []:
            ok, why = rules.change_qualifies(change)
            at = parse_ts(change.get("at"))
            if ok and at and at <= as_of:
                evidence.append(
                    {
                        "kind": kind,
                        "at": iso(at),
                        "ref": change.get("ref"),
                        "author": change.get("author"),
                        "code_paths_sample": sorted(rules.code_paths(change["paths"]))[:5],
                    }
                )
            else:
                disregarded.append({"kind": kind, "at": change.get("at"), "ref": change.get("ref"), "why": why})

    for kind, key in (("release", "releases"), ("tag", "tags")):
        for item in obs.get(key) or []:
            at = parse_ts(item.get("at"))
            ok, why = rules.release_qualifies(item)
            if ok and at and at <= as_of:
                shipped = sorted(p for p in item["paths"] if not rules.is_documentation(p))[:5]
                evidence.append({"kind": kind, "at": iso(at), "ref": item.get("ref"),
                                 "tagged_commit": str(item.get("commit") or item.get("ref")),
                                 "shipped_paths_basis": item.get("paths_basis"),
                                 "shipped_paths_sample": shipped})
            else:
                disregarded.append({"kind": kind, "at": item.get("at"), "ref": item.get("ref"), "why": why})

    evidence = [e for e in evidence if e["kind"] in rules.qualifying]
    evidence.sort(key=lambda e: (e["at"], e["kind"], str(e.get("ref"))), reverse=True)

    supporting = {"issues_closed_12m": obs.get("issues_closed_12m")}

    if not evidence:
        return {
            "status": "stale",
            "last_meaningful_activity_at": None,
            "days_since_meaningful_activity": None,
            "evidence": [],
            "disregarded": disregarded[:10],
            "supporting": supporting,
            "basis": "no qualifying evidence observed within the lookback window",
        }

    last = parse_ts(evidence[0]["at"])
    days = (as_of - last).days
    if days <= rules.active_days:
        status = "active"
    elif days <= rules.maintained_days:
        status = "maintained"
    else:
        status = "stale"
    # Keep the newest evidence of each kind: enough to justify the class, small enough to read.
    newest: dict[str, dict] = {}
    for e in evidence:
        newest.setdefault(e["kind"], e)
    return {
        "status": status,
        "last_meaningful_activity_at": iso(last),
        "days_since_meaningful_activity": days,
        "evidence": sorted(newest.values(), key=lambda e: e["at"], reverse=True),
        "disregarded": disregarded[:10],
        "supporting": supporting,
        "basis": f"newest qualifying evidence is a {evidence[0]['kind']}",
    }
