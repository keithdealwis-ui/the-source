"""The research + refresh cycle: weekly under KEI-807 (criteria 5, 7, 9-12), daily under
KEI-848 (light refresh for everything, deep enrichment and re-scoring only where something
changed, dated history, discovery reconciliation, upstream corroboration; see daily.py).

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
           "data/intake/outcomes", "data/history", "data/momentum", "data/discovery", "data/radar", "data/corpus", "data/quick", "api/v1"]


class CycleError(RuntimeError):
    pass


def _drill(step: str) -> None:
    """Failure drill (manual runs only): THE_SOURCE_DRILL_FAIL_AT=<step> stops the cycle at
    that step, so the failure path (nothing promoted, run record, durable issue, recovery)
    can be exercised for real. The scheduler never sets it."""
    if os.environ.get("THE_SOURCE_DRILL_FAIL_AT") == step:
        raise CycleError(f"failure drill: stopped at {step} (THE_SOURCE_DRILL_FAIL_AT)")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_schedule() -> dict:
    return common.load_yaml(SCHEDULE)


def runs() -> list[dict]:
    return [common.read_json(p) for p in sorted(RUNS.glob("*.json"))] if RUNS.exists() else []


LATEST_FIELDS = ("cycle_key", "ended_at", "error", "counts", "canonical")


def latest(runs_dir: Path | None = None) -> dict | None:
    """The newest cycle summary in runs/ (KEI-887): the record that ended last (a record still
    running counts from its start), ties broken by file name. None when there are no records."""
    d = RUNS if runs_dir is None else Path(runs_dir)
    records = [(common.read_json(p), p.name) for p in sorted(d.glob("*.json"))] if d.is_dir() else []
    if not records:
        return None
    rec, _ = max(records, key=lambda r: (r[0].get("ended_at") or r[0].get("started_at") or "", r[1]))
    return {k: rec.get(k) for k in LATEST_FIELDS}


def due(now: datetime | None = None) -> dict:
    cfg = load_schedule()
    tz = ZoneInfo(cfg["timezone"])
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    hh, mm = (int(x) for x in str(cfg["time"]).split(":"))
    cadence = cfg.get("cadence", "weekly")
    period = timedelta(days=1 if cadence == "daily" else 7)
    window, catch_up = float(cfg["window_hours"]), float(cfg.get("catch_up_hours", 0))
    if timedelta(hours=window + catch_up) >= period:
        raise CycleError(f"schedule: window_hours + catch_up_hours must stay under the {cadence} period")
    if cadence == "daily":
        slot_date = now.date()
    else:
        target = WEEKDAYS.index(cfg["weekday"].lower())
        slot_date = now.date() - timedelta(days=(now.weekday() - target) % 7)
    slot = datetime(slot_date.year, slot_date.month, slot_date.day, hh, mm, tzinfo=tz)
    if slot > now:
        slot -= period
    window_end = slot + timedelta(hours=window)
    # GitHub's hourly cron is best effort and can go hours between wake-ups, so a wake-up
    # can miss the whole window. Up to catch_up_hours after the window, the first wake-up
    # still runs the period's cycle (marked late) rather than skipping it.
    catch_up_end = window_end + timedelta(hours=catch_up)
    key = slot.date().isoformat()
    mine = [r for r in runs() if r["cycle_key"] == key and r["trigger"] == "schedule"]
    succeeded = any(r["status"] == "succeeded" for r in mine)
    attempts = len(mine)
    period_name = "today's" if cadence == "daily" else "this week's"
    out = {"cycle_key": key, "cadence": cadence, "slot": _iso(slot), "window_end": _iso(window_end),
           "catch_up_end": _iso(catch_up_end), "now": _iso(now),
           "timezone": cfg["timezone"], "attempts": attempts, "max_attempts": cfg["max_attempts"],
           "succeeded": succeeded}
    if succeeded:
        return {**out, "due": False, "reason": f"{period_name} cycle already succeeded"}
    if now >= catch_up_end:
        return {**out, "due": False, "reason": "outside the off-peak window"}
    if attempts >= cfg["max_attempts"]:
        return {**out, "due": False, "reason": "attempts exhausted for this cycle; see the failure issue"}
    if now >= window_end:
        return {**out, "due": True, "late": True,
                "reason": "window missed by the scheduler; catching up within catch_up_hours"}
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
    if trigger == "schedule":
        os.environ.pop("THE_SOURCE_DRILL_FAIL_AT", None)  # drills are never scheduled
    run_id = f"{key}-{trigger}-a{attempt}"
    (common.ROOT / "raw").mkdir(exist_ok=True)
    before = common.read_json(common.DATA / "canonical" / "MANIFEST.json")["dataset_version"] \
        if (common.DATA / "canonical" / "MANIFEST.json").exists() else None
    record = {"run_id": run_id, "cycle_key": key, "attempt": attempt, "trigger": trigger,
              "started_at": _iso(now), "ended_at": None, "status": "running",
              "schedule": {k: load_schedule().get(k) for k in ("cadence", "timezone", "weekday", "time", "window_hours")},
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
        record.update({k: result[k] for k in ("strategies", "counts", "transitions", "timings_s", "retries",
                                               "failures", "history_day", "failed_step", "corpus") if k in result})
        if proc.returncode != 0 or not result.get("ok"):
            raise CycleError(result.get("error") or f"cycle worker exited {proc.returncode} without a result")
        _promote(work)
        after = result["dataset_version"]
        record["canonical"].update({"dataset_version_after": after, "changed": after != before,
                                    "commit_tag": f"cycle/{run_id}"})
        record["status"] = "succeeded"
    except Exception as exc:  # the live copy is untouched; the failure is recorded, never swallowed
        record["status"] = "failed"
        record["error"] = (str(exc) if isinstance(exc, CycleError) else f"{type(exc).__name__}: {exc}")[:2000]
    finally:
        record["ended_at"] = _iso(datetime.now(timezone.utc))
        record["anomalies"] = _anomalies(record)
        common.write_json(path, record)
        shutil.rmtree(workdir, ignore_errors=True)
    _intake_report(record)
    print(json.dumps({k: record[k] for k in ("run_id", "status", "counts", "canonical", "error")}, indent=2))
    return record


def _anomalies(record: dict) -> list[dict]:
    """Corpus intake anomalies (corpus_admit.anomalies) against the earlier records; never fails the run."""
    from . import corpus_admit

    try:
        key_now = record["cycle_key"] if record["trigger"] == "schedule" else due()["cycle_key"]
        return corpus_admit.anomalies(record, [r for r in runs() if r.get("run_id") != record["run_id"]],
                                      load_schedule(), key_now)
    except Exception as exc:
        return [{"code": "anomaly_check_failed", "detail": f"{type(exc).__name__}: {exc}"[:300]}]


GOLDEN_BASELINE = common.ROOT / "observability" / "golden-baseline.json"
SCORES_FIT = common.DATA / "scores" / "fit.jsonl"


def _golden_step() -> dict:
    from . import golden

    try:
        cur = golden.evaluate()
        summ = cur["summary"]
        out = {"id": "golden_queries", "queries": summ["queries"], "recall@5": summ["recall@5"],
               "precision@5": summ["precision@5"], "constraint_violations": summ["constraint_violations"],
               "negative_passed": summ["negative_passed"], "errors": []}
        if summ["constraint_violations"]:
            out["errors"].append(f"constraint violations in {summ['queries_with_violations']}")
        if summ["negative_passed"] != summ["negative"]:
            out["errors"].append("a must-fail query did not fail safely")
        if GOLDEN_BASELINE.exists():
            cfg = common.load_yaml(common.CONFIG / "observability.yaml")["golden"]
            cmp = golden.compare(cur, common.read_json(GOLDEN_BASELINE), float(cfg["max_recall_drop"]),
                                 float(cfg["max_precision_drop"]))
            out["baseline_dataset_version"] = cmp["baseline_dataset_version"]
            out["errors"] += [p for p in cmp["problems"] if p not in out["errors"] and "violations" not in p]
        out["status"] = "ok" if not out["errors"] else "degraded"
        return out
    except Exception as exc:  # observability never fails the cycle
        return {"id": "golden_queries", "status": "failed", "errors": [f"{type(exc).__name__}: {exc}"[:300]]}


def _intake_report(record: dict) -> None:
    """runs/reports/<run_id>.intake.json (KEI-918 S0.1), from the live copy after promotion. A report that cannot
    be written is noted on stderr and never changes the run's outcome: observability must not fail the cycle."""
    from . import observability

    try:
        observability.write_intake_report(record)
    except Exception as exc:
        print(f"cycle: intake report not written: {type(exc).__name__}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------- the work (scratch copy)

def _lanes(path: Path) -> dict:
    if not path.exists():
        return {}
    return {p["oss_id"]: (p["derived"]["lane"], p["derived"]["eligibility"]["exclusion_reasons"])
            for p in common.read_jsonl(path)}


def _score_new(budget: int, res: dict) -> None:
    from . import canonical, score_build, score_one

    fit, health = canonical._load_scores()
    oss = {p["oss_id"]: p for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl")}
    snap = common.read_json(common.LIVE / "snapshot.json")["projects"]
    jobs = score_build.load_jobs()
    category = {s["saas_id"]: s["category"] for s in common.read_jsonl(common.DATASET / "saas_products.jsonl")}
    todo, no_job = {}, []
    for e in common.read_jsonl(common.DATASET / "relationships.jsonl"):
        p = oss[e["oss_id"]]
        if (e["validation"]["status"] != "validated" or p["derived"]["lane"] != "recommendable"
                or p["host"] != "github.com" or (e["saas_id"], e["oss_id"]) in fit):
            continue
        if category.get(e["saas_id"]) not in jobs:
            # config/saas_jobs.yaml leaves some categories undefined on purpose (Sales intelligence, ...): such a pair
            # can never be scored, so it is reported, never retried, and never degrades the strategy (KEI-961).
            no_job.append({"saas_id": e["saas_id"], "oss_id": e["oss_id"], "category": category.get(e["saas_id"])})
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
                              "not_scorable_no_job_definition": len(no_job),
                              "not_scorable": sorted(no_job, key=lambda x: (x["saas_id"], x["oss_id"]))[:20],
                              "errors": errors[:20]})


