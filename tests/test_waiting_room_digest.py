"""The morning waiting-room digest (KEI-932): what it reports, and that it changes nothing."""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from source_pipeline import waiting_room as W
from source_pipeline import waiting_room_digest as D
from test_waiting_room import _outcome, _seal, env, payload, producer  # noqa: F401  (env is a fixture)


def _fingerprint(state_dir):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(state_dir.iterdir()) if p.name in ("waiting-room.sqlite3", "audit.jsonl")}


def test_digest_reports_results_arrivals_and_waiting_without_changing_state(env):
    s = env["store"]
    gw = producer(env, "gitwatch")
    s.submit(gw, payload(), env["corpus"])
    s.submit(gw, payload("https://github.com/o/held", key="gitwatch-0002"), env["corpus"])
    _seal(env)
    _outcome(env, W.request_id_for("github.com/owner/repo", 0), "accepted", canonical="canonical",
             reasons=["passes every rule; relationship evidenced"])
    _outcome(env, W.request_id_for("github.com/o/held", 0), "needs_more_evidence", reasons=["no_evidenced_saas_relationship"])
    s.reconcile(env["corpus"])
    s.submit(gw, payload("https://github.com/o/later", key="gitwatch-0003"), env["corpus"])
    s.submit(gw, payload("https://example.com/not-a-repo", key="gitwatch-0004"), env["corpus"])

    before = _fingerprint(s.dir)
    now = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)
    d = D.collect(s.dir, now, 24)
    assert _fingerprint(s.dir) == before

    assert [r["canonical_id"] for r in d["results"]["canonical"]] == ["github.com/owner/repo"]
    assert d["results"]["held"][0]["reasons"] == ["no_evidenced_saas_relationship"]
    assert [w["canonical_id"] for w in d["waiting"]] == ["github.com/o/later"]
    assert len(d["arrived"]) == 4 and d["arrived"][-1]["state"] == "invalid"

    subject, text, html_body = D.render(d)
    assert subject.startswith("The Source waiting room: 2 results, 1 waiting")
    assert "Accepted and added to The Source" in text and "Held: needs more evidence" in text
    assert "not accepted: repo_url_not_a_repository" in text
    assert "private note do-not-leak" not in text + html_body   # free-text notes stay out of the email
    assert "<script" not in html_body


def test_digest_says_so_when_nothing_came_back(env):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    subject, text, _ = D.render(D.collect(env["store"].dir, now, 24))
    assert "0 results, 0 waiting" in subject and "No results came back" in text


def test_results_outside_the_window_are_left_out(env):
    s = env["store"]
    s.submit(producer(env, "gitwatch"), payload(), env["corpus"])
    _seal(env)
    _outcome(env, W.request_id_for("github.com/owner/repo", 0), "rejected", reasons=["no_licence"])
    s.reconcile(env["corpus"])
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    assert not any(D.collect(s.dir, later, 24)["results"].values())
