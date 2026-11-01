"""The waiting room (KEI-932): validation, cross-producer dedupe, signed batches, the consumer
and reason-coded results. No network: the consumer's assessment is stubbed and the HTTP tests
bind to loopback."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from source_pipeline import waiting_room as W
from source_pipeline import waiting_room_http as H

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen is required")

NOW = datetime(2026, 11, 1, 21, 45, tzinfo=timezone.utc)


def payload(url="https://github.com/Owner/Repo", key="gitwatch-0001", provider="chatgpt", **extra):
    p = {"contract": W.CONTRACT, "repo_url": url, "idempotency_key": key,
         "source": {"provider": provider, "observed_at": "2026-11-01T18:00:00Z",
                    "evidence_url": "https://example.org/post/1"},
         "provenance": {"note": "private note do-not-leak"}, "candidate": {"saas_hint": "notion"}}
    p.update(extra)
    return p


@pytest.fixture()
def env(tmp_path):
    corpus = tmp_path / "repo"
    (corpus / "data" / "corpus").mkdir(parents=True)
    (corpus / "data" / "canonical").mkdir(parents=True)
    (corpus / "data" / "intake" / "outcomes").mkdir(parents=True)
    (corpus / "data" / "corpus" / "projects.jsonl").write_text(
        json.dumps({"id": "github.com/known/project", "aliases": ["github.com/known/old-name"], "lanes": ["discover"]}) + "\n")
    (corpus / "data" / "canonical" / "oss_projects.jsonl").write_text(json.dumps({"oss_id": "github.com/known/project"}) + "\n")
    key = tmp_path / "signing_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", W.SIGNER_IDENTITY, "-f", str(key)], check=True)
    signers = tmp_path / "allowed_signers"
    signers.write_text(f'{W.SIGNER_IDENTITY} namespaces="{W.SIGNING_NAMESPACE}" {Path(str(key) + ".pub").read_text().strip()}\n')
    store = W.Store(tmp_path / "state")
    tokens = {name: store.add_producer(name) for name in ("gitwatch", "keith-cli")}
    return {"store": store, "corpus": W.Corpus(corpus), "root": corpus, "key": key, "signers": signers, "tokens": tokens}


def producer(env, name):
    return env["store"].authenticate(env["tokens"][name])


def test_validate_accepts_the_contract_and_normalises_identity():
    f = W.validate(payload("github.com/Owner/Repo.git"))
    assert f["canonical_id"] == "github.com/owner/repo"
    assert f["observed_at"] == "2026-11-01T18:00:00Z"


def test_validate_reports_every_reason():
    bad = {"contract": "v0", "repo_url": "https://example.com/x/y", "idempotency_key": "x", "source": {"provider": "Bad Provider"},
           "extra": 1}
    with pytest.raises(W.Invalid) as e:
        W.validate(bad)
    assert set(e.value.reasons) >= {"contract_unsupported", "unknown_field", "idempotency_key_invalid", "host_not_supported",
                                    "source_provider_invalid", "source_observed_at_invalid"}


def test_two_producers_same_repository_one_entity_two_observations(env):
    s = env["store"]
    c1, b1 = s.submit(producer(env, "gitwatch"), payload("https://github.com/Owner/Repo"), env["corpus"])
    c2, b2 = s.submit(producer(env, "keith-cli"), payload("github.com/owner/repo.git", key="cli-0001", provider="keith"), env["corpus"])
    assert (c1, c2) == (202, 202)
    assert b1["canonical_id"] == b2["canonical_id"] == "github.com/owner/repo"
    assert b2["reasons"] == ["duplicate_of_existing_entity"]
    repo = b2["repository"]
    assert repo["state"] == "received"
    assert repo["observations"] == [{"producer": "gitwatch", "provider": "chatgpt", "count": 1},
                                    {"producer": "keith-cli", "provider": "keith", "count": 1}]
    assert s.summary()["entities"] == {"received": 1}


def test_invalid_submission_is_recorded_with_reason_codes(env):
    code, body = env["store"].submit(producer(env, "gitwatch"), payload("https://example.com/not/a-host", key="gitwatch-bad1"), env["corpus"])
    assert code == 422
    assert body["submission_state"] == "invalid" and body["reasons"] == ["host_not_supported"]
    assert env["store"].readback(body["submission_id"], "gitwatch")["submission_state"] == "invalid"


def test_idempotent_replay_and_conflict(env):
    s, p = env["store"], producer(env, "gitwatch")
    first = s.submit(p, payload(), env["corpus"])[1]
    code, again = s.submit(p, payload(), env["corpus"])
    assert code == 200 and again["replayed"] and again["submission_id"] == first["submission_id"]
    code, conflict = s.submit(p, payload("https://github.com/other/repo"), env["corpus"])
    assert code == 409 and conflict["error"] == "idempotency_conflict"


def test_a_producer_reads_only_its_own_submissions(env):
    sid = env["store"].submit(producer(env, "gitwatch"), payload(), env["corpus"])[1]["submission_id"]
    assert env["store"].readback(sid, "keith-cli") is None


def test_rate_limit(env):
    s = env["store"]
    s.revoke_producer("gitwatch")
    token = s.add_producer("gitwatch", rate_hour=2, rate_day=10)
    p = s.authenticate(token)
    assert [s.submit(p, payload(f"https://github.com/o/r{i}", key=f"gitwatch-r{i:03}"), env["corpus"])[0] for i in range(3)] == [202, 202, 429]


def test_revoked_and_unknown_credentials_are_refused(env):
    s = env["store"]
    assert s.authenticate("swr_not-a-real-token") is None and s.authenticate(None) is None
    s.revoke_producer("keith-cli")
    assert s.authenticate(env["tokens"]["keith-cli"]) is None
    with pytest.raises(ValueError):
        s.add_producer("gitwatch")  # a live credential must be revoked before rotation


def test_a_repository_already_in_the_corpus_is_answered_at_once(env):
    code, body = env["store"].submit(producer(env, "gitwatch"), payload("https://github.com/known/old-name"), env["corpus"])
    assert code == 202
    assert body["repository"]["state"] == "already_known"
    assert body["repository"]["corpus"]["oss_id"] == "github.com/known/project"
    assert body["repository"]["corpus"]["canonical"] is True


def _seal(env, now=NOW):
    return env["store"].seal(env["key"], now=now)


def test_seal_dedupes_signs_and_is_idempotent(env):
    s = env["store"]
    s.submit(producer(env, "gitwatch"), payload(), env["corpus"])
    s.submit(producer(env, "keith-cli"), payload("github.com/owner/repo", key="cli-0001", provider="keith"), env["corpus"])
    s.submit(producer(env, "gitwatch"), payload("https://github.com/known/project", key="gitwatch-0002"), env["corpus"])
    r = _seal(env)
    assert r["items"] == 1 and not r["already_sealed"]
    assert _seal(env)["already_sealed"]
    b = s.latest_batch()
    assert W.ssh_verify(b["payload"], b["signature"], env["signers"])
    item = json.loads(b["payload"])["items"][0]
    assert item["canonical_id"] == "github.com/owner/repo"
    assert item["producers"] == ["gitwatch", "keith-cli"] and item["observations"] == 2
    assert item["request_id"] == W.request_id_for("github.com/owner/repo", 0)
    assert "do-not-leak" not in b["payload"] and "example.org" not in b["payload"]  # no notes or evidence in public batches


def test_consume_verifies_and_assesses_each_repository_once(env):
    env["store"].submit(producer(env, "gitwatch"), payload(), env["corpus"])
    env["store"].submit(producer(env, "keith-cli"), payload("github.com/owner/repo", key="cli-0001", provider="keith"), env["corpus"])
    _seal(env)
    calls = []

    def assess(url, rid, **kw):
        calls.append((url, rid, kw["submitted_by"], kw["saas_hint"]))
        return {"oss_id": "github.com/owner/repo", "outcome": "accepted"}

    out = W.consume(env["store"].latest_batch(), env["signers"], assess=assess, now=NOW + timedelta(minutes=30))
    assert out["status"] == "processed" and len(calls) == 1
    assert calls[0][2] == "waiting-room:gitwatch,keith-cli" and calls[0][3] == "notion"


def test_consume_refuses_a_tampered_batch_and_skips_a_stale_one(env):
    env["store"].submit(producer(env, "gitwatch"), payload(), env["corpus"])
    _seal(env)
    b = env["store"].latest_batch()
    tampered = {**b, "payload": b["payload"].replace("github.com/owner/repo", "github.com/evil/repo")}
    assert W.consume(tampered, env["signers"], assess=lambda *a, **k: pytest.fail("assessed"))["reason"] == "signature_invalid"
    stale = W.consume(b, env["signers"], assess=lambda *a, **k: pytest.fail("assessed"), now=NOW + timedelta(hours=31))
    assert stale["status"] == "skipped" and stale["reason"] == "no_fresh_batch"


def _outcome(env, rid, outcome, canonical="pending_promotion", reasons=("x",)):
    (env["root"] / "data" / "intake" / "outcomes" / f"{rid}.json").write_text(json.dumps(
        {"request_id": rid, "outcome": outcome, "reasons": list(reasons), "oss_id": "github.com/owner/repo",
         "canonical": {"status": canonical}, "scores": None}))


def test_reconcile_gives_reason_coded_results_through_to_canonical(env):
    s = env["store"]
    sid = s.submit(producer(env, "gitwatch"), payload(), env["corpus"])[1]["submission_id"]
    _seal(env)
    rid = W.request_id_for("github.com/owner/repo", 0)
    assert s.reconcile(env["corpus"], now=NOW + timedelta(hours=1)) == {}
    _outcome(env, rid, "accepted", reasons=["passes every rule; relationship evidenced"])
    assert s.reconcile(env["corpus"], now=NOW + timedelta(hours=1)) == {"accepted": 1}
    _outcome(env, rid, "accepted", canonical="canonical", reasons=["passes every rule; relationship evidenced"])
    assert s.reconcile(env["corpus"], now=NOW + timedelta(hours=3)) == {"canonical": 1}
    repo = s.readback(sid, "gitwatch")["repository"]
    assert repo["state"] == "canonical" and repo["attempts"] == 1 and repo["outcome"]["outcome"] == "accepted"


def test_held_rejected_and_dead_letter(env):
    s = env["store"]
    s.submit(producer(env, "gitwatch"), payload(), env["corpus"])
    day = NOW
    for attempt in range(W.MAX_ATTEMPTS):
        _seal(env, now=day)
        _outcome(env, W.request_id_for("github.com/owner/repo", attempt), "failed", reasons=["host_lookup_failed_retry_later"])
        s.reconcile(env["corpus"], now=day + timedelta(hours=1))
        day += timedelta(days=1)
    assert s.summary()["entities"] == {"dead_letter": 1}
    s.submit(producer(env, "gitwatch"), payload("https://github.com/o/held", key="gitwatch-0003"), env["corpus"])
    _seal(env, now=day)
    (env["root"] / "data" / "intake" / "outcomes" / f"{W.request_id_for('github.com/o/held', 0)}.json").write_text(json.dumps(
        {"request_id": "x", "outcome": "needs_more_evidence", "reasons": ["no_evidenced_saas_relationship"], "canonical": {}}))
    s.reconcile(env["corpus"], now=day + timedelta(hours=1))
    assert s.summary()["entities"] == {"dead_letter": 1, "held": 1}


def test_queued_with_no_outcome_is_retried_with_a_fresh_request_id(env):
    s = env["store"]
    s.submit(producer(env, "gitwatch"), payload(), env["corpus"])
    _seal(env)
    s.reconcile(env["corpus"], now=NOW + timedelta(hours=W.NO_OUTCOME_AFTER_H + 1))
    assert s.summary()["entities"] == {"failed_retry": 1}
    _seal(env, now=NOW + timedelta(days=2))
    item = json.loads(s.latest_batch()["payload"])["items"][0]
    assert item["request_id"] == W.request_id_for("github.com/owner/repo", 1) and item["attempt"] == 2


def test_audit_log_never_holds_tokens_or_notes(env):
    env["store"].submit(producer(env, "gitwatch"), payload(), env["corpus"])
    log = env["store"].audit_path.read_text()
    assert "do-not-leak" not in log and not any(t in log for t in env["tokens"].values())
    assert "submission_recorded" in log


@pytest.fixture()
def server(env):
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), H.make_handler(env["store"], env["corpus"]))
    except OSError as exc:  # pragma: no cover - sandboxes without loopback
        pytest.skip(f"no loopback: {exc}")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _call(url, method="GET", body=None, token=None, ctype="application/json"):
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode())
    if body is not None:
        req.add_header("Content-Type", ctype)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_http_round_trip(env, server):
    assert _call(server + "/intake/v1/healthz")[0] == 200
    assert _call(server + "/intake/v1/submissions", "POST", payload())[0] == 401
    assert _call(server + "/intake/v1/submissions", "POST", payload(), token="swr_wrong")[0] == 401
    code, body = _call(server + "/intake/v1/submissions", "POST", payload(), token=env["tokens"]["gitwatch"])
    assert code == 202 and body["canonical_id"] == "github.com/owner/repo"
    code, back = _call(server + body["status_url"], token=env["tokens"]["gitwatch"])
    assert code == 200 and back["repository"]["state"] == "received"
    assert _call(server + body["status_url"], token=env["tokens"]["keith-cli"])[0] == 404
    assert _call(server + "/intake/v1/submissions", "POST", payload(), token=env["tokens"]["gitwatch"], ctype="text/plain")[0] == 415
    assert _call(server + "/intake/v1/batches/latest")[0] == 404
    env["store"].seal(env["key"])
    code, batch = _call(server + "/intake/v1/batches/latest")
    assert code == 200 and W.ssh_verify(batch["payload"], batch["signature"], env["signers"])
    assert _call(server + "/intake/v1/openapi.json")[1]["info"]["title"] == "The Source waiting room"


def test_http_throttles_repeated_failed_authentication(env, server):
    codes = [_call(server + "/intake/v1/submissions", "POST", payload(), token="swr_guess")[0] for _ in range(H.AUTH_FAILURES_PER_MIN + 1)]
    assert codes[-1] == 429 and codes[0] == 401
