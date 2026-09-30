#!/usr/bin/env python3
"""The Source intake client (KEI-807 criteria 13-14). Standard library only.

    source_intake.py submit <repo-url> [--hint notion] [--note "..."] [--by keith] [--id REQUEST_ID]
    source_intake.py status <request-id>
    source_intake.py result <request-id>

Talks to GitHub's REST API on behalf of the caller. Authentication is the caller's own
GitHub token (GH_TOKEN or GITHUB_TOKEN, else `gh auth token`). The token needs only
"Actions: read and write" (to submit and follow) and "Contents: read" (to fetch the
outcome) on the canonical repository. It never needs, and should never have, contents
write access: the intake job is the only writer.

The same three calls are what a ChatGPT action, an AIQ employee or any other tool makes;
see docs/INTAKE.md for the raw HTTP form.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = os.environ.get("THE_SOURCE_REPO", "keithdealwis-ui/the-source")
WORKFLOW = "intake.yml"
API = "https://api.github.com"
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{6,80}$")


def _token() -> str:
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("no GitHub token: set GH_TOKEN or log in with `gh auth login`")


def _call(method: str, path: str, body: dict | None = None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "the-source-intake"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, None


def submit(url: str, hint: str | None, note: str | None, by: str | None, request_id: str | None) -> dict:
    rid = request_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
    if not REQUEST_ID.match(rid):
        sys.exit("request id must be 6-80 of [A-Za-z0-9._-]")
    inputs = {"repo_url": url, "request_id": rid, "submitted_by": by or "cli"}
    if hint:
        inputs["saas_hint"] = hint
    if note:
        inputs["note"] = note
    st, _ = _call("POST", f"/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches", {"ref": "main", "inputs": inputs})
    if st != 204:
        sys.exit(f"submit refused (HTTP {st}); check the token's Actions permission on {REPO}")
    return {"request_id": rid, "status": "submitted", "repo_url": url}


def status(rid: str) -> dict:
    st, runs = _call("GET", f"/repos/{REPO}/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=100")
    run = next((r for r in (runs or {}).get("workflow_runs", []) if r.get("display_title") == f"intake {rid}"), None)
    out = {"request_id": rid, "run": None, "outcome": None}
    if run:
        out["run"] = {"status": run["status"], "conclusion": run["conclusion"], "url": run["html_url"]}
    res = result(rid, quiet=True)
    if res:
        out["outcome"] = res["outcome"]
        out["canonical"] = res["canonical"]
    out["state"] = ("assessed" if res else "failed" if run and run["conclusion"] not in (None, "success")
                    else "processing" if run else "not_found")
    return out


def result(rid: str, quiet: bool = False) -> dict | None:
    st, doc = _call("GET", f"/repos/{REPO}/contents/data/intake/outcomes/{rid}.json?ref=main")
    if st != 200 or not doc:
        if quiet:
            return None
        sys.exit(f"no outcome recorded for {rid} yet (HTTP {st})")
    return json.loads(base64.b64decode(doc["content"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit")
    s.add_argument("url")
    s.add_argument("--hint")
    s.add_argument("--note")
    s.add_argument("--by")
    s.add_argument("--id")
    s.add_argument("--wait", action="store_true", help="poll until the outcome is recorded (up to 10 minutes)")
    for name in ("status", "result"):
        sub.add_parser(name).add_argument("request_id")
    a = ap.parse_args()
    if a.cmd == "submit":
        out = submit(a.url, a.hint, a.note, a.by, a.id)
        if a.wait:
            deadline = time.time() + 600
            while time.time() < deadline:
                time.sleep(15)
                st = status(out["request_id"])
                if st["state"] in ("assessed", "failed"):
                    out = st
                    break
    elif a.cmd == "status":
        out = status(a.request_id)
    else:
        out = result(a.request_id)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
