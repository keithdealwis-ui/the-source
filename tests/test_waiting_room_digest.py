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
    assert subject.startswith("The Source daily: 2 waiting-room results")
    assert "Accepted and added to The Source" in text and "Held: needs more evidence" in text
    assert "not accepted: repo_url_not_a_repository" in text
    assert "private note do-not-leak" not in text + html_body   # free-text notes stay out of the email
    assert "<script" not in html_body


def test_digest_says_so_when_nothing_came_back(env):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    subject, text, _ = D.render(D.collect(env["store"].dir, now, 24))
    assert "0 waiting-room results" in subject and "No results came back" in text
    assert "No run record found" in text and "No usage figures" in text


def test_results_outside_the_window_are_left_out(env):
    s = env["store"]
    s.submit(producer(env, "gitwatch"), payload(), env["corpus"])
    _seal(env)
    _outcome(env, W.request_id_for("github.com/owner/repo", 0), "rejected", reasons=["no_licence"])
    s.reconcile(env["corpus"])
    later = datetime.now(timezone.utc) + timedelta(hours=25)
    assert not any(D.collect(s.dir, later, 24)["results"].values())


RUN = {"run_id": "2026-11-02-schedule-a1", "status": "succeeded", "ended_at": "2026-11-02T00:52:45Z", "error": None,
       "corpus": {"membership_before": 25107, "membership_after": 25113,
                  "candidates_found_by_strategy": {"hn": {"found": 139, "new_to_corpus": 5}, "gh": {"found": 221, "new_to_corpus": 3}},
                  "top_new_by_stars": [{"id": "github.com/l0ng-ai/openidea", "stars": 3479}],
                  "quick": {"health_rescored": 183, "pairings_rescored": 0}},
       "counts": {"corpus_added": 6, "rescored": 40, "momentum_measured": 1525},
       "transitions": {"became_recommendable": ["github.com/a/b"], "no_longer_recommendable": []},
       "failures": [], "anomalies": []}
TELEMETRY = {"days": {
    "2026-11-01": {"source_search": {"ok": {"count": 8, "empty": 1, "latency": {"<50ms": 5, ">=1000ms": 3}}},
                   "source_get_project": {"ok": {"count": 3}, "tool_error": {"count": 1}}},
    "2026-10-31": {"source_search": {"ok": {"count": 7}}}}}


def test_cycle_summary_counts_new_and_rescored_projects():
    c = D.summarise_cycle(RUN)
    assert (c["added"], c["found"], c["new_to_corpus"], c["strategies"]) == (6, 360, 8, 2)
    assert (c["rescored"], c["health_rescored"], c["rescored_total"]) == (40, 183, 223)
    assert c["became"] == ["github.com/a/b"]


def test_fetch_cycle_falls_back_to_yesterday_and_says_so():
    now = datetime(2026, 11, 3, 7, tzinfo=timezone.utc)
    seen = []

    def fetch(url):
        seen.append(url)
        if "2026-11-03" in url:
            raise OSError("404")
        return RUN
    c = D.fetch_cycle(now, fetch)
    assert "2026-11-02" in c["stale"] and len(seen) == 2
    assert D.fetch_cycle(now, lambda url: (_ for _ in ()).throw(OSError())) is None


def test_usage_for_a_day_with_client_types():
    lines = ["2026-11-01\topenai-mcp/1.0.0", "2026-11-01\topenai-mcp/1.0.0", "2026-11-01\tMoonglade/1.0 (+https://moonglade.sh/bot)",
             "2026-10-31\topenai-mcp/1.0.0"]
    u = D.usage_for(TELEMETRY, "2026-11-01", lines)
    assert (u["calls"], u["tool_errors"], u["empty"]) == (12, 1, 1)
    assert u["tools"][0] == ("source_search", {"calls": 8, "errors": 0, "empty": 1, "slow": 3})
    assert u["avg7"] == 1.0 and u["clients"] == [("ChatGPT", 2), ("Bots and crawlers", 1)]


def test_extract_clients_keeps_only_day_and_agent(tmp_path):
    log = tmp_path / "access.log"
    log.write_text('9.9.9.9 - - [01/Nov/2026:10:00:00 +0000] "POST /mcp HTTP/1.1" 200 5 "-" "openai-mcp/1.0.0"\n'
                   '9.9.9.9 - - [01/Nov/2026:10:00:01 +0000] "POST /gitwatch/mcp HTTP/1.1" 200 5 "-" "x"\n'
                   '9.9.9.9 - - [01/Nov/2026:10:00:02 +0000] "GET /other HTTP/1.1" 200 5 "-" "y"\n')
    out = tmp_path / "clients.tsv"
    assert D.extract_clients([log, tmp_path / "missing.log"], out) == 1
    assert out.read_text() == "2026-11-01\topenai-mcp/1.0.0\n" and "9.9.9.9" not in out.read_text()


def test_render_with_cycle_and_usage(env):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    d = D.collect(env["store"].dir, now, 24)
    d["cycle"], d["usage"] = D.summarise_cycle(RUN), D.usage_for(TELEMETRY, "2026-11-01")
    subject, text, html_body = D.render(d)
    assert subject.startswith("The Source daily: 6 new projects, 223 rescored, 12 MCP calls, 0 waiting-room results")
    assert "Added to the corpus: 6 (corpus 25,107 to 25,113)" in text
    assert "https://github.com/l0ng-ai/openidea" in html_body and "now recommendable" in text
    assert "source_search: 8, 1 empty, 3 took over a second" in text
