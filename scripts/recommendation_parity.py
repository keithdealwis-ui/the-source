"""Recommendation parity (KEI-912 acceptance criterion 3).

    python scripts/recommendation_parity.py [--base REV] [--root DIR] [--out FILE]

Loads the MCP engine at REV (default: origin/main) and at the working tree, points both at the
same read layer, and calls every recommendation-producing tool over the whole layer:
source_get_alternatives for every catalogued product (limit 50), source_recommend with `saas`
for every product (limit 50), and source_recommend with `need` for a fixed query set, with and
without constraints. Every answer must be byte-for-byte identical (canonical JSON). The working
tree's answers must also contain only recommended-tier projects. Exits 1 on any difference.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEEDS = [
    "team wiki and notes like Notion", "password manager", "self-hosted analytics", "MCP server for web scraping",
    "project management and issue tracking", "video conferencing", "email marketing", "customer support helpdesk",
    "file sync and sharing", "CRM for small business", "form builder", "workflow automation like Zapier",
    "chat for teams", "observability and error tracking", "e-signature", "headless CMS",
]
CONSTRAINED = [
    {"need": "team wiki and notes", "licence_family": "permissive"},
    {"need": "analytics", "maintenance_status": ["active"], "min_fit": 60},
    {"need": "password manager", "min_stars": 1000, "licence": ["AGPL-3.0", "GPL-3.0"]},
    {"saas": "Slack", "min_health": 50, "max_days_since_activity": 60},
]


def load_engine(source: str, name: str):
    tmp = Path(tempfile.mkdtemp()) / f"{name}.py"
    tmp.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, tmp)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def calls(layer):
    out = []
    for sid in sorted(layer.saas):
        out.append(("source_get_alternatives", {"saas": sid, "limit": 50}))
        out.append(("source_recommend", {"saas": sid, "limit": 50}))
    for need in NEEDS:
        out.append(("source_recommend", {"need": need, "limit": 50}))
    for args in CONSTRAINED:
        out.append(("source_recommend", {**args, "limit": 50}))
    return out


def answer(mod, server, tool, args):
    fn = mod.TOOL_BY_NAME[tool][4]
    try:
        return {"ok": fn(server.layer, dict(args), server)}
    except mod.ToolError as exc:
        return {"tool_error": str(exc)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--now", default="2026-10-10T12:00:00Z")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    now = datetime.strptime(a.now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    base_src = subprocess.run(["git", "-C", str(ROOT), "show", f"{a.base}:source_pipeline/mcp_server.py"],
                              capture_output=True, text=True, check=True).stdout
    base = load_engine(base_src, "mcp_base")
    head = load_engine((ROOT / "source_pipeline" / "mcp_server.py").read_text(encoding="utf-8"), "mcp_head")
    sb, sh = base.Server(Path(a.root), now=now), head.Server(Path(a.root), now=now)
    diffs, tier_violations, n = [], [], 0
    digest = hashlib.sha256()
    for tool, args in calls(sb.layer):
        n += 1
        x = json.dumps(answer(base, sb, tool, args), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        y_obj = answer(head, sh, tool, args)
        y = json.dumps(y_obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest.update(x.encode())
        if x != y:
            diffs.append({"tool": tool, "args": args})
        for row in (y_obj.get("ok") or {}).get("recommendations") or []:
            if sh.layer.tier_of(row["oss_id"]) != "recommended":
                tier_violations.append({"tool": tool, "args": args, "oss_id": row["oss_id"]})
    report = {"base": a.base, "base_commit": subprocess.run(["git", "-C", str(ROOT), "rev-parse", a.base],
                                                            capture_output=True, text=True).stdout.strip(),
              "dataset_version": sb.layer.dataset_version, "calls": n, "identical": n - len(diffs),
              "different": diffs[:20], "different_total": len(diffs),
              "non_recommended_in_recommendations": tier_violations[:20],
              "answers_sha256": digest.hexdigest(),
              "ok": not diffs and not tier_violations}
    text = json.dumps(report, indent=2)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
