"""The waiting room: shared, authenticated intake for every Source producer (KEI-932).

    python -m source_pipeline.waiting_room serve      --state DIR --corpus-root DIR [--port 8852]
    (also `python -m source_pipeline waiting-room ...`; the units call the module directly)
    python -m source_pipeline waiting-room producer-add NAME --state DIR [--rate-hour N] [--rate-day N]
    python -m source_pipeline waiting-room producer-revoke NAME --state DIR
    python -m source_pipeline waiting-room producers  --state DIR
    python -m source_pipeline waiting-room seal       --state DIR --key FILE [--max-items N]
    python -m source_pipeline waiting-room reconcile  --state DIR --corpus-root DIR
    python -m source_pipeline waiting-room status     --state DIR [--submission ID]
    python -m source_pipeline waiting-room consume    --url URL --signers FILE [--max-age-hours H]

One boundary for GitWatch/ChatGPT, Community Radar adapters, future community submissions and
any other approved producer (docs/WAITING-ROOM.md). A producer submits a repository URL with a
versioned contract (`source-submission/1`) and a bearer credential of its own. The waiting
room validates it, canonicalises the repository identity, deduplicates it across producers and
against the corpus, records every source observation, and answers at once with what it already
knows. It never assesses, scores, admits or recommends anything itself.

Once a day it seals the repositories still waiting into a signed batch. The `Waiting room`
GitHub Actions workflow fetches that batch, verifies the signature against the committed
public key, and runs each repository through the existing intake assessment
(source_pipeline/intake.py), which is the only writer of intake outcomes and of the ledger the
daily cycle promotes from. The waiting room then reads the published outcomes back and gives
each submission its reason-coded result. GitHub needs no credential for any of this, and
producers never get write access to the repository.

Standard library plus the pipeline's own identity rules. State lives in one SQLite file and an
append-only audit log; credentials are stored only as SHA-256 hashes, and neither tokens nor
free-text notes ever reach the audit log, a batch or the repository.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .normalise import repo_key

CONTRACT = "source-submission/1"
SIGNING_NAMESPACE = "the-source-waiting-room"
SIGNER_IDENTITY = "waiting-room@the-source"
TOKEN_PREFIX = "swr_"
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._-]{6,80}$")
PRODUCER_NAME = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{6,80}$")
PROVIDER = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
SAAS_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
MAX_URL = 2048
MAX_NOTE = 500
MAX_TEXT = 300
MAX_ATTEMPTS = 3              # assessments per repository before it is dead-lettered
NO_OUTCOME_AFTER_H = 36       # a queued repository with no published outcome by then is retried
DEFAULT_RATE_HOUR = 30
DEFAULT_RATE_DAY = 200
DEFAULT_BATCH_MAX = 200       # at least one producer's daily quota, so a backlog cannot build
SUPPORTED_HOSTS = ("github.com", "gitlab.com", "codeberg.org")

TOP_KEYS = {"contract", "repo_url", "idempotency_key", "source", "provenance", "candidate"}
SOURCE_KEYS = {"provider", "evidence_url", "observed_at"}
PROVENANCE_KEYS = {"note", "on_behalf_of"}
CANDIDATE_KEYS = {"saas_hint", "name", "description"}

# Waiting-room states of a repository (an "entity"). Terminal: canonical, rejected, held,
# already_known, dead_letter. `accepted` waits for the daily cycle to promote it.
ENTITY_STATES = ("received", "queued", "accepted", "canonical", "not_promoted", "rejected", "held",
                 "already_known", "failed_retry", "dead_letter")
SEGMENT = re.compile(r"^[a-z0-9._-]+$")
BATCH_ID = re.compile(r"^wr-\d{8}$")
OUTCOME_STATE = {"accepted": "accepted", "rejected": "rejected", "needs_more_evidence": "held",
                 "already_known": "already_known", "failed": "failed_retry"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS producers (
  name TEXT PRIMARY KEY, token_sha256 TEXT NOT NULL UNIQUE, scopes TEXT NOT NULL,
  rate_per_hour INTEGER NOT NULL, rate_per_day INTEGER NOT NULL,
  created_at TEXT NOT NULL, revoked_at TEXT);
CREATE TABLE IF NOT EXISTS entities (
  canonical_id TEXT PRIMARY KEY, repo_url TEXT NOT NULL, state TEXT NOT NULL,
  reasons TEXT NOT NULL DEFAULT '[]', first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, request_id TEXT, batch_id TEXT, queued_at TEXT,
  corpus TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS submissions (
  submission_id TEXT PRIMARY KEY, producer TEXT NOT NULL, idempotency_key TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL, received_at TEXT NOT NULL, state TEXT NOT NULL,
  reasons TEXT NOT NULL DEFAULT '[]', repo_url TEXT, canonical_id TEXT,
  provider TEXT, evidence_url TEXT, observed_at TEXT, saas_hint TEXT, note TEXT,
  UNIQUE (producer, idempotency_key));
CREATE INDEX IF NOT EXISTS submissions_by_producer_time ON submissions (producer, received_at);
CREATE INDEX IF NOT EXISTS submissions_by_entity ON submissions (canonical_id);
CREATE TABLE IF NOT EXISTS batches (
  batch_id TEXT PRIMARY KEY, sealed_at TEXT NOT NULL, payload TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL, signature TEXT NOT NULL, items INTEGER NOT NULL);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(s: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return t if t.tzinfo else None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_id_for(canonical_id: str, attempt: int) -> str:
    """The intake request id of a repository's n-th assessment. Stable, so a re-sent batch is
    answered from the recorded outcome; a retry after `failed` needs a fresh one (intake.py)."""
    base = "wr-" + _sha(canonical_id)[:16]
    return base if attempt == 0 else f"{base}-r{attempt}"


class Invalid(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__(", ".join(reasons))
        self.reasons = reasons


def _is_http_url(u) -> bool:
    return isinstance(u, str) and len(u) <= MAX_URL and re.match(r"^https?://[^\s/$.?#][^\s]*$", u, re.I) is not None


def validate(payload) -> dict:
    """Check a submission against source-submission/1. Returns the normalised fields, or raises
    Invalid with every reason code that applies."""
    if not isinstance(payload, dict):
        raise Invalid(["payload_not_an_object"])
    reasons = []
    if payload.get("contract") != CONTRACT:
        reasons.append("contract_unsupported")
    extra = set(payload) - TOP_KEYS
    if extra:
        reasons.append("unknown_field")
    key = payload.get("idempotency_key")
    if not isinstance(key, str) or not IDEMPOTENCY_KEY.match(key):
        reasons.append("idempotency_key_invalid")
    url = payload.get("repo_url")
    canonical_id = None
    if not _is_http_url(url) and not (isinstance(url, str) and 0 < len(url) <= MAX_URL and " " not in url):
        reasons.append("repo_url_invalid")
    else:
        canonical_id, why = repo_key(url)
        if canonical_id is None:
            reasons.append(why)
        elif canonical_id.split("/", 1)[0] not in SUPPORTED_HOSTS:
            reasons.append("host_not_supported")
            canonical_id = None
        elif not all(SEGMENT.match(seg) and seg.strip(".") for seg in canonical_id.split("/")[1:]):
            reasons.append("repo_url_not_a_repository")  # percent-encoding, dot tricks and the like
            canonical_id = None
        elif "@" in url.split("://", 1)[-1].split("/", 1)[0]:
            reasons.append("repo_url_userinfo_not_allowed")
            canonical_id = None
    src = payload.get("source")
    provider = evidence_url = observed_at = None
    if not isinstance(src, dict) or set(src) - SOURCE_KEYS:
        reasons.append("source_invalid")
    else:
        provider = src.get("provider")
        if not isinstance(provider, str) or not PROVIDER.match(provider):
            reasons.append("source_provider_invalid")
        evidence_url = src.get("evidence_url")
        if evidence_url is not None and not _is_http_url(evidence_url):
            reasons.append("source_evidence_url_invalid")
        observed_at = src.get("observed_at")
        t = _parse_ts(observed_at) if isinstance(observed_at, str) else None
        if t is None:
            reasons.append("source_observed_at_invalid")
        elif t > _now() + timedelta(days=1):
            reasons.append("source_observed_at_in_future")
        else:
            observed_at = _iso(t.astimezone(timezone.utc))
    prov = payload.get("provenance") or {}
    note = None
    if not isinstance(prov, dict) or set(prov) - PROVENANCE_KEYS:
        reasons.append("provenance_invalid")
    else:
        note = prov.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
            reasons.append("provenance_note_invalid")
        obo = prov.get("on_behalf_of")
        if obo is not None and (not isinstance(obo, str) or not PROVIDER.match(obo)):
            reasons.append("provenance_on_behalf_of_invalid")
    cand = payload.get("candidate") or {}
    saas_hint = None
    if not isinstance(cand, dict) or set(cand) - CANDIDATE_KEYS:
        reasons.append("candidate_invalid")
    else:
        saas_hint = cand.get("saas_hint")
        if saas_hint is not None and (not isinstance(saas_hint, str) or not SAAS_ID.match(saas_hint)):
            reasons.append("candidate_saas_hint_invalid")
        for k in ("name", "description"):
            v = cand.get(k)
            if v is not None and (not isinstance(v, str) or len(v) > MAX_TEXT):
                reasons.append(f"candidate_{k}_invalid")
    if reasons:
        raise Invalid(sorted(set(reasons)))
    return {"idempotency_key": key, "repo_url": url.strip(), "canonical_id": canonical_id, "provider": provider,
            "evidence_url": evidence_url, "observed_at": observed_at, "saas_hint": saas_hint, "note": note}


class Corpus:
    """What The Source already holds, read from a checkout of the public repository (on Bravo,
    the MCP host's hourly-refreshed clone). Reloaded when the files change."""

    def __init__(self, root: Path | None):
        self.root = Path(root) if root else None
        self._sig = None
        self._index: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._checked = 0.0

    def _files(self):
        r = self.root / "data"
        return [r / "corpus" / "projects.jsonl", r / "canonical" / "oss_projects.jsonl", r / "dataset" / "oss_projects.jsonl"]

    def _load(self):
        idx: dict[str, dict] = {}
        corpus, canon, dataset = self._files()
        if corpus.exists():
            with corpus.open() as fh:
                for line in fh:
                    p = json.loads(line)
                    rec = {"oss_id": p["id"], "lanes": p.get("lanes") or [], "canonical": False, "held_in": ["corpus"]}
                    idx[p["id"]] = rec
                    for a in p.get("aliases") or []:
                        idx.setdefault(str(a).lower(), rec)
        if canon.exists():
            with canon.open() as fh:
                for line in fh:
                    p = json.loads(line)
                    rec = idx.setdefault(p["oss_id"], {"oss_id": p["oss_id"], "lanes": [], "canonical": False, "held_in": []})
                    rec["canonical"] = True
                    rec["held_in"].append("canonical")
        if dataset.exists():  # the Replace lane, including projects it excluded (as intake._known reads it)
            with dataset.open() as fh:
                for line in fh:
                    p = json.loads(line)
                    rec = idx.setdefault(p["oss_id"], {"oss_id": p["oss_id"], "lanes": [], "canonical": False, "held_in": []})
                    rec["held_in"].append("dataset")
                    rec["lanes"] = sorted(set(rec["lanes"]) | {"replace:" + str((p.get("derived") or {}).get("lane"))})
                    for a in p.get("harvested_as") or []:
                        idx.setdefault(str(a).lower(), rec)
        ledger = self.root / "data" / "intake" / "admitted"
        if ledger.exists():
            for f in ledger.glob("*.json"):
                row = json.loads(f.read_text())
                rec = idx.setdefault(row["oss_id"], {"oss_id": row["oss_id"], "lanes": [], "canonical": False, "held_in": []})
                rec["held_in"].append("intake_ledger")
        self._index = idx

    def lookup(self, canonical_id: str) -> dict | None:
        if self.root is None:
            return None
        with self._lock:
            if time.monotonic() - self._checked > 60 or self._sig is None:
                self._checked = time.monotonic()
                sig = tuple((f.stat().st_mtime_ns if f.exists() else 0) for f in self._files())
                if sig != self._sig:
                    self._load()
                    self._sig = sig
            hit = self._index.get(canonical_id)
            if not hit:
                return None
            hit = {**hit, "held_in": sorted(set(hit["held_in"]))}
            # The discovery tier alone is not "known": like intake._known, only the dataset, the
            # canonical layer and the ledger count, so a discovery-only project is still assessed.
            hit["assessed"] = bool(set(hit["held_in"]) & {"canonical", "dataset", "intake_ledger"})
            return hit

    def outcome(self, request_id: str) -> dict | None:
        p = self.root / "data" / "intake" / "outcomes" / f"{request_id}.json"
        return json.loads(p.read_text()) if p.exists() else None


class Store:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = self.dir / "audit.jsonl"
        self._lock = threading.RLock()   # one connection, shared by the server's threads
        self.db = sqlite3.connect(self.dir / "waiting-room.sqlite3", check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA)

    # -- audit ------------------------------------------------------------------------------
    def audit(self, event: str, **fields):
        """Append-only. Never a token, a payload or a free-text note."""
        rec = {"ts": _iso(_now()), "event": event, **fields}
        line = canonical_json(rec) + "\n"
        fd = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)

    # -- producers --------------------------------------------------------------------------
    def add_producer(self, name: str, rate_hour=DEFAULT_RATE_HOUR, rate_day=DEFAULT_RATE_DAY,
                     scopes=("submit", "read_own")) -> str:
        if not PRODUCER_NAME.match(name):
            raise ValueError("producer name must be 2-40 of [a-z0-9-], starting with a letter")
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            row = self.db.execute("SELECT revoked_at FROM producers WHERE name=?", (name,)).fetchone()
            if row and row["revoked_at"] is None:
                raise ValueError(f"producer {name} already has a live credential; revoke it first to rotate")
            self.db.execute("INSERT OR REPLACE INTO producers VALUES (?,?,?,?,?,?,NULL)",
                            (name, _sha(token), ",".join(scopes), rate_hour, rate_day, _iso(_now())))
        self.audit("producer_added", producer=name, scopes=list(scopes), rate_per_hour=rate_hour, rate_per_day=rate_day)
        return token

    def revoke_producer(self, name: str) -> bool:
        with self._lock:
            cur = self.db.execute("UPDATE producers SET revoked_at=? WHERE name=? AND revoked_at IS NULL",
                                  (_iso(_now()), name))
        if cur.rowcount:
            self.audit("producer_revoked", producer=name)
        return bool(cur.rowcount)

    def producers(self) -> list[dict]:
        return [{k: r[k] for k in ("name", "scopes", "rate_per_hour", "rate_per_day", "created_at", "revoked_at")}
                for r in self.db.execute("SELECT * FROM producers ORDER BY name")]

    def authenticate(self, token: str | None) -> dict | None:
        if not token or not token.startswith(TOKEN_PREFIX) or len(token) > 100:
            return None
        digest = _sha(token)
        with self._lock:
            row = self.db.execute("SELECT * FROM producers WHERE token_sha256=? AND revoked_at IS NULL", (digest,)).fetchone()
        if row is None or not hmac.compare_digest(row["token_sha256"], digest):
            return None
        return dict(row)

    def _rate_ok(self, producer: dict, now: datetime) -> bool:
        def count(since):
            return self.db.execute("SELECT COUNT(*) FROM submissions WHERE producer=? AND received_at>=?",
                                   (producer["name"], _iso(since))).fetchone()[0]
        return count(now - timedelta(hours=1)) < producer["rate_per_hour"] and count(now - timedelta(days=1)) < producer["rate_per_day"]

    # -- submissions ------------------------------------------------------------------------
    def submit(self, producer: dict, payload, corpus: Corpus) -> tuple[int, dict]:
        """Record one submission. Returns (HTTP status, body)."""
        now = _now()
        if "submit" not in producer["scopes"].split(","):
            return 403, {"error": "scope_missing", "detail": "this credential may not submit"}
        raw_key = payload.get("idempotency_key") if isinstance(payload, dict) else None
        digest = _sha(canonical_json(payload))
        with self._lock:
            if isinstance(raw_key, str) and IDEMPOTENCY_KEY.match(raw_key) and not raw_key.startswith("invalid-"):
                prior = self.db.execute("SELECT * FROM submissions WHERE producer=? AND idempotency_key=?",
                                        (producer["name"], raw_key)).fetchone()
                if prior is not None:
                    if prior["payload_sha256"] != digest:
                        return 409, {"error": "idempotency_conflict",
                                     "detail": "this idempotency_key was used for a different submission",
                                     "submission_id": prior["submission_id"]}
                    return 200, {**self.readback(prior["submission_id"], producer["name"]), "replayed": True}
            if not self._rate_ok(producer, now):
                self.audit("rate_limited", producer=producer["name"])
                return 429, {"error": "rate_limited", "detail": "producer rate limit reached; retry later"}
            sid = "sub-" + secrets.token_hex(8)
            try:
                f = validate(payload)
            except Invalid as exc:
                key = f"invalid-{sid}"  # never reserves the producer's key: a corrected payload may reuse it
                url = payload.get("repo_url") if isinstance(payload, dict) and isinstance(payload.get("repo_url"), str) else None
                self.db.execute(
                    "INSERT INTO submissions (submission_id, producer, idempotency_key, payload_sha256, received_at,"
                    " state, reasons, repo_url) VALUES (?,?,?,?,?,?,?,?)",
                    (sid, producer["name"], key, digest, _iso(now), "invalid", json.dumps(exc.reasons),
                     url[:MAX_URL] if url else None))
                self.audit("submission_invalid", producer=producer["name"], submission_id=sid, reasons=exc.reasons)
                return 422, self.readback(sid, producer["name"])
            cid = f["canonical_id"]
            ent = self.db.execute("SELECT * FROM entities WHERE canonical_id=?", (cid,)).fetchone()
            duplicate = ent is not None
            if ent is None:
                known = corpus.lookup(cid)
                state, reasons = (("already_known", ["already assessed by The Source"]) if known and known["assessed"]
                                  else ("received", ["in the discovery tier, not yet assessed"] if known else []))
                self.db.execute(
                    "INSERT INTO entities (canonical_id, repo_url, state, reasons, first_seen_at, updated_at, corpus)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (cid, "https://" + cid, state, json.dumps(reasons), _iso(now), _iso(now),
                     json.dumps(known) if known else None))
            self.db.execute(
                "INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, producer["name"], f["idempotency_key"], digest, _iso(now), "recorded",
                 json.dumps(["duplicate_of_existing_entity"] if duplicate else []), f["repo_url"], cid,
                 f["provider"], f["evidence_url"], f["observed_at"], f["saas_hint"], f["note"]))
        self.audit("submission_recorded", producer=producer["name"], submission_id=sid, canonical_id=cid,
                   provider=f["provider"], duplicate=duplicate)
        return 202, self.readback(sid, producer["name"])

    def readback(self, submission_id: str, producer: str | None) -> dict | None:
        """A submission and the standing of its repository. A producer reads only its own."""
        with self._lock:
            return self._readback(submission_id, producer)

    def _readback(self, submission_id: str, producer: str | None) -> dict | None:
        """producer None is the operator's view: every observation, by producer."""
        q, args = "SELECT * FROM submissions WHERE submission_id=?", [submission_id]
        if producer is not None:
            q += " AND producer=?"
            args.append(producer)
        s = self.db.execute(q, args).fetchone()
        if s is None:
            return None
        body = {"contract": CONTRACT, "submission_id": s["submission_id"], "producer": s["producer"],
                "idempotency_key": s["idempotency_key"], "received_at": s["received_at"],
                "submission_state": s["state"], "reasons": json.loads(s["reasons"]),
                "repo_url": s["repo_url"], "canonical_id": s["canonical_id"],
                "status_url": f"/intake/v1/submissions/{s['submission_id']}"}
        if s["canonical_id"]:
            e = self.db.execute("SELECT * FROM entities WHERE canonical_id=?", (s["canonical_id"],)).fetchone()
            obs = self.db.execute("SELECT producer, provider, COUNT(*) n FROM submissions WHERE canonical_id=?"
                                  " GROUP BY producer, provider ORDER BY producer, provider", (s["canonical_id"],)).fetchall()
            others = len({o["producer"] for o in obs if o["producer"] != producer}) if producer else None
            if producer is not None:
                obs = [o for o in obs if o["producer"] == producer]
            body["repository"] = {
                "canonical_id": e["canonical_id"], "state": e["state"], "reasons": json.loads(e["reasons"]),
                "first_seen_at": e["first_seen_at"], "updated_at": e["updated_at"], "attempts": e["attempts"],
                "intake_request_id": e["request_id"], "batch_id": e["batch_id"],
                "corpus": json.loads(e["corpus"]) if e["corpus"] else None,
                "outcome": json.loads(e["outcome"]) if e["outcome"] else None,
                "observations": [{"producer": o["producer"], "provider": o["provider"], "count": o["n"]} for o in obs]}
            if others is not None:
                body["repository"]["other_producers"] = others
        body["notice"] = ("A submission is a request for assessment, never a recommendation, endorsement or "
                          "trending claim. The repository is judged by the same pipeline as scheduled discovery.")
        return body

    # -- daily batch --------------------------------------------------------------------------
    def seal(self, key_file: Path, max_items=DEFAULT_BATCH_MAX, now: datetime | None = None,
             signer=None) -> dict:
        """Seal today's batch of waiting repositories and sign it. Idempotent per UTC day."""
        now = now or _now()
        batch_id = "wr-" + now.strftime("%Y%m%d")
        with self._lock:
            row = self.db.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row is not None:
                return {"batch_id": batch_id, "items": row["items"], "sealed_at": row["sealed_at"], "already_sealed": True}
            ents = self.db.execute(
                "SELECT * FROM entities WHERE state IN ('received','failed_retry') AND attempts<?"
                " ORDER BY first_seen_at, canonical_id LIMIT ?", (MAX_ATTEMPTS, max_items)).fetchall()
            items = []
            for e in ents:
                subs = self.db.execute("SELECT producer, saas_hint FROM submissions WHERE canonical_id=? AND state='recorded'",
                                       (e["canonical_id"],)).fetchall()
                hints = Counter(s["saas_hint"] for s in subs if s["saas_hint"])
                items.append({"request_id": request_id_for(e["canonical_id"], e["attempts"]),
                              "canonical_id": e["canonical_id"], "repo_url": e["repo_url"],
                              "saas_hint": sorted(hints.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if hints else None,
                              "producers": sorted({s["producer"] for s in subs}), "observations": len(subs),
                              "first_seen_at": e["first_seen_at"], "attempt": e["attempts"] + 1})
            payload = canonical_json({"contract": "source-waiting-room-batch/1", "batch_id": batch_id,
                                      "sealed_at": _iso(now), "items": items})
            signature = (signer or ssh_sign)(payload, Path(key_file))
            self.db.execute("BEGIN")
            self.db.execute("INSERT INTO batches VALUES (?,?,?,?,?,?)",
                            (batch_id, _iso(now), payload, _sha(payload), signature, len(items)))
            for it in items:
                self.db.execute("UPDATE entities SET state='queued', request_id=?, batch_id=?, queued_at=?, updated_at=?"
                                " WHERE canonical_id=?",
                                (it["request_id"], batch_id, _iso(now), _iso(now), it["canonical_id"]))
            self.db.execute("COMMIT")
        self.audit("batch_sealed", batch_id=batch_id, items=len(items), payload_sha256=_sha(payload))
        return {"batch_id": batch_id, "items": len(items), "sealed_at": _iso(now), "already_sealed": False}

    def latest_batch(self) -> dict | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM batches ORDER BY sealed_at DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {"batch_id": row["batch_id"], "sealed_at": row["sealed_at"], "payload": row["payload"],
                "payload_sha256": row["payload_sha256"], "signature": row["signature"],
                "signer_identity": SIGNER_IDENTITY, "namespace": SIGNING_NAMESPACE}

    # -- results ----------------------------------------------------------------------------
    def reconcile(self, corpus: Corpus, now: datetime | None = None) -> dict:
        """Give queued and accepted repositories their result from the published intake outcomes."""
        now = now or _now()
        counts = Counter()
        rows = self.db.execute("SELECT * FROM entities WHERE state IN ('queued','accepted','not_promoted')").fetchall()
        for e in rows:
            o = corpus.outcome(e["request_id"]) if e["request_id"] else None
            if o is None:
                if e["state"] == "queued" and _parse_ts(e["queued_at"]) < now - timedelta(hours=NO_OUTCOME_AFTER_H):
                    # The consumer never ran on it: back in line under the same request id, which
                    # costs no attempt (a late run is then answered from its recorded outcome).
                    with self._lock:
                        self.db.execute("UPDATE entities SET state='received', reasons=?, updated_at=? WHERE canonical_id=?",
                                        (json.dumps(["consumer_no_outcome"]), _iso(now), e["canonical_id"]))
                    self.audit("entity_result", canonical_id=e["canonical_id"], state="received", reasons=["consumer_no_outcome"])
                    counts["no_outcome"] += 1
                continue
            canonical = (o.get("canonical") or {}).get("status")
            state = OUTCOME_STATE.get(o.get("outcome"), "failed_retry")
            if state == "accepted" and canonical == "canonical":
                state = "canonical"
            elif state == "accepted" and canonical == "not_promoted":
                state = "not_promoted"  # the cycle's corpus controls held it back; its reasons are in the outcome
            summary = {"request_id": o.get("request_id"), "outcome": o.get("outcome"), "reasons": o.get("reasons"),
                       "oss_id": o.get("oss_id"), "canonical": o.get("canonical"), "assessed_at": o.get("assessed_at"),
                       "scores": None if not o.get("scores") else {
                           "project_health": (o["scores"].get("project_health") or {}).get("score")
                           if isinstance(o["scores"].get("project_health"), dict) else o["scores"].get("project_health"),
                           "preliminary": o["scores"].get("preliminary")}}
            if state == "failed_retry":
                self._retry_or_dead(e, list(o.get("reasons") or ["assessment_failed"]), now, summary)
                counts["failed"] += 1
                continue
            if state == e["state"] and json.loads(e["outcome"] or "null") == summary:
                continue
            with self._lock:
                self.db.execute("UPDATE entities SET state=?, reasons=?, outcome=?, attempts=attempts+?, updated_at=?"
                                " WHERE canonical_id=?",
                                (state, json.dumps(((o.get("canonical") or {}).get("detail") or {}).get("reasons") or o.get("reasons") or []
                                                   if state == "not_promoted" else o.get("reasons") or []), json.dumps(summary),
                                 1 if e["state"] == "queued" else 0, _iso(now), e["canonical_id"]))
            self.audit("entity_result", canonical_id=e["canonical_id"], state=state, request_id=e["request_id"])
            counts[state] += 1
        return dict(counts)

    def _retry_or_dead(self, e, reasons, now, summary=None):
        attempts = e["attempts"] + 1
        state = "dead_letter" if attempts >= MAX_ATTEMPTS else "failed_retry"
        with self._lock:
            self.db.execute("UPDATE entities SET state=?, reasons=?, attempts=?, outcome=?, updated_at=? WHERE canonical_id=?",
                            (state, json.dumps(reasons), attempts, json.dumps(summary) if summary else e["outcome"],
                             _iso(now), e["canonical_id"]))
        self.audit("entity_result", canonical_id=e["canonical_id"], state=state, attempts=attempts, reasons=reasons)

    def summary(self) -> dict:
        ents = dict(self.db.execute("SELECT state, COUNT(*) FROM entities GROUP BY state").fetchall())
        subs = dict(self.db.execute("SELECT state, COUNT(*) FROM submissions GROUP BY state").fetchall())
        by_prod = dict(self.db.execute("SELECT producer, COUNT(*) FROM submissions GROUP BY producer").fetchall())
        return {"entities": ents, "submissions": subs, "by_producer": by_prod,
                "batches": self.db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]}


