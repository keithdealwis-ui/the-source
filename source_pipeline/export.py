"""Public export guard (KEI-805 criterion 15).

Builds the view of the launch corpus that could be published, with the notices each
upstream licence requires. It writes to a local directory and publishes nothing:
publication is an external-facing action that needs Keith's approval.

Refuses to run if validation fails or any contributing source is not licence-cleared.
"""
from __future__ import annotations

from pathlib import Path

from . import common, validate

MIT_TEXT = """Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""


class ExportRefused(Exception):
    pass


def run(out: str | None = None, include_share_alike: bool = False) -> dict:
    # An export left over from an earlier dataset says nothing about this one.
    stale = common.DATA / "export-public"
    if out is None and stale.exists():
        import shutil

        shutil.rmtree(stale)
    report = validate.run()
    if not all(c["ok"] for c in report["checks"] if c["criterion"] != 6):
        raise ExportRefused("dataset validation failed; nothing exported")
    sources = {s["source_id"]: s for s in common.read_json(common.DATASET / "sources.json")}
    edges = [e for e in common.read_jsonl(common.DATASET / "relationships.jsonl") if e["launch"]]
    oss = {p["oss_id"]: p for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl")}
    saas = {s["saas_id"]: s for s in common.read_jsonl(common.DATASET / "saas_products.jsonl")}

    kept, withheld = [], []
    for e in edges:
        project = oss[e["oss_id"]]
        if (not e["recommendation_eligible"] or not project["derived"]["recommendation_eligible"]
                or project["derived"]["lane"] != "recommendable"):
            raise ExportRefused(f"{e['edge_id']} is not recommendation-eligible; it must never be exported")
        bad = [s for s in e["source_ids"] if sources[s]["decision"] not in ("allow", "allow_with_conditions")]
        if bad:
            raise ExportRefused(f"{e['edge_id']} depends on a source that is not licence-cleared: {bad}")
        if e["share_alike_only"] and not include_share_alike:
            withheld.append(e["edge_id"])
            continue
        kept.append(e)

    dest = Path(out) if out else common.DATA / "export-public"
    used_sources = sorted({s for e in kept for s in e["source_ids"]}, key=lambda s: sources[s]["priority"])
    if not include_share_alike:
        # An edge corroborated by a permissive source stands on that source alone.
        used_sources = [s for s in used_sources if not sources[s]["share_alike"]]
    rows = []
    for e in kept:
        p = oss[e["oss_id"]]
        cited = [s for s in e["source_ids"] if s in used_sources]
        rows.append(
            {
                "saas": {"id": e["saas_id"], "name": saas[e["saas_id"]]["name"],
                         "category": saas[e["saas_id"]]["category"]},
                "oss": {
                    "id": p["oss_id"], "name": p["name"], "repo_url": p["repo_url"],
                    "licence_spdx": p["derived"]["licence"]["spdx"],
                    "maintenance": p["derived"]["maintenance"]["status"],
                    "last_meaningful_activity_at": p["derived"]["maintenance"]["last_meaningful_activity_at"],
                    "verified_at": p["live"]["fetched_at"],
                },
                "sources": cited,
            }
        )
    rows.sort(key=lambda r: (r["saas"]["id"], r["oss"]["id"]))
    common.write_jsonl(dest / "relationships.jsonl", rows)

    notice = ["# NOTICE", "",
              "Relationship claims in this dataset were compiled from the sources below. Licence,",
              "archive state and maintenance status of every project were verified independently",
              "against the project's own repository host; they are not taken from these sources.", ""]
    for sid in used_sources:
        s = sources[sid]
        notice += [f"## {s['repo']}", "", f"- Source: {s['url']} at commit {s['pinned_commit']}",
                   f"- Licence: {s['licence_spdx']}", f"- Attribution: {s['attribution']}", ""]
        if s["licence_spdx"] == "MIT":
            notice += ["```", s["attribution"].split(", MIT License")[0].split(", ", 1)[1], "", MIT_TEXT, "```", ""]
        if s["share_alike"]:
            notice += ["Material from this source is licensed CC BY-SA 3.0 Unported",
                       "(https://creativecommons.org/licenses/by-sa/3.0/). This export, to the extent it",
                       "adapts that material, is released under the same licence.", ""]
    (dest / "NOTICE.md").write_text("\n".join(notice), encoding="utf-8")
    result = {
        "dataset_as_of": common.read_json(common.DATASET / "summary.json")["as_of"],
        "written_to": str(dest),
        "published": False,
        "relationships_exported": len(rows),
        "relationships_withheld_share_alike_only": len(withheld),
        "sources_attributed": used_sources,
        "export_licence_constraint": "CC-BY-SA-3.0" if include_share_alike else "none beyond attribution notices",
    }
    common.write_json(dest / "EXPORT.json", result)
    return result