RESCORE_QUEUE = common.STATE / "rescore_queue.json"
RELEASE_ONLY = ("release ",)


def _release_only(reasons: list[str]) -> bool:
    return bool(reasons) and all(r.startswith(RELEASE_ONLY) for r in reasons)


def _rescore(keys: list[str], budget: int, res: dict, reasons: dict | None = None, day: str | None = None,
             release_interval_days: int = 7) -> list[str]:
    """Re-score projects whose light refresh showed a material change (KEI-848). Only
    projects that already hold a score are re-scored here; unscored ones are _score_new's.

    KEI-961: what the budget leaves is carried in data/state/rescore_queue.json and asked first next cycle (before
    KEI-961 it was dropped: a change is detected once, so "deferred_to_next_cycle" never came back, and the
    alphabetical cut starved the same projects every day). A change other than a new release goes first; a
    release-only change waits until the project's Health is release_interval_days old, so a project that releases
    daily is re-scored once a week, not daily. Nothing is ever dropped while the project stays scorable."""
    from . import canonical, score_one

    reasons = reasons or {}
    today = day or datetime.now(timezone.utc).date().isoformat()
    fit, health = canonical._load_scores()
    oss = {p["oss_id"]: p for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl")}
    snap = common.read_json(common.LIVE / "snapshot.json")["projects"]
    pairs: dict[str, list] = {}
    for e in common.read_jsonl(common.DATASET / "relationships.jsonl"):
        if e["validation"]["status"] == "validated":
            pairs.setdefault(e["oss_id"], []).append({
                "saas_id": e["saas_id"], "status": "validated", "source_count": e["source_count"],
                "corroborated": e["corroborated"],
                "reviewed": e["validation"]["plausibility_review"]["outcome"] != "not reviewed"})
    queue = (common.read_json(RESCORE_QUEUE).get("queue") or {}) if RESCORE_QUEUE.exists() else {}
    carried = len(queue)
    for key in keys:
        live = snap.get(key) or {}
        oid = live.get("canonical_id") or key
        why = list(reasons.get(key) or [])
        q = queue.setdefault(oid, {"queued_on": today, "reasons": []})
        q["reasons"] = sorted(set(q["reasons"]) | set(why))[-10:]
    eligible, waiting, dropped = [], 0, 0
    for oid in sorted(queue):
        live = snap.get(oid) or next((v for k, v in snap.items() if v.get("canonical_id") == oid), None)
        p = oss.get(oid)
        if not (live and p and p["host"] == "github.com" and p["derived"]["lane"] == "recommendable" and oid in health):
            queue.pop(oid)  # no longer scorable here (left the lane, or now _score_new's)
            dropped += 1
            continue
        q = queue[oid]
        if _release_only(q["reasons"]):
            at = (health[oid].get("evaluated_at") or "")[:10]
            try:
                age = (datetime.fromisoformat(today) - datetime.fromisoformat(at)).days if at else None
            except ValueError:
                age = None
            if age is not None and age < release_interval_days:
                waiting += 1
                continue
        eligible.append((0 if not _release_only(q["reasons"]) else 1, q["queued_on"], oid, live, p))
    eligible.sort(key=lambda t: t[:3])
    done, errors = [], []
    for _, _, oid, live, p in eligible[:budget]:
        r = score_one.score(oid, live, p["derived"]["licence"], pairs.get(oid, []), p["derived"]["evaluated_as_of"])
        if r["health"] is None:
            errors.append(f"{oid}: {r['errors']}")
            continue
        score_one.store(r)
        done.append(oid)
        queue.pop(oid, None)
        errors += [f"{oid}: {e}" for e in r["errors"]]
    common.write_json(RESCORE_QUEUE, {"schema": "the-source.rescore-queue/1", "as_of": today,
                                      "release_interval_days": release_interval_days,
                                      "queue": dict(sorted(queue.items()))})
    oldest = min((q["queued_on"] for q in queue.values()), default=None)
    res["strategies"].append({"id": "rescoring", "status": "ok" if not errors else "degraded",
                              "candidates": len(eligible), "rescored": len(done), "projects": done[:50],
                              "queue": {"carried_in": carried, "detected_today": len(keys), "dropped_unscorable": dropped,
                                        "waiting_release_interval": waiting, "carried_out": len(queue),
                                        "oldest_queued_on": oldest},
                              "deferred_to_next_cycle": max(0, len(eligible) - budget), "errors": errors[:20]})
    return done


CORPUS_COUNTS = ("corpus_added", "corpus_removed", "corpus_collapsed", "membership_before", "membership_after")


def _corpus_step(as_of_dt: datetime, res: dict) -> dict:
    """Admission, then (when membership changed) the quick scores that are pinned to the corpus version
    (KEI-921, quick_score.py; no network). Both or neither: if quick scoring cannot follow, the previous corpus
    and quick layers are put back, so data/quick never describes a corpus that is not the one served."""
    from . import corpus, corpus_admit, daily, quick_score

    keep = Path(tempfile.mkdtemp(prefix="the-source-corpus-keep-"))
    for name, d in (("corpus", corpus.OUT), ("quick", quick_score.OUT)):
        if d.exists():
            shutil.copytree(d, keep / name)
    try:
        out = corpus_admit.admit(daily.load_pool(), as_of_dt)
        if out["corpus_version_after"] != out["corpus_version_before"] and quick_score.OUT.exists():
            q = quick_score.run(as_of_dt)
            check = quick_score.validate()
            if check["failed_checks"]:
                raise CycleError("quick scores do not validate after admission: "
                                 f"{[c['id'] for c in check['checks'] if not c['ok']]}")
            out["quick"] = {k: q["last_run"].get(k) for k in ("health_rescored", "pairings_rescored", "runtime_s")} \
                if isinstance(q, dict) and isinstance(q.get("last_run"), dict) else None
    except Exception as exc:  # put back the previous layers
        for name, d in (("corpus", corpus.OUT), ("quick", quick_score.OUT)):
            if (keep / name).exists():
                shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(keep / name, d)
        shutil.rmtree(keep, ignore_errors=True)
        res["failures"].append({"step": "corpus_admission", "key": "data/corpus",
                                "outcome": f"kept the previous corpus: {type(exc).__name__}: {exc}"[:300]})
        sha = corpus_admit.manifest_sha(corpus.OUT)
        res["corpus"] = {"manifest_sha256_before": sha, "manifest_sha256_after": sha, "error": str(exc)[:300]}
        return {"id": "corpus_admission", "status": "failed", "errors": [f"{type(exc).__name__}: {exc}"[:300]]}
    shutil.rmtree(keep, ignore_errors=True)
    res["corpus"] = out
    res["counts"].update({k: out[k] for k in CORPUS_COUNTS})
    return {"id": "corpus_admission", "status": "ok", "pool_candidates": out["pool_candidates"],
            "looked_up": out["looked_up"], "admitted": out["corpus_added"], "dropped": out["dropped"],
            "membership_as_of": out["membership_as_of"], "corpus_version_after": out["corpus_version_after"]}


class _Timer:
    def __init__(self, res: dict):
        self.res, self.t = res, None

    def __call__(self, step: str):
        import time

        now = time.monotonic()
        if self.t is not None:
            self.res["timings_s"][self.t[0]] = round(now - self.t[1], 1)
        self.t = (step, now) if step else None

    @property
    def current(self):
        return self.t[0] if self.t else None


def work(run_id: str, result_path: Path) -> int:
    """Everything a cycle does. Runs with THE_SOURCE_ROOT set to a scratch copy."""
    from . import (build, canonical, canonical_validate, corroborate, daily, discovery_search, enrich, harvest,
                   intake, momentum, radar, research, validate)
    from .normalise import reconcile

    cfg = load_schedule()
    policy = common.load_policy()
    res = {"ok": False, "strategies": [], "counts": {}, "transitions": {}, "error": None, "timings_s": {},
           "retries": {}, "failures": []}
    step = _Timer(res)
    as_of_dt = datetime.now(timezone.utc).replace(microsecond=0)
    as_of = _iso(as_of_dt)
    day = as_of_dt.astimezone(ZoneInfo(cfg["timezone"])).date().isoformat()
    try:
        prev = _lanes(common.DATASET / "oss_projects.jsonl")
        prev_rel = {(e["saas_id"], e["oss_id"]) for e in common.read_jsonl(common.DATA / "canonical" / "relationships.jsonl")} \
            if (common.DATA / "canonical" / "relationships.jsonl").exists() else set()
        prev_canon = {p["oss_id"] for p in common.read_jsonl(common.DATA / "canonical" / "oss_projects.jsonl")} \
            if (common.DATA / "canonical" / "oss_projects.jsonl").exists() else set()
        prev_snap = common.read_json(common.LIVE / "snapshot.json") if (common.LIVE / "snapshot.json").exists() else {}
        last_activity = {}
        if (common.DATASET / "oss_projects.jsonl").exists():
            for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl"):
                at = ((p["derived"].get("maintenance") or {}).get("last_meaningful_activity_at"))
                for k in [p["oss_id"], *p.get("harvested_as", [])]:
                    last_activity[k] = at

        # 1. research: upstream lists
        step("upstream_lists")
        pins = research.advance_pins(cfg["research"]["min_entry_ratio"], as_of)
        bad = [p for p in pins if p["status"] not in ("unchanged", "advanced")]
        res["strategies"].append({"id": "upstream_lists", "status": "ok" if not bad else "degraded",
                                  "sources": len(pins), "advanced": sum(p["status"] == "advanced" for p in pins),
                                  "kept": [{k: p.get(k) for k in ("source_id", "status", "detail")} for p in bad],
                                  "detail": pins})
        # 2. harvest every source at its pin, plus the intake ledger
        step("intake_ledger")
        stats = harvest.run()
        res["strategies"].append({"id": "intake_ledger", "status": "ok",
                                  "admitted_entries": stats.get("intake", {}).get("entries", 0),
                                  "ledger_version": stats.get("intake", {}).get("pinned_commit")})
        claims = common.read_jsonl(common.STAGING / "claims.jsonl")
        keys = sorted(reconcile(claims, common.load_sources())["oss"])

        # 3. light refresh of the whole corpus, retrying only what failed
        step("light_refresh")
        bs = int(cfg["refresh"].get("batch_size", 50))
        light = daily.light_refresh(keys, bs, as_of_dt)
        tries = 0
        while light["failed_keys"] and tries < cfg["refresh"]["retry_failed_lookups"]:
            tries += 1
            light = daily.retry_light(light, bs)
        res["retries"]["light_refresh"] = tries
        res["strategies"].append({"id": "light_refresh", "status": "ok" if not light["failed_keys"] else "failed",
                                  "repositories": len(keys),
                                  "found": sum(1 for v in light["projects"].values() if v.get("found")),
                                  "http_calls": light["http_calls"], "retries": tries,
                                  "still_failing": light["failed_keys"][:20], "as_of": light["as_of"]})
        if light["failed_keys"]:
            raise CycleError(f"{len(light['failed_keys'])} repositories could not be refreshed after {tries} "
                             f"retries (e.g. {light['failed_keys'][:3]}); nothing promoted")
        _drill("light_refresh")

        # 4. discovery: approved search lanes, reconciled against the corpus before anything is new
        step("discovery_search")
        disc = discovery_search.run(as_of_dt)
        rec = daily.reconcile_candidates(disc["candidates"], light["projects"], daily.load_pool(), day)
        daily.write_pool(rec["pool"])
        res["strategies"].append({"id": "discovery_search", "status": disc["status"], "lanes": disc["lanes"],
                                  "returned": len(disc["candidates"]), "known_in_corpus": len(rec["known"]),
                                  "new_candidates": rec["new"][:100], "new_count": len(rec["new"]),
                                  "seen_again": len(rec["refreshed"]), "pool_size": len(rec["pool"]),
                                  "errors": disc["errors"][:20]})

        # 5. corroboration: independent upstreams checked against the light refresh; never canonical
        step("corroboration")
        corr = corroborate.run(light["projects"], as_of_dt)
        res["strategies"].append({"id": "corroboration", **corr["summary"]})

        # 6. deep enrichment only where it can change something
        step("deep_enrichment")
        classes = daily.classify(light, prev_snap.get("projects") or {}, cfg["deep"], policy, as_of, last_activity)
        deep_report: dict = {}
        rmin = cfg["deep"].get("release_deep_min_age_days")
        selected, deferred = daily.select_deep(classes, int(cfg["deep"]["max_per_cycle"]),
                                               None if rmin is None else int(rmin), deep_report)
        deep = enrich.observe(selected, as_of=as_of_dt) if selected else {"projects": {}, "failed_keys": [],
                                                                          "http_calls": 0, "lookup_failures": []}
        dtries = 0
        while deep["failed_keys"] and dtries < cfg["refresh"]["retry_failed_lookups"]:
            dtries += 1
            deep = enrich.retry_failed(deep)
        res["retries"]["deep_enrichment"] = dtries
        prior = prev_snap.get("projects") or {}
        unrecoverable = [k for k in deep["failed_keys"] if k not in prior]
        if unrecoverable:
            raise CycleError(f"{len(unrecoverable)} new repositories could not be deep-enriched after {dtries} "
                             f"retries (e.g. {unrecoverable[:3]}); nothing promoted")
        kept_prior = list(deep["failed_keys"])
        for k in kept_prior:  # a known project whose deep lookup failed keeps its last good observation
            deep["projects"].pop(k, None)
        res["failures"] += [{"step": "deep_enrichment", "key": k, "outcome": "kept previous deep observation"}
                            for k in kept_prior]
        merged = daily.merge_snapshot(prev_snap, light, deep, keys)
        enrich.write_snapshot(merged)
        by_class = {}
        for k, c in classes.items():
            by_class.setdefault(c["class"], []).append(k)
        res["strategies"].append({"id": "deep_enrichment", "status": "ok" if not kept_prior else "degraded",
                                  "selected": len(selected), "http_calls": deep.get("http_calls", 0),
                                  "retries": dtries, "kept_previous_after_failure": kept_prior[:20],
                                  "deferred_rotation": len(deferred),
                                  "release_coalesced": deep_report.get("release_coalesced", 0),
                                  "max_age_days": int(cfg["deep"]["max_age_days"]),
                                  "by_class": {c: len(v) for c, v in sorted(by_class.items())},
                                  "changed": [{"key": k, "reasons": classes[k]["reasons"]}
                                              for k in sorted(by_class.get("changed", []))][:100]})

        # 7. build and validate the dataset
        step("build_validate")
        build.run()
        report = validate.run()
        failed = [c["id"] for c in report["checks"] if not c["ok"] and c["criterion"] != 6]
        now = _lanes(common.DATASET / "oss_projects.jsonl")
        new = sorted(set(now) - set(prev))
        dropped = sorted(k for k, (lane, _) in now.items() if k in prev and prev[k][0] == "recommendable" and lane != "recommendable")
        res["transitions"] = {
            "no_longer_recommendable": [{"oss_id": k, "lane": now[k][0], "reasons": now[k][1]} for k in dropped],
            "became_recommendable": sorted(k for k, (lane, _) in now.items()
                                           if lane == "recommendable" and k in prev and prev[k][0] != "recommendable"),
        }
        changed_keys = sorted(by_class.get("changed", []))
        res["counts"].update({
            "candidates": len(keys),
            "discovered": len(new) + len(rec["new"]),
            "discovered_in_corpus": len(new),
            "discovered_pool": len(rec["new"]),
            "retained": sum(1 for k in new if now[k][0] == "recommendable"),
            "rejected": sum(1 for k in new if now[k][0] != "recommendable"),
            "refreshed_records": sum(1 for v in light["projects"].values() if v.get("found")),
            "changed": len(changed_keys),
            "unchanged": len(by_class.get("unchanged", [])) + len(by_class.get("stale", [])),
            "deep_enriched": len(selected) - len(kept_prior),
            "failed": len(kept_prior),
            "deferred": len(deferred),
            "deferred_deep_rotation": len(deferred),
            "stale_or_abandoned_detected": len(dropped),
        })
        if failed:
            raise CycleError(f"dataset validation failed: {failed}")

        # 8. score what is new, re-score what materially changed
        step("scoring")
        fit_before = common.sha256_file(SCORES_FIT) if SCORES_FIT.exists() else None
        _score_new(cfg["scoring"]["max_new_projects_per_cycle"], res)
        rescored = _rescore(changed_keys, int(cfg["scoring"].get("max_rescored_per_cycle", 40)), res,
                            reasons={k: classes[k]["reasons"] for k in changed_keys}, day=day,
                            release_interval_days=int(cfg["scoring"].get("release_rescore_interval_days", 7)))
        res["counts"]["rescored"] = len(rescored)
        res["counts"]["deferred_rescoring"] = next(s for s in res["strategies"] if s["id"] == "rescoring")["deferred_to_next_cycle"]
        res["counts"]["deferred_new_pair_scoring"] = next(s for s in res["strategies"]
                                                          if s["id"] == "new_pair_scoring")["deferred_to_next_cycle"]
        res["counts"]["deferred"] += res["counts"]["deferred_rescoring"] + res["counts"]["deferred_new_pair_scoring"]
        # launch.catalogue_serving (KEI-930) serves from the scores the build finds, so once scoring has moved them the
        # dataset is rebuilt; otherwise the committed data lags its own rebuild by a cycle and CI's rebuild check fails.
        if common.sha256_file(SCORES_FIT) != fit_before:
            build.run()
            report = validate.run()
            failed = [c["id"] for c in report["checks"] if not c["ok"] and c["criterion"] != 6]
            if failed:
                raise CycleError(f"dataset validation failed after rescoring: {failed}")

        # 9. canonical dataset + read surface, then its own validation
        step("canonical")
        canonical.build()
        crep = canonical_validate.run()
        if not crep["ok"]:
            raise CycleError(f"canonical validation failed: {[c['id'] for c in crep['checks'] if not c['ok']]}")
        manifest = common.read_json(common.DATA / "canonical" / "MANIFEST.json")
        gate = manifest.get("recommendation_gate") or {}
        res["strategies"].append({"id": "recommendation_gate", "status": "ok", **gate})
        res["counts"].update({"recommendation_evaluated": gate.get("evaluated"),
                              "recommendation_passed": gate.get("passed"), "recommendation_failed": gate.get("failed")})
        _drill("canonical")

        # 10. intake outcomes learn whether they were promoted
        promo = intake.reconcile_promotions(run_id)

        # 11. the day's dated snapshot, for 1/7/30/90-day deltas
        step("history")
        hist = daily.write_history(day, light, classes, manifest["dataset_version"], run_id)
        pruned = daily.prune_history(int(cfg["history"]["retain_days"]), day)
        res["strategies"].append({"id": "history", "status": "ok", "day": day, "rows": hist["rows"],
                                  "sha256": hist["sha256"], "days_held": len(daily.history_days()),
                                  "pruned": pruned})
        _drill("history")

        # 12. momentum and acceleration over 1/7/30/90 days from the dated history (KEI-849);
        # measured only where the history reaches, never on an invented baseline
        step("momentum")
        mom = momentum.cycle_step(day)
        if not mom["ok"]:
            raise CycleError(f"momentum validation failed: {mom['failed_checks']}")
        res["strategies"].append(mom["strategy"])
        res["counts"]["momentum_measured"] = mom["measured"]
        _drill("momentum")

        # 13. community radar (KEI-850): Hacker News and Forem mentions, ranked and reconciled
        # against the corpus just built; new repositories join the discovery pool, never canonical.
        # A community signal never blocks canonical truth: if the radar fails, the last good radar
        # and pool are put back, the strategy says failed, and the cycle carries on.
        step("community_radar")
        rad = radar.guarded_cycle_step(as_of_dt, day, light["projects"])
        res["strategies"].append(rad["strategy"])
        if not rad["ok"]:
            res["failures"].append({"step": "community_radar", "key": "data/radar",
                                    "outcome": f"kept the previous radar and pool: {rad['failed_checks']}"})
        res["counts"].update({"radar_ranked": rad["ranked"], "radar_pool_added": rad["pool_added"]})
        _drill("community_radar")

        # 14. corpus admission: pool candidates the public discovery corpus does not hold are read from the
        # host and admitted under the Discover lane's bar (corpus_admit.py). Never blocks canonical truth: on
        # failure data/corpus is left exactly as it was and the strategy says why.
        step("corpus_admission")
        res["strategies"].append(_corpus_step(as_of_dt, res))

        # 15. golden queries (KEI-918): retrieval quality of the layer just built, against the baseline.
        # Measured and reported, never blocking: a regression degrades the strategy and raises an alert.
        step("golden_queries")
        res["strategies"].append(_golden_step())
        step(None)

        canon_now = {p["oss_id"] for p in common.read_jsonl(common.DATA / "canonical" / "oss_projects.jsonl")}
        rel_now = {(r["saas_id"], r["oss_id"]) for r in common.read_jsonl(common.DATA / "canonical" / "relationships.jsonl")}
        res["counts"].update({
            "added": len(canon_now - prev_canon),
            "canonical_projects_removed": len(prev_canon - canon_now),
            "canonical_relationships": len(rel_now),
            "canonical_relationships_added": len(rel_now - prev_rel),
            "canonical_relationships_removed": len(prev_rel - rel_now),
            "canonical_projects": manifest["counts"]["oss_projects"],
            "canonical_saas_products": manifest["counts"]["saas_products"],
            "supported_saas_ok": manifest["counts"]["supported_saas_ok"],
            "intake_promoted": promo["promoted"], "intake_pending": promo["pending"],
            "canonical_checks": len(crep["checks"]), "canonical_warnings": len(crep["warnings"]),
        })
        res.update(ok=True, dataset_version=manifest["dataset_version"], history_day=day)
    except Exception as exc:
        res["failed_step"] = step.current or "cycle"  # for failure fingerprints (KEI-918)
        step(None)
        res["error"] = f"{type(exc).__name__}: {exc}"[:2000]
    common.write_json(result_path, res)
    return 0 if res["ok"] else 1
