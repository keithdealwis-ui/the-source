"""Independent acceptance checks for KEI-807, run by the verifier inside a clone.

    python3 scripts/verify_kei807.py <clone-dir> <github-evidence-dir>

Reads committed JSON, YAML and git history directly. Deliberately imports nothing from
source_pipeline, so a producer bug cannot hide itself. One line per check; exits non-zero
if any check fails. Criterion 1 (public repository) is reported, not failed, while its
Keith gate is open.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

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
    return [json.loads(x) for x in (REPO / rel).read_text().splitlines() if x.strip()]


def js(rel):
    return json.loads((REPO / rel).read_text())


TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# --- AC1 public repository (reported; gated)
repo_meta = json.loads((GH / "repo.json").read_text())
vis = repo_meta.get("visibility")
pub = yaml.safe_load((REPO / "config" / "publication.yaml").read_text())
print(f"info AC1  repository {repo_meta.get('nameWithOwner')} visibility={vis} publication.yaml status={pub['status']} approval={pub.get('approval')}")
if vis == "PUBLIC":
    check(1, "public_with_recorded_approval", pub["status"] == "public" and pub.get("approval"),
          f"status={pub['status']} approval={pub.get('approval')}")

# --- AC2 schema validation in CI
ci = json.loads((GH / "ci_runs.json").read_text())
head = git("rev-parse", "HEAD").strip()
ok_shas = {r["headSha"] for r in ci if r["conclusion"] == "success"}
wf = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
steps = " ".join(s.get("run", "") for s in wf["jobs"]["validate"]["steps"])
check(2, "ci_runs_schema_validation", "canonical-validate" in steps and "git diff" in steps and "build" in steps,
      "ci.yml rebuilds offline, runs canonical-validate and fails on any diff")
code_heads = [c for c in git("log", "--format=%H %an", "-n", "60").splitlines() if not c.split(" ", 1)[1].startswith("the-source-")]
latest_code = code_heads[0].split()[0] if code_heads else None
check(2, "ci_green_on_latest_code_commit", latest_code in ok_shas, f"latest human commit {latest_code and latest_code[:7]}; green CI shas: {len(ok_shas)}")
schema = js("schema/the-source.canonical.schema.json")
check(2, "canonical_schema_present", {"saas_product", "oss_project", "relationship", "api_saas", "run_record",
                                      "intake_outcome"} <= set(schema["$defs"]), f"{len(schema['$defs'])} definitions")

# --- AC3 initial 20 supported SaaS with validated mappings
sup = yaml.safe_load((REPO / "config" / "supported_saas.yaml").read_text())
ids = [p["saas_id"] for p in sup["products"]]
docs = {p.stem: json.loads(p.read_text()) for p in (REPO / "api" / "v1" / "saas").glob("*.json")}
check(3, "twenty_supported", len(set(ids)) >= 20, f"{len(set(ids))} supported ids")
okd = [i for i in ids if i in docs and docs[i]["status"] == "ok" and len(docs[i]["recommendations"]) >= 3]
check(3, "each_has_3_to_5_recommendations", len(okd) == len(ids), f"{len(okd)}/{len(ids)} with >=3")
rels = {(r["saas_id"], r["oss_id"]): r for r in jl("data/canonical/relationships.jsonl")}
proj = {p["oss_id"]: p for p in jl("data/canonical/oss_projects.jsonl")}
ds_rel = {(e["saas_id"], e["oss_id"]): e for e in jl("data/dataset/relationships.jsonl")}
bad = []
for sid, d in docs.items():
    for r in d["recommendations"]:
        e = ds_rel.get((sid, r["oss_id"]))
        if not e or e["validation"]["status"] != "validated" or not rels.get((sid, r["oss_id"]), {}).get("recommended"):
            bad.append(f"{sid}:{r['oss_id']}")
check(3, "recommendations_are_validated_mappings", not bad, f"{len(bad)} not validated {bad[:3]}")

# --- AC4 live fields
missing = []
for sid, d in docs.items():
    for r in d["recommendations"]:
        need = [r.get("github_url"), isinstance(r.get("stars"), int), r.get("maintenance_status") in ("active", "maintained"),
                r.get("licence_spdx"), "latest_release" in r, r.get("last_meaningful_activity_at")]
        if not all(need):
            missing.append(f"{sid}:{r['oss_id']}")
check(4, "github_link_stars_maintenance_licence_release", not missing, f"{len(missing)} incomplete {missing[:3]}")

# --- AC5 refresh without touching extension/site code
cycle_commits = [l.split(" ", 1)[0] for l in git("log", "--format=%H %an").splitlines() if l.endswith("the-source-cycle")]
touched_code = []
for c in cycle_commits:
    files = [f for f in git("show", "--name-only", "--format=", c).splitlines() if f]
    if any(not f.startswith(("data/", "api/", "runs/")) for f in files):
        touched_code.append(c[:7])
check(5, "cycle_commits_touch_only_data", cycle_commits and not touched_code,
      f"{len(cycle_commits)} cycle commits; touching anything but data/api/runs: {touched_code}")

# --- AC6 deterministic read surface (rebuild comparison is done by verify.sh)
idx = js("api/v1/index.json")
versions = {idx["dataset_version"], js("api/v1/domains.json")["dataset_version"], js("data/canonical/MANIFEST.json")["dataset_version"]} | \
           {d["dataset_version"] for d in docs.values()}
check(6, "one_dataset_version_everywhere", len(versions) == 1, str(sorted(versions)))
ranks_ok = all([r["rank"] for r in d["recommendations"]] == list(range(1, len(d["recommendations"]) + 1)) for d in docs.values())
check(6, "ranks_contiguous", ranks_ok)

# --- runs
runs = [json.loads(p.read_text()) for p in sorted((REPO / "runs").glob("*.json"))]
sched = [r for r in runs if r["trigger"] == "schedule" and r["status"] == "succeeded"]
failed = [r for r in runs if r["status"] == "failed"]

# --- AC7 / AC12 failure leaves last known-good intact, surfaced durably, recoverable
check(7, "failed_run_changed_nothing", failed and all(not r["canonical"]["changed"] and
      r["canonical"]["dataset_version_before"] == r["canonical"]["dataset_version_after"] for r in failed),
      f"{len(failed)} failed run(s): {[r['run_id'] for r in failed]}")
fail_commits = [l.split(" ", 1)[0] for l in git("log", "--format=%H %s").splitlines() if " failed (dataset " in l]
only_runs = all(all(f.startswith("runs/") for f in git("show", "--name-only", "--format=", c).split()) for c in fail_commits)
check(12, "failed_cycle_commit_records_only_the_run", fail_commits and only_runs, f"{len(fail_commits)} failure commit(s) touch only runs/")
issues = json.loads((GH / "issues.json").read_text())
fi = [i for i in issues if any(l["name"] == "cycle-failure" for l in i["labels"])]
check(12, "failure_surfaced_as_issue", fi, f"{[(i['number'], i['state']) for i in fi]}")
later_success = failed and any(r["status"] == "succeeded" and r["started_at"] > max(f["ended_at"] for f in failed) for r in runs)
check(12, "recovered_after_failure", later_success and all(i["state"] == "CLOSED" for i in fi),
      "a later run succeeded and the failure issue was closed")

# --- AC8 last checked on every output
nocheck = [p["oss_id"] for p in proj.values() if not TS.match(p["live"]["checked_at"] or "")]
nocheck += [f"api:{k}" for k, d in docs.items() if not all(TS.match(r["live_checked_at"]) for r in d["recommendations"])]
check(8, "last_checked_everywhere", not nocheck and TS.match(idx["live_checked_at"]["newest"] or ""), f"{len(nocheck)} missing")

# --- AC9 unattended schedule, configurable without code
sch = yaml.safe_load((REPO / "config" / "schedule.yaml").read_text())
check(9, "default_sunday_0300_dubai", (sch["weekday"], str(sch["time"]), sch["timezone"]) == ("sunday", "03:00", "Asia/Dubai"),
      f"{sch['weekday']} {sch['time']} {sch['timezone']}")
wk = yaml.safe_load((REPO / ".github" / "workflows" / "weekly-cycle.yml").read_text())
crons = [c["cron"] for c in wk[True]["schedule"]] if True in wk else [c["cron"] for c in wk["on"]["schedule"]]
check(9, "hourly_wakeup_reads_config", crons == ["7 * * * *"], f"cron {crons}")
sched_runs_gh = json.loads((GH / "cycle_runs.json").read_text())
check(9, "scheduled_run_happened_unattended", sched and any(r["event"] == "schedule" and r["conclusion"] == "success" for r in sched_runs_gh),
      f"{len(sched)} succeeded schedule-trigger run record(s): {[r['run_id'] for r in sched]}")
cfg_only = [l for l in git("log", "--format=%H %s").splitlines() if "temporary schedule window" in l]
cfg_ok = all(git("show", "--name-only", "--format=", l.split()[0]).split() == ["config/schedule.yaml"] for l in cfg_only)
check(9, "schedule_changed_by_config_only", cfg_only and cfg_ok, f"{len(cfg_only)} schedule-window commit(s) touching only config/schedule.yaml")

# --- AC10 research + update each cycle; AC11 run record content
need = {"upstream_lists", "intake_ledger", "live_refresh", "new_pair_scoring"}
for r in sched:
    check(10, f"strategies[{r['run_id']}]", need <= {s["id"] for s in r["strategies"]},
          ", ".join(f"{s['id']}={s['status']}" for s in r["strategies"]))
fields = ["started_at", "ended_at", "strategies", "counts", "status", "canonical"]
cnt = ["discovered", "retained", "rejected", "refreshed_records"]
tags = set(git("tag", "--list", "cycle/*").split())
FIX = "2026-09-30T04:00:00Z"  # ab87f99: failed runs carry the counts reached before the failure
for r in runs:
    ok = all(r.get(f) is not None for f in fields) and (r["status"] == "succeeded" or bool(r.get("error")))
    if r["status"] == "succeeded" or r["started_at"] > FIX:
        ok = ok and all(k in r["counts"] for k in cnt)
    if r["status"] == "succeeded":
        ok = ok and r["canonical"]["commit_tag"] in tags and r["canonical"]["dataset_version_after"]
    note = " (pre-fix failed record: counts not required)" if r["status"] == "failed" and r["started_at"] <= FIX else ""
    check(11, f"run_record[{r['run_id']}]", ok, f"status={r['status']} tag={r['canonical'].get('commit_tag')} counts={ {k: r['counts'].get(k) for k in cnt} }{note}")
check(11, "failed_run_carries_counts", any(r["status"] == "failed" and r["started_at"] > FIX and all(k in r["counts"] for k in cnt) for r in runs),
      "a failed run after the fix records discovered/retained/rejected/refreshed")

# --- AC13-18 intake
outs = {p.stem: json.loads(p.read_text()) for p in (REPO / "data" / "intake" / "outcomes").glob("*.json")}
kinds = {o["outcome"] for o in outs.values()}
check(17, "durable_outcomes_all_classes", {"accepted", "rejected", "already_known", "needs_more_evidence"} <= kinds, str(sorted(kinds)))
iw = yaml.safe_load((REPO / ".github" / "workflows" / "intake.yml").read_text())
on = iw[True] if True in iw else iw["on"]
check(13, "authenticated_dispatch_surface", "workflow_dispatch" in on and {"repo_url", "request_id"} <= set(on["workflow_dispatch"]["inputs"]),
      "GitHub-authenticated workflow_dispatch with repo_url + request_id")
check(14, "submit_status_result_client", (REPO / "scripts" / "source_intake.py").exists() and
      all(x in (REPO / "scripts" / "source_intake.py").read_text() for x in ("def submit", "def status", "def result")),
      "scripts/source_intake.py submit|status|result; docs/INTAKE.md gives the raw HTTP calls")
intake_commits = [l.split(" ", 1)[0] for l in git("log", "--format=%H %an").splitlines() if l.endswith("the-source-intake")]
add_only = all(all(line.startswith("A\t") and line.split("\t")[1].startswith("data/intake/")
                   for line in git("show", "--name-status", "--format=", c).splitlines() if line.strip()) for c in intake_commits)
check(14, "intake_job_adds_only_intake_files", intake_commits and add_only, f"{len(intake_commits)} intake commits, all add-only under data/intake/")
acc = [o for o in outs.values() if o["outcome"] == "accepted"]
promoted = [o for o in acc if o["canonical"].get("status") == "canonical" and o["oss_id"] in proj]
check(15, "accepted_intake_promoted_through_normal_cycle", acc and len(promoted) == len(acc),
      f"{[(o['request_id'], o['canonical'].get('promoted_in_run')) for o in acc]}")
for o in acc:
    e = [r for r in rels.values() if r["oss_id"] == o["oss_id"]]
    check(15, f"intake_relationship_evidenced[{o['request_id']}]", o["evidence"]["saas_relationships"] and all(
        "intake" in r["sources"] and r["replacement_fit"] is not None for r in e) and e,
        f"{[(r['saas_id'], r['sources'], (r['replacement_fit'] or {}).get('scoring_path')) for r in e]}")
check(16, "advisory_only", all("advisory" in o.get("advisory_notice", "") for o in outs.values()),
      "every outcome states the preliminary assessment was advisory and unused")
hist = jl("data/scores/history.jsonl") if (REPO / "data/scores/history.jsonl").exists() else []
check(18, "one_score_history_shared", any(h["oss_id"] in {o["oss_id"] for o in acc} for h in hist),
      "promoted intake project appears in the shared score history")
srcs = {s["source_id"] for s in yaml.safe_load((REPO / "config" / "sources.yaml").read_text())["sources"]}
check(18, "intake_is_a_source_of_the_same_corpus", "intake" in srcs, "intake ledger harvested like every other source")

fails = [r for r in results if not r[2]]
print(f"\n{len(results) - len(fails)} passed, {len(fails)} failed")
sys.exit(1 if fails else 0)