# -- signing ------------------------------------------------------------------------------------
def ssh_sign(payload: str, key_file: Path) -> str:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "batch.json"
        p.write_text(payload)
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key_file), "-n", SIGNING_NAMESPACE, str(p)],
                       check=True, capture_output=True)
        return (Path(d) / "batch.json.sig").read_text()


def ssh_verify(payload: str, signature: str, signers_file: Path) -> bool:
    with tempfile.TemporaryDirectory() as d:
        sig = Path(d) / "batch.sig"
        sig.write_text(signature)
        r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(signers_file), "-I", SIGNER_IDENTITY,
                            "-n", SIGNING_NAMESPACE, "-s", str(sig)], input=payload.encode(), capture_output=True)
        return r.returncode == 0


# -- the consumer (runs in the `Waiting room` workflow) --------------------------------------------
def fetch_batch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "the-source-waiting-room"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read(1024 * 1024))


def consume(batch: dict, signers_file: Path, max_age_hours=30, assess=None, now: datetime | None = None) -> dict:
    """Verify a sealed batch and run each repository through the existing intake assessment.
    Every check fails closed; one repository's failure never stops the others."""
    now = now or _now()
    payload, signature = batch.get("payload"), batch.get("signature")
    if not isinstance(payload, str) or not isinstance(signature, str):
        return {"status": "refused", "reason": "batch_malformed"}
    if not ssh_verify(payload, signature, signers_file):
        return {"status": "refused", "reason": "signature_invalid"}
    body = json.loads(payload)
    if body.get("contract") != "source-waiting-room-batch/1":
        return {"status": "refused", "reason": "batch_contract_unsupported"}
    if not BATCH_ID.match(str(body.get("batch_id"))) or not isinstance(body.get("items"), list):
        return {"status": "refused", "reason": "batch_malformed"}
    sealed = _parse_ts(body.get("sealed_at") or "")
    if sealed is None or sealed < now - timedelta(hours=max_age_hours):
        return {"status": "skipped", "reason": "no_fresh_batch", "batch_id": body.get("batch_id"),
                "sealed_at": body.get("sealed_at")}
    if assess is None:
        from . import intake
        assess = intake.assess
    results = []
    for it in body["items"]:
        if not isinstance(it, dict):
            results.append({"request_id": None, "outcome": "skipped", "reason": "item_malformed"})
            continue
        rid, url = it.get("request_id"), it.get("repo_url")
        key, _ = repo_key(url or "")
        if not (isinstance(rid, str) and REQUEST_ID.match(rid) and rid.startswith("wr-")) or key != it.get("canonical_id"):
            results.append({"request_id": rid, "outcome": "skipped", "reason": "item_malformed"})
            continue
        producers = ",".join(p for p in it.get("producers") or [] if isinstance(p, str) and PRODUCER_NAME.match(p))
        try:
            o = assess(url, rid, submitted_by=f"waiting-room:{producers}" if producers else "waiting-room",
                       saas_hint=it.get("saas_hint") if isinstance(it.get("saas_hint"), str) and SAAS_ID.match(it["saas_hint"]) else None,
                       note=f"waiting-room batch {body['batch_id']}; {int(it.get('observations') or 0)} observation(s)",
                       submitted_at=it.get("first_seen_at"))
            results.append({"request_id": rid, "oss_id": o.get("oss_id"), "outcome": o.get("outcome")})
        except Exception as exc:  # isolation: the item is retried from the waiting room
            results.append({"request_id": rid, "outcome": "error", "reason": type(exc).__name__})
    return {"status": "processed", "batch_id": body["batch_id"], "sealed_at": body["sealed_at"],
            "payload_sha256": _sha(payload), "results": results}


