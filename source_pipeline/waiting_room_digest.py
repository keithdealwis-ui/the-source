"""Morning digest of The Source for Keith (KEI-932).

    python -m source_pipeline.waiting_room_digest --state DIR [--hours 24] [--dry-run]
        [--telemetry FILE] [--clients FILE] [--no-cycle]
        [--to ADDR] [--from ADDR] [--smtp-host HOST] [--smtp-port N]
    python -m source_pipeline.waiting_room_digest extract-clients --out FILE LOG...

Emails one plain summary in three parts:
  - last night's daily cycle: new projects found and projects rescored, from the run record the
    cycle publishes in the public repository;
  - yesterday's MCP usage, from the server's aggregate telemetry (counts only) and, when given,
    the client type of each /mcp request (`extract-clients` keeps only day and user agent);
  - the waiting room: results in the window, arrivals, and what is still waiting.
The waiting room's SQLite file is opened read-only. It changes nothing.

The SMTP password (a Resend API key) comes from the systemd credential `smtp_password`, or
from the file named by --password-file. It is never printed or logged. --dry-run prints the
message instead of sending it.
"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import os
import re
import smtplib
import sqlite3
import ssl
import sys
import urllib.request
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
    cycle, usage = d.get("cycle"), d.get("usage")
    parts = []
    if cycle and cycle.get("status"):
        parts.append(f"{cycle['added']} new projects, {cycle['rescored_total']} rescored")
    if usage:
        parts.append(f"{usage['calls']} MCP calls")
    parts.append(f"{decided} waiting-room result{'s' * (decided != 1)}")
    subject = f"The Source daily: {', '.join(parts)} ({d['now']:%d %b})"
    text, body = [], []

    def heading(title):
        text.append(f"\n== {title} ==")
        body.append(f"<h2 style='margin:28px 0 4px;font-size:17px;border-bottom:1px solid #ddd;padding-bottom:4px'>{html.escape(title)}</h2>")

    def section(title, rows, link=True):
        text.append(f"\n{title}")
        body.append(f"<h3 style='margin:16px 0 6px;font-size:15px'>{html.escape(title)}</h3><ul style='margin:0;padding-left:20px'>")
        for main, detail in rows:
            text.append(f"  - {main}" + (f": {detail}" if detail else ""))
            shown = f"<a href='{html.escape(main)}'>{html.escape(main)}</a>" if link and main.startswith("https://") else html.escape(main)
            body.append(f"<li>{shown}"
                        + (f"<br><span style='color:#666'>{html.escape(detail)}</span>" if detail else "") + "</li>")
        body.append("</ul>")

    def para(line):
        text.append(line)
        body.append(f"<p style='color:#444;margin:6px 0'>{html.escape(line)}</p>")

    heading("Last night's cycle")
    if cycle is None:
        para("No run record found for last night's cycle.")
    elif cycle.get("status") != "succeeded":
        para(f"Run {cycle['run_id']} did not succeed: {cycle.get('error') or cycle.get('status')}.")
    if cycle and cycle.get("status"):
        para(f"Run {cycle['run_id']}, {cycle['status']}, finished {_when(cycle['ended_at'])}"
             + (f" ({cycle['stale']})" if cycle.get("stale") else "") + ".")
        section("New projects found", [
            ("Added to the corpus", f"{cycle['added']} (corpus {cycle['before']:,} to {cycle['after']:,})"),
            ("Candidates looked at", f"{cycle['found']:,} found by {cycle['strategies']} discovery searches, "
                                     f"{cycle['new_to_corpus']} not already known"),
        ], link=False)
        if cycle["top_new"]:
            section("Biggest new arrivals", [(f"https://{p['id']}", f"{p.get('stars', 0):,} stars")
                                             for p in cycle["top_new"]])
        section("Projects rescored", [
            ("Fully rescored (recommendation scoring)", f"{cycle['rescored']:,}"),
            ("Project Health rescored", f"{cycle['health_rescored']:,} (of {cycle['projects']:,})"),
            ("SaaS pairings rescored", f"{cycle['pairings_rescored']:,}"),
            ("Momentum measured", f"{cycle['momentum_measured']:,}"),
        ], link=False)
        moves = ([(f"https://{p}" if "/" in p else p, "now recommendable") for p in cycle["became"]]
                 + [(f"https://{p}" if "/" in p else p, "no longer recommendable") for p in cycle["lost"]])
        if moves:
            section("Recommendation changes", moves)
        if cycle["failures"]:
            section("Failures and anomalies", [(str(f)[:200], "") for f in cycle["failures"]], link=False)

    heading("The Source MCP yesterday")
    if not usage:
        para("No usage figures were available.")
    else:
        para(f"{usage['day']}: {usage['calls']} tool calls (7-day average {usage['avg7']:.0f}), "
             f"{usage['tool_errors']} tool errors, {usage['internal_errors']} server errors, "
             f"{usage['empty']} empty answers.")
        if usage["tools"]:
            section("Calls by tool", [(t, f"{n['calls']}" + (f", {n['errors']} errors" if n["errors"] else "")
                                       + (f", {n['empty']} empty" if n["empty"] else "")
                                       + (f", {n['slow']} took over a second" if n["slow"] else ""))
                                      for t, n in usage["tools"]], link=False)
        if usage.get("clients"):
            section("Requests by client (HTTP, includes connection set-up)",
                    [(name, str(n)) for name, n in usage["clients"]], link=False)

    heading("Waiting room")
    para(f"Last {int((d['now'] - d['since']).total_seconds() // 3600)} hours, to {d['now']:%d %b %H:%M UTC}.")

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


RUN_URL = "https://raw.githubusercontent.com/keithdealwis-ui/the-source/main/runs/{key}-schedule-a1.json"


def fetch_cycle(now: datetime, fetch=None) -> dict | None:
    """Last night's daily-cycle run record from the public repository (no credential)."""
    def get(url):
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.load(r)
    fetch = fetch or get
    for back in (0, 1):
        key = (now - timedelta(days=back)).strftime("%Y-%m-%d")
        try:
            run = fetch(RUN_URL.format(key=key))
        except Exception:
            continue
        return summarise_cycle(run, stale=f"no run yet today; this is {key}" if back else None)
    return None


