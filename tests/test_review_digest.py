"""KEI-783 AC5: the weekly human review digest, on a fixture git history. No network."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from source_pipeline import common, review_digest


def _fit(score, publish=True):
    return {"score": score, "credible_alternative": publish, "publish": publish, "confidence": 84.5}


def _rel(saas, oss, score, recommended=True):
    return {"saas_id": saas, "oss_id": oss, "replacement_fit": None if score is None else _fit(score, recommended),
            "recommended": recommended}


def _proj(oss, name, health=70.0):
    return {"oss_id": oss, "name": name, "project_health": None if health is None else {"score": health}}


def _momentum(key, stars, delta, band):
    return {"key": key, "canonical_id": key, "on": "2026-10-20", "stars": stars,
            "windows": {"7": {"status": "measured", "momentum": band, "stars_delta": delta,
                              "growth_pct_per_30d": round(delta / stars * 100 * 30 / 7, 3),
                              "acceleration": {"label": "steady"}}}}


def _write(root, files):
    for rel, obj in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".jsonl"):
            p.write_text("".join(common.dumps(r) + "\n" for r in obj))
        else:
            p.write_text(json.dumps(obj, sort_keys=True))


def _commit(root, msg, when):
    env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, check=True, env=env)


@pytest.fixture
def history(tmp_path, monkeypatch):
    """Two cycle commits eight days apart: a week of change the digest has to find."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    _write(root, {
        "data/canonical/MANIFEST.json": {"dataset_version": "aaaaaaaaaaaaaaaa",
                                         "counts": {"recommended_relationships": 4, "oss_projects": 4}},
        "data/canonical/relationships.jsonl": [
            _rel("trello", "github.com/acme/board", 80.0), _rel("trello", "github.com/gone/away", 75.0),
            _rel("bitly", "github.com/old/short", 78.0), _rel("pingdom", "github.com/acme/ping", 86.5),
            _rel("trello", "github.com/acme/later", None, recommended=False)],
        "data/canonical/oss_projects.jsonl": [_proj("github.com/acme/board", "board"),
                                              _proj("github.com/gone/away", "away"),
                                              _proj("github.com/old/short", "short"),
                                              _proj("github.com/acme/ping", "ping"),
                                              _proj("github.com/acme/later", "later", health=None)],
        "data/momentum/projects.jsonl": [],
        "runs/2026-10-10-schedule-a1.json": {"status": "succeeded", "corpus": {"corpus_added": 2}},
    })
    _commit(root, "cycle: 2026-10-10-schedule-a1 succeeded", "2026-10-10T02:00:00Z")
    _write(root, {
        "data/canonical/MANIFEST.json": {"dataset_version": "bbbbbbbbbbbbbbbb",
                                         "counts": {"recommended_relationships": 4, "oss_projects": 5}},
        "data/canonical/relationships.jsonl": [
            _rel("trello", "github.com/acme/board", 80.5), _rel("trello", "github.com/acme/new", 82.0),
            _rel("bitly", "github.com/new/short", 78.0), _rel("pingdom", "github.com/acme/ping", 69.4),
            _rel("trello", "github.com/acme/later", 55.0, recommended=False)],
        "data/canonical/oss_projects.jsonl": [_proj("github.com/acme/board", "board"),
                                              _proj("github.com/acme/new", "new"),
                                              _proj("github.com/new/short", "short"),
                                              _proj("github.com/acme/ping", "ping"),
                                              _proj("github.com/acme/later", "later", health=60.0)],
        "data/dataset/oss_projects.jsonl": [{"oss_id": "github.com/gone/away", "name": "away", "live": {"archived": True},
                                             "derived": {"maintenance": {"status": "stale"}, "licence": None,
                                                         "eligibility": {"exclusion_reasons": ["archived"]}}}],
        "data/momentum/projects.jsonl": [_momentum("github.com/acme/board", 5000, 900, "surging"),
                                         _momentum("github.com/tiny/thing", 50, 40, "surging"),
                                         _momentum("github.com/acme/ping", 2000, -30, "declining"),
                                         _momentum("github.com/acme/flat", 2000, 2, "flat")],
        "data/intake/outcomes/req-1.json": {"outcome": "accepted", "request_id": "req-1",
                                            "oss_id": "github.com/acme/new", "saas_hint": "trello",
                                            "assessed_at": "2026-10-15T00:00:00Z"},
        "data/intake/outcomes/req-2.json": {"outcome": "rejected", "request_id": "req-2", "oss_id": "github.com/x/y"},
        "runs/2026-10-18-schedule-a1.json": {"status": "succeeded", "corpus": {"corpus_added": 3, "top_new_by_stars": [
            {"id": "github.com/acme/fresh", "stars": 300, "licence_status": "recognised"},
            {"id": "github.com/acme/unlicensed", "stars": 900, "licence_status": "issue"}]}},
        "data/verdicts/verdicts.jsonl": [],
    })
    _commit(root, "cycle: 2026-10-18-schedule-a1 succeeded", "2026-10-18T02:00:00Z")
    monkeypatch.setattr(common, "ROOT", root)
    return root


