"""Assemble the KEI-805 verifier pack: a tree a verifier can copy and run as delivered.

Unlike the first evidence pack, this one
  - keeps the layout the pipeline expects (data/dataset, data/staging, data/live, raw/);
  - carries the pinned upstream inputs the pipeline reads, each listed in raw/INPUTS.json
    with its SHA-256 and its git blob id at the pinned commit;
  - carries EXPECTED.json, the figures and file hashes a rebuild must reproduce;
  - is proven by running verify.sh inside a clean extraction of the final tarball, with
    network access refused, before it is offered to anyone.

Writes evidence/KEI-805-verifier-v2/ and evidence/KEI-805-verifier-v2.tar.gz. Transfers nothing.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from source_pipeline import common, harvest  # noqa: E402

NAME = "KEI-805-verifier-v2"
PACK = ROOT / "evidence" / NAME
PY = str(ROOT / ".venv" / "bin" / "python")
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".verify-*", ".DS_Store")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def blob(path):
    data = Path(path).read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def sh(args, cwd, env=None, log=None):
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env)
    text = f"$ {' '.join(map(str, args))}\nexit code: {proc.returncode}\n\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    if log:
        Path(log).write_text(text)
    return proc


STAGE = sys.argv[1] if len(sys.argv) > 1 else "all"


def manifest():
    lines = [f"{sha(p)}  {p.relative_to(PACK)}" for p in sorted(PACK.rglob("*"))
             if p.is_file() and p.name != "SHA256SUMS"]
    (PACK / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    return len(lines)


def clean_env(python):
    e = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN", "THE_SOURCE_ROOT")}
    e["PYTHON"] = python
    return e


if STAGE == "cleanroom":
    # Copy the assembled pack somewhere empty, fresh environment, verify.sh, network refused.
    label, python = sys.argv[2], sys.argv[3]
    work = Path(tempfile.mkdtemp(prefix=f"kei805-cleanroom-{label}-"))
    shutil.copytree(PACK, work / NAME)
    p = subprocess.run(["bash", "verify.sh"], cwd=work / NAME, capture_output=True, text=True, env=clean_env(python))
    (PACK / "logs").mkdir(exist_ok=True)
    (PACK / "logs" / f"producer_clean_room_{label}.txt").write_text(
        f"verify.sh run by the producer in a clean copy of the pack,\n"
        f"fresh virtual environment, interpreter {Path(python).name}, THE_SOURCE_NO_NETWORK=1.\n"
        f"exit code: {p.returncode}\n\n{p.stdout}\n--- stderr ---\n{p.stderr}")
    print(p.stdout[-2500:]); print(p.stderr[-800:]); print("exit", p.returncode)
    shutil.rmtree(work, ignore_errors=True)
    sys.exit(p.returncode)

if STAGE == "finalize":
    for label in ("py314", "py312"):
        log = PACK / "logs" / f"producer_clean_room_{label}.txt"
        if not log.exists() or "VERIFY: PASS" not in log.read_text():
            sys.exit(f"clean room {label} has not passed; not finalising")
    n = manifest()
    tarball = ROOT / "evidence" / f"{NAME}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(PACK, arcname=NAME)
    print("files:", n, "| SHA256SUMS sha256:", sha(PACK / "SHA256SUMS"))
    print("tarball:", tarball, tarball.stat().st_size, "bytes, sha256", sha(tarball))
    sys.exit(0)

if STAGE == "finalcheck":
    # The decisive run: verify.sh inside an extraction of the tarball itself.
    tarball = ROOT / "evidence" / f"{NAME}.tar.gz"
    before = sha(tarball)
    final = Path(tempfile.mkdtemp(prefix="kei805-final-"))
    with tarfile.open(tarball) as tar:
        tar.extractall(final, filter="data")
    p = subprocess.run(["bash", "verify.sh"], cwd=final / NAME, capture_output=True, text=True,
                       env=clean_env(sys.argv[2]))
    (ROOT / "evidence" / f"{NAME}.final-extraction-check.txt").write_text(
        f"verify.sh inside an extraction of the final tarball (sha256 {before}),\n"
        f"interpreter {Path(sys.argv[2]).name}\nexit code: {p.returncode}\n\n{p.stdout}\n--- stderr ---\n{p.stderr}")
    print(p.stdout[-2500:]); print(p.stderr[-800:]); print("exit", p.returncode)
    print("tarball sha256 unchanged:", sha(tarball) == before, before)
    shutil.rmtree(final, ignore_errors=True)
    sys.exit(p.returncode)

# STAGE assemble
# 1. Bring the working copy to its final state, offline.
env = dict(os.environ, THE_SOURCE_NO_NETWORK="1")
for cmd in (["-m", "source_pipeline", "run", "--offline"], ["-m", "source_pipeline", "export-public"],
            ["-m", "source_pipeline", "validate"], ["scripts/acceptance_report.py"]):
    p = sh([PY, *cmd], ROOT, env)
    if p.returncode:
        sys.exit(f"{cmd} failed:\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}")

# 2. Assemble the tree.
if PACK.exists():
    shutil.rmtree(PACK)
PACK.mkdir(parents=True)
for rel in ("config", "schema", "source_pipeline", "tests", "scripts", "docs"):
    shutil.copytree(ROOT / rel, PACK / rel, ignore=IGNORE)
for rel in ("README.md", "NOTICE.md"):
    shutil.copy2(ROOT / rel, PACK / rel)
shutil.copy2(ROOT / "docs" / "ACCEPTANCE.md", PACK / "ACCEPTANCE.md")
for rel in ("requirements.txt", "verify.sh", "VERIFY.md"):
    shutil.copy2(ROOT / "verifier" / rel, PACK / rel)
os.chmod(PACK / "verify.sh", 0o755)
(PACK / "data").mkdir()
shutil.copytree(ROOT / "data" / "dataset", PACK / "data" / "dataset", ignore=IGNORE)
shutil.copytree(ROOT / "data" / "export-public", PACK / "data" / "export-public", ignore=IGNORE)
(PACK / "data" / "staging").mkdir()
for n in ("claims.jsonl", "harvest_stats.json"):
    shutil.copy2(ROOT / "data" / "staging" / n, PACK / "data" / "staging" / n)
(PACK / "data" / "live").mkdir()
shutil.copy2(ROOT / "data" / "live" / "snapshot.json", PACK / "data" / "live" / "snapshot.json")
shutil.copy2(ROOT / "data" / "cross_check_launch.json", PACK / "data" / "cross_check_launch.json")

# 3. Pinned upstream inputs: exactly the files the pipeline reads, nothing else.
inputs = {"note": "Files read by the pipeline from each upstream at its pinned commit. "
                  "git_blob_sha1 is the id the upstream repository records for the file.",
          "sources": []}
for s in sorted(common.load_sources()["sources"], key=lambda s: s["priority"]):
    src = harvest.checkout_dir(s)
    rel_dir = src.relative_to(ROOT / "raw")
    dest = PACK / "raw" / rel_dir
    wanted = [src / s["licence_file"]]
    target = src / s["parse_path"]
    wanted += sorted(p for p in target.rglob("*") if p.is_file()) if target.is_dir() else [target]
    files = []
    for path in wanted:
        rel = path.relative_to(src)
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest / rel)
        files.append({"path": str(rel), "size": path.stat().st_size, "sha256": sha(path), "git_blob_sha1": blob(path)})
    (dest / ".complete").write_text(s["pinned_commit"] + "\n")
    inputs["sources"].append({"source_id": s["source_id"], "repo": s["repo"], "pinned_commit": s["pinned_commit"],
                              "licence_spdx": s["licence_spdx"], "decision": s["decision"],
                              "directory": str(rel_dir), "files": files})
(PACK / "raw" / "INPUTS.json").write_text(json.dumps(inputs, indent=2, sort_keys=True) + "\n")

# 4. What a rebuild must reproduce.
summary = common.read_json(common.DATASET / "summary.json")
report = common.read_json(common.REPORTS / "validation_report.json")
collected = sh([PY, "-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider"], ROOT)
n_tests = sum(1 for line in collected.stdout.splitlines() if "::" in line)
ds = PACK / "data" / "dataset"
expected = {
    "note": "A rebuild from this pack, offline, must reproduce these hashes and figures exactly.",
    "dataset_sha256": {str(p.relative_to(ds)): sha(p) for p in sorted(ds.rglob("*")) if p.is_file()},
    "tests_passing": n_tests,
    "figures": {
        "as_of": summary["as_of"],
        "launch_saas_products": summary["launch"]["saas_products"],
        "launch_oss_distinct_products": summary["launch"]["oss_distinct_products"],
        "launch_oss_repositories": summary["launch"]["oss_projects"],
        "launch_relationships": summary["launch"]["relationships"],
        "launch_relationships_share_alike_only": summary["launch"]["relationships_share_alike_only"],
        "licence_exception_projects": summary["licence_exception_lane"]["oss_projects"],
        "licence_exception_relationships": summary["licence_exception_lane"]["relationships_flagged"],
        "validation_checks": len(report["checks"]),
        "validation_checks_passing": sum(1 for c in report["checks"] if c["ok"]),
        "publication_status": summary["publication_status"],
    },
    "unchanged_since_first_pack": {
        "note": "The dataset records are byte-identical to those in the first evidence pack "
                "(KEI-805.tar.gz, sha256 2f292b4e...c30ec11). Only the validation report and the "
                "manifest that hashes it changed, because the share-alike figures were relabelled.",
        "first_pack_sha256": {
            "oss_projects.jsonl": "2cd7bb41e270881f26e8930ee4978ce330608ff07936b9e3c84a7bb3410cd4c1",
            "rejections.jsonl": "0d1a8d611669024c9526082910dfd6b471179233f981fc535dfe7df38473aabe",
            "relationships.jsonl": "729ccd0ee0e00f8ee50d0577542114d759c7821ed6d7c2684ce3e0e523b05fbb",
            "saas_products.jsonl": "ddde48d5cbfedc35d5d32c2f6964bc363f88c7aab7ab93eeb3f3c61b6eb266a4",
            "sources.json": "70a2fc0c383d939838356d38dc9f4d321a620444a794e5e5a4df7224e6b9baeb",
            "summary.json": "f95614ae400065fd00ff9547dd68930141c78c21a63d3ed9edfbb9c0be0b90c1",
        },
    },
}
for name, want in expected["unchanged_since_first_pack"]["first_pack_sha256"].items():
    if expected["dataset_sha256"][name] != want:
        sys.exit(f"dataset record {name} changed since the first pack; stopping")
(PACK / "EXPECTED.json").write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")


(PACK / "logs").mkdir()
print("assembled", manifest(), "files at", PACK)
print("tests expected:", n_tests, "| figures:", expected["figures"])
