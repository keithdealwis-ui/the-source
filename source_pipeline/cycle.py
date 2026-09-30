"""The weekly research + refresh cycle (KEI-807 criteria 5, 7, 9-12).

    python -m source_pipeline cycle --trigger schedule   # scheduler wake-up; runs only when due
    python -m source_pipeline cycle --trigger manual     # run now (workflow_dispatch)
    python -m source_pipeline cycle-due                  # print whether a cycle is due, and why

How a cycle protects the last known-good dataset:

  1. A run record is written first (runs/<run_id>.json, status running).
  2. All work happens in a scratch copy of the repository (THE_SOURCE_ROOT points there):
     research, harvest, live refresh, build, dataset validation, scoring, canonical build,
     canonical validation. The live copy is not touched.
  3. Only if every step succeeds are the produced paths copied back (promotion). Any
     failure leaves the live copy exactly as it was, and the run record says why.
  4. Every output is a deterministic function of its inputs, so a retry after a failure
     neither duplicates nor corrupts anything; attempts are counted per weekly cycle.

The scheduler then commits the promoted paths and the run record in one commit and tags
it `cycle/<run_id>` (the resulting canonical commit), or, on failure, commits only the
run record and raises a durable GitHub issue.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import common

RUNS = common.ROOT / "runs"
SCHEDULE = common.CONFIG / "schedule.yaml"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
PROMOTE = ["data/staging", "data/live", "data/dataset", "data/state", "data/scores", "data/canonical",
           "data/intake/outcomes", "api/v1"]


class CycleError(RuntimeError):
    pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_schedule() -> dict:
    return common.load_yaml(SCHEDULE)


def runs() -> list[dict]:
    return [common.read_json(p) for p in sorted(RUNS.glob("*.json"))] if RUNS.exists() else []


def due(now: datetime | None = None) -> dict:
    cfg = load_schedule()
    tz = ZoneInfo(cfg["timezone"])
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    hh, mm = (int(x) for x in str(cfg["time"]).split(":"))
    target = WEEKDAYS.index(cfg["weekday"].lower())
    slot_date = now.date() - timedelta(days=(now.weekday() - target) % 7)
    slot = datetime(slot_date.year, slot_date.month, slot_date.day, hh, mm, tzinfo=tz)
    if slot > now:
        slot -= timedelta(days=7)
    window_end = slot + timedelta(hours=float(cfg["window_hours"]))
    key = slot.date().isoformat()
    mine = [r for r in runs() if r["cycle_key"] == key and r["trigger"] == "schedule"]
    succeeded = any(r["status"] == "succeeded" for r in mine)
    attempts = len(mine)
    out = {"cycle_key": key, "slot": _iso(slot), "window_end": _iso(window_end), "now": _iso(now),
           "timezone": cfg["timezone"], "attempts": attempts, "max_attempts": cfg["max_attempts"],
           "succeeded": succeeded}
    if succeeded:
        return {**out, "due": False, "reason": "this week's cycle already succeeded"}
    if now >= window_end:
        return {**out, "due": False, "reason": "outside the off-peak window"}
    if attempts >= cfg["max_attempts"]:
        return {**out, "due": False, "reason": "attempts exhausted for this cycle; see the failure issue"}
    return {**out, "due": True, "reason": "inside the window and not yet succeeded"}


# ---------------------------------------------------------------- orchestration (live copy)

def _copy_tree(src: Path, dst: Path) -> None:
    ignore = shutil.ignore_patterns(".git", ".venv", "raw", "__pycache__", ".pytest_cache", "evidence",
                                    "score", "graph", "discover", "export-public")
    shutil.copytree(src, dst, ignore=ignore, symlinks=False)
    (dst / "raw").symlink_to((src / "raw").resolve(), target_is_directory=True) if (src / "raw").exists() else None


def _promote(work: Path) -> None:
    for rel in PROMOTE:
        src, dst = work / rel, common.ROOT / rel
        if not src.exists():
            continue
        tmp = dst.with_name(dst.name + ".promoting")
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(src, tmp)
        if dst.exists():
            shutil.rmtree(dst)
        tmp.rename(dst)


def run(trigger: str) -> dict:
    now = datetime.now(timezone.utc)
    if trigger == "schedule":
        d = due(now)
        if not d["due"]:
            print(json.dumps({"skipped": True, **d}, indent=2))
            return {"skipped": True, **d}
        key, attempt = d["cycle_key"], d["attempts"] + 1
    else:
        key, attempt = f"{trigger}-{now.strftime('%Y%m%dT%H%M%SZ')}", 1
    run_id = f"{key}-{trigger}-a{attempt}"
    (common.ROOT / "raw").mkdir(exist_ok=True)
    before = common.read_json(common.DATA / "canonical" / "MANIFEST.json")["dataset_version"] \
        if (common.DATA / "canonical" / "MANIFEST.json").exists() else None
    record = {"run_id": run_id, "cycle_key": key, "attempt": attempt, "trigger": trigger,
              "started_at": _iso(now), "ended_at": None, "status": "running",
              "schedule": {k: load_schedule()[k] for k in ("timezone", "weekday", "time", "window_hours")},
              "strategies": [], "counts": {}, "transitions": {},
              "canonical": {"dataset_version_before": before, "dataset_version_after": before, "changed": False,
                            "commit_tag": None},
              "error": None}
    path = RUNS / f"{run_id}.json"
    common.write_json(path, record)

    workdir = Path(tempfile.mkdtemp(prefix="the-source-cycle-"))
    work = workdir / "root"
    try:
        _copy_tree(common.ROOT, work)
        result_file = workdir / "result.json"
        env = dict(os.environ, THE_SOURCE_ROOT=str(work), PYTHONPATH=str(common.ROOT))
        proc = subprocess.run([sys.executable, "-m", "source_pipeline", "cycle-work", "--run-id", run_id,
                               "--result", str(result_file)], cwd=common.ROOT, env=env)
        result = common.read_json(result_file) if result_file.exists() else {}
        record.update({k: result[k] for k in ("strategies", "counts", "transitions") if k in result})
        if proc.returncode != 0 or not result.get("ok"):
            raise CycleError(result.get("error") or f"cycle worker exited {proc.returncode} without a result")
        _promote(work)
        after = result["dataset_version"]
        record["canonical"].update({"dataset_version_after": after, "changed": after != before,
                                    "commit_tag": f"cycle/{run_id}"})
        record["status"] = "succeeded"
    except Exception as exc:  # the live copy is untouched; the failure is recorded, never swallowed
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"[:2000]
    finally:
        record["ended_at"] = _iso(datetime.now(timezone.utc))
        common.write_json(path, record)
        shutil.rmtree(workdir, ignore_errors=True)
    print(json.dumps({k: record[k] for k in ("run_id", "status", "counts", "canonical", "error")}, indent=2))
    return record


# ---------------------------------------------------------------- the work (scratch copy)

def _lanes(path: Path) -> dict:
    if not path.exists():
        return {}
    return {p["oss_id"]: (p["derived"]["lane"], p["derived"]["eligibility"]["exclusion_reasons"])
            for p in common.read_jsonl(path)}


def _score_new(budget: int, res: dict) -> None:
    from . import canonical, score_one

    fit, health = canonical._load_scores()
    oss = {p["oss_id"]: p for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl")}
    snap = common.read_json(common.LIVE / "snapshot.json")["projects"]
    todo = {}
    for e in common.read_jsonl(common.DATASET / "relationships.jsonl"):
        p = oss[e["oss_id"]]
        if (e["validation"]["status"] != "validated" or p["derived"]["lane"] != "recommendable"
                or p["host"] != "github.com" or (e["saas_id"], e["oss_id"]) in fit):
            continue
        todo.setdefault(e["oss_id"], []).append({
            "saas_id": e["saas_id"], "status": "validated", "source_count": e["source_count"],
            "corroborated": e["corroborated"],
            "reviewed": e["validation"]["plausibility_review"]["outcome"] != "not reviewed"})
    for oid, p in oss.items():
        if p["derived"]["lane"] == "recommendable" and p["host"] == "github.com" and oid not in health:
            todo.setdefault(oid, [])
    keys = sorted(todo)[:budget]
    errors, scored_pairs = [], 0
    for key in keys:
        p = oss[key]
        live = next((v for k, v in snap.items() if (v.get("canonical_id") or k) == key), None)
        if live is None:
            errors.append(f"{key}: no live observation")
            continue
        r = score_one.score(key, live, p["derived"]["licence"], todo[key], p["derived"]["evaluated_as_of"])
        if r["health"] is None:
            errors.append(f"{key}: {r['errors']}")
            continue
        score_one.store(r)
        scored_pairs += len(r["fit"])
        errors += [f"{key}: {e}" for e in r["errors"]]
    res["strategies"].append({"id": "new_pair_scoring", "status": "ok" if not errors else "degraded",
                              "projects_scored": len(keys) - len([e for e in errors if "no live" in e]),
                              "pairs_scored": scored_pairs, "deferred_to_next_cycle": max(0, len(todo) - budget),
                              "errors": errors[:20]})


def work(run_id: str, result_path: Path) -> int:
    """Everything a cycle does. Runs with THE_SOURCE_ROOT set to a scratch copy."""
    from . import build, canonical, canonical_validate, enrich, harvest, intake, research, validate
    from .normalise import reconcile

    cfg = load_schedule()
    res = {"ok": False, "strategies": [], "counts": {}, "transitions": {}, "error": None}
    as_of = _iso(datetime.now(timezone.utc))
    try:
        prev = _lanes(common.DATASET / "oss_projects.jsonl")
        prev_rel = {(e["saas_id"], e["oss_id"]) for e in common.read_jsonl(common.DATA / "canonical" / "relationships.jsonl")} \
            if (common.DATA / "canonical" / "relationships.jsonl").exists() else set()

        # 1. research: upstream lists
        pins = research.advance_pins(cfg["research"]["min_entry_ratio"], as_of)
        bad = [p for p in pins if p["status"] not in ("unchanged", "advanced")]
        res["strategies"].append({"id": "upstream_lists", "status": "ok" if not bad else "degraded",
                                  "sources": len(pins), "advanced": sum(p["status"] == "advanced" for p in pins),
                                  "kept": [{k: p.get(k) for k in ("source_id", "status", "detail")} for p in bad],
                                  "detail": pins})
        # 2. harvest every source at its pin, plus the intake ledger
        stats = harvest.run()
        res["strategies"].append({"id": "intake_ledger", "status": "ok",
                                  "admitted_entries": stats.get("intake", {}).get("entries", 0),
                                  "ledger_version": stats.get("intake", {}).get("pinned_commit")})
        claims = common.read_jsonl(common.STAGING / "claims.jsonl")
        keys = sorted(reconcile(claims, common.load_sources())["oss"])

        # 3. live refresh, retrying only what failed
        snap = enrich.observe(keys)
        tries = 0
        while snap["failed_keys"] and tries < cfg["refresh"]["retry_failed_lookups"]:
            tries += 1
            snap = enrich.retry_failed(snap)
        found = sum(1 for p in snap["projects"].values() if p.get("found"))
        res["strategies"].append({"id": "live_refresh", "status": "ok" if not snap["failed_keys"] else "failed",
                                  "repositories": len(keys), "found": found, "http_calls": snap["http_calls"],
                                  "retries": tries, "still_failing": snap["failed_keys"][:20], "as_of": snap["as_of"]})
        if snap["failed_keys"]:
            raise CycleError(f"{len(snap['failed_keys'])} repositories could not be verified after {tries} "
                             f"retries (e.g. {snap['failed_keys'][:3]}); nothing promoted")
        enrich.write_snapshot(snap)

        # 4. build and validate the dataset
        build.run()
        report = validate.run()
        failed = [c["id"] for c in report["checks"] if not c["ok"] and c["criterion"] != 6]
        if failed:
            raise CycleError(f"dataset validation failed: {failed}")

        # 5. score what is new
        _score_new(cfg["scoring"]["max_new_projects_per_cycle"], res)

        # 6. canonical dataset + read surface, then its own validation
        canonical.build()
        crep = canonical_validate.run()
        if not crep["ok"]:
            raise CycleError(f"canonical validation failed: {[c['id'] for c in crep['checks'] if not c['ok']]}")
        manifest = common.read_json(common.DATA / "canonical" / "MANIFEST.json")

        # 7. intake outcomes learn whether they were promoted
        promo = intake.reconcile_promotions(run_id)

        now = _lanes(common.DATASET / "oss_projects.jsonl")
        new = sorted(set(now) - set(prev))
        rel_now = {(r["saas_id"], r["oss_id"]) for r in common.read_jsonl(common.DATA / "canonical" / "relationships.jsonl")}
        dropped = sorted(k for k, (lane, _) in now.items() if k in prev and prev[k][0] == "recommendable" and lane != "recommendable")
        res["transitions"] = {
            "no_longer_recommendable": [{"oss_id": k, "lane": now[k][0], "reasons": now[k][1]} for k in dropped],
            "became_recommendable": sorted(k for k, (lane, _) in now.items()
                                           if lane == "recommendable" and k in prev and prev[k][0] != "recommendable"),
        }
        res["counts"] = {
            "candidates": len(keys),
            "discovered": len(new),
            "retained": sum(1 for k in new if now[k][0] == "recommendable"),
            "rejected": sum(1 for k in new if now[k][0] != "recommendable"),
            "refreshed_records": found,
            "stale_or_abandoned_detected": len(dropped),
            "canonical_relationships": len(rel_now),
            "canonical_relationships_added": len(rel_now - prev_rel),
            "canonical_relationships_removed": len(prev_rel - rel_now),
            "canonical_projects": manifest["counts"]["oss_projects"],
            "supported_saas_ok": manifest["counts"]["supported_saas_ok"],
            "intake_promoted": promo["promoted"], "intake_pending": promo["pending"],
            "canonical_checks": len(crep["checks"]), "canonical_warnings": len(crep["warnings"]),
        }
        res.update(ok=True, dataset_version=manifest["dataset_version"])
    except Exception as exc:
        res["error"] = f"{type(exc).__name__}: {exc}"[:2000]
    common.write_json(result_path, res)
    return 0 if res["ok"] else 1
