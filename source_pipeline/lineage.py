"""Reproducible dataset lineage from git history (KEI-918 S0.6, docs/OBSERVABILITY.md).

    python -m source_pipeline obs-lineage history   [--from REV] [--to REV]   one record per canonical version
    python -m source_pipeline obs-lineage verify    [--from REV] [--to REV]   re-derive every version and its chain
    python -m source_pipeline obs-lineage reconcile --from REV [--to REV]     why the SaaS catalogue changed

REV is a git revision or a dataset_version (resolved to the first commit that carries it).

Every daily cycle commits its canonical MANIFEST, the dataset it was built from and its run
record together, so the whole lineage of the published layer can be recomputed from git alone,
by anyone with a clone:

* history: for each commit that changed data/canonical/MANIFEST.json, the dataset_version, the
  version it replaced, the run that produced it, its counts and file hashes, and the inputs it was
  built from: the upstream pins, and the git tree hashes of the code (source_pipeline/) and the
  configuration (config/) at that commit.
* verify: for each of those commits, the MANIFEST's file hashes equal the committed files, the
  dataset_version is the hash of those hashes, the counts equal the rows, and the run record names
  the same dataset_version_after and, as dataset_version_before, the version before it.
* reconcile: the canonical SaaS products at REV A and REV B, and for every product that left or
  joined, the cycle in which it did and why: the launch selection is re-run on that cycle's own
  committed dataset and policy (build.select_launch) and must reproduce the committed launch ranks
  exactly; the cause is then read from the recomputation (below the launch cut-off and by how
  much, no validated relationship left, or every relationship withheld as share-alike-only).

Reads git objects only. No network, and nothing is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

from . import build, common

SCHEMA = "the-source.lineage/1"
MANIFEST = "data/canonical/MANIFEST.json"


class LineageError(RuntimeError):
    pass


def _git(*args, root: Path | None = None) -> str:
    p = subprocess.run(["git", *args], cwd=root or common.ROOT, capture_output=True, text=True)
    if p.returncode:
        raise LineageError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def _blob(rev: str, path: str, missing_ok=False):
    p = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=common.ROOT, capture_output=True)
    if p.returncode:
        if missing_ok:
            return None
        raise LineageError(f"{path} is not in {rev}")
    return p.stdout


def _json(rev, path, missing_ok=False):
    b = _blob(rev, path, missing_ok)
    return None if b is None else json.loads(b)


def _jsonl(rev, path):
    return [json.loads(line) for line in _blob(rev, path).decode().splitlines() if line.strip()]


def manifest_commits(to: str = "HEAD") -> list[str]:
    """Every commit that changed the canonical MANIFEST, oldest first, up to `to`."""
    return _git("log", "--format=%H", "--reverse", to, "--", MANIFEST).split()


def resolve(rev: str) -> str:
    """A git revision, or a dataset_version (the first commit whose MANIFEST carries it)."""
    if re.fullmatch(r"[0-9a-f]{16}", rev or ""):
        for c in manifest_commits():
            if (_json(c, MANIFEST) or {}).get("dataset_version") == rev:
                return c
    try:
        return _git("rev-parse", "--verify", f"{rev}^{{commit}}").strip()
    except LineageError:
        raise LineageError(f"{rev!r} is neither a git revision nor a dataset_version in the history") from None


def _span(frm: str | None, to: str | None) -> list[str]:
    to_c = resolve(to or "HEAD")
    commits = manifest_commits(to_c)
    if frm:
        f = resolve(frm)
        if f not in commits:  # a commit that did not touch the MANIFEST: start from the version it carries
            f = _git("log", "-1", "--format=%H", f, "--", MANIFEST).strip()
        commits = commits[commits.index(f):]
    return commits


def _run_id(subject: str):
    m = re.match(r"cycle: (\S+) (succeeded|failed)", subject)
    return m.group(1) if m else None


def record(commit: str, prev_version: str | None) -> dict:
    m = _json(commit, MANIFEST)
    subject, when = _git("log", "-1", "--format=%s%n%cI", commit).splitlines()[:2]
    run_id = _run_id(subject)
    run = _json(commit, f"runs/{run_id}.json", missing_ok=True) if run_id else None
    return {"commit": commit, "committed_at": when, "subject": subject, "dataset_version": m["dataset_version"],
            "replaces": prev_version, "data_as_of": m.get("data_as_of"), "run_id": run_id,
            "run": None if run is None else {"status": run.get("status"), "trigger": run.get("trigger"),
                                             **{k: (run.get("canonical") or {}).get(k) for k in (
                                                 "dataset_version_before", "dataset_version_after")}},
            "counts": m.get("counts"), "files": m.get("files"),
            "inputs": {"source_pins": {s: v.get("pinned_commit") for s, v in sorted((m.get("sources") or {}).items())},
                       "code_tree": _git("rev-parse", f"{commit}:source_pipeline").strip(),
                       "config_tree": _git("rev-parse", f"{commit}:config").strip(),
                       "dataset_manifest_sha256": hashlib.sha256(_blob(commit, "data/dataset/MANIFEST.json")).hexdigest()
                       if _blob(commit, "data/dataset/MANIFEST.json", missing_ok=True) else None}}


def history(frm=None, to=None) -> dict:
    commits = _span(frm, to)
    out, prev = [], None
    first = commits[0] if commits else None
    if first:
        parent = _git("log", "-1", "--format=%H", f"{first}^", "--", MANIFEST).strip() if \
            _git("rev-list", "--count", first).strip() != "1" else ""
        prev = (_json(parent, MANIFEST) or {}).get("dataset_version") if parent else None
    for c in commits:
        r = record(c, prev)
        if r["dataset_version"] == prev:  # a commit that rewrote the MANIFEST without changing the version
            continue
        out.append(r)
        prev = r["dataset_version"]
    return {"schema": SCHEMA, "versions": out, "count": len(out)}


def verify(frm=None, to=None) -> dict:
    """Re-derive each version from its committed files and check the chain."""
    h = history(frm, to)
    checks = []
    for r in h["versions"]:
        c, problems = r["commit"], []
        for name, sha in (r["files"] or {}).items():
            got = hashlib.sha256(_blob(c, f"data/canonical/{name}")).hexdigest()
            if got != sha:
                problems.append(f"{name}: committed file hash {got[:12]} != MANIFEST {sha[:12]}")
        derived = hashlib.sha256(common.dumps(r["files"]).encode()).hexdigest()[:16]
        if derived != r["dataset_version"]:
            problems.append(f"dataset_version {r['dataset_version']} is not the hash of the file hashes ({derived})")
        for kind, fname in (("saas_products", "saas_products.jsonl"), ("oss_projects", "oss_projects.jsonl"),
                            ("relationships", "relationships.jsonl")):
            n = len([x for x in _blob(c, f"data/canonical/{fname}").decode().splitlines() if x.strip()])
            if (r["counts"] or {}).get(kind) != n:
                problems.append(f"counts.{kind} {(r['counts'] or {}).get(kind)} != {n} rows")
        run = r["run"]
        if run is not None:
            if run["dataset_version_after"] != r["dataset_version"]:
                problems.append(f"run {r['run_id']} says dataset_version_after {run['dataset_version_after']}")
            if r["replaces"] is not None and run["dataset_version_before"] != r["replaces"]:
                problems.append(f"run {r['run_id']} says it replaced {run['dataset_version_before']}, "
                                f"the history says {r['replaces']}")
        checks.append({"commit": c[:12], "dataset_version": r["dataset_version"], "run_id": r["run_id"],
                       "produced_by_cycle": run is not None, "ok": not problems, "problems": problems})
    return {"schema": SCHEMA, "ok": all(x["ok"] for x in checks), "versions": len(checks),
            "not_from_a_cycle": [x["commit"] for x in checks if not x["produced_by_cycle"]], "checks": checks}


# ------------------------------------------------------------------ catalogue reconciliation

def _launch_state(commit: str) -> dict:
    """Re-run the launch selection on this commit's committed dataset and policy, and check it reproduces."""
    edges = _jsonl(commit, "data/dataset/relationships.jsonl")
    projects = {p["oss_id"]: p for p in _jsonl(commit, "data/dataset/oss_projects.jsonl")}
    rows = {s["saas_id"]: s for s in _jsonl(commit, "data/dataset/saas_products.jsonl")}
    policy = yaml.safe_load(_blob(commit, "config/policy.yaml"))
    saas = {sid: {"name": s["name"], "source_ids": s["imported"]["named_by_sources"]} for sid, s in rows.items()}
    sel = build.select_launch(saas, edges, projects, policy)
    committed = {sid: s["derived"]["launch_rank"] for sid, s in rows.items() if s["derived"].get("launch_rank")}
    recomputed = {sid: i for i, sid in enumerate(sel["saas_ranked"], 1)}
    valid = {}
    for e in edges:
        if e["validation"]["status"] == "validated":
            valid.setdefault(e["saas_id"], []).append(e)
    # the same ordering select_launch ranks by, continued past the cut-off so a product below it has a rank
    full = sorted(valid, key=lambda s: (-len(saas[s]["source_ids"]), -len(valid[s]), saas[s]["name"].lower()))
    if full[:len(sel["saas_ranked"])] != sel["saas_ranked"]:
        raise LineageError(f"{commit[:12]}: the continued ranking does not extend select_launch's")
    canon = {s["saas_id"] for s in _jsonl(commit, "data/canonical/saas_products.jsonl")}
    supported = {p["saas_id"] for p in (yaml.safe_load(_blob(commit, "config/supported_saas.yaml")) or {})
                 .get("products", [])}
    launch_edges = {}
    for e in edges:
        if e.get("launch"):
            launch_edges.setdefault(e["saas_id"], []).append(e)
    return {"commit": commit, "canonical": canon, "supported": supported, "rank": {s: i for i, s in enumerate(full, 1)},
            "cutoff": len(sel["saas_ranked"]), "cap_per_saas": sel["cap_per_saas"],
            "beyond_floor": sel["saas_added_beyond_floor"], "floor": sel["saas_floor"],
            "validated": {s: len(v) for s, v in valid.items()}, "launch_edges": launch_edges,
            "in_catalogue": set(rows), "names": {sid: s["name"] for sid, s in rows.items()},
            "reproduced": committed == recomputed,
            "reproduction_diff": sorted(set(committed.items()) ^ set(recomputed.items()))[:10]}


