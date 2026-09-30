"""Cross-check the launch projects against a second endpoint.

The pipeline verifies GitHub repositories through the GraphQL API. This script asks
the REST API the same questions about every launch project hosted on GitHub and
reports any disagreement on identity, archive state or licence. Read-only.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from source_pipeline import common  # noqa: E402
from source_pipeline.enrich import Http  # noqa: E402

http = Http()
projects = [p for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl")
            if p["derived"]["launch"] and p["host"] == "github.com"]


def check(p):
    status, repo = http.call(f"https://api.github.com/repos/{p['live']['canonical_name']}", auth=True)
    if status != 200 or not repo:
        return {"oss_id": p["oss_id"], "problem": f"rest_http_{status}"}
    issues = []
    if repo["id"] != p["live"]["host_repo_id"]:
        issues.append(f"repository id {repo['id']} != {p['live']['host_repo_id']}")
    if repo["full_name"].lower() != p["live"]["canonical_name"].lower():
        issues.append(f"name {repo['full_name']} != {p['live']['canonical_name']}")
    if bool(repo["archived"]) != bool(p["live"]["archived"]) or repo["archived"]:
        issues.append(f"archived: rest={repo['archived']} dataset={p['live']['archived']}")
    if repo.get("disabled"):
        issues.append("disabled")
    rest_spdx = (repo.get("license") or {}).get("spdx_id")
    if rest_spdx != p["live"]["licence_spdx"]:
        issues.append(f"licence: rest={rest_spdx} dataset={p['live']['licence_spdx']}")
    return {"oss_id": p["oss_id"], "problem": "; ".join(issues) or None}


with ThreadPoolExecutor(max_workers=8) as pool:
    results = list(pool.map(check, projects))
problems = [r for r in results if r["problem"]]
report = {
    "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "dataset_as_of": common.read_json(common.DATASET / "summary.json")["as_of"],
    "endpoint": "GitHub REST GET /repos/{owner}/{repo}",
    "launch_projects_on_github": len(projects),
    "agree": len(results) - len(problems),
    "disagree": len(problems),
    "disagreements": problems,
}
out = common.ROOT / "data" / "cross_check_launch.json"
common.write_json(out, report)
print(json.dumps(report, indent=2))
