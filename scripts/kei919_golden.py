"""KEI-919 WP2 correctness golden set: precision@5, recall@5, constraint violations, padding, lookups and
integrations among top-five discovery results, over the MCP tools in-process.

    python scripts/kei919_golden.py                       # score the working tree
    python scripts/kei919_golden.py --server path/to/mcp_server.py --out baseline.json   # score another revision

The integration audit is deliberately broader and independent of the server's own classifier, so the server
cannot pass by agreeing with itself.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
K = 5

# What a project IS, from its own name and description (tags are ignored: many real replacements also ship an
# MCP server). Independent of the server's classifier.
AUDIT_NAME = re.compile(r"(^|[-_. ])(mcp|sdk|bot|plugin|api[-_ ]?client|connector)([-_. ]|$)")
AUDIT_DESC = re.compile(r"\b(mcp server|model context protocol server|sdk for|bot for|plugin for|extension for|"
                        r"integration for|integrates with|connector for|client for|wrapper for|library for)\b")
AUDIT_DESC_API_TOOLING = re.compile(r"\b(mcp server|model context protocol server|sdk for|bot for|plugin for|"
                                    r"extension for|integration for|connector for|wrapper for|library for)\b")


def load_server(path: Path | None):
    if path is None:
        sys.path.insert(0, str(ROOT))
        from source_pipeline import mcp_server as m
        return m
    spec = importlib.util.spec_from_file_location("source_pipeline.mcp_server_under_test", path,
                                                  submodule_search_locations=None)
    m = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT))
    import source_pipeline  # noqa: F401  (relative imports resolve against the real package)
    m.__package__ = "source_pipeline"
    spec.loader.exec_module(m)
    return m


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", type=Path)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    m = load_server(a.server)
    srv = m.Server(ROOT)
    layer = srv.layer

    def call(tool, args):
        fn = m.TOOL_BY_NAME[tool][4]
        try:
            return fn(layer, args, srv)
        except m.ToolError as exc:
            return {"error": str(exc)}

    gold = yaml.safe_load((ROOT / "config" / "golden_kei919.yaml").read_text())
    pages = {}
    for p in json.loads((ROOT / "api" / "v1" / "index.json").read_text())["supported_saas"]:
        pages[p["saas_id"]] = json.loads((ROOT / "api" / "v1" / p["path"]).read_text())
    by_cat = {}
    for sid, d in pages.items():
        by_cat.setdefault(layer.saas[sid]["category"], set()).update(r["oss_id"] for r in d["recommendations"])

    def relevant(sid):
        return by_cat.get(layer.saas[sid]["category"], set())

    report = {"k": K, "need": [], "constraints": [], "nonsense": [], "lookups": [], "integrations": []}
    for group in ("need_named", "need_unnamed"):
        for q in gold[group]:
            r = call("source_recommend", {"need": q["need"], "limit": K})
            got = [x["oss_id"] for x in r.get("recommendations", [])][:K]
            rel = relevant(q["saas"])
            hit = len(set(got) & rel)
            report["need"].append({"group": group, "need": q["need"], "saas": q["saas"], "returned": got,
                                   "precision": round(hit / len(got), 4) if got else 0.0,
                                   "recall": round(hit / min(K, len(rel)), 4) if rel else None,
                                   "error": r.get("error")})

    fam = getattr(m, "LICENCE_FAMILIES", {})
    for q in gold["constraints"]:
        args = {k: v for k, v in q.items()}
        args["limit"] = K
        r = call("source_recommend", args)
        viol = []
        for x in r.get("recommendations", []):
            if "licence_family" in q and fam.get(x["licence_spdx"] or "", "unknown") != q["licence_family"]:
                viol.append((x["oss_id"], "licence_family"))
            if "licence" in q and (x["licence_spdx"] or "") not in q["licence"]:
                viol.append((x["oss_id"], "licence"))
            if q.get("self_hostable") and not (x.get("self_hosting") or {}).get("self_hostable"):
                viol.append((x["oss_id"], "self_hostable"))
            if "min_stars" in q and (x["stars"] or 0) < q["min_stars"]:
                viol.append((x["oss_id"], "min_stars"))
            if "max_stars" in q and (x["stars"] or 0) > q["max_stars"]:
                viol.append((x["oss_id"], "max_stars"))
            if "maintenance_status" in q and x["maintenance_status"] not in q["maintenance_status"]:
                viol.append((x["oss_id"], "maintenance_status"))
            if "min_health" in q and ((x.get("project_health") or {}).get("score") or 0) < q["min_health"]:
                viol.append((x["oss_id"], "min_health"))
        report["constraints"].append({"query": q, "returned": len(r.get("recommendations", [])),
                                      "violations": viol, "explicit": bool(r.get("excluded_by_constraint") is not None),
                                      "shortfall": r.get("shortfall")})

    for text in gold["nonsense"]:
        r = call("source_recommend", {"need": text, "limit": K, "include_discovery": True})
        d = (r.get("discovery_candidates") or {}).get("candidates") or []
        report["nonsense"].append({"need": text, "recommendations": len(r.get("recommendations", [])),
                                   "discovery": [x["id"] for x in d]})

    for q in gold["lookups"]:
        r = call("source_get_project", {"project": q["value"]})
        got = None if "error" in r else (r["project"].get("oss_id") or r["project"].get("id"))
        report["lookups"].append({"value": q["value"], "expect": q["expect"], "got": got,
                                  "ok": got == q["expect"], "error": r.get("error")})

    for sid in sorted(pages):
        r = call("source_get_alternatives", {"saas": sid, "include_discovery": True, "limit": K})
        top = ((r.get("discovery_candidates") or {}).get("candidates") or [])[:K]
        # An API client IS the product for API tooling (Postman), so "client for" is not flagged there.
        desc_rx = AUDIT_DESC_API_TOOLING if layer.saas[sid]["category"] == "API tooling" else AUDIT_DESC
        flagged = [x["id"] for x in top
                   if AUDIT_NAME.search(x["full_name"].split("/")[-1].lower())
                   or desc_rx.search(" ".join((x.get("description") or "").lower().split()))]
        report["integrations"].append({"saas": sid, "top5": [x["id"] for x in top], "flagged": flagged})

    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 4) if xs else None

    s = {}
    for group in ("need_named", "need_unnamed"):
        rows = [r for r in report["need"] if r["group"] == group]
        s[group] = {"queries": len(rows), "precision_at_5": mean(r["precision"] for r in rows),
                    "recall_at_5": mean(r["recall"] for r in rows),
                    "empty": sum(1 for r in rows if not r["returned"])}
    s["constraint_violations"] = sum(len(r["violations"]) for r in report["constraints"])
    s["constraint_queries"] = len(report["constraints"])
    s["padding_rows"] = sum(r["recommendations"] + len(r["discovery"]) for r in report["nonsense"])
    s["lookups_ok"] = f"{sum(r['ok'] for r in report['lookups'])}/{len(report['lookups'])}"
    s["integrations_in_top5"] = sum(len(r["flagged"]) for r in report["integrations"])
    s["supported_products_audited"] = len(report["integrations"])
    report["summary"] = s
    text = json.dumps(report, indent=1, sort_keys=True)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(text + "\n")
    print(json.dumps(s, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
