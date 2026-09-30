"""Knowledge graph (KEI-844) — run the representative query suite (config/graph_queries.yaml).

For every question: execute its plan, check its expectations, and resolve every evidence
ref in every returned result back to the canonical source record it names. A result whose
evidence does not resolve is a failure, not a warning.
"""
from __future__ import annotations

from . import common
from .graph_build import GRAPH, resolve_ref
from .graph_retrieval import Executor, Graph, RetrievalIndex

QUERIES = common.CONFIG / "graph_queries.yaml"


def refs_in(result: dict) -> list[str]:
    out = []
    ev = result["evidence"]
    for e in ev["edges"]:
        out.append(f"graph:edges.jsonl#{e['edge']}")
        out.extend(s["ref"] for s in e["sources"])
    out.extend(f["ref"] for f in ev["facets"])
    out.extend(s["ref"] for s in ev["semantic"])
    if "record" in result:
        out.append(result["record"])
    return out


def check(q: dict, res: dict) -> list[str]:
    exp, problems = q.get("expect", {}), []
    if res["total"] < exp.get("min_results", 0):
        problems.append(f"{res['total']} results < min {exp['min_results']}")
    if "max_results" in exp and res["total"] > exp["max_results"]:
        problems.append(f"{res['total']} results > max {exp['max_results']}")
    for r in res["results"]:
        rels = {e["relation"] for e in r["evidence"]["edges"]}
        if exp.get("every_result_type") and r["type"] != exp["every_result_type"]:
            problems.append(f"{r['id']} is {r['type']}")
        for need in exp.get("every_result_has_relation", []):
            if need not in rels:
                problems.append(f"{r['id']} lacks {need} evidence")
        for banned in exp.get("no_result_has_relation", []):
            if banned in rels:
                problems.append(f"{r['id']} has {banned} evidence")
        if not (r["evidence"]["edges"] or r["evidence"]["facets"] or r["evidence"]["semantic"] or "record" in r):
            problems.append(f"{r['id']} has no evidence")
    return problems


def run(write: bool = True) -> dict:
    spec = common.load_yaml(QUERIES)
    g = Graph.load()
    ex = Executor(g, RetrievalIndex.load(GRAPH / "retrieval"))
    report = []
    for q in spec["queries"]:
        res = ex.run(q["plan"])
        problems = check(q, res)
        unresolved, resolved = [], 0
        for r in res["results"]:
            for rf in refs_in(r):
                try:
                    resolve_ref(rf)
                    resolved += 1
                except (KeyError, StopIteration) as err:
                    unresolved.append(f"{rf}: {err}")
        if unresolved:
            problems.append(f"{len(unresolved)} evidence refs did not resolve")
        hops = max((len(r["evidence"]["edges"]) for r in res["results"]), default=0)
        steps = q["plan"].get("steps", [])
        chain_hops = sum(1 for s in steps if s["op"] in ("traverse", "group")) + \
            max((len(s["path"]) for s in steps if s["op"] in ("require", "exclude")), default=0)
        multi_hop = chain_hops >= 2
        report.append({"id": q["id"], "question": q["question"], "kind": q.get("kind"),
                       "limitations": q.get("limitations", []), "multi_hop": multi_hop, "chain_hops": chain_hops,
                       "ok": not problems, "problems": problems, "total": res["total"], "trace": res["trace"],
                       "evidence_refs_resolved": resolved, "unresolved": unresolved[:5],
                       "max_evidence_edges_per_result": hops, "results": res["results"]})
    out = {"queries": len(report), "ok": all(r["ok"] for r in report),
           "multi_hop": sum(r["multi_hop"] for r in report), "report": report}
    if write:
        common.write_json(GRAPH / "reports" / "query_results.json", out)
    return out
