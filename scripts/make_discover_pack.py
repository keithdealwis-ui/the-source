"""Assemble the KEI-811 aiq-verifier-pack/1 evidence pack.

    make_discover_pack.py            build offline, lay out evidence/KEI-811-discover/,
                                     write EXPECTED_DISCOVER.json and VERIFY.json

Then, from the aiq-control-plane checkout (the stage route only accepts tarballs under
an aiq-control-plane*/evidence/ directory):

    aiq-verifier-pack seal <this>/evidence/KEI-811-discover --out <acp>/evidence/KEI-811-discover.tar.gz
    aiq-verifier-pack run --local <acp>/evidence/KEI-811-discover.tar.gz
    sudo /usr/local/aiq/bin/aiq-stage-artifact stage-evidence --ticket KEI-811 ...

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
NAME = "KEI-811-discover"
PACK = ROOT / "evidence" / NAME
PY = str(ROOT / ".venv" / "bin" / "python")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".verify-*", ".DS_Store")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


env = dict(os.environ, THE_SOURCE_NO_NETWORK="1", PYTHONDONTWRITEBYTECODE="1")
p = subprocess.run([PY, "-m", "source_pipeline", "discover-build"], cwd=ROOT, env=env, capture_output=True, text=True)
if p.returncode:
    sys.exit(f"discover-build failed:\n{p.stdout[-3000:]}\n{p.stderr[-2000:]}")

if PACK.exists():
    shutil.rmtree(PACK)
PACK.mkdir(parents=True)
for rel in ("config", "schema", "source_pipeline", "docs"):
    shutil.copytree(ROOT / rel, PACK / rel, ignore=IGNORE)
(PACK / "tests").mkdir()
shutil.copy2(ROOT / "tests" / "test_discover.py", PACK / "tests" / "test_discover.py")
(PACK / "scripts").mkdir()
for s in ("check_discover_expected.py", "run_discover_tests.py"):
    shutil.copy2(ROOT / "scripts" / s, PACK / "scripts" / s)
for rel in ("README.md", "NOTICE.md"):
    shutil.copy2(ROOT / rel, PACK / rel)
shutil.copy2(ROOT / "docs" / "DISCOVER-ACCEPTANCE.md", PACK / "ACCEPTANCE.md")
shutil.copy2(ROOT / "verifier" / "rebuild_discover.sh", PACK / "rebuild.sh")
shutil.copy2(ROOT / "verifier" / "verify_discover.sh", PACK / "verify.sh")
# The Replace lane is read from the KEI-805 dataset, which the pack carries unchanged.
shutil.copytree(ROOT / "data" / "dataset", PACK / "data" / "dataset", ignore=IGNORE)
d = PACK / "data" / "discover"
d.mkdir(parents=True)
for rel in ("frontier.json", "live.json", "kei805_baseline.sha256"):
    shutil.copy2(ROOT / "data" / "discover" / rel, d / rel)
shutil.copytree(ROOT / "data" / "discover" / "history", d / "history")
shutil.copytree(ROOT / "data" / "discover" / "corpus", d / "corpus")
(d / "rebuild-output.json").write_text(p.stdout)
logs = ROOT / "evidence" / "KEI-811" / "logs"
if logs.exists():
    shutil.copytree(logs, PACK / "logs", ignore=IGNORE)

tests = subprocess.run([PY, "scripts/run_discover_tests.py"], cwd=ROOT, env=env, capture_output=True, text=True)
n_tests = int(tests.stdout.strip().splitlines()[-1].split()[0])
if "failed" in tests.stdout:
    sys.exit(tests.stdout[-3000:])
summary = json.loads((d / "corpus" / "summary.json").read_text())
c = summary["counts"]
corpus = d / "corpus"
expected = {
    "note": "An offline rebuild from this pack must reproduce these hashes and figures exactly.",
    "corpus_sha256": {str(p.relative_to(corpus)): sha(p) for p in sorted(corpus.rglob("*")) if p.is_file()},
    "tests_passing": n_tests,
    "figures": {k: c[k] for k in ("raw_candidate_addresses", "raw_unique_repositories", "retained",
                                  "recommendation_eligible", "rejected_repositories", "keith_seed_rows")},
}
(PACK / "EXPECTED_DISCOVER.json").write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")

verify = {
    "schema": "aiq-verifier-pack/1",
    "ticket": "KEI-811",
    "pack": NAME,
    "producer": "claude-code",
    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "layout": ["config", "schema", "source_pipeline", "scripts", "tests", "data/dataset",
               "data/discover", "data/discover/history", "data/discover/corpus", "data/discover/corpus/reports"],
    "pinned_inputs": {"paths": ["data/discover/frontier.json", "data/discover/live.json",
                                "data/discover/history/observations.jsonl", "data/dataset/oss_projects.jsonl",
                                "data/dataset/relationships.jsonl", "config/discover.yaml",
                                "config/discover_seeds.md", "config/policy.yaml"]},
    "environment": {"runtimes": ["python3>=3.10", "sh", "bash"], "network": "none",
                    "env": {"PYTHONDONTWRITEBYTECODE": "1", "THE_SOURCE_NO_NETWORK": "1"}},
    "rebuild": {"argv": ["sh", "rebuild.sh"], "timeout_s": 1800},
    "entrypoint": {"argv": ["bash", "verify.sh"], "timeout_s": 3600},
    "expected_outputs": [{"path": f"data/discover/corpus/{n}", "sha256": expected["corpus_sha256"][n]}
                         for n in ("MANIFEST.json", "projects.jsonl", "rejections.jsonl",
                                   "relationship_candidates.jsonl", "summary.json", "seed_coverage.json")],
    "acceptance_map": "see ACCEPTANCE.md",
}
(PACK / "VERIFY.json").write_text(json.dumps(verify, indent=2) + "\n")
print("assembled", PACK, "| tests:", n_tests, "| figures:", expected["figures"])