# -- CLI ------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="waiting-room", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    def state(p):
        p.add_argument("--state", required=True, type=Path, help="state directory (SQLite + audit log)")
    p = sub.add_parser("serve"); state(p)
    p.add_argument("--corpus-root", type=Path, help="checkout of the public repository")
    p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8852)
    p = sub.add_parser("producer-add"); state(p); p.add_argument("name")
    p.add_argument("--rate-hour", type=int, default=DEFAULT_RATE_HOUR); p.add_argument("--rate-day", type=int, default=DEFAULT_RATE_DAY)
    p.add_argument("--token-out", type=Path, help="write the credential here (0600) instead of stdout")
    p = sub.add_parser("producer-revoke"); state(p); p.add_argument("name")
    p = sub.add_parser("producers"); state(p)
    p = sub.add_parser("seal"); state(p); p.add_argument("--key", required=True, type=Path)
    p.add_argument("--max-items", type=int, default=DEFAULT_BATCH_MAX)
    p = sub.add_parser("reconcile"); state(p); p.add_argument("--corpus-root", required=True, type=Path)
    p = sub.add_parser("status"); state(p); p.add_argument("--submission")
    p = sub.add_parser("consume")
    p.add_argument("--url", required=True); p.add_argument("--signers", required=True, type=Path)
    p.add_argument("--max-age-hours", type=float, default=30); p.add_argument("--receipt-dir", type=Path)
    a = ap.parse_args(argv)

    if a.cmd == "consume":
        try:
            batch = fetch_batch(a.url)
        except Exception as exc:
            out = {"status": "skipped", "reason": f"batch_unavailable:{type(exc).__name__}"}
        else:
            out = consume(batch, a.signers, a.max_age_hours)
        if out["status"] == "processed" and a.receipt_dir:
            a.receipt_dir.mkdir(parents=True, exist_ok=True)
            receipt = a.receipt_dir / f"{out['batch_id']}.json"
            if not receipt.exists():
                receipt.write_text(json.dumps({**out, "processed_at": _iso(_now())}, indent=2, sort_keys=True) + "\n")
        print(json.dumps(out, indent=2, sort_keys=True))
        # A refused or unreachable batch fails the job so it is seen; a stale one is a quiet skip.
        return 2 if out["status"] == "refused" or out.get("reason", "").startswith("batch_unavailable") else 0
    if a.cmd == "serve":
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            sys.exit("refusing to serve as root")
        from . import waiting_room_http
        return waiting_room_http.serve(Store(a.state), Corpus(a.corpus_root), a.host, a.port)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        sys.exit("refusing to run as root: files it creates would lock the service out. Use "
                 "`runuser -u the-source-intake -- python3 -s -m source_pipeline.waiting_room ...`")
    store = Store(a.state)
    if a.cmd == "producer-add":
        token = store.add_producer(a.name, a.rate_hour, a.rate_day)
        if a.token_out:
            fd = os.open(a.token_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(token + "\n")
            print(f"credential for {a.name} written to {a.token_out}")
        else:
            print(token)
        return 0
    if a.cmd == "producer-revoke":
        print("revoked" if store.revoke_producer(a.name) else "no live credential")
        return 0
    if a.cmd == "producers":
        print(json.dumps(store.producers(), indent=2))
        return 0
    if a.cmd == "seal":
        print(json.dumps(store.seal(a.key, a.max_items), indent=2))
        return 0
    if a.cmd == "reconcile":
        print(json.dumps(store.reconcile(Corpus(a.corpus_root)), indent=2, sort_keys=True))
        return 0
    if a.cmd == "status":
        print(json.dumps(store.readback(a.submission, None) if a.submission else store.summary(), indent=2, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
