"""Before/after regression of the served recommendations between two read layers (launch-policy amendment).

    python scripts/launch_regression.py --before <git rev> --after <repository root> [--out report.json]

Compares data/canonical/relationships.jsonl, oss_projects.jsonl and api/v1/saas/*.json and reports every
recommended relationship as retained, added, removed or changed (Replacement Fit, rank, disclosure), each with
its reason taken from the after layer and its dataset (data/dataset/relationships.jsonl validation reasons,
the fit's refusal reasons). Read-only.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _git(rev: str, path: str, root: Path) -> str | None:
    p = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=root, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def _jsonl(text: str | None) -> list:
    return [json.loads(l) for l in (text or "").splitlines() if l.strip()]


def load(before_rev: str | None, root: Path) -> dict:
    def read(path):
        return _git(before_rev, path, root) if before_rev else ((root / path).read_text() if (root / path).exists()
                                                                 else None)
    rels = {(r["saas_id"], r["oss_id"]): r for r in _jsonl(read("data/canonical/relationships.jsonl"))}
    projects = {p["oss_id"]: p for p in _jsonl(read("data/canonical/oss_projects.jsonl"))}
    index = json.loads(read("api/v1/index.json") or "{}")
    pages = {}
    for row in index.get("supported_saas", []):
        doc = json.loads(read(f"api/v1/{row['path']}") or "{}")
        for rec in doc.get("recommendations", []):
            pages[(row["saas_id"], rec["oss_id"])] = rec
    dataset = {(e["saas_id"], e["oss_id"]): e for e in _jsonl(read("data/dataset/relationships.jsonl"))}
    return {"rels": rels, "projects": projects, "pages": pages, "dataset": dataset}


def _fit(r):
    f = r.get("replacement_fit") or {}
    return f.get("score")


def compare(before: dict, after: dict, fit_after: dict | None = None) -> dict:
    rb = {k for k, r in before["rels"].items() if r["recommended"]}
    ra = {k for k, r in after["rels"].items() if r["recommended"]}
    rows = {"retained": [], "added": [], "removed": [], "changed": []}

    def why_not(k):
        e = after["dataset"].get(k)
        r = after["rels"].get(k)
        if r is None:
            if e is None:
                return "no longer claimed by any source"
            if not e.get("launch"):
                return ("not in the launch set: " + ", ".join(e["validation"]["reasons"][:3])
                        if e["validation"]["status"] != "validated" else "not in the launch set")
            return "withheld by the export guard"
        f = (fit_after or {}).get(k) or {}
        return "not publishable: " + "; ".join(f.get("refusal_reasons") or ["Replacement Fit below the gate"])

    for k in sorted(rb | ra):
        b, a = before["rels"].get(k), after["rels"].get(k)
        if k in rb and k in ra:
            diff = {}
            if _fit(b) != _fit(a):
                diff["replacement_fit"] = [_fit(b), _fit(a)]
            pb, pa = before["pages"].get(k) or {}, after["pages"].get(k) or {}
            if pb.get("rank") != pa.get("rank"):
                diff["rank"] = [pb.get("rank"), pa.get("rank")]
            if pb.get("licence_disclosure") != pa.get("licence_disclosure"):
                diff["licence_disclosure"] = [pb.get("licence_disclosure"), pa.get("licence_disclosure")]
            rows["changed" if diff else "retained"].append({"saas_id": k[0], "oss_id": k[1], **diff})
        elif k in ra:
            rows["added"].append({"saas_id": k[0], "oss_id": k[1], "replacement_fit": _fit(a),
                                  "licence_disclosure": (after["pages"].get(k) or {}).get("licence_disclosure")})
        else:
            rows["removed"].append({"saas_id": k[0], "oss_id": k[1], "reason": why_not(k)})
    new_pairs = sorted(set(after["rels"]) - set(before["rels"]))
    held = [{"saas_id": k[0], "oss_id": k[1], "replacement_fit": _fit(after["rels"][k]),
             "reason": why_not(k) if k not in ra else "recommended"} for k in new_pairs]
    sb = {s for s, _ in before["rels"]}
    sa = {s for s, _ in after["rels"]}
    return {"summary": {k: len(v) for k, v in rows.items()} | {
                "recommended_before": len(rb), "recommended_after": len(ra),
                "relationships_before": len(before["rels"]), "relationships_after": len(after["rels"]),
                "saas_before": len(sb), "saas_after": len(sa), "saas_removed": sorted(sb - sa),
                "saas_added": sorted(sa - sb), "new_relationships_not_recommended": sum(
                    1 for h in held if h["reason"] != "recommended")},
            "recommendations": rows, "new_relationships": held}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True, help="git revision of the served layer")
    ap.add_argument("--after", required=True, help="repository root holding the rebuilt layer")
    ap.add_argument("--repo", default=".", help="repository the --before revision is read from")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    after_root = Path(a.after).resolve()
    sys.path.insert(0, str(after_root))
    from source_pipeline import canonical  # the after layer's scores, for refusal reasons

    fit, _ = canonical._load_scores()
    report = compare(load(a.before, Path(a.repo).resolve()), load(None, after_root), fit)
    text = json.dumps(report, indent=2, sort_keys=True)
    if a.out:
        Path(a.out).write_text(text + "\n")
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
