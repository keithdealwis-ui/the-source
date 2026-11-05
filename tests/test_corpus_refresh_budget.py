"""KEI-961: the corpus refresh is bounded, says what it is waiting on, and resumes. No network.

Root cause of the 2026-10-29..11-04 cancellations: GitLab's anonymous API answered 429 with
Retry-After ~1160 s (ratelimit-name unauthenticated_traffic_per_ip) and Http slept on it, silently,
up to 30 min a retry, past the job's 60-minute timeout.
"""
from __future__ import annotations

import io
import shutil
import urllib.error
from datetime import datetime, timezone

import pytest

from source_pipeline import common, corpus as C, discover, enrich

from tests.test_corpus import FakeApi, _meta, cfg_root  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def _no_network(monkeypatch):
    monkeypatch.setattr(enrich.common, "refuse_network", lambda url: None)


# --------------------------------------------------------------------------- Http bounds


def test_a_retry_after_longer_than_max_sleep_is_a_logged_failure_not_a_silent_sleep(monkeypatch, capsys):
    attempts = []

    def limited(req, timeout=60):
        attempts.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     {"Retry-After": "1162", "ratelimit-name": "unauthenticated_traffic_per_ip"},
                                     io.BytesIO(b""))

    _no_network(monkeypatch)
    monkeypatch.setattr(enrich.urllib.request, "urlopen", limited)
    monkeypatch.setattr(enrich.time, "sleep", lambda s: (_ for _ in ()).throw(AssertionError(f"slept {s}")))
    http = enrich.Http()
    http.max_sleep = 120
    assert http.call("https://gitlab.com/api/v4/projects/a%2Fb?license=true") == (429, None)
    assert len(attempts) == 1 and http.failures == ["429 https://gitlab.com/api/v4/projects/a%2Fb"]
    err = capsys.readouterr().err
    assert "Retry-After" in err and "1162s exceeds max_sleep 120s" in err and "license=true" not in err


def test_long_waits_are_logged_and_counted(monkeypatch, capsys):
    calls = []

    def flaky(req, timeout=60):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {"Retry-After": "60"}, io.BytesIO(b""))

        class R:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"
        return R()

    slept = []
    _no_network(monkeypatch)
    monkeypatch.setattr(enrich.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(enrich.time, "sleep", slept.append)
    http = enrich.Http()
    assert http.call("https://api.github.com/graphql") == (200, {})
    assert slept == [60.0] and http.waited == 60.0
    assert "waiting 60s (attempt 1/5)" in capsys.readouterr().err


def test_past_the_deadline_no_request_is_made(monkeypatch):
    _no_network(monkeypatch)
    monkeypatch.setattr(enrich.urllib.request, "urlopen", lambda *a, **k: pytest.fail("request made"))
    http = enrich.Http()
    http.deadline = 0.0  # long past
    assert http.expired() and http.call("https://api.github.com/graphql") == (0, None)
    assert http.failures == ["deadline https://api.github.com/graphql"]


def test_a_wait_that_would_pass_the_deadline_is_not_taken(monkeypatch):
    def limited(req, timeout=60):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {"Retry-After": "90"}, io.BytesIO(b""))

    _no_network(monkeypatch)
    monkeypatch.setattr(enrich.urllib.request, "urlopen", limited)
    monkeypatch.setattr(enrich.time, "sleep", lambda s: pytest.fail("slept"))
    http = enrich.Http()
    http.deadline = enrich.time.monotonic() + 30
    assert http.call("https://api.github.com/graphql")[0] == 403


# --------------------------------------------------------------------------- meta_batch


class GqlHttp:
    def __init__(self, status):
        self.status, self.calls, self.failures, self.n = status, 0, [], 0

    def expired(self):
        return False

    def call(self, url, data=None, auth=False, **kw):
        self.n += 1
        return self.status, None


def test_a_rate_limited_batch_is_not_halved_into_more_refused_requests():
    api = discover.Api.__new__(discover.Api)
    api.http = GqlHttp(403)
    out = api.meta_batch([f"o/r{i}" for i in range(50)], size=50)
    assert api.http.n == 1  # one request, not 99
    assert len(out) == 50 and all(v == {"found": False, "error": "graphql_http_403"} for v in out.values())


def test_a_heavy_batch_that_times_out_is_still_halved():
    api = discover.Api.__new__(discover.Api)
    api.http = GqlHttp(502)
    out = api.meta_batch([f"o/r{i}" for i in range(4)], size=4)
    assert api.http.n == 7 and len(out) == 4  # 4 -> 2+2 -> 1+1+1+1


# --------------------------------------------------------------------------- resumable refresh


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class SlowApi(FakeApi):
    """Every lookup takes `step` seconds of the fake clock."""

    def __init__(self, answers, clock, step):
        super().__init__(answers)
        self.clock, self.step = clock, step

    def meta_batch(self, slugs, size=50):
        self.clock.t += self.step.pop(0) if isinstance(self.step, list) else self.step
        return super().meta_batch(slugs, size)


ANSWERS = {"alice/scraper": _meta("alice/scraper", 99), "bob/seeded": _meta("bob/seeded", 5),
           "carol/nolicence": _meta("carol/nolicence", 7)}


