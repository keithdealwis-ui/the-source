"""Assemble the KEI-806 aiq-verifier-pack/1 evidence pack.

    make_score_pack.py     build offline, lay out evidence/KEI-806-score/,
                           write EXPECTED_SCORE.json and VERIFY.json

Then, from the aiq-control-plane checkout:

    aiq-verifier-pack seal <this>/evidence/KEI-806-score --out <acp>/evidence/KEI-806-score.tar.gz
    aiq-verifier-pack run --local <acp>/evidence/KEI-806-score.tar.gz

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
NAME = "KEI-806-score"
PACK = ROOT / "evidence" / NAME
PY = str(ROOT / ".venv" / "bin" / "python")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".verify-*", ".DS_Store")
SCORED = ["project_health.jsonl", "replacement_fit.jsonl", "records.jsonl", "signals.jsonl", "pair_signals.jsonl",
          "signals_meta.json", "summary.json", "reports/validation_report.json", "reports/calibration.json",
          "history/scores.jsonl"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


env = dict(os.environ, THE_SOURCE_NO_NETWORK="1", PYTHONDONTWRITEBYTECODE="1")
p = subprocess.run([PY, "-m", "source_pipeline", "score-build"], cwd=ROOT, env=env, capture_output=True, text=True)
if p.returncode:
    sys.exit(f"score-build failed:\n{p.stdout[-3000:]}\n{p.stderr[-2000:]}")
subprocess.run([PY, "scripts/score_acceptance.py"], cwd=ROOT, env=env, check=True, capture_output=True)

if PACK.exists():
    shutil.rmtree(PACK)
PACK.mkdir(parents=True)
for rel in ("config", "schema", "source_pipeline", "docs"):
    shutil.copytree(ROOT / rel, PACK / rel, ignore=IGNORE)
(PACK / "tests").mkdir()
shutil.copy2(ROOT / "tests" / "test_score.py", PACK / "tests" / "test_score.py")
(PACK / "scripts").mkdir()
for s in ("check_score_expected.py", "run_score_tests.py", "score_acceptance.py", "score_refresh_probe.py"):
    shutil.copy2(ROOT / "scripts" / s, PACK / "scripts" / s)
for rel in ("README.md", "NOTICE.md"):
    shutil.copy2(ROOT / rel, PACK / rel)
shutil.copy2(ROOT / "docs" / "SCORING-ACCEPTANCE.md", PACK / "ACCEPTANCE.md")
shutil.copy2(ROOT / "verifier" / "rebuild_score.sh", PACK / "rebuild.sh")
shutil.copy2(ROOT / "verifier" / "verify_score.sh", PACK / "verify.sh")
(PACK / "evidence").mkdir()
shutil.copy2(ROOT / "evidence" / "KEI-806-refresh-probe.json", PACK / "evidence" / "KEI-806-refresh-probe.json")
# Inputs, unchanged: the KEI-844 scoring view and edges, the KEI-805 catalogue/projects,
# the KEI-811 deep observations, and this ticket's raw evidence and history.
for rel in ("data/graph/views/scoring_inputs.jsonl", "data/graph/edges.jsonl", "data/graph/MANIFEST.json",
            "data/dataset/saas_products.jsonl", "data/dataset/oss_projects.jsonl", "data/discover/live.json"):
    (PACK / rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / rel, PACK / rel)
shutil.copytree(ROOT / "data" / "score", PACK / "data" / "score", ignore=IGNORE)
(PACK / "data" / "score-rebuild-output.json").write_text(p.stdout)

tests = subprocess.run([PY, "scripts/run_score_tests.py"], cwd=PACK, env=env, capture_output=True, text=True)
tail = tests.stdout.strip().splitlines()[-1]
if "failed" in tail or tests.returncode:
    sys.exit(tests.stdout[-3000:])
n_tests = int(tail.split()[0])
summary = json.loads((PACK / "data" / "score" / "summary.json").read_text())
score = PACK / "data" / "score"
expected = {
    "note": "An offline rebuild from this pack must reproduce these hashes and counts exactly.",
    "score_sha256": {n: sha(score / n) for n in SCORED},
    "tests_passing": n_tests,
    "counts": {k: summary[k] for k in ("projects_scored", "pairs_scored", "credible_alternatives",
                                       "mappings_publishable", "recommendation_eligible")},
}
(PACK / "EXPECTED_SCORE.json").write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")

verify = {
    "schema": "aiq-verifier-pack/1",
    "ticket": "KEI-806",
    "pack": NAME,
    "producer": "claude-code",
    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "layout": ["config", "schema", "source_pipeline", "scripts", "tests", "docs", "evidence", "data/graph",
               "data/dataset", "data/discover", "data/score", "data/score/evidence", "data/score/history",
               "data/score/reports"],
    "pinned_inputs": {"paths": ["data/graph/views/scoring_inputs.jsonl", "data/graph/edges.jsonl",
                                "data/dataset/saas_products.jsonl", "data/dataset/oss_projects.jsonl",
                                "data/discover/live.json", "data/score/evidence/github_meta.json",
                                "data/score/evidence/readmes.json", "data/score/history/observations.jsonl",
                                "config/scoring.yaml", "config/saas_jobs.yaml", "config/score_calibration.yaml"]},
    "environment": {"runtimes": ["python3>=3.10", "sh", "bash"], "network": "none",
                    "env": {"PYTHONDONTWRITEBYTECODE": "1", "THE_SOURCE_NO_NETWORK": "1"}},
    "rebuild": {"argv": ["sh", "rebuild.sh"], "timeout_s": 1800},
    "entrypoint": {"argv": ["bash", "verify.sh"], "timeout_s": 3600},
    "expected_outputs": [{"path": f"data/score/{n}", "sha256": expected["score_sha256"][n]} for n in SCORED],
    "acceptance_map": "see ACCEPTANCE.md",
}
(PACK / "VERIFY.json").write_text(json.dumps(verify, indent=2) + "\n")
print("assembled", PACK, "| tests:", n_tests, "| projects:", summary["projects_scored"], "| pairs:", summary["pairs_scored"])