def summarise_cycle(run: dict, stale=None) -> dict:
    corpus, counts = run.get("corpus") or {}, run.get("counts") or {}
    quick = corpus.get("quick") or {}
    found = corpus.get("candidates_found_by_strategy") or {}
    trans = run.get("transitions") or {}
    return {
        "run_id": run.get("run_id"), "status": run.get("status"), "error": run.get("error"),
        "ended_at": run.get("ended_at") or run.get("started_at"), "stale": stale,
        "added": counts.get("corpus_added", corpus.get("corpus_added", 0)),
        "before": corpus.get("membership_before", 0), "after": corpus.get("membership_after", 0),
        "found": sum(s.get("found", 0) for s in found.values()), "strategies": len(found),
        "new_to_corpus": sum(s.get("new_to_corpus", 0) for s in found.values()),
        "top_new": (corpus.get("top_new_by_stars") or [])[:5],
        "rescored": counts.get("rescored", 0), "health_rescored": quick.get("health_rescored", 0),
        "pairings_rescored": quick.get("pairings_rescored", 0), "momentum_measured": counts.get("momentum_measured", 0),
        "projects": corpus.get("membership_after", 0),
        "rescored_total": counts.get("rescored", 0) + quick.get("health_rescored", 0),
        "became": [p if isinstance(p, str) else p.get("oss_id") or p.get("id") or str(p) for p in trans.get("became_recommendable") or []],
        "lost": [p if isinstance(p, str) else p.get("oss_id") or p.get("id") or str(p) for p in trans.get("no_longer_recommendable") or []],
        "failures": list(run.get("failures") or []) + list(run.get("anomalies") or []),
    }


CLIENTS = (("openai-mcp", "ChatGPT"), ("claude", "Claude"), ("anthropic", "Claude"), ("cursor", "Cursor"),
           ("python", "Scripts (Python)"), ("node", "Scripts (Node)"), ("curl", "curl"), ("bot", "Bots and crawlers"))


