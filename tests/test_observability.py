"""KEI-918 WP1 observability: intake reports (S0.1), failure fingerprints and alerts (S0.2), last_run in
source_dataset_info (S0.3), golden queries (S0.4), privacy-preserving telemetry and weekly reports (S0.5),
reproducible lineage and the 118 -> 102 catalogue reconciliation (S0.6)."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from source_pipeline import common, golden, lineage, observability as O, telemetry_report as T
from source_pipeline import mcp_http as H
from source_pipeline import mcp_server as M

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
NOW = M.datetime(2026, 10, 17, 6, tzinfo=M.timezone.utc)
CFG = common.load_yaml(ROOT / "config" / "observability.yaml")


def _rec(run_id, started, status="succeeded", error=None, failures=(), strategies=(), counts=None, after="v1",
         failed_step=None):
    r = {"run_id": run_id, "cycle_key": run_id[:10], "attempt": 1, "trigger": "schedule", "started_at": started,
         "ended_at": started, "status": status, "error": error, "failures": list(failures),
         "strategies": list(strategies), "counts": counts or {},
         "canonical": {"dataset_version_before": "v0", "dataset_version_after": after, "changed": True,
                       "commit_tag": None}}
    if failed_step:
        r["failed_step"] = failed_step
    return r


def _write_runs(d: Path, recs):
    d.mkdir(parents=True, exist_ok=True)
    for r in recs:
        (d / f"{r['run_id']}.json").write_text(json.dumps(r))
    return d


# ------------------------------------------------------------------ S0.2 fingerprints and alerts

def test_same_fault_on_another_day_has_the_same_fingerprint():
    a = "RuntimeError: 4 host lookups failed (e.g. ['403 https://gitlab.com/api/v4/projects/x%2Fy/commits'])"
    b = "RuntimeError: 7 host lookups failed (e.g. ['429 https://gitlab.com/api/v4/projects/z%2Fw/commits'])"
    assert O.fingerprint("build_validate", a) == O.fingerprint("build_validate", b)
    assert O.fingerprint("build_validate", a) != O.fingerprint("light_refresh", a)
    assert O.fingerprint("x", "CycleError: CycleError: boom") == O.fingerprint("x", "CycleError: boom")
    assert O.fingerprint("x", "kept previous (github.com/a/b) 2026-10-10T01:02:03Z") == \
        O.fingerprint("x", "kept previous (github.com/c/d) 2026-10-11T09:09:09Z")
    assert O.fingerprint("x", "dataset validation failed") != O.fingerprint("x", "canonical validation failed")


def test_real_history_groups_repeat_failures():
    hist = O.fingerprint_history(O.records(RUNS))
    by_runs = {tuple(h["runs"]): h for h in hist.values()}
    gitlab = by_runs[("manual-20261010T124109Z-manual-a1", "manual-20261010T132630Z-manual-a1")]
    assert gitlab["occurrences"] == 2
    drills = [h for h in hist.values() if "failure drill" in h["signature"]]
    assert {h["step"] for h in drills} == {"canonical", "history"}
    assert next(h for h in drills if h["step"] == "canonical")["occurrences"] == 2


def test_failed_step_is_recorded_or_inferred():
    assert O.failed_step({"failed_step": "momentum", "error": "x"}) == "momentum"
    assert O.failed_step({"error": "CycleError: failure drill: stopped at history (X)"}) == "history"
    assert O.failed_step({"error": "CycleError: canonical validation failed: ['a']"}) == "canonical"
    assert O.failed_step({"error": "something else"}) == "cycle"
    from source_pipeline.cycle import _Timer
    t = _Timer({"timings_s": {}})
    t("light_refresh")
    assert t.current == "light_refresh"
    t(None)
    assert t.current is None


def test_alerts_new_then_recurring_then_recovered(tmp_path):
    fail = "CycleError: 3 repositories could not be refreshed after 2 retries (e.g. ['github.com/a/b'])"
    degraded = {"id": "corroboration", "status": "degraded", "degraded_providers": ["deps.dev"]}
    recs = [_rec("2026-10-01-s", "2026-10-01T00:00:00Z", counts={"canonical_saas_products": 118}),
            _rec("2026-10-02-s", "2026-10-02T00:00:00Z", "failed", fail, failed_step="light_refresh"),
            _rec("2026-10-03-s", "2026-10-03T00:00:00Z", "failed", fail.replace("3 ", "5 "),
                 failed_step="light_refresh"),
            _rec("2026-10-04-s", "2026-10-04T00:00:00Z", strategies=[degraded],
                 counts={"canonical_saas_products": 117}),
            _rec("2026-10-05-s", "2026-10-05T00:00:00Z", strategies=[degraded],
                 counts={"canonical_saas_products": 116}),
            _rec("2026-10-06-s", "2026-10-06T00:00:00Z", strategies=[degraded],
                 counts={"canonical_saas_products": 107})]
    d = _write_runs(tmp_path / "runs", recs)
    a2 = O.alerts(d, "2026-10-02-s")
    assert [(x["rule"], x["status"]) for x in a2["alerts"]] == [("cycle_failed", "new")]
    a3 = O.alerts(d, "2026-10-03-s")
    assert a3["alerts"][0]["rule"] == "cycle_failed" and a3["alerts"][0]["status"] == "recurring"
    assert a3["alerts"][0]["fingerprint"] == a2["alerts"][0]["fingerprint"]
    assert a3["alerts"][0]["consecutive_runs"] == 2
    a4 = O.alerts(d, "2026-10-04-s")
    assert [x["rule"] for x in a4["alerts"]] == ["new_failure_fingerprint"]
    assert O.alerts(d, "2026-10-05-s")["alerts"] == []  # seen before, not yet recurring
    a6 = O.alerts(d, "2026-10-06-s")
    assert sorted(x["rule"] for x in a6["alerts"]) == ["canonical_shrink", "recurring_fault"]
    shrink = next(x for x in a6["alerts"] if x["rule"] == "canonical_shrink")
    assert (shrink["before"], shrink["after"]) == (116, 107)
    late = O.alerts(d, "2026-10-06-s", now=datetime(2026, 10, 8, 0, tzinfo=timezone.utc))
    assert "no_recent_success" in {x["rule"] for x in late["alerts"]}
    assert all(set(x) >= {"rule", "severity", "fingerprint", "title", "detail"} for x in late["alerts"])


def test_surface_alerts_files_one_issue_per_fingerprint(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import surface_alerts as S
    calls = []
    open_issues = [{"number": 7, "title": "Daily cycle failed at light_refresh [fp:aaaaaaaaaaaa]"},
                   {"number": 8, "title": "New degraded fault at corroboration [fp:bbbbbbbbbbbb]"},
                   {"number": 9, "title": "Daily cycle failing"}]

    def gh(args, dry, capture=False):
        calls.append(args)
        if args[:2] == ["issue", "list"]:
            label = args[args.index("--label") + 1]
            return json.dumps([i for i in open_issues if ("fp:a" in i["title"] or "failing" in i["title"])
                               == (label == "cycle-failure")])
        return ""
    monkeypatch.setattr(S, "gh", gh)
    doc = {"run_id": "r", "run_status": "succeeded", "run_fingerprints": [],
           "alerts": [{"rule": "cycle_failed", "severity": "critical", "fingerprint": "aaaaaaaaaaaa", "title": "t",
                       "detail": "d"},
                      {"rule": "canonical_shrink", "severity": "warning", "fingerprint": "cccccccccccc", "title": "u",
                       "detail": "e"}]}
    (tmp_path / "a.json").write_text(json.dumps(doc))
    assert S.main([str(tmp_path / "a.json")]) == 1
    acts = [c[:3] for c in calls if c[0] == "issue" and c[1] != "list"]
    assert ["issue", "comment", "7"] in acts                      # same fingerprint: same issue
    assert any(c[:2] == ["issue", "create"] and "[fp:cccccccccccc]" in c[3] for c in calls)  # new one: new issue
    assert ["issue", "close", "8"] in acts                        # not raised, not seen: recovered
    assert ["issue", "close", "9"] in acts                        # pre-fingerprint issue closed on success


# ------------------------------------------------------------------ S0.1 intake report

def test_intake_report_follows_the_funnel_of_the_live_run():
    # The newest successful run, whichever it is: a fixed run id breaks on the next cycle (KEI-919).
    rec = max((r for r in O.records(RUNS) if r.get("status") == "succeeded"),
              key=lambda r: (r.get("ended_at") or r.get("started_at") or "", r["run_id"]))
    rep = O.intake_report(rec, ROOT)
    assert rep == O.intake_report(rec, ROOT)  # deterministic
    st = {s["id"]: s for s in rep["stages"]}
    manifest = common.read_json(ROOT / "data" / "canonical" / "MANIFEST.json")
    summary = common.read_json(ROOT / "data" / "dataset" / "summary.json")
    assert rep["dataset_version"] == manifest["dataset_version"] and rep["promoted"]
    assert st["canonical"]["out"] == manifest["counts"]["relationships"]
    assert st["export_guard"]["stopped"] == manifest["export_guard"]["relationships_withheld_share_alike_only"]
    assert st["launch_selection"]["out"] == summary["launch"]["relationships"] == st["export_guard"]["in"]
    assert st["relationship_validation"]["out"] == st["launch_selection"]["in"]
    assert st["upstream_lists"]["in"] == len(summary["harvest"]["by_source"])
    reasons = st["eligibility"]["stopped_by_reason"]
    assert [r["count"] for r in reasons] == sorted((r["count"] for r in reasons), reverse=True)
    assert rep["manual_intake"]["submissions"] == len(list((ROOT / "data" / "intake" / "outcomes").glob("*.json")))
    committed = common.read_json(RUNS / "reports" / f"{rec['run_id']}.intake.json")
    assert {k: v for k, v in committed.items() if k != "change_from_previous"} == rep


def test_intake_report_delta_and_failed_run():
    rec = common.read_json(RUNS / "2026-10-17-schedule-a1.json")
    rep = O.intake_report(rec, ROOT)
    prev = copy.deepcopy(rep)
    prev["run_id"] = "earlier"
    prev["stages"][-1]["out"] -= 3
    d = O.with_delta(rep, prev)
    assert d["change_from_previous"] == {"run_id": "earlier", "changes": {"canonical.out": 3}}
    failed = O.intake_report({**rec, "status": "failed"}, ROOT)
    assert failed["promoted"] is False and "promoted nothing" in failed["reflects"]


# ------------------------------------------------------------------ S0.3 last_run in source_dataset_info

def _info(root, now=NOW):
    s = M.Server(root, now)
    s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "source_dataset_info", "arguments": {}}})
    return r["result"]["structuredContent"]["result"]


def _bare(tmp_path):
    root = tmp_path / "layer"
    shutil.copytree(ROOT / "data" / "canonical", root / "data" / "canonical")
    shutil.copytree(ROOT / "api" / "v1", root / "api" / "v1")
    return root


def test_dataset_info_reports_the_last_run():
    lr = _info(ROOT)["last_run"]
    newest = O._pick(O.records(RUNS), None)
    assert lr["run"]["run_id"] == newest["run_id"] and lr["run"]["status"] == newest["status"]
    assert lr["serving_last_success"] is True and lr["status"] == "ok"
    assert lr["run"]["dataset_version_after"] == common.read_json(
        ROOT / "data" / "canonical" / "MANIFEST.json")["dataset_version"]
    assert set(lr["run"]["counts"]) <= set(M.RUN_COUNTS)
    assert _info(ROOT, M.datetime(2026, 10, 25, tzinfo=M.timezone.utc))["last_run"]["status"] == "stale"


def test_last_run_degrades_and_names_a_failure(tmp_path):
    root = _bare(tmp_path)
    assert _info(root)["last_run"]["status"] == "unavailable"  # a bare read layer still serves
    version = common.read_json(root / "data" / "canonical" / "MANIFEST.json")["dataset_version"]
    _write_runs(root / "runs", [
        _rec("a", "2026-10-16T23:00:00Z", after=version),
        _rec("b", "2026-10-17T05:00:00Z", "failed", "CycleError: boom " + "x" * 1000, after=version)])
    lr = _info(root)["last_run"]
    assert lr["run"]["status"] == "failed" and len(lr["run"]["error"]) == M.MAX_TEXT
    assert lr["last_success"]["run_id"] == "a" and lr["serving_last_success"] is True


# ------------------------------------------------------------------ S0.5 telemetry: optional, aggregate, weekly

def test_stdio_counts_nothing_and_needs_no_telemetry():
    s = M.Server(ROOT, NOW)
    assert s.telemetry is None
    s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "source_search", "arguments": {"query": "wiki"}}})
    assert r["result"]["isError"] is False and s.telemetry is None
    assert "--telemetry" not in subprocess.run([sys.executable, "-m", "source_pipeline.mcp_server", "--help"],
                                               capture_output=True, text=True, cwd=ROOT).stdout


def test_http_counts_only_with_a_sink_and_counts_empty_answers(tmp_path):
    assert H.App(M.Server(ROOT, NOW)).server.telemetry is None
    app = H.App(M.Server(ROOT, NOW), telemetry_path=tmp_path / "t.json")
    s = app.server
    s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    for q in ("wiki", "zzzzqqqq"):
        s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "source_search", "arguments": {"query": q}}})
    app.maybe_flush(force=True)
    snap = json.loads((tmp_path / "t.json").read_text())
    assert snap["schema"] == M.TELEMETRY_SCHEMA
    cell = snap["days"]["2026-10-17"]["source_search"]["ok"]
    assert cell["count"] == 2 and cell["empty"] == 1
    assert "zzzzqqqq" not in (tmp_path / "t.json").read_text()


def _search(s, q="wiki"):
    s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
    s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "source_search", "arguments": {"query": q}}})


def test_restart_keeps_the_retained_days(tmp_path):
    """KEI-927: every flush replaces the file with the in-memory counters, so a restart that starts from zero
    erased the retained days. The next App resumes them and keeps counting on top."""
    path = tmp_path / "t.json"
    first = H.App(M.Server(ROOT, NOW), telemetry_path=path)
    _search(first.server)
    first.maybe_flush(force=True)
    later = M.datetime(2026, 10, 18, 6, tzinfo=M.timezone.utc)
    for _ in range(2):  # two restarts: each resumes what the last one flushed
        app = H.App(M.Server(ROOT, later), telemetry_path=path)
        _search(app.server)
        app.maybe_flush(force=True)
    days = json.loads(path.read_text())["days"]
    assert days["2026-10-17"]["source_search"]["ok"]["count"] == 1
    assert days["2026-10-18"]["source_search"]["ok"]["count"] == 2


def test_restart_resumes_a_schema_1_file(tmp_path):
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"schema": "the-source.mcp-telemetry/1", "retain_days": 90, "days": {
        "2026-10-16": {"source_search": {"ok": {"count": 7, "latency": {"<50ms": 7}}}}}}))
    app = H.App(M.Server(ROOT, NOW), telemetry_path=path)
    _search(app.server)
    app.maybe_flush(force=True)
    snap = json.loads(path.read_text())
    assert snap["schema"] == M.TELEMETRY_SCHEMA
    assert snap["days"]["2026-10-16"]["source_search"]["ok"] == {"count": 7, "latency": {"<50ms": 7}}
    assert snap["days"]["2026-10-17"]["source_search"]["ok"]["count"] == 1


@pytest.mark.parametrize("text", ["{not json", json.dumps({"schema": "other/1", "days": {}}),
                                  json.dumps({"schema": M.TELEMETRY_SCHEMA, "days": {"2026-10-16": {
                                      "source_search": {"ok": {"count": "7", "latency": {}}}}}})])
def test_an_unresumable_file_is_kept_aside_never_overwritten(tmp_path, text):
    path = tmp_path / "t.json"
    path.write_text(text)
    app = H.App(M.Server(ROOT, NOW), telemetry_path=path)
    assert app.server.telemetry.days == {}
    kept = list(tmp_path.glob("t.json.unreadable-*"))
    assert len(kept) == 1 and kept[0].read_text() == text
    _search(app.server)
    app.maybe_flush(force=True)
    assert json.loads(path.read_text())["days"]["2026-10-17"]["source_search"]["ok"]["count"] == 1


def test_a_stopped_server_resumes_its_counts(tmp_path):
    """The deployed path end to end: serve, answer a call, SIGTERM (as systemd does), start again, call again."""
    import http.client
    import signal
    import socket
    import time
    path = tmp_path / "t.json"

    def run_once():
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        p = subprocess.Popen([sys.executable, "-m", "source_pipeline.mcp_http", "--root", str(ROOT),
                              "--port", str(port), "--telemetry", str(path)], cwd=ROOT,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                               "params": {"name": "source_search", "arguments": {"query": "wiki"}}})
            deadline = time.monotonic() + 60
            while True:
                try:
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                    c.request("POST", "/mcp", body, {"Content-Type": "application/json",
                                                     "Accept": "application/json, text/event-stream",
                                                     "MCP-Protocol-Version": "2025-06-18"})
                    r = c.getresponse()
                    r.read()
                    assert r.status == 200
                    break
                except (ConnectionRefusedError, ConnectionResetError):
                    assert p.poll() is None and time.monotonic() < deadline, p.stderr.read()
                    time.sleep(0.2)
        finally:
            p.send_signal(signal.SIGTERM)
            assert p.wait(timeout=30) == 0
        return json.loads(path.read_text())["days"]

    def calls(days):  # summed over days, so a run that straddles UTC midnight still reads true
        return sum(row["source_search"]["ok"]["count"] for row in days.values())

    first = run_once()
    assert calls(first) == 1
    second = run_once()
    assert calls(second) == 2


def _telemetry(days):
    return {"schema": "the-source.mcp-telemetry/2", "days": days}


def test_weekly_report_suppresses_small_counts_and_has_no_daily_figures():
    cell = lambda n, empty=0: {"count": n, "latency": {"<10ms": n}, "empty": empty}
    tel = _telemetry({
        "2026-10-12": {"source_search": {"ok": cell(30, 6), "tool_error": {"count": 2, "latency": {"<10ms": 2}}},
                       "source_get_project": {"ok": cell(3)}},
        "2026-10-18": {"source_search": {"ok": cell(10, 1)}},
        "2026-10-19": {"source_search": {"ok": cell(99)}},  # the next week: not counted
        "2026-10-05": {"source_search": {"ok": cell(20)}}})
    rep = T.report(tel, "2026-W42", 5, {"week": "2026-W41"})
    rows = {r["tool"]: r for r in rep["tools"]}
    assert rows["source_search"]["calls"] == 42 and rows["source_search"]["outcomes"]["tool_error"] == "<5"
    assert rows["source_search"]["error_rate"] is None  # computed from a suppressed count: withheld
    assert rows["source_search"]["empty_answers"] == 7 and rows["source_search"]["empty_rate"] == round(7 / 40, 4)
    assert rows["source_get_project"]["calls"] == "<5" and rows["source_get_project"]["latency_median_bucket"] is None
    assert rep["totals"]["calls"] == 45 and rep["previous_week"]["calls"] == 20
    text = json.dumps(rep)
    assert "2026-10-18" in text and "2026-10-14" not in text  # only the week's bounds, never a day's figures
    assert rep["days_with_data"] == 2
    old = T.report({"schema": "the-source.mcp-telemetry/1", "days": {"2026-10-12": {"source_search": {
        "ok": {"count": 9, "latency": {"<10ms": 9}}}}}}, "2026-W42")
    assert old["tools"][0]["empty_rate"] is None
    with pytest.raises(ValueError):
        T.report({"schema": "something/else", "days": {}}, "2026-W42")
    assert "| `source_search` | 42 |" in T.markdown(rep)


def test_weekly_report_cli_writes_json_and_markdown(tmp_path):
    (tmp_path / "t.json").write_text(json.dumps(_telemetry({})))
    p = subprocess.run([sys.executable, "-S", "-m", "source_pipeline.telemetry_report", "--telemetry",
                        str(tmp_path / "t.json"), "--week", "2026-W42", "--out", str(tmp_path / "r")],
                       capture_output=True, text=True, cwd=ROOT, env={"PYTHONPATH": str(ROOT)})
    assert p.returncode == 0, p.stderr
    assert json.loads((tmp_path / "r" / "2026-W42.json").read_text())["totals"]["calls"] == 0
    assert (tmp_path / "r" / "2026-W42.md").read_text().startswith("# The Source MCP: usage, week 2026-W42")


# ------------------------------------------------------------------ S0.4 golden queries

@pytest.fixture(scope="module")
def evaluation():
    return golden.evaluate(ROOT)


def test_golden_set_size_and_live_measures(evaluation):
    s = evaluation["summary"]
    assert 40 <= s["queries"] <= 50
    assert s["constraint_violations"] == 0 and s["negative_passed"] == s["negative"] == 2
    assert 0 < s["recall@5"] <= 1 and 0 < s["precision@5"] <= 1
    tools = {q["tool"] for q in common.load_yaml(golden.QUERIES)["queries"]}
    assert tools == {"source_get_alternatives", "source_recommend", "source_search"}
    assert sum(1 for q in common.load_yaml(golden.QUERIES)["queries"]
               if set(q["args"]) & set(golden.CONSTRAINT_KEYS)) >= 7


def test_golden_labels_name_real_projects():
    canon = {r["oss_id"] for r in common.read_jsonl(ROOT / "data" / "canonical" / "oss_projects.jsonl")}
    corpus = {r["id"] for r in common.read_jsonl(ROOT / "data" / "corpus" / "projects.jsonl")}
    for q in common.load_yaml(golden.QUERIES)["queries"]:
        for label in q.get("relevant", []):
            assert golden._key(label) in canon | corpus, (q["id"], label)


def test_violation_check_is_independent_of_the_server(evaluation):
    facts = golden.Facts(ROOT)
    server = M.Server(ROOT, NOW)
    small = min((p for p in facts.projects.values() if p["live"]["stars"]), key=lambda p: p["live"]["stars"])
    rel = next(r for r in facts.rel.values() if r["oss_id"] == small["oss_id"])
    q = {"tool": "source_recommend", "args": {"saas": rel["saas_id"], "min_stars": small["live"]["stars"] + 1}}
    bad = {"recommendations": [{"oss_id": small["oss_id"], "for_saas": rel["saas_id"]}]}
    v = golden._violations(q, bad, facts, server)
    assert any("breaks min_stars" in x for x in v)
    if not rel["recommended"]:
        assert any("not a recommended relationship" in x for x in v)
    q2 = {"tool": "source_recommend", "args": {"need": "x"}}
    assert any("not a recommended relationship" in x for x in golden._violations(
        q2, {"recommendations": [{"oss_id": "github.com/nobody/nothing", "for_saas": "notion"}]}, facts, server))


def test_golden_baseline_comparison(evaluation):
    base = common.read_json(ROOT / "observability" / "golden-baseline.json")
    cmp = golden.compare(evaluation, base, 0.05, 0.05)
    assert cmp["ok"], cmp["problems"]
    worse = copy.deepcopy(evaluation)
    worse["summary"]["recall@5"] = base["summary"]["recall@5"] - 0.2
    worse["summary"]["constraint_violations"] = 1
    worse["summary"]["queries_with_violations"] = ["x"]
    assert len(golden.compare(worse, base, 0.05, 0.05)["problems"]) == 2


def test_cycle_golden_step_reports_and_never_raises(monkeypatch):
    from source_pipeline import cycle
    st = cycle._golden_step()
    assert st["id"] == "golden_queries" and st["status"] == "ok", st
    monkeypatch.setattr(golden, "evaluate", lambda *a, **k: 1 / 0)
    assert cycle._golden_step()["status"] == "failed"


# ------------------------------------------------------------------ S0.6 lineage and reconciliation

def _has_history(*revs):
    return all(subprocess.run(["git", "cat-file", "-e", f"{r}^{{commit}}"], cwd=ROOT,
                              capture_output=True).returncode == 0 for r in revs)


needs_history = pytest.mark.skipif(not _has_history("de72d34", "23c313f"),
                                   reason="needs the git history from de72d34 to 23c313f (a full clone)")


@needs_history
def test_lineage_verifies_every_version_from_git():
    v = lineage.verify("de72d34", "23c313f")
    assert v["ok"], [c for c in v["checks"] if not c["ok"]]
    assert v["checks"][0]["dataset_version"] == "a6e5bef0eaf47a24"
    assert v["checks"][-1]["dataset_version"] == "02568a5dd656e3a8"
    h = lineage.history("a6e5bef0eaf47a24", "02568a5dd656e3a8")
    assert all(b["replaces"] == a["dataset_version"] for a, b in zip(h["versions"], h["versions"][1:]))
    assert all(r["inputs"]["code_tree"] and r["inputs"]["config_tree"] for r in h["versions"])


@needs_history
def test_catalogue_118_to_102_is_reconciled_product_by_product():
    r = lineage.reconcile("a6e5bef0eaf47a24", "02568a5dd656e3a8")
    assert (r["from"]["saas_products"], r["to"]["saas_products"]) == (118, 102)
    assert r["reproduced"] and r["balances"]
    assert len(r["removed"]) == 17 and [a["saas_id"] for a in r["added"]] == ["sap-business-one"]
    assert r["removed_by_cause"] == {"below_launch_cutoff": sorted(x["saas_id"] for x in r["removed"])}
    assert r["left_and_returned"] == ["pingdom"]
    for step in r["steps"]:
        for x in step["removed"]:
            assert x["rank"] > x["cutoff"]
    assert [s["saas_products"] for s in r["steps"]] == [[118, 117], [117, 107], [107, 100], [100, 102]]
