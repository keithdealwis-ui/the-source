"""Hybrid search, canonical-only versus the full corpus (KEI-912 acceptance criterion 5).

    python3 -S scripts/corpus_search_compare.py [--root DIR] [--top N]

For each representative query, the top project hits from the canonical layer alone (KEI-851 behaviour) and from
the full corpus (canonical + discovery, with host descriptions and topics), each with its tier and its lexical /
semantic / graph score components. Standard library only; deterministic.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
QUERIES = ["MCP server for web scraping", "self-hosted analytics", "vector database for embeddings",
           "browser automation for AI agents", "terminal file manager", "local LLM inference"]


def main(argv=None) -> int:
    from source_pipeline import mcp_server as M

    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--top", type=int, default=5)
    a = ap.parse_args(argv)
    layer = M.Layer(Path(a.root))
    if layer.full is None:
        print(json.dumps({"error": f"corpus unavailable: {layer.corpus_state['reason']}"}))
        return 1
    out = []
    for q in QUERIES:
        row = {"query": q}
        for label, index in (("canonical_only", layer), ("full_corpus", layer.full)):
            hits, _ = index.hybrid(q, "project")
            row[label] = {"hits": len(hits), "top": [
                {"id": h["key"], "tier": layer.tier_of(h["key"]), "score": h["score"], "components": h["components"],
                 "description": ((layer.corpus.get(h["key"]) or layer.canonical_corpus.get(h["key"]) or {})
                                 .get("description") or "")[:90]}
                for h in hits[:a.top]]}
        row["top_score"] = {k: (row[k]["top"][0]["score"] if row[k]["top"] else 0.0)
                            for k in ("canonical_only", "full_corpus")}
        out.append(row)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