def usage_for(telemetry: dict, day: str, client_lines=()) -> dict:
    """Yesterday's MCP figures from the aggregate telemetry (counts only, no identities)."""
    days = telemetry.get("days") or {}

    def total(d):
        return sum(o.get("count", 0) for t in (days.get(d) or {}).values() for o in t.values())

    tools = []
    for name, outcomes in sorted((days.get(day) or {}).items()):
        ok = outcomes.get("ok") or {}
        calls = sum(o.get("count", 0) for o in outcomes.values())
        errors = sum((outcomes.get(k) or {}).get("count", 0) for k in ("tool_error", "internal_error"))
        slow = sum((o.get("latency") or {}).get(">=1000ms", 0) for o in outcomes.values())
        tools.append((name, {"calls": calls, "errors": errors, "empty": ok.get("empty", 0), "slow": slow}))
    tools.sort(key=lambda t: -t[1]["calls"])
    d0 = datetime.strptime(day, "%Y-%m-%d")
    prior = [total((d0 - timedelta(days=i)).strftime("%Y-%m-%d")) for i in range(1, 8)]
    by_kind = lambda k: sum((o.get(k) or {}).get("count", 0) for o in (days.get(day) or {}).values())
    clients = {}
    for line in client_lines:
        date, _, agent = line.rstrip("\n").partition("\t")
        if date != day:
            continue
        name = next((label for needle, label in CLIENTS if needle in agent.lower()), "Other")
        clients[name] = clients.get(name, 0) + 1
    return {"day": day, "calls": total(day), "avg7": sum(prior) / 7, "tools": tools,
            "tool_errors": by_kind("tool_error"), "internal_errors": by_kind("internal_error"),
            "empty": sum((o.get("ok") or {}).get("empty", 0) for o in (days.get(day) or {}).values()),
            "clients": sorted(clients.items(), key=lambda c: -c[1])}


def extract_clients(logs: list[Path], out: Path) -> int:
    """Run as root before the digest: keep only the day and user agent of /mcp requests (no addresses)."""
    months = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}
    rows = []
    for p in logs:
        if not p.exists():
            continue
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt", errors="replace") as fh:
            for line in fh:
                f = line.split('"')
                if len(f) < 6 or not re.match(r"^[A-Z]+ /mcp[ ?]", f[1]):
                    continue
                m = re.search(r"\[(\d{2})/(\w{3})/(\d{4}):", f[0])
                if m and m.group(2) in months:
                    rows.append(f"{m.group(3)}-{months[m.group(2)]:02d}-{m.group(1)}\t{f[5][:120]}")
    out.write_text("\n".join(rows) + ("\n" if rows else ""))
    return len(rows)


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
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["extract-clients"]:
        ep = argparse.ArgumentParser(prog="waiting-room-digest extract-clients")
        ep.add_argument("--out", required=True, type=Path)
        ep.add_argument("logs", nargs="+", type=Path)
        e = ep.parse_args(argv[1:])
        print(f"{extract_clients(e.logs, e.out)} /mcp requests kept")
        return 0
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
    ap.add_argument("--telemetry", type=Path, help="MCP telemetry.json (default: the systemd credential mcp_telemetry)")
    ap.add_argument("--clients", type=Path, help="output of extract-clients")
    ap.add_argument("--no-cycle", action="store_true", help="skip fetching last night's run record")
    a = ap.parse_args(argv)

    now = datetime.now(timezone.utc).replace(microsecond=0)
    d = collect(a.state, now, a.hours)
    d["cycle"] = None if a.no_cycle else fetch_cycle(now)
    tele = a.telemetry or (Path(os.environ["CREDENTIALS_DIRECTORY"]) / "mcp_telemetry"
                           if os.environ.get("CREDENTIALS_DIRECTORY") else None)
    if tele and tele.exists():
        lines = a.clients.read_text().splitlines() if a.clients and a.clients.exists() else ()
        d["usage"] = usage_for(json.loads(tele.read_text()), (now - timedelta(days=1)).strftime("%Y-%m-%d"), lines)
    subject, text, html_body = render(d)
    if a.dry_run:
        print(f"Subject: {subject}\nTo: {a.to}\n\n{text}")
        return 0
    send(subject, text, html_body, to=a.to, sender=a.sender, host=a.smtp_host, port=a.smtp_port,
         user=a.smtp_user, password=_password(a.password_file))
    print(f"sent: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
