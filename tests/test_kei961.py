"""KEI-961: upstream list parsers for the November 2026 layouts, the deep-rotation and rescoring backlogs, deliberate
job-definition gaps, and the recommendation gate. No network."""
from __future__ import annotations

import json

from source_pipeline import canonical, common, cycle, daily, rec_gate, score_build, score_one
from source_pipeline.parsers import parse_diegoleme, parse_opensaasdirectory

POLICY = {"maintenance": {"active_days": 90, "maintained_days": 365}}

# ---------------------------------------------------------------- upstream_lists: both layouts parse

OSD_OLD = """# Awesome
## Content Management System Platforms

### [Ghost](https://github.com/TryGhost/Ghost)

**💡 Open-source alternative to:** Substack, Medium

---
"""

OSD_NEW = """# Awesome Open Source SaaS Alternatives

### ➕

**Submit Your Project**

## 📈 Analytics & Monitoring

### 🩺 Monitoring & Observability

<table>
<tr>
<td>
<strong><a href="https://github.com/louislam/uptime-kuma">Uptime Kuma</a></strong><br>
<sub>A self-hosted monitoring tool.<br>
💡 <b>Open-source alternative to UptimeRobot &amp; Pingdom</b></sub>
</td>
</tr>
<tr>
<td>
<strong><a href="https://github.com/apache/superset">Apache Superset</a></strong><br>
<sub>Data exploration.<br>
💡 <b>Open-source alternative to Tableau, Looker, and Power BI</b></sub>
</td>
</tr>
</table>
"""

DL_OLD = """# Awesome Open-source Alternatives
## Apps
- [1Password](#1password)

## Resources

### [1Password](https://1password.com/)
- [Bitwarden](https://github.com/bitwarden/server)
"""

DL_NEW = """# Awesome Open-source Alternatives
## Contents
- [AI Assistants and Coding](#ai-assistants-and-coding)
  - [ChatGPT / Claude](#chatgpt--claude)

## AI Assistants and Coding

### [ChatGPT](https://chatgpt.com) / [Claude](https://claude.ai)
- [Ollama](https://github.com/ollama/ollama) - Run open models locally.

## Security and Networking

### [1Password](https://1password.com/)
- [Bitwarden](https://github.com/bitwarden/server)

## Contributing
- [Read the guide](https://github.com/diegoleme/awesome-open-source-alternatives/blob/main/CONTRIBUTING.md)
"""


def _parse(fn, tmp_path, text):
    (tmp_path / "README.md").write_text(text, encoding="utf-8")
    return list(fn(tmp_path, "README.md"))


def test_opensaasdirectory_reads_the_old_and_the_november_layout(tmp_path):
    old = _parse(parse_opensaasdirectory, tmp_path, OSD_OLD)
    assert [(r["oss_name"], r["repo_url"], r["saas_names"], r["category"]) for r in old] == [
        ("Ghost", "https://github.com/TryGhost/Ghost", ["Substack", "Medium"], "Content Management System Platforms")]
    new = _parse(parse_opensaasdirectory, tmp_path, OSD_NEW)
    assert [(r["oss_name"], r["repo_url"], r["saas_names"]) for r in new] == [
        ("Uptime Kuma", "https://github.com/louislam/uptime-kuma", ["UptimeRobot", "Pingdom"]),
        ("Apache Superset", "https://github.com/apache/superset", ["Tableau", "Looker", "Power BI"])]
    assert {r["category"] for r in new} == {"Monitoring & Observability"}
    assert all(r["locator"].startswith("README.md#L") for r in new)


def test_diegoleme_reads_every_category_section_and_multi_product_headings(tmp_path):
    old = _parse(parse_diegoleme, tmp_path, DL_OLD)
    assert [(r["oss_name"], r["saas_names"]) for r in old] == [("Bitwarden", ["1Password"])]
    new = _parse(parse_diegoleme, tmp_path, DL_NEW)
    assert [(r["oss_name"], r["saas_names"]) for r in new] == [("Ollama", ["ChatGPT", "Claude"]),
                                                              ("Bitwarden", ["1Password"])]


