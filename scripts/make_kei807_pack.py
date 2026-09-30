"""Assemble the KEI-807 aiq-verifier-pack/1 evidence pack.

    python3 scripts/make_kei807_pack.py      -> evidence/KEI-807-canonical/

Contents: a git bundle of the canonical repository (full history, cycle tags), GitHub
API captures (repository visibility, workflow runs, issues) that the verifier cannot
read itself, and a verify route that rebuilds offline and checks every criterion
independently (scripts/verify_kei807.py imports nothing from the producer's pipeline).

Then, from the aiq-control-plane checkout:
    aiq-verifier-pack seal <the-source>/evidence/KEI-807-canonical --out <acp>/evidence/KEI-807-canonical.tar.gz
Transfers nothing itself.
"""
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "KEI-807-canonical"
PACK = ROOT / "evidence" / NAME
REPO = "keithdealwis-ui/the-source"


def gh(*args):
    return json.loads(subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout)


def git(*args):
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


subprocess.run(["git", "-C", str(ROOT), "fetch", "-q", "--tags", "origin"], check=True)
status = git("status", "--porcelain", "--untracked-files=no")
if status:
    raise SystemExit(f"working tree has uncommitted changes:\n{status}")
head = git("rev-parse", "HEAD")
if head != git("rev-parse", "origin/main"):
    raise SystemExit("HEAD is not origin/main; push first")

if PACK.exists():
    shutil.rmtree(PACK)
(PACK / "github-evidence").mkdir(parents=True)
subprocess.run(["git", "-C", str(ROOT), "bundle", "create", str(PACK / "repo.bundle"), "--all"], check=True,
               capture_output=True)

E = PACK / "github-evidence"
captures = {
    "repo.json": ["repo", "view", REPO, "--json", "nameWithOwner,visibility,defaultBranchRef,createdAt,url"],
    "ci_runs.json": ["run", "list", "-R", REPO, "--workflow", "ci.yml", "-L", "100",
                     "--json", "databaseId,headSha,conclusion,status,event,createdAt,url"],
    "cycle_runs.json": ["run", "list", "-R", REPO, "--workflow", "weekly-cycle.yml", "-L", "200",
                        "--json", "databaseId,headSha,conclusion,status,event,createdAt,url,displayTitle"],
    "intake_runs.json": ["run", "list", "-R", REPO, "--workflow", "intake.yml", "-L", "100",
                         "--json", "databaseId,headSha,conclusion,status,event,createdAt,url,displayTitle"],
    "issues.json": ["issue", "list", "-R", REPO, "--state", "all", "-L", "100",
                    "--json", "number,title,state,labels,createdAt,closedAt,url"],
}
for fname, argv in captures.items():
    (E / fname).write_text(json.dumps(gh(*argv), indent=2, sort_keys=True) + "\n")
(E / "CAPTURED.md").write_text(
    "GitHub API captures taken by the producer (claude-code) with `gh` at pack time, because the\n"
    "verifier has no credential for the private repository. They corroborate, and are\n"
    "cross-checked against, the first-hand evidence in repo.bundle: bot-authored cycle and\n"
    "intake commits, cycle/* tags and run records.\n")

