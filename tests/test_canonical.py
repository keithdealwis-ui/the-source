"""KEI-807: canonical dataset, read surface, weekly cycle and intake. No network."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from source_pipeline import canonical, canonical_validate, common, cycle, harvest, intake, research

HAVE_DATASET = (common.DATASET / "summary.json").exists()


# ---------------------------------------------------------------- read surface

@pytest.mark.skipif(not HAVE_DATASET, reason="needs the committed dataset")
def test_committed_canonical_validates_and_rebuilds_identically():
    r = canonical_validate.run()
    assert r["ok"], [c for c in r["checks"] if not c["ok"]]
    assert any(c["id"] == "rebuild_is_byte_identical" and c["ok"] for c in r["checks"])


@pytest.mark.skipif(not HAVE_DATASET, reason="needs the committed dataset")
def test_every_supported_domain_resolves_to_a_deterministic_record():
    api = common.ROOT / "api" / "v1"
    index = json.loads((api / "index.json").read_text())
    domains = json.loads((api / "domains.json").read_text())
    assert len(index["supported_saas"]) >= 20
    for row in index["supported_saas"]:
        doc = json.loads((api / row["path"]).read_text())
        assert doc["saas"]["saas_id"] == row["saas_id"]
        assert 3 <= len(doc["recommendations"]) <= 5 or doc["status"] == "insufficient_recommendations"
        assert [r["rank"] for r in doc["recommendations"]] == list(range(1, len(doc["recommendations"]) + 1))
        for rec in doc["recommendations"]:
            assert rec["live_checked_at"] and rec["maintenance_status"] in ("active", "maintained")
    assert {r["saas_id"] for r in domains["rules"]} == {r["saas_id"] for r in index["supported_saas"]}


def _match(domains, url):
    """Reference implementation of the domain match semantics (docs/READ-API.md)."""
    from urllib.parse import urlparse

    u = urlparse(url)
    host = (u.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    hits = [r for r in domains["rules"]
            if (r.get("host") == host or (r.get("host_suffix") and host.endswith(r["host_suffix"])))
            and (not r.get("path_prefix") or u.path.startswith(r["path_prefix"]))]
    hits.sort(key=lambda r: 0 if r.get("path_prefix") else 1)
    return hits[0]["saas_id"] if hits else None


@pytest.mark.skipif(not HAVE_DATASET, reason="needs the committed dataset")
def test_domain_matching_examples():
    domains = json.loads((common.ROOT / "api" / "v1" / "domains.json").read_text())
    assert _match(domains, "https://www.notion.so/workspace/page") == "notion"
    assert _match(domains, "https://acme.atlassian.net/jira/software/projects") == "jira"
    assert _match(domains, "https://acme.atlassian.net/wiki/spaces") is None
    assert _match(domains, "https://aws.amazon.com/s3/pricing/") == "amazon-s3"
    assert _match(domains, "https://aws.amazon.com/ec2/") is None
    assert _match(domains, "https://shop-name.myshopify.com/admin") == "shopify"
    assert _match(domains, "https://example.com/") is None


def test_note_is_derived_and_bounded():
    fit = {"dimensions": {"core_use_case_coverage": 90.0, "feature_equivalence": 20.0, "ease_of_adoption": 80.0,
                          "migration_friction": None, "economic_advantage": 50.0}, "evidence_families": []}
    n = canonical._note(fit)
    assert n["origin"] == "derived"
    assert n["strengths"] == ["core use-case coverage", "ease of adoption"]
    assert n["gaps"] == ["feature parity"]
    assert n["text"].startswith("Strongest on core use-case coverage")
    assert canonical._note(None)["text"] is None


def test_utc_normalisation():
    assert canonical._utc("2026-09-27T05:53:49+02:00") == "2026-09-27T03:53:49Z"
    assert canonical._utc("2026-09-27T03:53:49Z") == "2026-09-27T03:53:49Z"


# ---------------------------------------------------------------- intake evidence

CATALOGUE = [
    {"saas_id": "github", "name": "GitHub", "aliases": [], "category": "Source control"},
    {"saas_id": "notion", "name": "Notion", "aliases": [], "category": "Knowledge management"},
    {"saas_id": "sendgrid", "name": "SendGrid", "aliases": [], "category": "Email delivery"},
    {"saas_id": "postmark", "name": "Postmark", "aliases": [], "category": "Email delivery"},
    {"saas_id": "trello", "name": "Trello", "aliases": [], "category": "Project management"},
    {"saas_id": "x", "name": "X", "aliases": [], "category": "Social network"},
]


@pytest.mark.parametrize("desc,topics,expected", [
    ("Self-hosted pastebin, open-source alternative to Github Gist.", [], []),
    ("Open source alternative to Resend, Sendgrid, Postmark etc.", [], ["postmark", "sendgrid"]),
    ("An open-source Notion alternative with AI", [], ["notion"]),
    ("Kanban board", ["trello-alternative"], ["trello"]),
    ("Imports from Notion and syncs with GitHub", [], []),
    ("A replacement for the X app", [], []),  # names under 3 characters are never matched
])
def test_find_evidence(desc, topics, expected):
    assert [e["saas_id"] for e in intake.find_evidence(desc, topics, "", CATALOGUE)] == expected


def test_intake_rejects_invalid_url_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "OUTCOMES", tmp_path / "outcomes")
    o = intake.assess("not a url", "req-invalid-1")
    assert o["outcome"] == "rejected" and o["reasons"] == ["repo_url_not_a_repository"]
    again = intake.assess("https://github.com/other/thing", "req-invalid-1")
    assert again == o  # same request id -> the recorded outcome, unchanged
    with pytest.raises(ValueError):
        intake.assess("https://github.com/a/b", "../../etc")


def test_intake_refuses_unsupported_host(tmp_path, monkeypatch):
    monkeypatch.setattr(intake, "OUTCOMES", tmp_path / "outcomes")
    o = intake.assess("https://bitbucket.org/owner/repo", "req-host-001")
    assert o["outcome"] == "rejected" and "host_not_independently_verifiable" in o["reasons"]


# ---------------------------------------------------------------- schedule

def _write_schedule(tmp_path, **over):
    cfg = {"schema": "the-source.schedule/1", "timezone": "Asia/Dubai", "weekday": "sunday", "time": "03:00",
           "window_hours": 4, "max_attempts": 3}
    cfg.update(over)
    p = tmp_path / "schedule.yaml"
    p.write_text(json.dumps(cfg))
    return p


def test_due_inside_and_outside_window(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle, "SCHEDULE", _write_schedule(tmp_path))
    monkeypatch.setattr(cycle, "RUNS", tmp_path / "runs")
    # Sunday 2026-10-04 03:30 Dubai = Saturday 23:30 UTC
    d = cycle.due(datetime(2026, 10, 3, 23, 30, tzinfo=timezone.utc))
    assert d["due"] and d["cycle_key"] == "2026-10-04"
    d = cycle.due(datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc))  # 12:00 Dubai, window over
    assert not d["due"] and d["reason"] == "outside the off-peak window"
    d = cycle.due(datetime(2026, 10, 3, 22, 30, tzinfo=timezone.utc))  # 02:30 Dubai, before the window
    assert not d["due"] and d["cycle_key"] == "2026-09-27"


def test_due_catches_up_a_window_the_scheduler_missed(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle, "SCHEDULE", _write_schedule(tmp_path, catch_up_hours=24))
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(cycle, "RUNS", runs)
    d = cycle.due(datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc))  # 12:00 Dubai, window over
    assert d["due"] and d["late"] and d["cycle_key"] == "2026-10-04"
    d = cycle.due(datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc))  # past window + 24 h
    assert not d["due"] and d["reason"] == "outside the off-peak window"
    (runs / "r1.json").write_text(json.dumps({"cycle_key": "2026-10-04", "trigger": "schedule", "status": "succeeded"}))
    assert not cycle.due(datetime(2026, 10, 4, 8, 0, tzinfo=timezone.utc))["due"]


def test_schedule_is_configuration_not_code(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle, "SCHEDULE", _write_schedule(tmp_path, weekday="wednesday", time="01:00",
                                                           timezone="Europe/London"))
    monkeypatch.setattr(cycle, "RUNS", tmp_path / "runs")
    d = cycle.due(datetime(2026, 9, 30, 0, 30, tzinfo=timezone.utc))  # Wed 01:30 London (BST)
    assert d["due"] and d["cycle_key"] == "2026-09-30"


def test_due_counts_attempts_and_success(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle, "SCHEDULE", _write_schedule(tmp_path))
    runs = tmp_path / "runs"
    runs.mkdir()
    monkeypatch.setattr(cycle, "RUNS", runs)
    now = datetime(2026, 10, 3, 23, 30, tzinfo=timezone.utc)
    for i in (1, 2, 3):
        (runs / f"r{i}.json").write_text(json.dumps({"cycle_key": "2026-10-04", "trigger": "schedule", "status": "failed"}))
    assert cycle.due(now)["reason"].startswith("attempts exhausted")
    (runs / "r4.json").write_text(json.dumps({"cycle_key": "2026-10-04", "trigger": "schedule", "status": "succeeded"}))
    assert cycle.due(now)["reason"] == "this week's cycle already succeeded"


# ---------------------------------------------------------------- failure safety

def test_failed_cycle_leaves_last_known_good_untouched(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "data" / "canonical").mkdir(parents=True)
    (root / "api" / "v1").mkdir(parents=True)
    (root / "config").mkdir()
    good = {"dataset_version": "aaaaaaaaaaaaaaaa"}
    (root / "data" / "canonical" / "MANIFEST.json").write_text(json.dumps(good))
    (root / "api" / "v1" / "index.json").write_text('{"dataset_version": "aaaaaaaaaaaaaaaa"}')
    shutil.copy(common.CONFIG / "schedule.yaml", root / "config" / "schedule.yaml")
    monkeypatch.setattr(common, "ROOT", root)
    monkeypatch.setattr(common, "DATA", root / "data")
    monkeypatch.setattr(cycle, "RUNS", root / "runs")
    monkeypatch.setattr(cycle, "SCHEDULE", root / "config" / "schedule.yaml")

    def worker_that_corrupts_then_fails(cmd, cwd, env):
        work = Path(env["THE_SOURCE_ROOT"])
        (work / "data" / "canonical" / "MANIFEST.json").write_text("corrupted")
        Path(cmd[cmd.index("--result") + 1]).write_text(json.dumps({"ok": False, "error": "simulated upstream outage"}))
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(cycle.subprocess, "run", worker_that_corrupts_then_fails)
    rec = cycle.run("manual")
    assert rec["status"] == "failed" and "simulated upstream outage" in rec["error"]
    assert json.loads((root / "data" / "canonical" / "MANIFEST.json").read_text()) == good
    assert rec["canonical"]["dataset_version_after"] == good["dataset_version"] and not rec["canonical"]["changed"]
    stored = json.loads((root / "runs" / f"{rec['run_id']}.json").read_text())
    assert stored["status"] == "failed" and stored["ended_at"]


def test_research_keeps_pin_when_licence_changes(tmp_path, monkeypatch):
    src = {"source_id": "demo", "repo": "o/r", "pinned_commit": "a" * 40, "licence_file": "LICENSE",
           "licence_file_sha256": "0" * 64, "decision": "allow", "parser": "p", "parse_path": ".", "priority": 1}
    monkeypatch.setattr(research, "PINS", tmp_path / "pins.json")
    monkeypatch.setattr(research.common, "load_sources", lambda: {"sources": [src]})

    class FakeHttp:
        def call(self, url, data=None, auth=False, tries=5):
            return 200, {"sha": "b" * 40}

    monkeypatch.setattr(research.enrich, "Http", FakeHttp)
    d = tmp_path / "new"
    d.mkdir()
    (d / "LICENSE").write_text("a different licence")
    monkeypatch.setattr(research.harvest, "fetch", lambda s: d)
    res = research.advance_pins(as_of="2026-10-04T00:00:00Z")
    assert res[0]["status"] == "licence_changed_needs_review"
    assert json.loads((tmp_path / "pins.json").read_text())["pins"] == {}


def test_intake_policy_is_pinned():
    s = next(x for x in common.load_sources()["sources"] if x["source_id"] == "intake")
    assert harvest.verify_licence(s, common.ROOT) == s["licence_file_sha256"]
