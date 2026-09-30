"""Weekly research: move upstream lists forward, safely (KEI-807 criterion 10).

Each licence-cleared upstream list is pinned to a commit and to the SHA-256 of the licence
file its reuse decision was made about. Research asks each upstream for its newest commit
and advances the pin only when all of these hold:

  - the newest commit's licence file is byte-identical to the reviewed one;
  - the parser still reads it (no rise in unparseable files);
  - it still yields at least `min_entry_ratio` of the entries the current pin yields.

Otherwise the source stays where it is, and the reason is recorded in the run. A
licence change means a human has to review the source again; research never does that.
Advanced pins are written to data/state/source_pins.json, never into config/sources.yaml,
so the reviewed decision and the operational pin stay separate.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from . import common, enrich, harvest
from .parsers import PARSERS

PINS = common.STATE / "source_pins.json"


def _count(source: dict, src) -> tuple[int, int]:
    entries = unparseable = 0
    for raw in PARSERS[source["parser"]](src, source["parse_path"]):
        if "_unparseable" in raw:
            unparseable += 1
        else:
            entries += 1
    return entries, unparseable


def advance_pins(min_entry_ratio: float = 0.8, as_of: str | None = None) -> list[dict]:
    as_of = as_of or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cfg = common.load_sources()
    doc = common.read_json(PINS) if PINS.exists() else {"pins": {}}
    http = enrich.Http()
    results = []
    for s in sorted(cfg["sources"], key=lambda s: s["priority"]):
        sid = s["source_id"]
        if s.get("kind") == "intake" or s["decision"] not in harvest.ALLOWED_DECISIONS:
            continue
        res = {"source_id": sid, "from": s["pinned_commit"]}
        st, head = http.call(f"https://api.github.com/repos/{s['repo']}/commits/HEAD", auth=True)
        if st != 200 or not head or not head.get("sha"):
            results.append({**res, "status": "head_lookup_failed", "http_status": st})
            continue
        sha = head["sha"]
        if sha == s["pinned_commit"]:
            results.append({**res, "status": "unchanged"})
            continue
        new = dict(s, pinned_commit=sha)
        try:
            new_src = harvest.fetch(new)
            harvest.verify_licence(new, new_src)
        except harvest.LicenceDrift as exc:
            results.append({**res, "to": sha, "status": "licence_changed_needs_review", "detail": str(exc)})
            continue
        except Exception as exc:  # recorded, never swallowed; the source keeps its pin
            results.append({**res, "to": sha, "status": "fetch_failed", "detail": f"{type(exc).__name__}: {exc}"})
            continue
        old_entries, old_bad = _count(s, harvest.fetch(s))
        new_entries, new_bad = _count(new, new_src)
        if new_bad > old_bad or new_entries < min_entry_ratio * old_entries:
            results.append({**res, "to": sha, "status": "parse_regression_kept_pin",
                            "entries": [old_entries, new_entries], "unparseable": [old_bad, new_bad]})
            continue
        doc["pins"][sid] = {"pinned_commit": sha, "previous_commit": s["pinned_commit"], "advanced_at": as_of,
                            "entries": [old_entries, new_entries],
                            "licence_file_sha256": s["licence_file_sha256"]}
        results.append({**res, "to": sha, "status": "advanced", "entries": [old_entries, new_entries]})
        print(f"research: {sid} advanced {s['pinned_commit'][:8]} -> {sha[:8]} ({old_entries} -> {new_entries} entries)",
              file=sys.stderr)
    doc["pins"] = dict(sorted(doc["pins"].items()))
    common.write_json(PINS, doc)
    return results
