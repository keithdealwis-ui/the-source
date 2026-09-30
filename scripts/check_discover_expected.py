"""Compare the Discover corpus (or the test count) with EXPECTED_DISCOVER.json.

    check_discover_expected.py delivered|rebuilt
    check_discover_expected.py tests <pytest tail file>
"""
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = json.loads((ROOT / "EXPECTED_DISCOVER.json").read_text())
CORPUS = ROOT / "data" / "discover" / "corpus"

mode = sys.argv[1]
if mode == "tests":
    text = Path(sys.argv[2]).read_text()
    m = re.search(r"^(\d+) passed$", text, re.M)
    got = int(m.group(1)) if m else -1
    print(f"tests passed {got}, expected {EXPECTED['tests_passing']}")
    sys.exit(0 if got == EXPECTED["tests_passing"] else 1)

got = {str(p.relative_to(CORPUS)): hashlib.sha256(p.read_bytes()).hexdigest()
       for p in sorted(CORPUS.rglob("*")) if p.is_file()}
want = EXPECTED["corpus_sha256"]
diff = sorted(k for k in set(got) | set(want) if got.get(k) != want.get(k))
report = json.loads((CORPUS / "reports" / "validation_report.json").read_text())
summary = json.loads((CORPUS / "summary.json").read_text())
print(f"{mode}: {len(got)} files, {len(diff)} differ from EXPECTED; "
      f"validation {sum(c['ok'] for c in report['checks'])}/{len(report['checks'])} ok")
for k in diff:
    print("  differs:", k)
for k, v in EXPECTED["figures"].items():
    have = summary["counts"].get(k, summary.get(k))
    if have != v:
        print(f"  figure {k}: {have} != expected {v}")
        diff.append(k)
sys.exit(0 if not diff and report["ok"] else 1)