# ---------------------------------------------------------------- deep rotation: release-only changes coalesce

def test_select_deep_coalesces_release_only_changes_on_young_deep_records():
    classes = {"r_young": {"class": "changed", "reasons": ["release v1 -> v2"], "deep_age_days": 2},
               "r_old": {"class": "changed", "reasons": ["release v1 -> v2"], "deep_age_days": 9},
               "lic": {"class": "changed", "reasons": ["licence MIT -> None"], "deep_age_days": 1},
               "s1": {"class": "stale", "deep_age_days": 20}, "s2": {"class": "stale", "deep_age_days": 15}}
    rep = {}
    sel, deferred = daily.select_deep(classes, 3, release_min_age_days=7, report=rep)
    assert sel == ["lic", "r_old", "s1"]  # material + old-release first, then the oldest rotation
    assert deferred == ["s2"]  # coalesced releases are not counted as overdue
    assert rep == {"release_coalesced": 1}
    # without the setting, the pre-KEI-961 behaviour: every change goes, whatever the budget
    sel, _ = daily.select_deep(classes, 3)
    assert sel == ["lic", "r_old", "r_young"]


def test_maintenance_boundary_window_is_independent_of_rotation_age():
    prev = {"projects": {"github.com/o/a": {"found": True, "canonical_id": "github.com/o/a", "host_repo_id": 1,
                                            "licence_spdx": "MIT", "stars": 10, "pushed_at": "2026-09-01T00:00:00Z",
                                            "fetched_at": "2026-09-30T00:00:00Z"}}}
    cur = {"projects": {"github.com/o/a": {**prev["projects"]["github.com/o/a"], "pushed_at": "2026-10-01T00:00:00Z",
                                           "fetched_at": "2026-10-02T00:00:00Z"}}}
    # 80 days since activity: within 14 of the 90-day boundary, not within 7
    act = {"github.com/o/a": "2026-07-14T00:00:00Z"}
    wide = {"max_age_days": 14, "material_star_change": {"absolute": 500, "relative": 0.1}}
    narrow = {**wide, "maintenance_boundary_days": 7}
    as_of = "2026-10-02T00:00:00Z"
    assert daily.classify(cur, prev["projects"], wide, POLICY, as_of, act)["github.com/o/a"]["class"] == "changed"
    assert daily.classify(cur, prev["projects"], narrow, POLICY, as_of, act)["github.com/o/a"]["class"] == "unchanged"


# ---------------------------------------------------------------- the recommendation gate

GATE = {"enabled": True, "replacement_fit_min": {"corroborated": 60, "single_source": 65, "job_matched": 75}}


def _fit(score, publish=True, credible=True):
    return {"score": score, "publish": publish, "credible_alternative": credible}


def test_gate_floor_rises_as_evidence_thins():
    assert rec_gate.verdict(_fit(62), "listed", True, GATE) == {"recommended": True, "tier": "corroborated", "fails": []}
    assert rec_gate.verdict(_fit(62), "listed", False, GATE)["fails"] == ["fit_below_single_source_floor"]
    assert rec_gate.verdict(_fit(66), "self_described", False, GATE)["recommended"]
    assert rec_gate.verdict(_fit(74.9), "job_matched", False, GATE)["fails"] == ["fit_below_job_matched_floor"]
    assert rec_gate.verdict(_fit(75), "job_matched", False, GATE)["recommended"]
    # the base rule still applies first, and is the only rule when the gate is disabled
    assert rec_gate.verdict(_fit(90, publish=False), "listed", True, GATE)["fails"] == ["not_publishable"]
    off = {"enabled": False, "replacement_fit_min": GATE["replacement_fit_min"]}
    assert rec_gate.verdict(_fit(61), "job_matched", False, off)["recommended"]
    assert rec_gate.verdict(None, "listed", False, GATE)["fails"] == ["not_scored"]


