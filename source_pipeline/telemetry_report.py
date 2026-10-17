"""Weekly MCP usage report from the aggregate telemetry file (KEI-918 S0.5, docs/OBSERVABILITY.md).

    python -m source_pipeline.telemetry_report --telemetry /var/lib/the-source-mcp/telemetry.json \
        [--week 2026-W42] [--out DIR] [--k-min 5]
    python -m source_pipeline obs-telemetry-report ...   (the same)

Reads the counters mcp_http.py writes (the-source.mcp-telemetry/1 or /2: per UTC day, per tool,
per outcome, a count, a latency histogram and, from /2, empty answers). They hold no
arguments, results, client identity, sessions or addresses, and this report adds nothing to
them. It only adds up whole ISO weeks (Monday to Sunday, UTC) and makes them coarser:

* No daily figures. At low volume a single call on a given day could be matched to the one
  person who made it; a weekly total cannot be matched that way.
* Small-count suppression. Every count from 1 to k_min-1 is published as "<k_min", and any
  rate computed from a suppressed count is withheld. A zero stays zero.
* Latency is the histogram bucket holding the median and the 95th percentile, not a value.

Standard library only, so the hosted server's own code (python3 -S) can run it on a timer
(deploy/the-source-mcp/the-source-mcp-report.*). Writes <week>.json and <week>.md to --out,
or prints the JSON. Never published by default: the reports stay on the host.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = "the-source.mcp-usage-week/1"
ACCEPTED = ("the-source.mcp-telemetry/1", "the-source.mcp-telemetry/2")
BUCKET_ORDER = ("<10ms", "<50ms", "<200ms", "<1000ms", ">=1000ms")
OUTCOMES = ("ok", "tool_error", "internal_error")
K_MIN = 5


def week_days(week: str) -> list[str]:
    y, w = week.split("-W")
    monday = date.fromisocalendar(int(y), int(w), 1)
    return [(monday + timedelta(days=i)).isoformat() for i in range(7)]


def last_full_week(today: date) -> str:
    y, w, _ = (today - timedelta(days=7)).isocalendar()
    return f"{y}-W{w:02d}"


def _sup(n: int, k: int):
    return n if n == 0 or n >= k else f"<{k}"


def _rate(num: int, den: int, k: int):
    if den == 0 or 0 < num < k or den < k:
        return None
    return round(num / den, 4)


def _bucket_at(hist: dict, q: float):
    total = sum(hist.values())
    if not total:
        return None
    seen = 0
    for b in BUCKET_ORDER:
        seen += hist.get(b, 0)
        if seen >= q * total:
            return b
    return BUCKET_ORDER[-1]


def aggregate(telemetry: dict, week: str) -> dict:
    """Raw weekly sums (unsuppressed): tool -> {outcome counts, empty, latency histogram}."""
    if telemetry.get("schema") not in ACCEPTED:
        raise ValueError(f"telemetry schema {telemetry.get('schema')!r} is not one of {ACCEPTED}")
    days = telemetry.get("days") or {}
    tools = {}
    covered = [d for d in week_days(week) if d in days]
    for d in covered:
        for tool, outcomes in days[d].items():
            t = tools.setdefault(tool, {"outcomes": dict.fromkeys(OUTCOMES, 0), "empty": 0, "latency": {}})
            for outcome, cell in outcomes.items():
                t["outcomes"][outcome] = t["outcomes"].get(outcome, 0) + int(cell.get("count", 0))
                t["empty"] += int(cell.get("empty", 0))
                for b, n in (cell.get("latency") or {}).items():
                    t["latency"][b] = t["latency"].get(b, 0) + int(n)
    return {"week": week, "days_with_data": len(covered), "tools": tools,
            "empty_counted": telemetry.get("schema") != "the-source.mcp-telemetry/1"}


def report(telemetry: dict, week: str, k: int = K_MIN, previous: dict | None = None) -> dict:
    agg = aggregate(telemetry, week)
    rows, totals, latency_all = [], dict.fromkeys(OUTCOMES, 0), {}
    for tool in sorted(agg["tools"]):
        t = agg["tools"][tool]
        calls = sum(t["outcomes"].values())
        for o in OUTCOMES:
            totals[o] += t["outcomes"].get(o, 0)
        for b, n in t["latency"].items():
            latency_all[b] = latency_all.get(b, 0) + n
        ok = t["outcomes"].get("ok", 0)
        rows.append({"tool": tool, "calls": _sup(calls, k),
                     "outcomes": {o: _sup(t["outcomes"].get(o, 0), k) for o in OUTCOMES},
                     "error_rate": _rate(calls - ok, calls, k),
                     "empty_answers": _sup(t["empty"], k) if agg["empty_counted"] else None,
                     "empty_rate": _rate(t["empty"], ok, k) if agg["empty_counted"] else None,
                     "latency_median_bucket": _bucket_at(t["latency"], 0.5) if calls >= k else None,
                     "latency_p95_bucket": _bucket_at(t["latency"], 0.95) if calls >= k else None})
    total = sum(totals.values())
    out = {"schema": SCHEMA, "week": week, "days": [week_days(week)[0], week_days(week)[-1]],
           "days_with_data": agg["days_with_data"],
           "totals": {"calls": _sup(total, k), "outcomes": {o: _sup(totals[o], k) for o in OUTCOMES},
                      "error_rate": _rate(total - totals["ok"], total, k),
                      "latency_median_bucket": _bucket_at(latency_all, 0.5) if total >= k else None,
                      "latency_p95_bucket": _bucket_at(latency_all, 0.95) if total >= k else None},
           "tools": rows,
           "privacy": {"k_min": k, "granularity": "ISO week, UTC; no daily figures",
                       "suppression": f"counts from 1 to {k - 1} are shown as \"<{k}\"; rates from them are withheld",
                       "source": "aggregate counters only: no arguments, results, client identity, sessions or "
                                 "addresses exist to report"}}
    if previous is not None:
        prev_total = sum(sum(t["outcomes"].values()) for t in aggregate(telemetry, previous["week"])["tools"].values())
        out["previous_week"] = {"week": previous["week"], "calls": _sup(prev_total, k),
                                "change": (total - prev_total) if total >= k and prev_total >= k else None}
    return out


def markdown(rep: dict) -> str:
    t = rep["totals"]
    lines = [f"# The Source MCP: usage, week {rep['week']} ({rep['days'][0]} to {rep['days'][1]}, UTC)", "",
             f"Calls: **{t['calls']}** (ok {t['outcomes']['ok']}, tool errors {t['outcomes']['tool_error']}, "
             f"internal errors {t['outcomes']['internal_error']}). Error rate: {t['error_rate']}. "
             f"Latency median {t['latency_median_bucket']}, p95 {t['latency_p95_bucket']}. "
             f"Days with data: {rep['days_with_data']}/7.", ""]
    if "previous_week" in rep:
        p = rep["previous_week"]
        lines += [f"Previous week {p['week']}: {p['calls']} calls (change {p['change']}).", ""]
    lines += ["| Tool | Calls | Errors | Error rate | Empty answers | Empty rate | Median | p95 |",
              "|---|---|---|---|---|---|---|---|"]
    for r in rep["tools"]:
        errs = [r["outcomes"]["tool_error"], r["outcomes"]["internal_error"]]
        lines.append(f"| `{r['tool']}` | {r['calls']} | {errs[0]} / {errs[1]} | {r['error_rate']} | "
                     f"{r['empty_answers']} | {r['empty_rate']} | {r['latency_median_bucket']} | "
                     f"{r['latency_p95_bucket']} |")
    pv = rep["privacy"]
    lines += ["", f"Privacy: {pv['granularity']}; {pv['suppression']}. {pv['source']}.", ""]
    return "\n".join(lines)


def _write(path: Path, text: str):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".report-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="the-source-mcp-report", description=__doc__.split("\n\n")[0])
    ap.add_argument("--telemetry", required=True, help="the telemetry JSON mcp_http.py writes")
    ap.add_argument("--week", help="ISO week YYYY-Www (default: the last complete week)")
    ap.add_argument("--out", help="write <week>.json and <week>.md here instead of printing")
    ap.add_argument("--k-min", type=int, default=K_MIN, help="suppress counts below this (default 5)")
    a = ap.parse_args(argv)
    if a.k_min < 2:
        ap.error("--k-min must be at least 2")
    week = a.week or last_full_week(datetime.now(timezone.utc).date())
    try:
        week_days(week)
        telemetry = json.loads(Path(a.telemetry).read_text(encoding="utf-8"))
        y, w, _ = (date.fromisoformat(week_days(week)[0]) - timedelta(days=7)).isocalendar()
        rep = report(telemetry, week, a.k_min, {"week": f"{y}-W{w:02d}"})
    except (OSError, ValueError) as exc:
        print(f"the-source-mcp-report: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(rep, indent=1, sort_keys=True) + "\n"
    if a.out:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        _write(out / f"{week}.json", text)
        _write(out / f"{week}.md", markdown(rep))
        print(f"the-source-mcp-report: wrote {out / week}.json and .md", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