@pytest.fixture
def one_row_chunks(cfg_root, monkeypatch):  # noqa: F811
    text = (cfg_root / "config" / "corpus.yaml").read_text().replace("chunk_size: 2500", "chunk_size: 1")
    (cfg_root / "config" / "corpus.yaml").write_text(text)
    out = cfg_root / "data" / "corpus"
    C.export(cfg_root / "data" / "discover" / "corpus", out)
    clock = Clock()
    import time

    monkeypatch.setattr(time, "monotonic", clock)
    return out, clock


def test_a_refresh_that_reaches_its_budget_promotes_what_it_read_and_resumes(one_row_chunks):
    out, clock = one_row_chunks
    first = C.refresh(NOW, out, SlowApi(ANSWERS, clock, 100), budget_seconds=250)
    assert first["status"] == "partial" and first["this_run"]["chunks"] == 2
    assert "budget" in first["this_run"]["stopped"]
    m = common.read_json(out / "MANIFEST.json")
    prog = m["refresh"]["in_progress"]
    assert prog["started_at"] == "2026-10-10T12:00:00Z" and (prog["done"], prog["total"]) == (2, 3)
    assert prog["cursor"] == "github.com/bob/seeded" and prog["stats"]["resolved"] == 2
    assert m["refresh"]["last"] is None  # a part-pass never counts as a refresh
    assert C.validate(out)["failed_checks"] == []
    live = {r["id"]: r for r in common.read_jsonl(out / "live.jsonl")}
    assert live["github.com/alice/scraper"]["stars"] == 99
    assert live["github.com/carol/nolicence"]["observed_as_of"] == "2026-09-29T10:34:38Z"  # not read yet
    d = C.due(LATER, out)
    assert d["due"] is True and d["in_progress"]["done"] == 2

    second = C.refresh(LATER, out, SlowApi(ANSWERS, clock, 100), budget_seconds=3600)
    assert second["status"] == "complete" and second["this_run"]["chunks"] == 1
    m = common.read_json(out / "MANIFEST.json")
    last = m["refresh"]["last"]
    assert "in_progress" not in m["refresh"]
    # The pass is dated from its start, so the layer never claims to be fresher than its oldest row.
    assert last["at"] == "2026-10-10T12:00:00Z" and last["completed_at"] == "2026-10-11T12:00:00Z"
    assert last["runs"] == 2 and last["resolved"] == 3
    live = {r["id"]: r for r in common.read_jsonl(out / "live.jsonl")}
    assert live["github.com/alice/scraper"]["observed_as_of"] == "2026-10-10T12:00:00Z"
    assert live["github.com/carol/nolicence"]["observed_as_of"] == "2026-10-11T12:00:00Z"
    assert C.validate(out)["failed_checks"] == []
    assert C.due(LATER, out)["due"] is False


def test_a_chunk_overtaken_by_the_deadline_is_not_promoted(one_row_chunks):
    out, clock = one_row_chunks
    res = C.refresh(NOW, out, SlowApi(ANSWERS, clock, [10, 200]), budget_seconds=150)
    # chunk 1 (10 s) is kept; chunk 2 runs 200 s, past the deadline, and is discarded
    assert res["status"] == "partial" and res["this_run"]["chunks"] == 1 and "deadline" in res["this_run"]["stopped"]
    live = {r["id"]: r for r in common.read_jsonl(out / "live.jsonl")}
    assert live["github.com/alice/scraper"]["stars"] == 99 and live["github.com/bob/seeded"]["stars"] == 10
    assert common.read_json(out / "MANIFEST.json")["refresh"]["in_progress"]["cursor"] == "github.com/alice/scraper"


def test_an_unreadable_chunk_after_progress_stops_the_pass_without_promoting_it(one_row_chunks):
    out, clock = one_row_chunks
    answers = {k: v for k, v in ANSWERS.items() if k != "bob/seeded"}  # bob: graphql_http_502
    res = C.refresh(NOW, out, SlowApi(answers, clock, 1), budget_seconds=3600)
    assert res["status"] == "partial" and res["this_run"]["stopped"].startswith("unresolved")
    m = common.read_json(out / "MANIFEST.json")
    assert m["refresh"]["in_progress"]["cursor"] == "github.com/alice/scraper" and m["refresh"]["last"] is None


def test_due_reports_staleness(cfg_root):  # noqa: F811
    out = cfg_root / "data" / "corpus"
    C.export(cfg_root / "data" / "discover" / "corpus", out)
    assert C.due(datetime(2026, 10, 5, tzinfo=timezone.utc), out)["stale"] is False
    d = C.due(datetime(2026, 10, 12, tzinfo=timezone.utc), out)
    assert d["stale"] is True and d["stale_after_days"] == 10 and d["due"] is True


def test_a_pass_older_than_a_refresh_cycle_is_restarted_not_finished(one_row_chunks):
    out, clock = one_row_chunks
    C.refresh(NOW, out, SlowApi(ANSWERS, clock, 100), budget_seconds=250)  # 2 of 3 rows, pass from NOW
    week_later = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    res = C.refresh(week_later, out, SlowApi(ANSWERS, clock, 1), budget_seconds=3600)
    assert res["status"] == "complete" and res["this_run"]["chunks"] == 3
    last = common.read_json(out / "MANIFEST.json")["refresh"]["last"]
    assert last["at"] == "2026-10-17T12:00:00Z" and last["runs"] == 1 and last["resolved"] == 3