(PACK / "rebuild.sh").write_text("""#!/bin/sh
# Materialise the repository from the bundle (no network).
set -eu
cd "$(dirname "$0")"
rm -rf repo
git clone -q repo.bundle repo
git -C repo checkout -q "$(cat PACK_COMMIT)"
git -C repo remote set-url origin https://github.com/keithdealwis-ui/the-source
echo "repo at $(git -C repo rev-parse --short HEAD)"
""")
(PACK / "verify.sh").write_text("""#!/bin/bash
# KEI-807 verifier route. rebuild.sh has cloned the bundle into ./repo.
# Needs python3 >= 3.10 with pyyaml and jsonschema, and git; no pytest, no network.
set -uo pipefail
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
export THE_SOURCE_NO_NETWORK=1 PYTHONDONTWRITEBYTECODE=1
unset GITHUB_TOKEN GH_TOKEN
fail=0
say() { printf '\\n== %s\\n' "$*"; }
bad() { echo "FAIL: $*"; fail=1; }
hash256() { if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }

say "1. environment"
"$PY" -c 'import sys, yaml, jsonschema; assert sys.version_info >= (3,10); print("python", sys.version.split()[0], "| pyyaml", yaml.__version__)' || bad "python/deps"

say "2. the clone is the delivered commit"
[ "$(git -C repo rev-parse HEAD)" = "$(cat PACK_COMMIT)" ] && echo "HEAD $(cat PACK_COMMIT)" || bad "HEAD mismatch"
echo "cycle tags: $(git -C repo tag --list 'cycle/*' | wc -l | tr -d ' ')"

say "3. offline rebuild of the dataset reproduces the committed data exactly (AC2, AC6)"
(cd repo && "$PY" -m source_pipeline build > ../.verify-build.json 2> ../.verify-build.err) || { bad "build"; tail -5 .verify-build.err; }
git -C repo status --porcelain -- data api | head -5
git -C repo diff --quiet -- data api && echo "committed data == rebuild" || bad "rebuild differs from committed data"

say "4. canonical validation: schema, integrity, read surface, byte-identical rebuild (AC2-AC8)"
(cd repo && "$PY" -m source_pipeline canonical-validate > ../.verify-canonical.json) || bad "canonical-validate"
"$PY" -c 'import json; r=json.load(open(".verify-canonical.json")); print("ok" if r["ok"] else "FAIL", r["checks"], "checks; failed:", r["failed"], "warnings:", r["warnings"])'

say "5. a second canonical build is byte-identical (AC6, AC12 retry safety)"
h1=$(cd repo && find api/v1 data/canonical -type f | sort | xargs cat | hash256 | cut -d' ' -f1)
(cd repo && "$PY" -m source_pipeline canonical-build > /dev/null) || bad "canonical-build"
h2=$(cd repo && find api/v1 data/canonical -type f | sort | xargs cat | hash256 | cut -d' ' -f1)
echo "before: $h1"; echo "after:  $h2"; [ "$h1" = "$h2" ] || bad "second build differs"
git -C repo diff --quiet -- data api || bad "rebuild left a diff"

say "6. tests (stdlib runner)"
(cd repo && "$PY" scripts/run_canonical_tests.py > ../.verify-tests.txt 2>&1) || bad "tests"
tail -1 .verify-tests.txt

say "7. independent acceptance checks (no producer code imported)"
"$PY" repo/scripts/verify_kei807.py repo github-evidence || bad "acceptance checks"

echo
[ $fail = 0 ] && echo "KEI-807 VERIFY: PASS" || echo "KEI-807 VERIFY: FAIL"
exit $fail
""")
(PACK / "PACK_COMMIT").write_text(head + "\n")
shutil.copy2(ROOT / "docs" / "ACCEPTANCE-KEI-807.md", PACK / "ACCEPTANCE.md")

sums = []
for p in sorted(PACK.rglob("*")):
    if p.is_file():
        sums.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(PACK)}")
(PACK / "SHA256SUMS").write_text("\n".join(sums) + "\n")
verify = {
    "schema": "aiq-verifier-pack/1",
    "ticket": "KEI-807",
    "pack": NAME,
    "producer": "claude-code",
    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "layout": ["github-evidence"],
    "pinned_inputs": {"paths": ["repo.bundle", "PACK_COMMIT", "github-evidence/repo.json", "github-evidence/ci_runs.json",
                                "github-evidence/cycle_runs.json", "github-evidence/intake_runs.json",
                                "github-evidence/issues.json"]},
    "environment": {"runtimes": ["python3>=3.10", "sh", "bash", "git"], "network": "none",
                    "env": {"PYTHONDONTWRITEBYTECODE": "1", "THE_SOURCE_NO_NETWORK": "1"}},
    "rebuild": {"argv": ["sh", "rebuild.sh"], "timeout_s": 600},
    "entrypoint": {"argv": ["bash", "verify.sh"], "timeout_s": 3600},
    "expected_outputs": [],
    "acceptance_map": "see ACCEPTANCE.md",
}
(PACK / "VERIFY.json").write_text(json.dumps(verify, indent=2) + "\n")
print("assembled", PACK, "| commit", head[:7])
