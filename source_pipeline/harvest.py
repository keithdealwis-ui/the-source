"""Stage 1 — harvest.

Fetches each allowed upstream at its pinned commit, confirms the licence file is the
one the recorded decision was made about, and writes raw claims to staging. A source
whose decision is `refuse`, or whose licence file no longer matches, is not parsed.
"""
from __future__ import annotations

import io
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from . import common
from .parsers import PARSERS

ALLOWED_DECISIONS = {"allow", "allow_with_conditions"}


class LicenceDrift(Exception):
    pass


def _token() -> str | None:
    import os

    if os.environ.get("THE_SOURCE_NO_NETWORK"):
        return None
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    try:
        return subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=True
        ).stdout.strip() or None
    except (OSError, subprocess.CalledProcessError):
        return None


def checkout_dir(source: dict) -> Path:
    return common.RAW / source["repo"].replace("/", "_") / source["pinned_commit"]


def fetch(source: dict) -> Path:
    """Materialise the pinned commit under raw/. No-op if already present."""
    dest = checkout_dir(source)
    if (dest / ".complete").exists():
        return dest
    url = f"https://api.github.com/repos/{source['repo']}/tarball/{source['pinned_commit']}"
    common.refuse_network(url)
    req = urllib.request.Request(url, headers={"User-Agent": "the-source-harvest"})
    tok = _token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        blob = resp.read()
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts[1:]
            if not parts or ".." in parts or member.issym() or member.islnk():
                continue
            member.name = str(Path(*parts))
            tar.extract(member, dest, filter="data")
    (dest / ".complete").write_text(source["pinned_commit"] + "\n")
    return dest


def verify_licence(source: dict, src: Path) -> str:
    found = common.sha256_file(src / source["licence_file"])
    if found != source["licence_file_sha256"]:
        raise LicenceDrift(
            f"{source['source_id']}: licence file hash {found} differs from the reviewed "
            f"{source['licence_file_sha256']}; the licence decision must be re-reviewed"
        )
    return found


def _harvest_intake(source: dict, claims: list, st: dict) -> None:
    """The intake ledger (KEI-807) as a claims source. Each admitted repository carries
    the SaaS relationships its own public statement evidenced; the ledger is The Source's
    own record, so no upstream licence applies. Same claim shape as every other source."""
    st["licence_file_sha256"] = verify_licence(source, common.ROOT)
    ledger = common.INTAKE / "admitted.jsonl"
    rows = common.read_jsonl(ledger) if ledger.exists() else []
    version = common.intake_ledger_version()
    with_claims = 0
    for r in rows:
        claims.append({"category": r.get("category"), "declared_licence": r.get("declared_licence"),
                       "locator": f"data/intake/admitted.jsonl#request_id={r['request_id']}",
                       "oss_name": r["oss_name"], "repo_url": r["repo_url"], "saas_names": r["saas_names"],
                       "source_commit": version, "source_id": source["source_id"]})
        with_claims += bool(r["saas_names"])
    st.update(harvested=True, pinned_commit=version, entries=len(rows), entries_with_saas_claim=with_claims,
              unparseable_files=0, relationship_claims=sum(len(r["saas_names"]) for r in rows))


def run(offline: bool = False) -> dict:
    cfg = common.load_sources()
    claims: list[dict] = []
    stats: dict[str, dict] = {}
    for source in sorted(cfg["sources"], key=lambda s: s["priority"]):
        sid = source["source_id"]
        st = stats.setdefault(sid, {"decision": source["decision"]})
        if source["decision"] not in ALLOWED_DECISIONS:
            st["harvested"] = False
            st["reason"] = "licence_decision_refuse"
            continue
        if source.get("kind") == "intake":
            _harvest_intake(source, claims, st)
            continue
        src = checkout_dir(source)
        if not (src / ".complete").exists():
            if offline:
                raise FileNotFoundError(f"{sid}: pinned checkout missing and --offline set")
            src = fetch(source)
        st["licence_file_sha256"] = verify_licence(source, src)
        st["pinned_commit"] = source["pinned_commit"]
        entries = unparseable = with_claims = 0
        for raw in PARSERS[source["parser"]](src, source["parse_path"]):
            if "_unparseable" in raw:
                unparseable += 1
                continue
            entries += 1
            if raw["saas_names"]:
                with_claims += 1
            raw["source_id"] = sid
            raw["source_commit"] = source["pinned_commit"]
            claims.append(raw)
        st.update(
            harvested=True,
            entries=entries,
            entries_with_saas_claim=with_claims,
            unparseable_files=unparseable,
            relationship_claims=sum(
                len(c["saas_names"]) for c in claims if c["source_id"] == sid
            ),
        )
    claims.sort(key=lambda c: (c["source_id"], c["locator"], c["oss_name"]))
    common.write_jsonl(common.STAGING / "claims.jsonl", claims)
    common.write_json(common.STAGING / "harvest_stats.json", stats)
    return stats
