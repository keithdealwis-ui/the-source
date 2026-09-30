"""Compare the knowledge graph (or the test count) with EXPECTED_GRAPH.json.

    check_graph_expected.py delivered|rebuilt
    check_graph_expected.py tests <runner tail file>
"""
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPECTED = json.loads((ROOT / "EXPECTED_GRAPH.json").read_text())
GRAPH = ROOT / "data" / "graph"

mode = sys.argv[1]
if mode == "tests":
    text = Path(sys.argv[2]).read_text()
    m = re.search(r"^(\d+) passed(?:, \d+ skipped)?$", text, re.M)
    got = int(m.group(1)) if m else -1
    print(f"tests passed {got}, expected {EXPECTED['tests_passing']}")
    sys.exit(0 if got == EXPECTED["tests_passing"] else 1)

got = {str(p.relative_to(GRAPH)): hashlib.sha256(p.read_bytes()).hexdigest()
       for p in sorted(GRAPH.rglob("*")) if p.is_file()}
want = EXPECTED["graph_sha256"]
diff = sorted(k for k in set(got) | set(want) if got.get(k) != want.get(k))
report = json.loads((GRAPH / "reports" / "validation_report.json").read_text())
summary = json.loads((GRAPH / "summary.json").read_text())
print(f"{mode}: {len(got)} files, {len(diff)} differ from EXPECTED; "
      f"validation {sum(c['ok'] for c in report['checks'])}/{len(report['checks'])} ok")
for k in diff:
    print("  differs:", k)
if summary["counts"]["entities"] != EXPECTED["entities"] or summary["counts"]["edges"] != EXPECTED["edges"]:
    print("  entity/edge counts differ from EXPECTED")
    diff.append("counts")
sys.exit(0 if not diff and report["ok"] else 1)
