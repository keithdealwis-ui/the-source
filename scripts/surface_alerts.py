"""Turn a cycle's alerts into GitHub issues, one issue per failure fingerprint (KEI-918 S0.2).

    python -m source_pipeline obs-alerts --run RUN > alerts.json
    python scripts/surface_alerts.py alerts.json [--run-url URL] [--dry-run]

Each alert is filed under its fingerprint: the issue title ends "[fp:<fingerprint>]", so the same
fault on another day comments on the open issue instead of opening a new one, and a different
fault gets its own issue. Critical alerts carry the label cycle-failure, warnings cycle-alert.
An open issue whose fingerprint is not raised by this run, and did not occur in it, is closed
as recovered; an older issue without a fingerprint is closed when a run succeeds. Uses the gh CLI (the workflow's GH_TOKEN). --dry-run prints the gh commands.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

LABELS = {"critical": ("cycle-failure", "B60205", "Daily cycle failed"),
          "warning": ("cycle-alert", "FBCA04", "Daily cycle alert (fingerprinted)")}


def gh(args, dry, capture=False):
    if dry:
        print("gh " + " ".join(json.dumps(a) if " " in a else a for a in args))
        return "[]" if capture else ""
    p = subprocess.run(["gh", *args], capture_output=True, text=True)
    if p.returncode and not capture:
        print(p.stderr, file=sys.stderr)
    return p.stdout


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("alerts")
    ap.add_argument("--run-url", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    doc = json.load(open(a.alerts))
    run = doc["run_id"]
    for label, color, desc in LABELS.values():
        gh(["label", "create", label, "--color", color, "--description", desc], a.dry_run, capture=True)
    open_issues = []
    for label, _, _ in LABELS.values():
        out = gh(["issue", "list", "--label", label, "--state", "open", "--json", "number,title", "--limit", "200"],
                 a.dry_run, capture=True)
        open_issues += json.loads(out or "[]")
    by_fp, legacy = {}, []
    for i in open_issues:
        m = re.search(r"\[fp:([0-9a-f]{12})\]", i["title"])
        if m:
            by_fp[m.group(1)] = i["number"]
        else:
            legacy.append(i["number"])  # opened before fingerprints (the single "Daily cycle failing" issue)
    raised = set()
    for al in doc["alerts"]:
        fp = al["fingerprint"]
        raised.add(fp)
        body = (f"**{al['severity']}**: {al['title']}\n\n{al['detail']}\n\n"
                f"Run `{run}` ({a.run_url or 'no log URL'}). Rule `{al['rule']}`, fingerprint `{fp}`"
                + (f", first seen in `{al['first_seen']}`, {al['consecutive_runs']} consecutive run(s)"
                   if "first_seen" in al else "")
                + ".\n\nRecord: `runs/" + run + ".json`. Fingerprints: "
                  "`python -m source_pipeline obs-fingerprints`; rules: `config/observability.yaml`.")
        if fp in by_fp:
            gh(["issue", "comment", str(by_fp[fp]), "--body", body], a.dry_run)
        else:
            gh(["issue", "create", "--title", f"{al['title']} [fp:{fp}]", "--label", LABELS[al["severity"]][0],
                "--body", body], a.dry_run)
    still = raised | set(doc.get("run_fingerprints") or [])
    for fp, number in sorted(by_fp.items()):
        if fp not in still:
            gh(["issue", "close", str(number), "--comment",
                f"Recovered: not raised by run `{run}` ({a.run_url or 'no log URL'})."], a.dry_run)
    if doc.get("run_status") == "succeeded":
        for number in legacy:
            gh(["issue", "close", str(number), "--comment",
                f"Recovered: run `{run}` succeeded ({a.run_url or 'no log URL'})."], a.dry_run)
    critical = [al for al in doc["alerts"] if al["severity"] == "critical"]
    print(json.dumps({"run_id": run, "alerts": len(doc["alerts"]), "critical": len(critical)}))
    return 1 if critical else 0


if __name__ == "__main__":
    raise SystemExit(main())
