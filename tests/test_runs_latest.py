"""KEI-887: python -m source_pipeline runs-latest. No network, no data changes."""
from __future__ import annotations

import json

from source_pipeline import cli, cycle

FIELDS = {"cycle_key", "ended_at", "error", "counts", "canonical"}


def _run(d, name, **kw):
    rec = {"run_id": name, "cycle_key": name, "status": "succeeded", "started_at": None, "ended_at": None,
           "error": None, "counts": {"changed": 0}, "canonical": {"changed": False}, "failures": []}
    rec.update(kw)
    (d / f"{name}.json").write_text(json.dumps(rec))
    return rec


def test_latest_is_by_ended_at_not_file_name(tmp_path):
    # "manual-..." sorts after "2026-..." by name, but the scheduled run ended later
    _run(tmp_path, "manual-20261001T000000Z-manual-a1", ended_at="2026-10-01T00:05:00Z")
    _run(tmp_path, "2026-10-02-schedule-a1", cycle_key="2026-10-02", ended_at="2026-10-02T00:05:00Z",
         counts={"changed": 3}, canonical={"changed": True, "dataset_version_after": "abc"})
    out = cycle.latest(tmp_path)
    assert set(out) == FIELDS
    assert out == {"cycle_key": "2026-10-02", "ended_at": "2026-10-02T00:05:00Z", "error": None,
                   "counts": {"changed": 3}, "canonical": {"changed": True, "dataset_version_after": "abc"}}


def test_failed_run_reports_its_error(tmp_path):
    _run(tmp_path, "a", ended_at="2026-10-01T00:00:00Z")
    _run(tmp_path, "b", ended_at="2026-10-03T00:00:00Z", status="failed", error="build: boom", canonical=None)
    out = cycle.latest(tmp_path)
    assert out["cycle_key"] == "b" and out["error"] == "build: boom" and out["canonical"] is None


def test_running_record_counts_from_its_start(tmp_path):
    _run(tmp_path, "done", ended_at="2026-10-01T00:00:00Z")
    _run(tmp_path, "running", started_at="2026-10-02T00:00:00Z", status="running")
    assert cycle.latest(tmp_path)["cycle_key"] == "running"


def test_ties_break_on_file_name(tmp_path):
    _run(tmp_path, "a", ended_at="2026-10-01T00:00:00Z")
    _run(tmp_path, "b", ended_at="2026-10-01T00:00:00Z")
    assert cycle.latest(tmp_path)["cycle_key"] == "b"


def test_none_when_empty_or_missing(tmp_path):
    assert cycle.latest(tmp_path) is None
    assert cycle.latest(tmp_path / "absent") is None
    (tmp_path / "notes.txt").write_text("not a run")
    assert cycle.latest(tmp_path) is None


def test_default_reads_repo_runs_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cycle, "RUNS", tmp_path)
    _run(tmp_path, "x", ended_at="2026-10-01T00:00:00Z")
    assert cycle.latest()["cycle_key"] == "x"


def test_cli_prints_json_and_exits_0(tmp_path, capsys):
    _run(tmp_path, "x", ended_at="2026-10-01T00:00:00Z", counts={"added": 1})
    assert cli.main(["runs-latest", "--runs-dir", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"cycle_key": "x", "ended_at": "2026-10-01T00:00:00Z", "error": None,
                   "counts": {"added": 1}, "canonical": {"changed": False}}


def test_cli_exits_1_with_message_when_none(tmp_path, capsys):
    assert cli.main(["runs-latest", "--runs-dir", str(tmp_path)]) == 1
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "no cycle summaries" in cap.err and str(tmp_path) in cap.err


def test_cli_does_not_write(tmp_path):
    _run(tmp_path, "x", ended_at="2026-10-01T00:00:00Z")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    cli.main(["runs-latest", "--runs-dir", str(tmp_path)])
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
