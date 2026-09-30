"""Compare the dataset on disk, or a pytest summary, with EXPECTED.json."""
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
exp = json.loads((ROOT / "EXPECTED.json").read_text())
mode = sys.argv[1]
bad = 0
if mode in ("delivered", "rebuilt"):
    ds = ROOT / "data" / "dataset"
    have = {str(p.relative_to(ds)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(ds.rglob("*")) if p.is_file()}
    for name, want in exp["dataset_sha256"].items():
        if have.get(name) != want:
            print(f"DIFFERS {name}: expected {want}, found {have.get(name)}"); bad += 1
    for name in sorted(set(have) - set(exp["dataset_sha256"])):
        print(f"UNEXPECTED FILE {name}"); bad += 1
    summary = json.loads((ds / "summary.json").read_text())
    report = json.loads((ds / "reports" / "validation_report.json").read_text())
    got = {
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
    }
    for k, want in exp["figures"].items():
        if got.get(k) != want:
            print(f"FIGURE {k}: expected {want}, found {got.get(k)}"); bad += 1
    print(f"{mode} dataset: {len(exp['dataset_sha256'])} files and {len(exp['figures'])} figures "
          f"{'match EXPECTED.json' if not bad else 'DO NOT match'}")
elif mode == "tests":
    text = Path(sys.argv[2]).read_text()
    m = re.search(r"(\d+) passed", text)
    n = int(m.group(1)) if m else -1
    if n != exp["tests_passing"] or re.search(r"failed|error", text):
        print(f"TESTS: expected {exp['tests_passing']} passed and none failed, found: {text.strip()[-120:]}"); bad += 1
    else:
        print(f"{n} tests passed, as expected")
sys.exit(1 if bad else 0)
