"""Print the ranking behind config/supported_saas.yaml (KEI-807 criterion 3).

    python scripts/rank_supported_saas.py [--top 20]

Ranks catalogued SaaS products by recommended alternatives in the canonical dataset,
then by the number of upstream sources naming the product, then by saas_id.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from source_pipeline import common  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()
    rels = common.read_jsonl(common.DATA / "canonical" / "relationships.jsonl")
    saas = {s["saas_id"]: s for s in common.read_jsonl(common.DATASET / "saas_products.jsonl")}
    rec = Counter(r["saas_id"] for r in rels if r["recommended"])
    order = sorted(rec, key=lambda s: (-rec[s], -saas[s]["derived"]["source_count"], s))
    supported = {p["saas_id"] for p in common.load_yaml(common.CONFIG / "supported_saas.yaml")["products"]}
    for i, sid in enumerate(order[: args.top], 1):
        flag = "" if sid in supported else "   <- not in supported_saas.yaml"
        print(f"{i:>3}  {sid:<22} recommended={rec[sid]}  sources={saas[sid]['derived']['source_count']}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
