"""Morning digest of the waiting room for Keith (KEI-932).

    python -m source_pipeline.waiting_room_digest --state DIR [--hours 24] [--dry-run]
        [--to ADDR] [--from ADDR] [--smtp-host HOST] [--smtp-port N]

Reads the waiting room's SQLite file (read-only) and audit log, and emails one plain summary:
which repositories got a result in the window (accepted, held, rejected, ...), which arrived,
and which are still waiting for the next sealed batch. It changes nothing.

The SMTP password (a Resend API key) comes from the systemd credential `smtp_password`, or
from the file named by --password-file. It is never printed or logged. --dry-run prints the
message instead of sending it.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import smtplib
import sqlite3
import ssl
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

DEFAULT_TO = "keith@de-alwis.com"
DEFAULT_FROM = "The Source waiting room <the-source@de-alwis.com>"
SEAL_TIME = "21:45 UTC"

# Result states, in the order they are reported, with the heading each gets.
RESULTS = (
    ("canonical", "Accepted and added to The Source"),
    ("accepted", "Accepted, waiting for tonight's cycle to add it"),
    ("not_promoted", "Accepted, but held back by the corpus controls"),
    ("held", "Held: needs more evidence"),
    ("rejected", "Rejected"),
    ("already_known", "Already in The Source"),
    ("dead_letter", "Gave up after repeated assessment failures"),
    ("failed_retry", "Assessment failed, will retry"),
)
WAITING = ("received", "queued")


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def collect(state_dir: Path, now: datetime, hours: float) -> dict:
    since = now - timedelta(hours=hours)
    db = sqlite3.connect(f"file:{state_dir / 'waiting-room.sqlite3'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    entities = {r["canonical_id"]: dict(r) for r in db.execute("SELECT * FROM entities")}

    # The latest result each repository got inside the window, from the append-only audit log.
    latest = {}
    audit = state_dir / "audit.jsonl"
    if audit.exists():
        for line in audit.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("event") == "entity_result" and _parse(rec["ts"]) >= since:
                latest[rec["canonical_id"]] = rec["state"]

    results = {state: [] for state, _ in RESULTS}
    for cid, state in sorted(latest.items()):
        e = entities.get(cid)
        if e and state in results and e["state"] == state:   # skip results already superseded
            results[state].append({"canonical_id": cid, "repo_url": e["repo_url"],
                                   "reasons": json.loads(e["reasons"] or "[]")})

    arrived = [dict(r) for r in db.execute(
        "SELECT submission_id, producer, provider, received_at, state, reasons, repo_url, canonical_id"
        " FROM submissions WHERE received_at >= ? ORDER BY received_at", (since.strftime("%Y-%m-%dT%H:%M:%SZ"),))]
    for a in arrived:
        a["reasons"] = json.loads(a["reasons"] or "[]")
        e = entities.get(a["canonical_id"]) if a["canonical_id"] else None
        a["entity_state"] = e["state"] if e else None

    waiting = [{"canonical_id": e["canonical_id"], "repo_url": e["repo_url"], "state": e["state"],
                "since": e["first_seen_at"]}
               for e in sorted(entities.values(), key=lambda e: e["first_seen_at"]) if e["state"] in WAITING]
    db.close()
    return {"since": since, "now": now, "results": results, "arrived": arrived, "waiting": waiting}


def _when(ts: str) -> str:
    return _parse(ts).strftime("%d %b %H:%M UTC")


def render(d: dict) -> tuple[str, str, str]:
    decided = sum(len(v) for v in d["results"].values())
    subject = (f"The Source waiting room: {decided} result{'s' * (decided != 1)}, "
               f"{len(d['waiting'])} waiting ({d['now']:%d %b})")
    text, body = [], []

    def section(title, rows):
        text.append(f"\n{title}")
        body.append(f"<h3 style='margin:20px 0 6px;font-size:15px'>{html.escape(title)}</h3><ul style='margin:0;padding-left:20px'>")
        for main, detail in rows:
            text.append(f"  - {main}" + (f"  ({detail})" if detail else ""))
            body.append(f"<li><a href='{html.escape(main)}'>{html.escape(main)}</a>"
                        + (f"<br><span style='color:#666'>{html.escape(detail)}</span>" if detail else "") + "</li>")
        body.append("</ul>")

    intro = f"Last {int((d['now'] - d['since']).total_seconds() // 3600)} hours, to {d['now']:%d %b %H:%M UTC}."
    text.append(intro)
    body.append(f"<p style='color:#444'>{html.escape(intro)}</p>")

    if decided:
        for state, title in RESULTS:
            if d["results"][state]:
                section(title, [(r["repo_url"], "; ".join(r["reasons"])) for r in d["results"][state]])
    else:
        text.append("\nNo results came back in this window.")
        body.append("<p>No results came back in this window.</p>")

    if d["arrived"]:
        rows = []
        for a in d["arrived"]:
            if a["state"] == "invalid":
                detail = "not accepted: " + "; ".join(a["reasons"])
            else:
                detail = {"already_known": "already in The Source", "received": "waiting",
                          "queued": "in tonight's batch"}.get(a["entity_state"], a["entity_state"] or "")
            rows.append((a["repo_url"] or "(no URL)", f"{_when(a['received_at'])}, via {a['producer']}" + (f"/{a['provider']}" if a['provider'] else "") + f": {detail}"))
        section(f"Submitted ({len(d['arrived'])})", rows)

    if d["waiting"]:
        section(f"Still waiting ({len(d['waiting'])}), go into the next daily batch (sealed at {SEAL_TIME})",
                [(w["repo_url"], f"{'in a sealed batch' if w['state'] == 'queued' else 'waiting'} since {_when(w['since'])}")
                 for w in d["waiting"]])

    text.append("\nCheck any submission in ChatGPT with getSubmission.")
    html_body = ("<div style='font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#111;max-width:640px'>"
                 + "".join(body) + "<p style='color:#666;margin-top:24px'>Check any submission in ChatGPT with getSubmission.</p></div>")
    return subject, "\n".join(text) + "\n", html_body


def _password(path: str | None) -> str:
    if path:
        return Path(path).read_text().strip()
    creds = os.environ.get("CREDENTIALS_DIRECTORY")
    if not creds:
        sys.exit("no SMTP password: run under systemd with LoadCredential=smtp_password, or pass --password-file")
    return (Path(creds) / "smtp_password").read_text().strip()


def send(subject, text, html_body, *, to, sender, host, port, user, password):
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
        s.login(user, password)
        s.send_message(msg)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="waiting-room-digest", description=__doc__.split("\n\n")[0])
    ap.add_argument("--state", required=True, type=Path)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--to", default=DEFAULT_TO)
    ap.add_argument("--from", dest="sender", default=DEFAULT_FROM)
    ap.add_argument("--smtp-host", default="smtp.resend.com")
    ap.add_argument("--smtp-port", type=int, default=465)
    ap.add_argument("--smtp-user", default="resend")
    ap.add_argument("--password-file")
    a = ap.parse_args(argv)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    subject, text, html_body = render(collect(a.state, now, a.hours))
    if a.dry_run:
        print(f"Subject: {subject}\nTo: {a.to}\n\n{text}")
        return 0
    send(subject, text, html_body, to=a.to, sender=a.sender, host=a.smtp_host, port=a.smtp_port,
         user=a.smtp_user, password=_password(a.password_file))
    print(f"sent: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