def test_gate_summary_reports_the_pass_fail_ratio_with_reasons():
    vs = [rec_gate.verdict(f, c, k, GATE) for f, c, k in (
        (_fit(80), "job_matched", False), (_fit(70), "job_matched", False), (_fit(61), "listed", True),
        (_fit(50, credible=False), "listed", False), (None, "listed", False))]
    s = rec_gate.summarise(vs, GATE)
    assert (s["relationships"], s["evaluated"], s["passed"], s["failed"], s["not_scored"]) == (5, 4, 2, 2, 1)
    assert s["pass_rate"] == 0.5 and s["rule"] == rec_gate.RULE_ID
    assert s["failed_by_reason"] == {"fit_below_job_matched_floor": 1, "not_credible": 1, "not_scored": 1}
    assert s["by_tier"]["job_matched"] == {"evaluated": 2, "passed": 1, "pass_rate": 0.5}


def test_policy_ships_the_gate_enabled():
    cfg = rec_gate.load()
    assert cfg["enabled"] and cfg["replacement_fit_min"] == GATE["replacement_fit_min"]


# ---------------------------------------------------------------- scoring backlogs

def _scoring_root(tmp_path, monkeypatch, rels, health, saas_category="Note taking"):
    data = tmp_path / "data"
    monkeypatch.setattr(common, "DATASET", data / "dataset")
    monkeypatch.setattr(common, "LIVE", data / "live")
    monkeypatch.setattr(cycle, "RESCORE_QUEUE", data / "state" / "rescore_queue.json")
    oss = sorted({r["oss_id"] for r in rels} | set(health))
    common.write_jsonl(data / "dataset" / "oss_projects.jsonl", [
        {"oss_id": o, "host": "github.com", "derived": {"lane": "recommendable", "licence": {"spdx": "MIT"},
                                                        "evaluated_as_of": "2026-11-05T00:00:00Z"}} for o in oss])
    common.write_jsonl(data / "dataset" / "relationships.jsonl", rels)
    common.write_jsonl(data / "dataset" / "saas_products.jsonl", [
        {"saas_id": "notes", "category": saas_category}, {"saas_id": "pocus", "category": "Sales intelligence"}])
    common.write_json(data / "live" / "snapshot.json", {"projects": {o: {"found": True} for o in oss}})
    monkeypatch.setattr(canonical, "_load_scores", lambda: ({}, dict(health)))
    monkeypatch.setattr(score_build, "load_jobs", lambda: {"Note taking": {}})
    calls = []

    def fake_score(key, live, licence, pairs, as_of, http=None):
        calls.append(key)
        return {"health": {"oss_id": key}, "fit": [], "errors": [], "unscorable": []}

    monkeypatch.setattr(score_one, "score", fake_score)
    monkeypatch.setattr(score_one, "store", lambda r: 0)
    return calls


def _rel(saas_id, oss_id):
    return {"saas_id": saas_id, "oss_id": oss_id, "source_count": 1, "corroborated": False,
            "validation": {"status": "validated", "plausibility_review": {"outcome": "not reviewed"}}}


def test_new_pair_scoring_reports_job_definition_gaps_without_degrading(tmp_path, monkeypatch):
    calls = _scoring_root(tmp_path, monkeypatch, [_rel("pocus", "github.com/o/insp")],
                          {"github.com/o/insp": {"evaluated_at": "2026-11-01T00:00:00Z"}})
    res = {"strategies": []}
    cycle._score_new(60, res)
    s = res["strategies"][0]
    assert s["status"] == "ok" and s["errors"] == [] and calls == []  # never re-scored for a pair it cannot score
    assert s["not_scorable_no_job_definition"] == 1
    assert s["not_scorable"] == [{"saas_id": "pocus", "oss_id": "github.com/o/insp", "category": "Sales intelligence"}]


