"""Fail when a source whose licence file lives in this repository (intake, job_match) no longer matches the
reviewed SHA-256 pinned in config/sources.yaml.

harvest.verify_licence raises LicenceDrift on exactly this at cycle time, failing the whole daily cycle (run
manual-20261124T184147Z-manual-a1: a docs rewrite edited docs/JOB-MATCH.md). Upstream sources are pinned to a
commit and research.py already holds them back as licence_changed_needs_review; these local files have no pin
but the hash, so the check belongs at PR time. Fix: review the diff of the licence section, then re-pin the hash.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from source_pipeline import common  # noqa: E402

LOCAL_KINDS = ("intake", "job_match")


def drift(sources):
    out = []
    for s in sources:
        if s.get("kind") not in LOCAL_KINDS:
            continue
        found = common.sha256_file(ROOT / s["licence_file"])
        if found != s["licence_file_sha256"]:
            out.append((s["source_id"], s["licence_file"], s["licence_file_sha256"], found))
    return out


def main() -> int:
    bad = drift(common.load_sources()["sources"])
    for sid, path, want, got in bad:
        print(f"LICENCE DRIFT {sid}: {path} sha256 {got} != reviewed {want} (config/sources.yaml). Review the "
              f"change, then re-pin; the daily cycle fails with LicenceDrift until then.")
    if not bad:
        print("local licence pins ok")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