def _why(sid: str, st: dict, prev: dict, left: bool) -> dict:
    here, there = (st, prev)
    r, cut = here["rank"].get(sid), here["cutoff"]
    base = {"rank": r, "cutoff": cut, "validated_relationships": here["validated"].get(sid, 0),
            "rank_before": there["rank"].get(sid), "cutoff_before": there["cutoff"],
            "validated_relationships_before": there["validated"].get(sid, 0),
            "cap_per_saas": [there["cap_per_saas"], here["cap_per_saas"]],
            "launch_beyond_floor": [there["beyond_floor"], here["beyond_floor"]]}
    if left:
        if sid not in here["in_catalogue"]:
            return {"cause": "left_the_catalogue", **base}
        if not here["validated"].get(sid):
            return {"cause": "no_validated_relationship", **base}
        if r is not None and r > cut:
            return {"cause": "below_launch_cutoff", **base,
                    "explanation": (f"ranked {r}, cut-off {cut} (floor {here['floor']} + {here['beyond_floor']} "
                                    f"added to reach the OSS minimum); before: ranked {base['rank_before']}, cut-off "
                                    f"{there['cutoff']} (floor + {there['beyond_floor']}). The launch rule extends "
                                    "past the 100-product floor only as far as the 250-OSS minimum needs; with a "
                                    f"per-product cap of {here['cap_per_saas']} (was {there['cap_per_saas']}) fewer "
                                    "products were needed.")}
        edges = here["launch_edges"].get(sid, [])
        if edges and all(e["share_alike_only"] for e in edges):
            return {"cause": "share_alike_only_withheld", **base}
        return {"cause": "other", **base}
    if sid in here["supported"] and sid not in there["supported"]:
        return {"cause": "became_supported", **base}
    if there["validated"].get(sid, 0) == 0:
        return {"cause": "gained_validated_relationships", **base}
    if there["rank"].get(sid) is not None and there["rank"][sid] > there["cutoff"]:
        return {"cause": "above_launch_cutoff", **base}
    return {"cause": "other", **base}


