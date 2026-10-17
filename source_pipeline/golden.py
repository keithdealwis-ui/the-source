"""Golden-query evaluation of the MCP retrieval surface (KEI-918 S0.4, docs/OBSERVABILITY.md).

    python -m source_pipeline obs-golden                          evaluate config/golden_queries.yaml
    python -m source_pipeline obs-golden --write-baseline FILE    ... and record the result as the baseline
    python -m source_pipeline obs-golden --baseline FILE --check  exit 1 on any violation or a regression

Each golden query is a real tool call on the MCP engine (mcp_server.Server, in process, the
clock pinned to the layer's data_as_of so the run is deterministic). Three measures:

* recall@k: labelled-relevant projects in the top k / min(k, relevant that could be returned).
  For a constrained call, "could be returned" first drops relevant projects whose published
  facts fail a constraint, so a constraint that rightly excludes a project is not a miss.
* precision@k: labelled-relevant projects in the top k / rows in the top k. A call that returns
  nothing has no precision (it is reported, not averaged).
* constraint violations: every row returned is checked against the call's constraints and the
  publication rules (a recommendation must be a recommended relationship in the canonical layer,
  for the product asked about, never a discovery-tier project, and a supported product's first
  rows are its published api/v1 page), recomputed from the published files, not taken from the
  server's own filter. Any violation fails --check.

The labels are written from domain knowledge, not from the ranking; see config/golden_queries.yaml.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import common
from . import mcp_server as M

QUERIES = common.CONFIG / "golden_queries.yaml"
SCHEMA = "the-source.golden-eval/1"


def _key(label: str) -> str:
    return label if label.split("/")[0] in ("github.com", "gitlab.com", "codeberg.org") else f"github.com/{label}"


class Facts:
    """The published files, read directly: the independent side of the violation check."""

    def __init__(self, root: Path):
        canon = root / "data" / "canonical"
        self.projects = {r["oss_id"]: r for r in common.read_jsonl(canon / "oss_projects.jsonl")}
        self.rel = {(r["saas_id"], r["oss_id"]): r for r in common.read_jsonl(canon / "relationships.jsonl")}
        self.saas = {r["saas_id"]: r for r in common.read_jsonl(canon / "saas_products.jsonl")}
        self.pages = {}
        for p in sorted((root / "api" / "v1" / "saas").glob("*.json")):
            doc = common.read_json(p)
            self.pages[doc["saas"]["saas_id"]] = doc
        self.page_rows = {(sid, r["oss_id"]): r for sid, d in self.pages.items() for r in d["recommendations"]}
        mom = root / "data" / "momentum" / "projects.jsonl"
        self.momentum = {}
        for r in common.read_jsonl(mom) if mom.exists() else []:
            self.momentum.setdefault(r.get("canonical_id") or r["key"], r)

    def failed_constraints(self, oss_id: str, saas_id: str | None, args: dict) -> list[str]:
        """The constraints in args this project (for this product) does not meet, from the published facts."""
        p = self.projects.get(oss_id)
        if p is None:
            return ["not_in_canonical_layer"]
        bad = []
        spdx = p["licence"]["spdx"] or ""
        if args.get("licence") and spdx not in args["licence"]:
            bad.append("licence")
        if args.get("licence_family") and M.LICENCE_FAMILIES.get(spdx) != args["licence_family"]:
            bad.append("licence_family")
        if args.get("maintenance_status") and p["maintenance"]["status"] not in args["maintenance_status"]:
            bad.append("maintenance_status")
        stars = p["live"]["stars"] or 0
        if "min_stars" in args and stars < args["min_stars"]:
            bad.append("min_stars")
        if "max_stars" in args and stars > args["max_stars"]:
            bad.append("max_stars")
        days = p["maintenance"]["days_since_meaningful_activity"]
        if "max_days_since_activity" in args and (days is None or days > args["max_days_since_activity"]):
            bad.append("max_days_since_activity")
        h = p["project_health"]
        if "min_health" in args and (h is None or h["score"] < args["min_health"]):
            bad.append("min_health")
        if saas_id is not None:
            r = self.rel.get((saas_id, oss_id))
            fit = ((r or {}).get("replacement_fit") or {}).get("score") or 0
            if "min_fit" in args and fit < args["min_fit"]:
                bad.append("min_fit")
            if args.get("self_hostable") is True:
                sh = (self.page_rows.get((saas_id, oss_id)) or {}).get("self_hosting")
                if not sh or sh.get("self_hostable") is not True:
                    bad.append("self_hostable")
        if args.get("repository_momentum_7d"):
            w = ((self.momentum.get(oss_id) or {}).get("windows") or {}).get("7") or {}
            if w.get("momentum") not in args["repository_momentum_7d"]:
                bad.append("repository_momentum_7d")
        return bad


CONSTRAINT_KEYS = ("licence", "licence_family", "self_hostable", "maintenance_status", "min_fit", "min_health",
                   "min_stars", "max_stars", "max_days_since_activity", "repository_momentum_7d")


def _rows(tool: str, result: dict) -> list[dict]:
    if tool == "source_search":
        return [{"oss_id": r["id"], "tier": r.get("tier")} for r in result.get("results", []) if r["kind"] == "project"]
    return result.get("recommendations", [])


def _violations(q: dict, result: dict, facts: Facts, server) -> list[str]:
    tool, args = q["tool"], q["args"]
    out = []
    if tool == "source_search":
        return out  # search ranks every tier by design; nothing in it is a recommendation
    rows = _rows(tool, result)
    if len(rows) > args.get("limit", 5):
        out.append(f"returned {len(rows)} rows for limit {args.get('limit', 5)}")
    if tool == "source_get_alternatives":
        sid = result["saas"]["saas_id"]
        page = facts.pages.get(sid)
        if page and [r["oss_id"] for r in rows[:len(page["recommendations"])]] != \
                [r["oss_id"] for r in page["recommendations"]][:len(rows)]:
            out.append(f"{sid}: first rows are not the published api/v1 page")
    for r in rows:
        oid = r["oss_id"]
        sid = r.get("for_saas") or (result.get("saas") or {}).get("saas_id")
        rel = facts.rel.get((sid, oid))
        if rel is None or not rel["recommended"]:
            out.append(f"{oid}: not a recommended relationship for {sid}")
        if server.layer.tier_of(oid) == "discovery":
            out.append(f"{oid}: discovery-tier project returned as a recommendation")
        bad = facts.failed_constraints(oid, sid, {k: args[k] for k in CONSTRAINT_KEYS if k in args})
        out += [f"{oid}: breaks {c}" for c in bad]
    return out


def _call(server, tool: str, args: dict):
    msg = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}})
    res = msg.get("result") or {}
    if "error" in msg or res.get("isError"):
        return None, (res.get("content") or [{}])[0].get("text") or (msg.get("error") or {}).get("message")
    return res["structuredContent"]["result"], None


def evaluate(root: Path | None = None, queries_path: Path | None = None) -> dict:
    root = Path(root or common.ROOT)
    spec = common.load_yaml(queries_path or QUERIES)
    k = int(spec.get("k", 5))
    manifest = common.read_json(root / "data" / "canonical" / "MANIFEST.json")
    now = datetime.strptime(manifest["data_as_of"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    server = M.Server(root, now)
    server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    facts = Facts(root)
    rows_out = []
    for q in spec["queries"]:
        result, error = _call(server, q["tool"], q["args"])
        row = {"id": q["id"], "tool": q["tool"]}
        if "expect" in q:
            if q["expect"] == "tool_error":
                ok = error is not None
            else:  # empty
                ok = error is None and not _rows(q["tool"], result)
            row.update(kind="negative", expect=q["expect"], passed=ok, error=error)
            row["violations"] = [] if ok else [f"expected {q['expect']}, got {'error: ' + error if error else 'rows'}"]
            rows_out.append(row)
            continue
        if error is not None:
            row.update(kind="positive", error=error, violations=[f"tool error: {error}"], top=[], recall=0.0,
                       precision=None, relevant=len(q["relevant"]))
            rows_out.append(row)
            continue
        top = [r["oss_id"] for r in _rows(q["tool"], result)[:k]]
        relevant = [_key(x) for x in q["relevant"]]
        constraints = {c: q["args"][c] for c in CONSTRAINT_KEYS if c in q["args"]}
        if constraints:
            sid = (result.get("matched_products") or [{}])[0].get("saas_id")
            reachable = [x for x in relevant if not facts.failed_constraints(x, sid, constraints)]
        else:
            reachable = relevant
        hits = [x for x in top if x in relevant]
        denom = min(k, len(reachable))
        row.update(kind="positive", top=top, hits=hits, relevant=len(relevant), reachable=len(reachable),
                   recall=None if denom == 0 else round(len(hits) / denom, 4),
                   precision=None if not top else round(len(hits) / len(top), 4),
                   violations=_violations(q, result, facts, server),
                   status=result.get("status"))
        rows_out.append(row)
    pos = [r for r in rows_out if r["kind"] == "positive"]
    rec = [r["recall"] for r in pos if r["recall"] is not None]
    pre = [r["precision"] for r in pos if r["precision"] is not None]
    by_tool = {}
    for t in sorted({r["tool"] for r in pos}):
        rr = [r["recall"] for r in pos if r["tool"] == t and r["recall"] is not None]
        pp = [r["precision"] for r in pos if r["tool"] == t and r["precision"] is not None]
        by_tool[t] = {"queries": sum(1 for r in pos if r["tool"] == t),
                      f"recall@{k}": round(sum(rr) / len(rr), 4) if rr else None,
                      f"precision@{k}": round(sum(pp) / len(pp), 4) if pp else None}
    summary = {"queries": len(rows_out), "positive": len(pos), "negative": len(rows_out) - len(pos),
               f"recall@{k}": round(sum(rec) / len(rec), 4) if rec else None,
               f"precision@{k}": round(sum(pre) / len(pre), 4) if pre else None,
               "recall_measured_on": len(rec), "precision_measured_on": len(pre),
               "no_rows": sorted(r["id"] for r in pos if not r["top"]),
               "constraint_violations": sum(len(r["violations"]) for r in rows_out),
               "queries_with_violations": sorted(r["id"] for r in rows_out if r["violations"]),
               "negative_passed": sum(1 for r in rows_out if r["kind"] == "negative" and r["passed"]),
               "by_tool": by_tool}
    return {"schema": SCHEMA, "k": k, "dataset_version": server.layer.dataset_version,
            "data_as_of": manifest["data_as_of"], "server_version": M.SERVER_VERSION,
            "queries_file_sha256": common.sha256_file(queries_path or QUERIES),
            "summary": summary, "queries": rows_out}


def compare(current: dict, baseline: dict, max_recall_drop: float, max_precision_drop: float) -> dict:
    k = current["k"]
    problems = []
    s, b = current["summary"], baseline["summary"]
    if s["constraint_violations"]:
        problems.append(f"{s['constraint_violations']} constraint violations in {s['queries_with_violations']}")
    for m, limit in ((f"recall@{k}", max_recall_drop), (f"precision@{k}", max_precision_drop)):
        if s.get(m) is not None and b.get(m) is not None and s[m] < b[m] - limit:
            problems.append(f"{m} fell from {b[m]} to {s[m]} (allowed drop {limit})")
    base = {q["id"]: q for q in baseline["queries"]}
    moved = []
    for q in current["queries"]:
        o = base.get(q["id"])
        if o and q["kind"] == "positive" and o.get("recall") is not None and q.get("recall") is not None \
                and q["recall"] != o["recall"]:
            moved.append({"id": q["id"], "recall_before": o["recall"], "recall_now": q["recall"]})
    return {"ok": not problems, "problems": problems, "baseline_dataset_version": baseline["dataset_version"],
            "dataset_version": current["dataset_version"], "per_query_recall_changes": moved}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="source_pipeline obs-golden", description=__doc__.split("\n\n")[0])
    ap.add_argument("--queries", help="golden queries YAML (default: config/golden_queries.yaml)")
    ap.add_argument("--baseline", help="compare with this earlier evaluation")
    ap.add_argument("--write-baseline", help="write this evaluation to this file")
    ap.add_argument("--check", action="store_true", help="exit 1 on any violation or a regression past the limits")
    ap.add_argument("--summary", action="store_true", help="print the summary only")
    a = ap.parse_args(argv[1:])
    res = evaluate(queries_path=Path(a.queries) if a.queries else None)
    out = res
    if a.write_baseline:
        common.write_json(Path(a.write_baseline), res)
    ok = res["summary"]["constraint_violations"] == 0 and \
        res["summary"]["negative_passed"] == res["summary"]["negative"]
    if a.baseline:
        cfg = common.load_yaml(common.CONFIG / "observability.yaml")["golden"]
        cmp = compare(res, common.read_json(Path(a.baseline)), float(cfg["max_recall_drop"]),
                      float(cfg["max_precision_drop"]))
        out = {**res, "comparison": cmp}
        ok = ok and cmp["ok"]
    if a.summary:
        out = {k: out[k] for k in ("dataset_version", "k", "summary", "comparison") if k in out}
    print(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if ok or not a.check else 1
