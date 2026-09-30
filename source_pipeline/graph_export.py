"""Knowledge graph (KEI-844) — storage adapters.

The canonical graph is entities.jsonl + edges.jsonl + observations.jsonl. These adapters
write it in the shapes other stores load, to show the canonical form does not depend on
any one of them. Nothing here is read back by the pipeline.

  property_graph_csv   nodes.csv + relationships.csv in the header convention Neo4j's
                       bulk importer and most property-graph loaders accept
                       (:ID / :LABEL / :START_ID / :END_ID / :TYPE); nested values are JSON.
"""
from __future__ import annotations

import csv
import json


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return v


def property_graph_csv(entities, edges, out_dir) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "nodes.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["id:ID", ":LABEL", "name", "aliases", "attrs", "evidence"])
        for e in entities:
            w.writerow([e["id"], e["type"], e["name"], _cell(e["aliases"]), _cell(e["attrs"]), _cell(e["evidence"])])
    with open(out_dir / "relationships.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["id", ":START_ID", ":END_ID", ":TYPE", "assertion", "confidence:float", "attrs", "evidence"])
        for e in edges:
            w.writerow([e["id"], e["from"], e["to"], e["relation"], e["assertion"], e["confidence"],
                        _cell(e["attrs"]), _cell(e["evidence"])])
    return {"nodes": len(entities), "relationships": len(edges)}


def read_property_graph_csv(out_dir):
    """Inverse of property_graph_csv, for the round-trip check."""
    ents, edges = [], []
    with open(out_dir / "nodes.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            ents.append({"id": r["id:ID"], "type": r[":LABEL"], "name": r["name"],
                         "aliases": json.loads(r["aliases"]), "attrs": json.loads(r["attrs"]),
                         "evidence": json.loads(r["evidence"])})
    with open(out_dir / "relationships.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            edges.append({"id": r["id"], "from": r[":START_ID"], "to": r[":END_ID"], "relation": r[":TYPE"],
                          "assertion": r["assertion"], "confidence": float(r["confidence:float"]),
                          "attrs": json.loads(r["attrs"]), "evidence": json.loads(r["evidence"])})
    return ents, edges
