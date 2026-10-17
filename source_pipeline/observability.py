"""Cycle observability (KEI-918, docs/OBSERVABILITY.md): what each daily cycle took in, and what went wrong.

    python -m source_pipeline obs-intake-report [--run RUN_ID] [--write]   S0.1: the intake funnel of one run
    python -m source_pipeline obs-alerts [--run RUN_ID] [--now TS]         S0.2: failure fingerprints and alerts

S0.1 The intake report follows one cycle's material from the upstream lists to the canonical
layer, stage by stage: how much came in, how much went on, and why the rest stopped. It is a
deterministic function of the files the run left (data/dataset, data/canonical, data/intake,
data/staging) and its run record, and is written to runs/reports/<run_id>.intake.json beside
the record. Each report also carries its change from the previous run's report.

S0.2 Every failure the cycle records (a failed run, a per-repository failure it survived, a
degraded strategy) gets a fingerprint: a hash of the step and the message with the volatile
parts (numbers, hashes, timestamps, repository keys, quoted lists) masked. The same fault on
another day has the same fingerprint, so an alert names whether a fault is new, recurring or
back, and the workflow keeps one issue per fingerprint instead of one issue for everything.
Alert rules and thresholds live in config/observability.yaml.

Reads only public, committed files; writes only runs/reports/. No network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import common

REPORT_SCHEMA = "the-source.intake-report/1"
ALERTS_SCHEMA = "the-source.cycle-alerts/1"
CONFIG = common.CONFIG / "observability.yaml"
TOP = 10


def _runs_dir(root: Path) -> Path:
    return root / "runs"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(value) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def load_config() -> dict:
    return common.load_yaml(CONFIG)


def records(runs_dir: Path) -> list[dict]:
    """Every run record, oldest first (by start, then file name)."""
    out = []
    for p in sorted(runs_dir.glob("*.json")) if runs_dir.is_dir() else []:
        rec = common.read_json(p)
        if isinstance(rec, dict) and rec.get("run_id"):
            out.append(rec)
    return sorted(out, key=lambda r: (r.get("started_at") or "", r["run_id"]))


def _pick(recs: list[dict], run_id: str | None) -> dict:
    if not recs:
        raise ValueError("no run records")
    if run_id is None:
        return max(recs, key=lambda r: (r.get("ended_at") or r.get("started_at") or "", r["run_id"]))
    for r in recs:
        if r["run_id"] == run_id:
            return r
    raise ValueError(f"no run record {run_id!r}")


# ------------------------------------------------------------------ S0.2 fingerprints

_MASKS = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}(t\d{2}:\d{2}(:\d{2})?(\.\d+)?z?)?\b"), "<ts>"),
    (re.compile(r"https?://\S+"), "<url>"),
    (re.compile(r"\b(github\.com|gitlab\.com|codeberg\.org)/[\w.-]+(/[\w.-]+)?"), "<repo>"),
    (re.compile(r"\b[0-9a-f]{7,64}\b"), "<hex>"),
    (re.compile(r"\[[^\[\]]*\]"), "<list>"),
    (re.compile(r"\b\d+(\.\d+)?\b"), "<n>"),
    (re.compile(r"\s+"), " "),
]


def signature(message: str) -> str:
    """The stable part of a failure message: lower case, exception-class prefixes collapsed, volatile parts masked."""
    s = (message or "").strip().lower()
    s = re.sub(r"^(\w+(error|exception): )+", lambda m: m.group(0).split(": ")[0] + ": ", s)
    for pat, rep in _MASKS:
        s = pat.sub(rep, s)
    return s.strip()[:300]


def fingerprint(step: str, message: str) -> str:
    return hashlib.sha256(f"{step}\0{signature(message)}".encode()).hexdigest()[:12]


_STEP_HINTS = [  # for records written before the cycle recorded failed_step (KEI-918)
    (re.compile(r"failure drill: stopped at (\w+)"), None),
    (re.compile(r"could not be refreshed"), "light_refresh"),
    (re.compile(r"host lookups failed during live verification"), "build_validate"),
    (re.compile(r"could not be deep-enriched"), "deep_enrichment"),
    (re.compile(r"dataset validation failed"), "build_validate"),
    (re.compile(r"canonical validation failed"), "canonical"),
    (re.compile(r"momentum validation failed"), "momentum"),
]


def failed_step(rec: dict) -> str:
    if rec.get("failed_step"):
        return rec["failed_step"]
    err = rec.get("error") or ""
    for pat, step in _STEP_HINTS:
        m = pat.search(err)
        if m:
            return step or m.group(1)
    return "cycle"


def signals(rec: dict) -> list[dict]:
    """Every failure signal in one run record, fingerprinted. kind: run_failed | survived | degraded."""
    out = []
    if rec.get("status") == "failed":
        step = failed_step(rec)
        out.append({"kind": "run_failed", "step": step, "message": (rec.get("error") or "")[:500]})
    for f in rec.get("failures") or []:
        out.append({"kind": "survived", "step": f.get("step") or "cycle",
                    "message": f"{f.get('outcome') or ''} ({f.get('key') or ''})", "key": f.get("key")})
    for s in rec.get("strategies") or []:
        if s.get("status") in ("ok", None):
            continue
        detail = (s.get("errors") or s.get("still_failing") or s.get("kept") or s.get("degraded_providers")
                  or s.get("kept_previous_after_failure") or [])
        first = detail[0] if isinstance(detail, list) and detail else detail
        if isinstance(first, dict):
            first = " ".join(f"{k}={v}" for k, v in sorted(first.items()) if k != "detail")
        out.append({"kind": "degraded", "step": s.get("id") or "cycle",
                    "message": f"{s.get('status')}: {first or ''}"})
    seen, unique = set(), []
    for x in out:
        x["fingerprint"] = fingerprint(x["step"], x["message"])
        x["signature"] = signature(x["message"])
        if (x["kind"], x["fingerprint"]) not in seen:  # one signal per fault per run, however many keys hit it
            seen.add((x["kind"], x["fingerprint"]))
            x["occurrences_in_run"] = sum(1 for y in out if y["kind"] == x["kind"]
                                          and fingerprint(y["step"], y["message"]) == x["fingerprint"])
            unique.append(x)
    return unique


def fingerprint_history(recs: list[dict]) -> dict:
    """fingerprint -> first/last run, runs it appeared in, kinds, step and signature. recs oldest first."""
    hist = {}
    for r in recs:
        for s in signals(r):
            h = hist.setdefault(s["fingerprint"], {"fingerprint": s["fingerprint"], "step": s["step"],
                                                   "signature": s["signature"], "kinds": [], "runs": []})
            if s["kind"] not in h["kinds"]:
                h["kinds"].append(s["kind"])
            if r["run_id"] not in h["runs"]:
                h["runs"].append(r["run_id"])
    for h in hist.values():
        h["first_seen"], h["last_seen"], h["occurrences"] = h["runs"][0], h["runs"][-1], len(h["runs"])
    return hist


def _consecutive(recs: list[dict], upto: int, fp: str) -> int:
    n = 0
    for r in reversed(recs[:upto + 1]):
        if any(s["fingerprint"] == fp for s in signals(r)):
            n += 1
        else:
            break
    return n


def alerts(runs_dir: Path, run_id: str | None = None, now: datetime | None = None, cfg: dict | None = None) -> dict:
    """The alerts one run raises (default: the newest), judged against every run before it."""
    cfg = (cfg or load_config())["alerts"]
    recs = records(runs_dir)
    rec = _pick(recs, run_id)
    i = next(n for n, r in enumerate(recs) if r["run_id"] == rec["run_id"])
    prior = fingerprint_history(recs[:i])
    out = []

    def add(rule, severity, title, detail, fp=None, **extra):
        out.append({"rule": rule, "severity": severity, "fingerprint": fp or fingerprint(rule, title),
                    "title": title, "detail": detail, **extra})

    for s in signals(rec):
        before = prior.get(s["fingerprint"])
        consecutive = _consecutive(recs, i, s["fingerprint"])
        seen = {"seen_before": before["occurrences"] if before else 0,
                "first_seen": before["first_seen"] if before else rec["run_id"], "consecutive_runs": consecutive,
                "step": s["step"], "signature": s["signature"]}
        if s["kind"] == "run_failed":
            add("cycle_failed", "critical", f"Daily cycle failed at {s['step']}",
                f"{rec['run_id']}: {s['message'][:300]}. Nothing was promoted; the last good dataset is served.",
                s["fingerprint"], status="new" if not before else "recurring", **seen)
        elif not before:
            add("new_failure_fingerprint", "warning", f"New {s['kind']} fault at {s['step']}",
                f"{rec['run_id']}: {s['message'][:300]} ({s['occurrences_in_run']} in this run)",
                s["fingerprint"], status="new", **seen)
        elif consecutive >= int(cfg["recurring_after_runs"]):
            add("recurring_fault", "warning", f"Recurring {s['kind']} fault at {s['step']}",
                f"{rec['run_id']}: seen in {consecutive} consecutive runs: {s['message'][:300]}",
                s["fingerprint"], status="recurring", **seen)

    # no successful run for too long
    good = [r for r in recs[:i + 1] if r.get("status") == "succeeded"]
    ref = now or _ts(rec.get("ended_at") or rec.get("started_at")) or datetime.now(timezone.utc)
    last_good = _ts(good[-1].get("ended_at")) if good else None
    hours = None if last_good is None else round((ref - last_good).total_seconds() / 3600, 1)
    if hours is None or hours > float(cfg["no_success_after_hours"]):
        add("no_recent_success", "critical", "No successful cycle recently",
            f"newest success {good[-1]['run_id'] if good else 'none'} ended {hours} h before {_iso(ref)} "
            f"(limit {cfg['no_success_after_hours']} h)", fingerprint("no_recent_success", "stale"),
            hours_since_success=hours)

    # the canonical layer shrinking sharply in one successful run
    if rec.get("status") == "succeeded":
        prev = next((r for r in reversed(recs[:i]) if r.get("status") == "succeeded"), None)
        if prev:
            for key in cfg["shrink"]["counts"]:
                a, b = (prev.get("counts") or {}).get(key), (rec.get("counts") or {}).get(key)
                if not isinstance(a, int) or not isinstance(b, int) or b >= a:
                    continue
                drop = a - b
                if drop >= int(cfg["shrink"]["min_items"]) and drop / a >= float(cfg["shrink"]["min_fraction"]):
                    add("canonical_shrink", "warning", f"Canonical {key} fell {a} -> {b}",
                        f"{prev['run_id']} -> {rec['run_id']}: -{drop} ({drop / a:.1%}); "
                        "see `obs-lineage reconcile` for the per-item cause", fingerprint("canonical_shrink", key),
                        before=a, after=b)
    sev = {"critical": 0, "warning": 1}
    out.sort(key=lambda a: (sev[a["severity"]], a["rule"], a["fingerprint"]))
    return {"schema": ALERTS_SCHEMA, "run_id": rec["run_id"], "run_status": rec.get("status"),
            "evaluated_at": _iso(ref), "alerts": out, "ok": not out,
            "run_fingerprints": sorted({s["fingerprint"] for s in signals(rec)}),
            "rules": {k: cfg[k] for k in sorted(cfg)}}


# ------------------------------------------------------------------ S0.1 intake report

def _counter(values) -> dict:
    return dict(sorted(Counter(values).items(), key=lambda kv: (-kv[1], kv[0])))


def _top(d: dict, n=TOP) -> dict:
    return dict(list(d.items())[:n])


def _stage(stage_id, title, entered, passed, stopped=None, detail=None, note=None):
    st = {"id": stage_id, "title": title, "in": entered, "out": passed,
          "stopped": None if entered is None or passed is None else entered - passed}
    if stopped is not None:  # a list, so the ranking survives sorted-key JSON
        st["stopped_by_reason"] = [{"reason": k, "count": v} for k, v in _top(stopped).items()]
    if detail is not None:
        st["detail"] = detail
    if note is not None:
        st["note"] = note
    return st


def intake_report(rec: dict, root: Path | None = None) -> dict:
    """The intake funnel of one run, from the files it left in root (default: the repository).

    A failed run promoted nothing, so its funnel is the last good one; the report says so."""
    root = Path(root or common.ROOT)
    ds, canon = root / "data" / "dataset", root / "data" / "canonical"
    summary = common.read_json(ds / "summary.json")
    manifest = common.read_json(canon / "MANIFEST.json")
    strategies = {s.get("id"): s for s in rec.get("strategies") or []}
    counts = rec.get("counts") or {}
    rel = common.read_jsonl(ds / "relationships.jsonl")
    oss = common.read_jsonl(ds / "oss_projects.jsonl")
    rejections = common.read_jsonl(ds / "rejections.jsonl")
    by_stage = {}
    for r in rejections:
        for reason in str(r.get("reason") or "").split(";"):
            by_stage.setdefault(r.get("stage"), []).append(reason.strip())
    rec_sum, harvest = summary.get("reconciled") or {}, summary.get("harvest") or {}
    verified = summary.get("verified") or {}
    launch = summary.get("launch") or {}
    guard = manifest.get("export_guard") or {}
    mcounts = manifest.get("counts") or {}

    upstream = strategies.get("upstream_lists") or {}
    sources = {sid: {k: v.get(k) for k in ("decision", "pinned_commit", "entries", "entries_with_saas_claim",
                                          "relationship_claims", "unparseable_files")}
               for sid, v in sorted((harvest.get("by_source") or {}).items())}
    for d in upstream.get("detail") or []:
        if d.get("source_id") in sources:
            sources[d["source_id"]]["pin"] = d.get("status")
    claims_read = harvest.get("relationship_claims_read")
    rel_lanes = _counter(e["validation"]["status"] for e in rel)
    rel_reasons = _counter(x for e in rel if e["validation"]["status"] != "validated"
                           for x in e["validation"].get("reasons") or [])
    lanes = _counter(p["derived"]["lane"] for p in oss)
    light = strategies.get("light_refresh") or {}
    disc = strategies.get("discovery_search") or {}
    radar = strategies.get("community_radar") or {}

    outcomes_dir = root / "data" / "intake" / "outcomes"
    outcomes = [common.read_json(p) for p in sorted(outcomes_dir.glob("*.json"))] if outcomes_dir.is_dir() else []
    admitted = len(list((root / "data" / "intake" / "admitted").glob("*.json"))) \
        if (root / "data" / "intake" / "admitted").is_dir() else 0

    stages = [
        _stage("upstream_lists", "Upstream lists harvested at their reviewed pins", len(sources),
               sum(1 for s in sources.values() if (s.get("entries") or 0) > 0),
               detail={"sources": sources, "pins_advanced": upstream.get("advanced"),
                       "pins_kept": upstream.get("kept"), "entries_read": harvest.get("entries_read")}),
        _stage("claims", "Relationship claims reconciled to products and repositories", claims_read,
               rec_sum.get("relationship_candidates"), _counter(by_stage.get("normalise", [])),
               detail={"saas_products_named": rec_sum.get("saas_products_named"),
                       "repositories_before_identity_merge": rec_sum.get("oss_candidates_before_identity_merge"),
                       "renamed_or_duplicate_merged": rec_sum.get("renamed_or_duplicate_repositories_merged")},
               note="in counts every list's claim; the same pairing claimed by several lists is one relationship "
                    "candidate, so most of stopped is merging, and stopped_by_reason lists the claims refused"),
        _stage("live_verification", "Repositories verified live on their host", counts.get("candidates"),
               light.get("found", counts.get("refreshed_records")),
               detail={"http_calls": light.get("http_calls"), "retries": light.get("retries"),
                       "still_failing": light.get("still_failing")}),
        _stage("eligibility", "Repositories meeting the quality bar (licence, activity, not archived)",
               rec_sum.get("oss_candidates"), lanes.get("recommendable", 0),
               _counter(by_stage.get("eligibility", [])),
               detail={"lanes": lanes, "maintenance": verified.get("maintenance_status")}),
        _stage("relationship_validation", "Relationships validated (provenance, catalogue, live, plausibility)",
               len(rel), rel_lanes.get("validated", 0), rel_reasons,
               detail={"by_status": rel_lanes, "plausibility_rejected": len(by_stage.get("plausibility_review", [])),
                       "saas_with_validated_alternatives": verified.get("saas_with_validated_alternatives")},
               note="a relationship can stop for several reasons, so the reasons add up to more than stopped"),
        _stage("launch_selection", "Launch selection (SaaS floor, OSS minimum, edges cap)",
               rel_lanes.get("validated", 0), launch.get("relationships"),
               detail={k: launch.get(k) for k in ("saas_products", "saas_added_beyond_floor", "cap_per_saas",
                                                   "oss_projects", "oss_distinct_products", "targets",
                                                   "saas_target_is")},
               note="stopped are validated relationships outside the launch set: below the SaaS cut-off, past the "
                    "per-product cap, or a second repository of the same product (docs/OBSERVABILITY.md, lineage)"),
        _stage("export_guard", "Licence export guard (share-alike-only claims withheld)", launch.get("relationships"),
               guard.get("relationships_exported"),
               {"share_alike_only": guard.get("relationships_withheld_share_alike_only") or 0}),
        _stage("canonical", "Canonical layer and read surface", guard.get("relationships_exported"),
               mcounts.get("relationships"),
               detail={"counts": mcounts, "dataset_version": manifest.get("dataset_version"),
                       "recommended_relationships": mcounts.get("recommended_relationships")}),
    ]
    by_outcome = _counter(o.get("outcome") for o in outcomes)
    manual = {"submissions": len(outcomes), "by_outcome": by_outcome,
              "by_canonical_status": _counter((o.get("canonical") or {}).get("status") for o in outcomes),
              "admitted_to_ledger": admitted, "promoted_this_run": counts.get("intake_promoted"),
              "pending_promotion": counts.get("intake_pending")}
    discovery = {"search_returned": disc.get("returned"), "known_in_corpus": disc.get("known_in_corpus"),
                 "new_candidates": disc.get("new_count"), "pool_size": disc.get("pool_size"),
                 "radar_ranked": counts.get("radar_ranked"), "radar_pool_added": counts.get("radar_pool_added"),
                 "note": "candidates join the discovery pool; none becomes canonical without a licence-cleared claim"}
    return {"schema": REPORT_SCHEMA, "run_id": rec["run_id"], "cycle_key": rec.get("cycle_key"),
            "status": rec.get("status"), "trigger": rec.get("trigger"),
            "started_at": rec.get("started_at"), "ended_at": rec.get("ended_at"),
            "promoted": rec.get("status") == "succeeded",
            "reflects": ("this run's promoted output" if rec.get("status") == "succeeded" else
                         "the last good dataset: this run promoted nothing"),
            "dataset_version": manifest.get("dataset_version"), "data_as_of": summary.get("as_of"),
            "stages": stages, "manual_intake": manual, "discovery": discovery,
            "run_counts": {k: counts[k] for k in sorted(counts)}}


def _flat(report: dict) -> dict:
    out = {}
    for st in report["stages"]:
        for k in ("in", "out", "stopped"):
            if isinstance(st.get(k), int):
                out[f"{st['id']}.{k}"] = st[k]
    for k, v in (report["stages"][-1].get("detail") or {}).get("counts", {}).items():
        out[f"canonical.counts.{k}"] = v
    return out


def with_delta(report: dict, previous: dict | None) -> dict:
    if previous is None:
        return {**report, "change_from_previous": None}
    a, b = _flat(previous), _flat(report)
    change = {k: b[k] - a[k] for k in sorted(set(a) & set(b)) if b[k] != a[k]}
    return {**report, "change_from_previous": {"run_id": previous["run_id"], "changes": change}}


def report_path(runs_dir: Path, run_id: str) -> Path:
    return runs_dir / "reports" / f"{run_id}.intake.json"


def write_intake_report(rec: dict, root: Path | None = None) -> Path:
    """Write runs/reports/<run_id>.intake.json, with the change from the newest earlier report."""
    root = Path(root or common.ROOT)
    rd = _runs_dir(root)
    rep = intake_report(rec, root)
    earlier = sorted(p for p in (rd / "reports").glob("*.intake.json")
                     if p.name != f"{rec['run_id']}.intake.json") if (rd / "reports").is_dir() else []
    prev = None
    for p in earlier:
        cand = common.read_json(p)
        if (cand.get("started_at") or "") < (rec.get("started_at") or "") and cand.get("promoted") and \
                (prev is None or cand["started_at"] > prev["started_at"]):
            prev = cand
    path = report_path(rd, rec["run_id"])
    common.write_json(path, with_delta(rep, prev))
    return path


# ------------------------------------------------------------------ CLI

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="source_pipeline " + argv[0], description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", help="run id (default: the newest record)")
    ap.add_argument("--runs-dir", help="cycle records (default: runs/)")
    ap.add_argument("--now", help="obs-alerts: judge staleness at this UTC time (default: the run's end)")
    ap.add_argument("--write", action="store_true", help="obs-intake-report: write runs/reports/<run_id>.intake.json")
    ap.add_argument("--fail-on", choices=["never", "warning", "critical"], default="never",
                    help="obs-alerts: exit 1 when an alert of at least this severity is raised")
    a = ap.parse_args(argv[1:])
    runs_dir = Path(a.runs_dir) if a.runs_dir else _runs_dir(common.ROOT)
    try:
        if argv[0] == "obs-intake-report":
            rec = _pick(records(runs_dir), a.run)
            if a.write:
                path = write_intake_report(rec)
                out = common.read_json(path)
                print(f"wrote {path.relative_to(common.ROOT)}", file=sys.stderr)
            else:
                out = intake_report(rec)
        elif argv[0] == "obs-alerts":
            now = _ts(a.now) if a.now else None
            if a.now and now is None:
                ap.error("--now must be YYYY-MM-DDTHH:MM:SSZ")
            out = alerts(runs_dir, a.run, now)
        elif argv[0] == "obs-fingerprints":
            out = {"fingerprints": sorted(fingerprint_history(records(runs_dir)).values(),
                                          key=lambda h: (h["first_seen"], h["fingerprint"]))}
        else:
            ap.error(f"unknown command {argv[0]}")
    except ValueError as exc:
        print(f"{argv[0]}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, sort_keys=True, ensure_ascii=False))
    if argv[0] == "obs-alerts" and a.fail_on != "never":
        levels = {"warning": ("warning", "critical"), "critical": ("critical",)}[a.fail_on]
        return 1 if any(x["severity"] in levels for x in out["alerts"]) else 0
    return 0
