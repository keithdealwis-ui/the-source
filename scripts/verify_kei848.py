"""Independent acceptance checks for KEI-848 (daily intelligence cycle), run inside a clone.

    python3 scripts/verify_kei848.py <clone-dir> <github-evidence-dir>

Reads committed JSON, YAML, gzip snapshots and git history directly and recomputes what
it can. Deliberately imports nothing from source_pipeline, so a producer bug cannot hide
itself. One line per check; exits non-zero if any check fails.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import jsonschema
import yaml

REPO = Path(sys.argv[1]).resolve()
GH = Path(sys.argv[2]).resolve()
results = []


def check(ac, cid, ok, detail=""):
    results.append((ac, cid, bool(ok), detail))
    print(f"{'ok  ' if ok else 'FAIL'} AC{ac:<2} {cid}: {detail}")


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True).stdout


def jl(rel):
    p = REPO / rel
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.exists() else []


def js(rel):
    return json.loads((REPO / rel).read_text())


runs = sorted((json.loads(p.read_text()) for p in (REPO / "runs").glob("*.json")), key=lambda r: r["started_at"])
daily = [r for r in runs if "timings_s" in r]  # records written by the KEI-848 cycle
ok_daily = [r for r in daily if r["status"] == "succeeded"]
sched_daily = [r for r in ok_daily if r["trigger"] == "schedule"]
strat = lambda r, sid: next((s for s in r["strategies"] if s["id"] == sid), None)  # noqa: E731
print(f"info      {len(runs)} run records, {len(daily)} daily-cycle records, {len(ok_daily)} succeeded, "
      f"{len(sched_daily)} succeeded on schedule")

# --- AC1 a production-like daily scheduled cycle, end to end
sch = yaml.safe_load((REPO / "config" / "schedule.yaml").read_text())
check(1, "cadence_is_daily", sch.get("cadence") == "daily" and float(sch["window_hours"]) + float(sch["catch_up_hours"]) < 24,
      f"cadence={sch.get('cadence')} {sch['time']} {sch['timezone']} window={sch['window_hours']}h catch_up={sch['catch_up_hours']}h")
wf = yaml.safe_load((REPO / ".github" / "workflows" / "daily-cycle.yml").read_text())
on = wf[True] if True in wf else wf["on"]
check(1, "hourly_wakeup_asks_the_config", [c["cron"] for c in on["schedule"]] == ["7 * * * *"] and
      "cycle-due" in json.dumps(wf["jobs"]["check"]), "cron 7 * * * *; the check job runs cycle-due")
gh_runs = json.loads((GH / "daily_cycle_runs.json").read_text())
gh_sched_ok = [r for r in gh_runs if r["event"] == "schedule" and r["conclusion"] == "success"]
tags = set(git("tag", "--list", "cycle/*").split())
for r in sched_daily:
    tag = r["canonical"]["commit_tag"]
    bot = git("log", "-1", "--format=%an", tag).strip() if tag in tags else None
    check(1, f"scheduled_daily_run[{r['run_id']}]", tag in tags and bot == "the-source-cycle" and r.get("history_day"),
          f"unattended, committed by {bot}, tag {tag}, history {r.get('history_day')}, "
          f"{sum(r['timings_s'].values()):.0f}s of steps")
check(1, "scheduled_daily_run_happened", sched_daily and gh_sched_ok,
      f"{len(sched_daily)} succeeded schedule-trigger daily record(s); {len(gh_sched_ok)} successful scheduled workflow runs captured")
keys = sorted({r["cycle_key"] for r in sched_daily})
check(1, "every_strategy_ran_in_a_scheduled_cycle", sched_daily and all(
    {"upstream_lists", "intake_ledger", "light_refresh", "discovery_search", "corroboration", "deep_enrichment",
     "rescoring", "history"} <= {s["id"] for s in r["strategies"]} for r in sched_daily), f"cycle keys {keys}")

# --- AC2 discovery + reconciliation
snap = js("data/live/snapshot.json")["projects"]
pool = {c["key"]: c for c in jl("data/discovery/candidates.jsonl")}
corpus_ids = set(snap) | {v.get("canonical_id") for v in snap.values() if v.get("canonical_id")}
corpus_hids = {(k.split("/", 1)[0], v["host_repo_id"]) for k, v in snap.items() if v.get("host_repo_id")}
for r in ok_daily[-1:]:
    d = strat(r, "discovery_search")
    check(2, f"discovery_lanes_ran[{r['run_id']}]", d and d["lanes"] and d["status"] in ("ok", "degraded"),
          f"{[(l['id'], l['status'], l['returned']) for l in d['lanes']]}; known in corpus {d['known_in_corpus']}, "
          f"new {d['new_count']}, pool {d['pool_size']}")
clash_key = sorted(k for k in pool if k in corpus_ids or any(a in corpus_ids for a in pool[k].get("aliases", [])))
clash_id = sorted(k for k, c in pool.items() if c.get("host_repo_id") and (k.split("/", 1)[0], c["host_repo_id"]) in corpus_hids)
check(2, "pool_is_reconciled_against_corpus", pool and not clash_key and not clash_id,
      f"{len(pool)} candidates; 0 share a key, resolved name or repository id with the {len(snap)}-key corpus "
      f"(key clashes {clash_key[:3]}, id clashes {clash_id[:3]})")
check(2, "pool_carries_provenance", all(c["providers"] and c["first_seen"] and c["status"] == "awaiting_relationship_evidence"
                                        for c in pool.values()), "every candidate names its provider/lane and first_seen")
known_reported = sum(strat(r, "discovery_search")["known_in_corpus"] for r in ok_daily)
check(2, "known_candidates_recognised", known_reported > 0, f"{known_reported} sightings matched to existing corpus entities across daily runs")

# --- AC3 light refresh without unnecessary full re-enrichment
for r in ok_daily:
    lr, de = strat(r, "light_refresh"), strat(r, "deep_enrichment")
    check(3, f"light_vs_deep[{r['run_id']}]",
          lr["repositories"] == r["counts"]["candidates"] and de["selected"] < lr["repositories"] and
          lr["http_calls"] < lr["repositories"] / 5,
          f"light: {lr['repositories']} repos in {lr['http_calls']} calls; deep: {de['selected']} selected "
          f"({de['by_class']}), {de['http_calls']} calls, deferred {de['deferred_rotation']}")
overlay = [k for k, v in snap.items() if v.get("found") and v.get("deep_fetched_at") and v["deep_fetched_at"] < v["fetched_at"]]
check(3, "snapshot_keeps_deep_evidence_under_daily_light_fields", overlay,
      f"{len(overlay)} of {sum(1 for v in snap.values() if v.get('found'))} found projects carry today's light fields "
      "over an earlier deep observation (deep_fetched_at < fetched_at)")

# --- AC4 dated snapshots, retained and queryable
idx = js("data/history/INDEX.json")
days = sorted(idx["days"])
loaded = {}
bad_hash = []
for d in days:
    raw = (REPO / "data" / "history" / idx["days"][d]["file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != idx["days"][d]["sha256"]:
        bad_hash.append(d)
    loaded[d] = {r["key"]: r for r in map(json.loads, gzip.decompress(raw).decode().splitlines())}
check(4, "snapshots_hashed_and_readable", days and not bad_hash and all(len(loaded[d]) == idx["days"][d]["rows"] for d in days),
      f"{len(days)} day(s) {days}; rows {[idx['days'][d]['rows'] for d in days]}; hash mismatches {bad_hash}")
tracked = sorted(git("ls-files", "data/history").split())
check(4, "snapshots_committed", all(f"data/history/{idx['days'][d]['file']}" in tracked for d in days), f"{len(tracked)} tracked files")
if len(days) >= 2:
    a, b = loaded[days[-2]], loaded[days[-1]]
    common_k = [k for k in b if k in a and b[k].get("found") and a[k].get("found")]
    moved = sum(1 for k in common_k if b[k]["stars"] != a[k]["stars"])
    check(4, "delta_computable_between_days", common_k,
          f"{days[-2]} -> {days[-1]}: {len(common_k)} projects comparable, {moved} with a star change")
else:
    check(4, "delta_computable_between_days", False, f"only {len(days)} day(s) held; a delta needs two")
rs = sch["history"]["retain_days"]
check(4, "retention_covers_90_day_deltas", int(rs) >= 90, f"retain_days={rs}")
cli = (REPO / "source_pipeline" / "cli.py").read_text()
check(4, "delta_query_surface", "history-delta" in cli and "history-days" in cli, "history-days / history-delta --days N [--on] [--key]")

# --- AC5 new or materially changed projects trigger deep enrichment and re-scoring
hist = jl("data/scores/history.jsonl")
trig = []
for r in ok_daily:
    de, rsc = strat(r, "deep_enrichment"), strat(r, "rescoring")
    must = de["by_class"].get("new", 0) + de["by_class"].get("changed", 0)
    as_of = strat(r, "light_refresh")["as_of"]
    rescored = rsc["projects"]
    in_hist = [p for p in rescored if any(h["oss_id"] == p and h["kind"] == "health" and h["evaluated_at"] == as_of for h in hist)]
    if must:
        trig.append(r["run_id"])
        check(5, f"triggered_deep_and_rescore[{r['run_id']}]",
              de["selected"] >= must and len(in_hist) == len(rescored),
              f"new+changed {must} -> deep {de['selected']}; reasons {[c['reasons'][0] for c in de['changed'][:4]]}; "
              f"re-scored {len(rescored)}, all in append-only score history at {as_of}")
check(5, "a_trigger_was_exercised_live", trig, f"runs with new/changed projects: {trig}")

# --- AC6 no duplicate canonical entities
canon = jl("data/canonical/oss_projects.jsonl")
ids = [p["oss_id"] for p in canon]
check(6, "canonical_ids_unique", len(ids) == len(set(ids)), f"{len(ids)} canonical projects")
found = {k: v for k, v in snap.items() if v.get("found")}
by_cid = {}
for k, v in found.items():
    by_cid.setdefault(v["canonical_id"], []).append(k)
by_hid = {}
for k, v in found.items():
    by_hid.setdefault((k.split("/", 1)[0], v.get("host_repo_id")), set()).add(v["canonical_id"])
multi_hid = {h: c for h, c in by_hid.items() if h[1] and len(c) > 1}
check(6, "one_identity_per_repository_id", not multi_hid, f"{len(by_hid)} repository ids, each resolving to one canonical id")
canon_by_hid = {}
for p in canon:
    v = snap.get(p["oss_id"]) or next((snap[k] for k in by_cid.get(p["oss_id"], [])), None)
    if v and v.get("host_repo_id"):
        canon_by_hid.setdefault((p["host"], v["host_repo_id"]), []).append(p["oss_id"])
dupes = {h: c for h, c in canon_by_hid.items() if len(c) > 1}
check(6, "no_two_canonical_projects_share_a_repository", not dupes, f"{len(canon_by_hid)} canonical projects keyed by repository id; duplicates {dupes}")
check(6, "pool_ids_unique", len({c['host_repo_id'] for c in pool.values() if c.get('host_repo_id')}) ==
      sum(1 for c in pool.values() if c.get("host_repo_id")), "no two pool candidates share a repository id (aliases merged)")

# --- AC7 run records
schema = js("schema/the-source.canonical.schema.json")
rr = {"$defs": schema["$defs"], **schema["$defs"]["run_record"]}
need = ["discovered", "added", "changed", "unchanged", "failed", "deferred"]
for r in daily:
    errs = sorted(e.message for e in jsonschema.Draft202012Validator(rr).iter_errors(r))
    ok = not errs and r["timings_s"] and "retries" in r and "failures" in r and r["canonical"]["dataset_version_after"]
    if r["status"] == "succeeded":
        ok = ok and all(k in r["counts"] for k in need) and r["canonical"]["commit_tag"] in tags
    else:
        ok = ok and bool(r["error"])
    check(7, f"run_record[{r['run_id']}]", ok,
          f"{r['status']}; {sum(r['timings_s'].values()):.0f}s; counts {({k: r['counts'].get(k) for k in need})}; "
          f"version {r['canonical']['dataset_version_after']}; retries {r['retries']}; failures {len(r['failures'])}"
          + (f"; error {r['error'][:60]}" if r["error"] else "") + (f"; schema {errs[:2]}" if errs else ""))
neg = dict(ok_daily[-1]) if ok_daily else {}
if neg:
    neg["counts"] = {k: v for k, v in neg["counts"].items() if k != "changed"}
    check(7, "schema_rejects_daily_record_without_counts",
          any(True for _ in jsonschema.Draft202012Validator(rr).iter_errors(neg)), "negative control: drop counts.changed -> invalid")

# --- AC8 a failed partial run leaves last known-good readable
failed = [r for r in daily if r["status"] == "failed"]
for r in failed:
    c = git("log", "--format=%H", "-1", "--grep", f"cycle: {r['run_id']} failed").strip()
    files = git("show", "--name-only", "--format=", c).split() if c else []
    check(8, f"failed_run_promoted_nothing[{r['run_id']}]",
          c and files == [f"runs/{r['run_id']}.json"] and not r["canonical"]["changed"] and not r.get("history_day"),
          f"error: {r['error'][:70]}; commit {c[:7]} touches {files}")
check(8, "failure_drilled_at_last_step", any("stopped at history" in (r["error"] or "") for r in failed),
      "drill stopped after canonical build/validation and history, before promotion")
issues = json.loads((GH / "issues.json").read_text())
daily_issues = [i for i in issues if i["title"] == "Daily cycle failing"]
later = failed and any(r["status"] == "succeeded" and r["started_at"] > max(f["ended_at"] for f in failed) for r in daily)
check(8, "failure_surfaced_then_recovered", daily_issues and later and all(i["state"] == "CLOSED" for i in daily_issues),
      f"issues {[(i['number'], i['state']) for i in daily_issues]}; later success {bool(later)}")
man = js("data/canonical/MANIFEST.json")
check(8, "last_known_good_readable", man["dataset_version"] and (REPO / "api" / "v1" / "index.json").exists(),
      f"canonical {man['dataset_version']} with api/v1 intact")

# --- AC9 KEI-807 guarantees: run by verify.sh (canonical-validate, offline rebuild, verify_kei807.py)
check(9, "kei807_verifier_present", (REPO / "scripts" / "verify_kei807.py").exists(), "verify.sh runs it against the same clone")

# --- AC10 / AC11 documented build-vs-ingest decision
doc = (REPO / "docs" / "BUILD-VS-INGEST.md").read_text()
for up in ("GitHub", "GitLab", "Codeberg", "ecosyste.ms", "deps.dev", "OSS Insight", "GH Archive", "Libraries.io",
           "Software Heritage"):
    check(10, f"evaluated[{up}]", up in doc, "named in the decision record")
for topic in ("Trusted for", "Licence", "Limits", "Freshness", "Fallback", "Data-quality risks", "Revisit when"):
    check(10, f"covers[{topic}]", topic.lower() in doc.lower(), "")
check(11, "core_refresh_reason_documented", "Why the core refresh is not ingested" in doc and "Criterion 11" in doc,
      "the one thing built (a batched origin query) is justified against every upstream")
disc = yaml.safe_load((REPO / "config" / "discovery.yaml").read_text())
corr = yaml.safe_load((REPO / "config" / "corroboration.yaml").read_text())
check(11, "upstreams_used_where_they_meet_the_need", {"ecosystems_awesome", "github_search"} <= {l["provider"] for l in disc["lanes"]}
      and {"ecosystems", "depsdev"} <= set(corr["providers"]), "seed lane + two corroborators wired, as decided")

# --- AC12 provider independence
for r in ok_daily:
    c = strat(r, "corroboration")
    eff = {p: v["canonical_effect"] for p, v in c["providers"].items()}
    check(12, f"corroboration_flag_only[{r['run_id']}]", all(e.startswith("none") for e in eff.values()) and
          all(v["status"] in ("ok", "degraded", "skipped") for v in c["providers"].values()),
          "; ".join(f"{p}: {v['status']} coverage {v.get('coverage')} agreement {v['agreement']}" for p, v in c["providers"].items()))
# Canonical values equal the host's own answer, even where a provider disagreed.
mismatch, disagreed = [], 0
cproj = {p["oss_id"]: p for p in canon}
last = ok_daily[-1] if ok_daily else None
if last:
    for pv in strat(last, "corroboration")["providers"].values():
        for k, fields in pv["disagreeing_keys"].items():
            if "stars" not in fields:
                continue
            disagreed += 1
            cid = (snap.get(k) or {}).get("canonical_id")
            if cid in cproj and cproj[cid]["live"]["stars"] != snap[k]["stars"]:
                mismatch.append(k)
check(12, "canonical_follows_host_not_provider", not mismatch,
      f"{disagreed} star disagreements flagged; canonical stars equal the host's in every case ({mismatch[:3]})")
src = (REPO / "source_pipeline" / "corroborate.py").read_text()
check(12, "corroborator_has_no_write_path", not re.search(r"write_json|write_jsonl|write_snapshot|open\([^)]*['\"]w", src),
      "corroborate.py contains no file write")
check(12, "provider_outage_is_visible_not_fatal", "degraded" in src and "degraded_providers" in src,
      "a provider over the error/agreement threshold is marked degraded in the run record; tests prove an outage")

failed_checks = [r for r in results if not r[2]]
print(f"\n{len(results) - len(failed_checks)}/{len(results)} checks passed")
sys.exit(1 if failed_checks else 0)