def test_digest_lists_only_the_meaningful_changes(history):
    d = review_digest.build(7)
    assert d["week"] == "2026-W42" and d["window"]["since_days"] == 7
    assert d["dataset_version"] == {"before": "aaaaaaaaaaaaaaaa", "after": "bbbbbbbbbbbbbbbb"}
    assert [(r["saas_id"], r["oss_id"]) for r in d["newly_recommended"]] == \
        [("bitly", "github.com/new/short"), ("trello", "github.com/acme/new")]
    gone = {r["oss_id"]: r["reason"] for r in d["no_longer_recommended"]}
    assert set(gone) == {"github.com/gone/away", "github.com/old/short"}
    assert "archived" in gone["github.com/gone/away"] and "maintenance stale" in gone["github.com/gone/away"]
    assert "renamed or moved: github.com/new/short" in gone["github.com/old/short"]
    assert [r["oss_id"] for r in d["dropped_projects"]] == ["github.com/gone/away", "github.com/old/short"]
    assert [r["oss_id"] for r in d["newly_scored"]] == ["github.com/acme/later"]
    assert [r["oss_id"] for r in d["newly_health_scored"]] == ["github.com/acme/later"]
    # A 0.5-point wobble is not a review item; a 17-point drop is.
    assert [(m["oss_id"], m["change"]) for m in d["fit_moves"]] == [("github.com/acme/ping", -17.1)]
    assert [m["key"] for m in d["momentum"]["surging"]] == ["github.com/acme/board"]  # tiny projects left out
    assert d["momentum"]["surging"][0]["canonical"] is True
    assert [m["key"] for m in d["momentum"]["declining"]] == ["github.com/acme/ping"]
    assert [r["request_id"] for r in d["intake_accepted"]] == ["req-1"]
    assert d["corpus_added"] == 3 and d["cycles"] == 1
    assert [r["oss_id"] for r in d["corpus_admissions"]] == ["github.com/acme/fresh"]
    assert d["keith_verdicts_recorded"] == [] and d["review_items"] > 0


def test_digest_is_deterministic_and_written_as_markdown_and_json(history, tmp_path):
    a, b = review_digest.build(7), review_digest.build(7)
    assert a == b
    paths = review_digest.write(a, tmp_path / "out")
    md = (tmp_path / "out" / "digest-2026-W42.md").read_text()
    assert paths["markdown"].endswith("digest-2026-W42.md")
    assert md.startswith("# Review digest 2026-W42\n")
    for heading in ("## Summary", "## Newly recommended", "## No longer recommended", "## Projects dropped out",
                    "## Intake acceptances", "## Keith verdicts recorded"):
        assert heading in md
    assert "`github.com/acme/new` for **trello**" in md
    assert json.loads((tmp_path / "out" / "digest-2026-W42.json").read_text()) == json.loads(json.dumps(a))


def test_window_shorter_than_the_history_compares_with_the_first_version(history):
    d = review_digest.build(30)
    assert d["window"]["note"] and d["dataset_version"]["before"] == "aaaaaaaaaaaaaaaa"
    one_day = review_digest.build(1)  # the newest MANIFEST commit at least a day before the end is the first
    assert one_day["window"]["base"] == d["window"]["base"]


def test_cli(history, tmp_path, capsys):
    assert review_digest.main(["--since-days", "7", "--out", str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] and out["week"] == "2026-W42" and out["counts"]["newly_recommended"] == 2
