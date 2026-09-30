"""Compare the scores (or the test count) with EXPECTED_SCORE.json.

    check_score_expected.py delivered|rebuilt
    check_score_expected.py tests <runner tail file>
"""
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = json.loads((ROOT / "EXPECTED_SCORE.json").read_text())
SCORE = ROOT / "data" / "score"

mode = sys.argv[1]
if mode == "tests":
    text = Path(sys.argv[2]).read_text()
    m = re.search(r"^(\d+) passed(?:, \d+ skipped)?$", text, re.M)
    got = int(m.group(1)) if m else -1
    print(f"tests passed {got}, expected {EXPECTED['tests_passing']}")
    sys.exit(0 if got == EXPECTED["tests_passing"] else 1)

got = {n: hashlib.sha256((SCORE / n).read_bytes()).hexdigest() for n in EXPECTED["score_sha256"] if (SCORE / n).exists()}
want = EXPECTED["score_sha256"]
diff = sorted(k for k in want if got.get(k) != want[k])
report = json.loads((SCORE / "reports" / "validation_report.json").read_text())
summary = json.loads((SCORE / "summary.json").read_text())
print(f"{mode}: {len(want)} scored files, {len(diff)} differ from EXPECTED; "
      f"validation {sum(c['ok'] for c in report['checks'])}/{len(report['checks'])} ok")
for k in diff:
    print("  differs:", k)
for k in ("projects_scored", "pairs_scored", "credible_alternatives", "mappings_publishable", "recommendation_eligible"):
    if summary[k] != EXPECTED["counts"][k]:
        print(f"  count {k}: {summary[k]} != {EXPECTED['counts'][k]}")
        diff.append(k)
sys.exit(0 if not diff and report["ok"] else 1)