def reconcile(frm: str, to: str | None = None) -> dict:
    """The canonical SaaS catalogue at frm vs to, with each change attributed to the cycle that made it."""
    commits = _span(frm, to)
    if len(commits) < 2:
        raise LineageError("need two canonical versions to reconcile")
    states, steps, problems = [], [], []
    hist = {r["commit"]: r for r in history(frm, to)["versions"]}
    commits = [c for c in commits if c in hist]
    for c in commits:
        st = _launch_state(c)
        if not st["reproduced"]:
            problems.append(f"{c[:12]}: launch selection does not reproduce: {st['reproduction_diff']}")
        states.append(st)
    for prev, st in zip(states, states[1:]):
        gone, new = sorted(prev["canonical"] - st["canonical"]), sorted(st["canonical"] - prev["canonical"])
        if not gone and not new:
            continue
        r = hist[st["commit"]]
        steps.append({"commit": st["commit"][:12], "run_id": r["run_id"], "dataset_version": r["dataset_version"],
                      "saas_products": [len(prev["canonical"]), len(st["canonical"])],
                      "removed": [{"saas_id": s, "name": prev["names"].get(s), **_why(s, st, prev, True)} for s in gone],
                      "added": [{"saas_id": s, "name": st["names"].get(s), **_why(s, st, prev, False)} for s in new]})
    a, b = states[0], states[-1]
    removed, added = sorted(a["canonical"] - b["canonical"]), sorted(b["canonical"] - a["canonical"])
    causes = {}
    for s in steps:
        for x in s["removed"]:
            if x["saas_id"] in removed:
                causes.setdefault(x["cause"], []).append(x["saas_id"])
    churned = sorted({x["saas_id"] for s in steps for x in s["removed"] + s["added"]} - set(removed) - set(added))
    return {"schema": SCHEMA, "from": {"commit": a["commit"][:12], "dataset_version": hist[a["commit"]]["dataset_version"],
                                       "saas_products": len(a["canonical"])},
            "to": {"commit": b["commit"][:12], "dataset_version": hist[b["commit"]]["dataset_version"],
                   "saas_products": len(b["canonical"])},
            "net_change": len(b["canonical"]) - len(a["canonical"]),
            "removed": [{"saas_id": s, "name": a["names"].get(s)} for s in removed],
            "added": [{"saas_id": s, "name": b["names"].get(s)} for s in added],
            "removed_by_cause": {k: sorted(v) for k, v in sorted(causes.items())},
            "left_and_returned": churned,
            "balances": len(a["canonical"]) - len(removed) + len(added) == len(b["canonical"]),
            "reproduced": not problems, "problems": problems, "steps": steps,
            "versions_examined": len(commits)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="source_pipeline obs-lineage", description=__doc__.split("\n\n")[0])
    ap.add_argument("action", choices=["history", "verify", "reconcile"])
    ap.add_argument("--from", dest="frm", help="git revision or dataset_version (default: the first version)")
    ap.add_argument("--to", help="git revision or dataset_version (default: HEAD)")
    a = ap.parse_args(argv[1:])
    try:
        if a.action == "history":
            out = history(a.frm, a.to)
        elif a.action == "verify":
            out = verify(a.frm, a.to)
        else:
            if not a.frm:
                ap.error("reconcile needs --from")
            out = reconcile(a.frm, a.to)
    except LineageError as exc:
        print(f"obs-lineage: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if out.get("ok", True) and out.get("reproduced", True) else 1