def test_rescoring_carries_what_the_budget_leaves_and_spaces_release_only_changes(tmp_path, monkeypatch):
    keys = [f"github.com/o/p{i}" for i in range(5)]
    fresh, old = {"evaluated_at": "2026-11-04T00:00:00Z"}, {"evaluated_at": "2026-10-20T00:00:00Z"}
    health = {k: (fresh if k == keys[4] else old) for k in keys}
    calls = _scoring_root(tmp_path, monkeypatch, [_rel("notes", k) for k in keys], health)
    reasons = {k: ["release v1 -> v2"] for k in keys}
    reasons[keys[3]] = ["licence MIT -> None"]

    res = {"strategies": []}
    done = cycle._rescore(keys, 2, res, reasons=reasons, day="2026-11-05")
    s = res["strategies"][0]
    assert done == [keys[3], keys[0]]  # a material change first, then the release changes in queue order
    assert s["queue"]["waiting_release_interval"] == 1  # p4 was scored yesterday: its release waits
    assert s["deferred_to_next_cycle"] == 2 and s["queue"]["carried_out"] == 3
    queue = common.read_json(cycle.RESCORE_QUEUE)["queue"]
    assert sorted(queue) == [keys[1], keys[2], keys[4]]

    # next cycle: nothing new is detected, yet the carried projects are asked first
    calls.clear()
    res = {"strategies": []}
    done = cycle._rescore([], 2, res, day="2026-11-06")
    assert done == [keys[1], keys[2]] and res["strategies"][0]["queue"]["carried_in"] == 3
    assert sorted(common.read_json(cycle.RESCORE_QUEUE)["queue"]) == [keys[4]]


def test_rescoring_queue_drops_projects_that_left_the_lane(tmp_path, monkeypatch):
    calls = _scoring_root(tmp_path, monkeypatch, [_rel("notes", "github.com/o/a")],
                          {"github.com/o/a": {"evaluated_at": "2026-10-01T00:00:00Z"}})
    common.write_json(cycle.RESCORE_QUEUE, {"queue": {"github.com/o/gone": {"queued_on": "2026-11-01", "reasons": []}}})
    res = {"strategies": []}
    assert cycle._rescore([], 5, res, day="2026-11-05") == []
    assert res["strategies"][0]["queue"]["dropped_unscorable"] == 1 and calls == []
    assert json.loads(cycle.RESCORE_QUEUE.read_text())["queue"] == {}


# KEI-961 AC2 (Keith gate-KEI-961-aa90748c, option A): catalogue serving places gate-passing relationships before
# gate-failing ones, so a preserved gate-failing slot yields, and no recommended relationship is displaced.
def test_gate_first_is_on_and_documented():
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    cs = yaml.safe_load((root / "config/policy.yaml").read_text())["launch"]["catalogue_serving"]
    assert cs.get("gate_first") is True


def test_warpdrive_is_served_under_pipedrive_not_salesforce():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    rels = [json.loads(l) for l in (root / "data/canonical/relationships.jsonl").read_text().splitlines() if l.strip()]
    w = {r["saas_id"]: r for r in rels if r["oss_id"] == "github.com/sneg55/warpdrive"}
    assert "salesforce" not in w
    assert w["pipedrive"]["recommended"] is True


def test_no_saas_serves_a_gate_failing_relationship_while_a_passing_one_waits():
    """Within catalogue serving, for every product, each served gate-failing catalogue pick ranks below every
    gate-passing pick (the sort places all passing candidates first)."""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    ds = [json.loads(l) for l in (root / "data/dataset/relationships.jsonl").read_text().splitlines() if l.strip()]
    rels = {(r["saas_id"], r["oss_id"]): r for r in
            map(json.loads, (root / "data/canonical/relationships.jsonl").read_text().splitlines())}
    by = {}
    for e in ds:
        lp = e.get("launch_position") or {}
        if lp.get("via") == "catalogue_serving":
            by.setdefault(e["saas_id"], []).append((lp["position"], rels[(e["saas_id"], e["oss_id"])]["recommended"]))
    for sid, picks in by.items():
        picks.sort()
        seen_fail = False
        for _, ok in picks:
            seen_fail |= not ok
            assert not (seen_fail and ok), sid
