"""Verify the pinned upstream inputs under raw/ against raw/INPUTS.json.

Every file is checked by SHA-256 and by git blob SHA-1. The git blob id is what the
upstream repository itself records for that file at the pinned commit, so a verifier
with network access can compare it to the host independently:

    GET https://api.github.com/repos/<repo>/git/trees/<commit>?recursive=1
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
doc = json.loads((ROOT / "raw" / "INPUTS.json").read_text())
bad = 0
seen = set()
for src in doc["sources"]:
    base = ROOT / "raw" / src["directory"]
    for f in src["files"]:
        path = base / f["path"]
        seen.add(path.resolve())
        if not path.is_file():
            print("MISSING", path); bad += 1; continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != f["sha256"]:
            print("SHA256 MISMATCH", path); bad += 1
        if hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() != f["git_blob_sha1"]:
            print("GIT BLOB MISMATCH", path); bad += 1
extra = [p for p in (ROOT / "raw").rglob("*")
         if p.is_file() and p.name not in ("INPUTS.json", ".complete") and p.resolve() not in seen]
for p in extra:
    print("UNLISTED FILE", p); bad += 1
n = sum(len(s["files"]) for s in doc["sources"])
print(f"{n} input files across {len(doc['sources'])} pinned sources: {'all match' if not bad else str(bad) + ' problems'}")
sys.exit(1 if bad else 0)
