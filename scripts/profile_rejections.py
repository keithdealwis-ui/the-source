"""Exploration aid: profile why projects were rejected."""
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from source_pipeline import common  # noqa: E402

oss = common.read_jsonl(common.DATASET / "oss_projects.jsonl")
edges = common.read_jsonl(common.DATASET / "relationships.jsonl")
waiting = collections.Counter(e["oss_id"] for e in edges)

lic = collections.Counter()
for p in oss:
    if "no_recognised_open_source_licence" in p["derived"]["eligibility"]["exclusion_reasons"]:
        lic[(p["live"].get("licence_spdx"), p["live"].get("licence_name"))] += 1
print("licence rejections by reported licence:")
for k, n in lic.most_common():
    print(f"  {n}\t{k}")

print("\nnot found / unverified:")
for p in oss:
    if not p["live"]["found"]:
        print(" ", p["oss_id"], p["live"].get("error"), [u["value"] for u in p["imported"]["repo_urls"]][:2])

print("\nstale but not archived (edges waiting, commits in window, inspected, disregarded reasons):")
for p in oss:
    m = p["derived"]["maintenance"]
    if m and m["status"] == "stale" and not p["live"].get("archived"):
        why = collections.Counter(d["why"] for d in m["disregarded"])
        print(" ", p["oss_id"], waiting[p["oss_id"]], p["live"]["observed"]["commits_in_12m_window"],
              p["live"]["observed"]["commits_inspected"], p["live"]["pushed_at"], dict(why))

print("\nNOASSERTION / null licence, sole rejection reason, by edges waiting:")
rows = [p for p in oss if p["derived"]["eligibility"]["exclusion_reasons"] == ["no_recognised_open_source_licence"]]
for p in sorted(rows, key=lambda p: -waiting[p["oss_id"]])[:120]:
    print(" ", waiting[p["oss_id"]], p["oss_id"], p["live"].get("licence_spdx"),
          [d["value"] for d in p["imported"]["declared_licences"]])
