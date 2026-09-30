"""Assemble the KEI-844 aiq-verifier-pack/1 evidence pack.

    make_graph_pack.py     build offline, lay out evidence/KEI-844-graph/,
                           write EXPECTED_GRAPH.json and VERIFY.json

Then, from the aiq-control-plane checkout:

    aiq-verifier-pack seal <this>/evidence/KEI-844-graph --out <acp>/evidence/KEI-844-graph.tar.gz
    aiq-verifier-pack run --local <acp>/evidence/KEI-844-graph.tar.gz

Transfers nothing itself.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "KEI-844-graph"
PACK = ROOT / "evidence" / NAME
PY = str(ROOT / ".venv" / "bin" / "python")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".verify-*", ".DS_Store")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


env = dict(os.environ, THE_SOURCE_NO_NETWORK="1", PYTHONDONTWRITEBYTECODE="1")
p = subprocess.run([PY, "-m", "source_pipeline", "graph-build"], cwd=ROOT, env=env, capture_output=True, text=True)
if p.returncode:
    sys.exit(f"graph-build failed:\n{p.stdout[-3000:]}\n{p.stderr[-2000:]}")
for s in ("graph_docs.py", "graph_acceptance.py"):
    subprocess.run([PY, f"scripts/{s}"], cwd=ROOT, env=env, check=True, capture_output=True)

if PACK.exists():
    shutil.rmtree(PACK)
PACK.mkdir(parents=True)
for rel in ("config", "schema", "source_pipeline", "docs"):
    shutil.copytree(ROOT / rel, PACK / rel, ignore=IGNORE)
(PACK / "tests").mkdir()
shutil.copy2(ROOT / "tests" / "test_graph.py", PACK / "tests" / "test_graph.py")
(PACK / "scripts").mkdir()
for s in ("check_graph_expected.py", "run_graph_tests.py", "graph_docs.py", "graph_acceptance.py"):
    shutil.copy2(ROOT / "scripts" / s, PACK / "scripts" / s)
for rel in ("README.md", "NOTICE.md"):
    shutil.copy2(ROOT / rel, PACK / rel)
shutil.copy2(ROOT / "docs" / "GRAPH-ACCEPTANCE.md", PACK / "ACCEPTANCE.md")
shutil.copy2(ROOT / "verifier" / "rebuild_graph.sh", PACK / "rebuild.sh")
shutil.copy2(ROOT / "verifier" / "verify_graph.sh", PACK / "verify.sh")
# Inputs, unchanged: the KEI-805 dataset and its live snapshot, the KEI-811 corpus and history.
shutil.copytree(ROOT / "data" / "dataset", PACK / "data" / "dataset", ignore=IGNORE)
(PACK / "data" / "live").mkdir(parents=True)
shutil.copy2(ROOT / "data" / "live" / "snapshot.json", PACK / "data" / "live" / "snapshot.json")
shutil.copytree(ROOT / "data" / "discover" / "corpus", PACK / "data" / "discover" / "corpus")
shutil.copytree(ROOT / "data" / "discover" / "history", PACK / "data" / "discover" / "history")
shutil.copytree(ROOT / "data" / "graph", PACK / "data" / "graph")
(PACK / "data" / "graph-rebuild-output.json").write_text(p.stdout)

tests = subprocess.run([PY, "scripts/run_graph_tests.py"], cwd=PACK, env=env, capture_output=True, text=True)
tail = tests.stdout.strip().splitlines()[-1]
if "failed" in tail or tests.returncode:
    sys.exit(tests.stdout[-3000:])
n_tests = int(tail.split()[0])
summary = json.loads((PACK / "data" / "graph" / "summary.json").read_text())
graph = PACK / "data" / "graph"
expected = {
    "note": "An offline rebuild from this pack must reproduce these hashes and counts exactly.",
    "graph_sha256": {str(q.relative_to(graph)): sha(q) for q in sorted(graph.rglob("*")) if q.is_file()},
    "tests_passing": n_tests,
    "entities": summary["counts"]["entities"],
    "edges": summary["counts"]["edges"],
}
(PACK / "EXPECTED_GRAPH.json").write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")

verify = {
    "schema": "aiq-verifier-pack/1",
    "ticket": "KEI-844",
    "pack": NAME,
    "producer": "claude-code",
    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "layout": ["config", "schema", "source_pipeline", "scripts", "tests", "docs", "data/dataset", "data/live",
               "data/discover/corpus", "data/discover/history", "data/graph", "data/graph/reports"],
    "pinned_inputs": {"paths": ["data/discover/corpus/MANIFEST.json", "data/discover/corpus/projects.jsonl",
                                "data/discover/corpus/relationship_candidates.jsonl",
                                "data/discover/history/observations.jsonl", "data/dataset/MANIFEST.json",
                                "data/dataset/relationships.jsonl", "data/dataset/saas_products.jsonl",
                                "data/live/snapshot.json", "config/graph_ontology.yaml",
                                "config/graph_queries.yaml"]},
    "environment": {"runtimes": ["python3>=3.10", "sh", "bash"], "network": "none",
                    "env": {"PYTHONDONTWRITEBYTECODE": "1", "THE_SOURCE_NO_NETWORK": "1"}},
    "rebuild": {"argv": ["sh", "rebuild.sh"], "timeout_s": 1800},
    "entrypoint": {"argv": ["bash", "verify.sh"], "timeout_s": 3600},
    "expected_outputs": [{"path": f"data/graph/{n}", "sha256": expected["graph_sha256"][n]}
                         for n in ("MANIFEST.json", "entities.jsonl", "edges.jsonl", "observations.jsonl",
                                   "identity.jsonl", "retrieval/documents.jsonl", "views/scoring_inputs.jsonl",
                                   "reports/validation_report.json", "reports/query_results.json", "summary.json")],
    "acceptance_map": "see ACCEPTANCE.md",
}
(PACK / "VERIFY.json").write_text(json.dumps(verify, indent=2) + "\n")
print("assembled", PACK, "| tests:", n_tests, "| entities:", summary["counts"]["entities"])
