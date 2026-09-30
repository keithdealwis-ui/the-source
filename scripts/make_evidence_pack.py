"""Assemble the KEI-805 producer evidence pack locally, with an integrity manifest.

Writes evidence/KEI-805/ and evidence/KEI-805.tar.gz. Transfers nothing anywhere.
Upstream checkouts (raw/) are third-party material and are not included; they are
reproducible from the pinned commits in config/sources.yaml.
"""
import hashlib
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "evidence" / "KEI-805"
PY = str(ROOT / ".venv" / "bin" / "python")


def run(args, log):
    proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    (PACK / "logs").mkdir(parents=True, exist_ok=True)
    (PACK / "logs" / log).write_text(
        f"$ {' '.join(args)}\nexit code: {proc.returncode}\n\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )
    return proc


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


if PACK.exists():
    shutil.rmtree(PACK)
PACK.mkdir(parents=True)

# 1. Repeatability: two offline runs, manifests compared.
first = run([PY, "-m", "source_pipeline", "run", "--offline"], "offline_run_1.txt")
m1 = sha(ROOT / "data/dataset/MANIFEST.json")
second = run([PY, "-m", "source_pipeline", "run", "--offline"], "offline_run_2.txt")
m2 = sha(ROOT / "data/dataset/MANIFEST.json")
(PACK / "logs" / "determinism.txt").write_text(
    "Two consecutive `python -m source_pipeline run --offline` invocations.\n"
    f"run 1: exit {first.returncode}, sha256(MANIFEST.json) = {m1}\n"
    f"run 2: exit {second.returncode}, sha256(MANIFEST.json) = {m2}\n"
    f"stdout identical: {first.stdout == second.stdout}\n"
    f"result: {'IDENTICAL' if m1 == m2 and first.stdout == second.stdout else 'DIFFERENT'}\n"
)

# 2. Tests.
tests = run([PY, "-m", "pytest", "tests", "-v", "-p", "no:cacheprovider"], "pytest.txt")

# 3. Local export guard dry run (publishes nothing).
export = run([PY, "-m", "source_pipeline", "export-public"], "export_guard_dry_run.txt")

# 4. Acceptance mapping rendered from the dataset.
run([PY, "scripts/acceptance_report.py"], "acceptance_report.txt")

# 5. Copy artefacts.
for rel in ("config", "schema", "source_pipeline", "tests", "scripts", "docs", "data/dataset"):
    dest = PACK / ("dataset" if rel == "data/dataset" else rel)
    shutil.copytree(ROOT / rel, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
for rel in ("README.md", "NOTICE.md"):
    shutil.copy2(ROOT / rel, PACK / rel)
shutil.copy2(ROOT / "docs" / "ACCEPTANCE.md", PACK / "ACCEPTANCE.md")
(PACK / "live").mkdir()
shutil.copy2(ROOT / "data/live/snapshot.json", PACK / "live" / "snapshot.json")
(PACK / "staging").mkdir()
for name in ("claims.jsonl", "harvest_stats.json"):
    shutil.copy2(ROOT / "data/staging" / name, PACK / "staging" / name)
shutil.copytree(ROOT / "data/export-public", PACK / "export-guard-dry-run")
cross = ROOT / "data" / "cross_check_launch.json"
if cross.exists():
    shutil.copy2(cross, PACK / "logs" / "cross_check_launch_rest_api.json")

# 6. Integrity manifest.
lines = []
for path in sorted(PACK.rglob("*")):
    if path.is_file() and path.name != "SHA256SUMS":
        lines.append(f"{sha(path)}  {path.relative_to(PACK)}")
(PACK / "SHA256SUMS").write_text("\n".join(lines) + "\n")

tarball = ROOT / "evidence" / "KEI-805.tar.gz"
with tarfile.open(tarball, "w:gz") as tar:
    tar.add(PACK, arcname="KEI-805")

print("pack:", PACK)
print("files:", len(lines))
print("SHA256SUMS sha256:", sha(PACK / "SHA256SUMS"))
print("tarball:", tarball, tarball.stat().st_size, "bytes, sha256", sha(tarball))
print("determinism:", "IDENTICAL" if m1 == m2 else "DIFFERENT")
print("pytest exit:", tests.returncode, "| export exit:", export.returncode,
      "| offline run exits:", first.returncode, second.returncode)
sys.exit(0 if (m1 == m2 and tests.returncode == 0 and first.returncode == 0) else 1)
