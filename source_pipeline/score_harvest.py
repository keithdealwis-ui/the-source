"""Scoring evidence (KEI-806) — the one stage of scoring that talks to GitHub.

    score-harvest   ask GitHub (GraphQL, read-only, public metadata) for the evidence the
                    Project Health rubric needs that the KEI-805/811 stores never collected:
                    issue and PR samples with first-response times, commit-author spread,
                    dated commit windows, release history, security policy, and the root,
                    .github and workflow listings (docs, containers, CI, governance files).
                    For projects with a REPLACES edge it also fetches README text, which is
                    the evidence Replacement Fit reads.

It decides nothing. Records land in data/score/evidence/ with the time they were
observed, and every observation is appended to data/score/history/observations.jsonl,
so a later refresh adds to history rather than replacing it. `score-build` reads only
these files and the graph, and runs with no network.

Resumable: records are checkpointed; a rerun asks only for what is missing or failed,
unless --refresh is given, which re-observes everything under a new as_of.
"""
from __future__ import annotations

import hashlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import common, enrich

SCORE = common.DATA / "score"
EVIDENCE = SCORE / "evidence"
META = EVIDENCE / "github_meta.json"
READMES = EVIDENCE / "readmes.json"
HISTORY = SCORE / "history" / "observations.jsonl"
GRAPH_VIEW = common.DATA / "graph" / "views" / "scoring_inputs.jsonl"
GQL = "https://api.github.com/graphql"

BATCH = 4
README_MAX = 60000

REPO_FIELDS = """
 nameWithOwner createdAt pushedAt updatedAt isArchived isDisabled isFork isMirror isTemplate
 description homepageUrl stargazerCount forkCount
 watchers{totalCount}
 isSecurityPolicyEnabled securityPolicyUrl hasIssuesEnabled hasDiscussionsEnabled hasWikiEnabled
 licenseInfo{spdxId}
 fundingLinks{platform}
 repositoryTopics(first:20){nodes{topic{name}}}
 releases(first:10,orderBy:{field:CREATED_AT,direction:DESC}){totalCount nodes{tagName publishedAt createdAt isPrerelease isDraft}}
 openIssues:issues(states:OPEN){totalCount}
 closedIssues:issues(states:CLOSED){totalCount}
 openPRs:pullRequests(states:OPEN){totalCount}
 mergedPRs:pullRequests(states:MERGED){totalCount}
 recentIssues:issues(first:20,orderBy:{field:CREATED_AT,direction:DESC}){nodes{createdAt closedAt state authorAssociation comments(first:3){totalCount nodes{createdAt authorAssociation}}}}
 recentPRs:pullRequests(first:20,orderBy:{field:CREATED_AT,direction:DESC}){nodes{createdAt mergedAt closedAt state authorAssociation}}
 root:object(expression:"HEAD:"){... on Tree{entries{name type object{... on Blob{byteSize}}}}}
 dotgithub:object(expression:"HEAD:.github"){... on Tree{entries{name type}}}
 workflows:object(expression:"HEAD:.github/workflows"){... on Tree{entries{name}}}
 defaultBranchRef{name target{... on Commit{
   c90:history(since:$s90){totalCount}
   c180:history(since:$s180){totalCount}
   c365:history(since:$s365,first:100){totalCount nodes{committedDate author{user{login}}}}
   cprev:history(since:$s730,until:$s365){totalCount}
 }}}
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def windows(as_of: str) -> dict:
    t = datetime.strptime(as_of, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    f = lambda d: (t - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"s90": f(90), "s180": f(180), "s365": f(365), "s730": f(730)}


def query_hash() -> str:
    return hashlib.sha256(REPO_FIELDS.encode()).hexdigest()[:16]


def _targets() -> tuple[list, set]:
    rows = common.read_jsonl(GRAPH_VIEW)
    gh = sorted(r["canonical"] for r in rows if r["canonical"].startswith("github.com/"))
    replace = {r["canonical"] for r in rows if r["replaces"] and r["canonical"].startswith("github.com/")}
    return gh, replace


def _batch_query(keys: list) -> str:
    parts = []
    for i, key in enumerate(keys):
        owner, name = key.split("/")[1:3]
        parts.append(f'r{i}:repository(owner:{common.json.dumps(owner)},name:{common.json.dumps(name)}){{{REPO_FIELDS}}}')
    head = "query($s90:GitTimestamp!,$s180:GitTimestamp!,$s365:GitTimestamp!,$s730:GitTimestamp!)"
    return head + "{" + " ".join(parts) + " rateLimit{remaining resetAt cost}}"


def _ask(http, keys: list, win: dict, as_of: str) -> dict:
    """One batch; on failure split it, so one slow repository cannot sink nine others."""
    st, doc = http.call(GQL, {"query": _batch_query(keys), "variables": win}, auth=True, tries=3)
    data = (doc or {}).get("data") if st == 200 else None
    if data is None:
        if len(keys) > 1:
            mid = len(keys) // 2
            return {**_ask(http, keys[:mid], win, as_of), **_ask(http, keys[mid:], win, as_of)}
        return {keys[0]: {"error": f"graphql_http_{st}", "observed_at": as_of}}
    errs = {}
    for e in (doc.get("errors") or []):
        path = e.get("path") or []
        if path:
            errs.setdefault(path[0], []).append(e.get("type") or e.get("message", "")[:120])
    out = {}
    for i, key in enumerate(keys):
        node = data.get(f"r{i}")
        if node is None:
            kind = (errs.get(f"r{i}") or ["missing"])[0]
            out[key] = {"error": "not_found" if kind == "NOT_FOUND" else f"graphql_{kind}", "observed_at": as_of}
        else:
            out[key] = {"observed_at": as_of, "partial_errors": errs.get(f"r{i}", []), "repo": node}
    rl = data.get("rateLimit") or {}
    if rl.get("remaining") is not None and rl["remaining"] < 200 and rl.get("resetAt"):
        reset = datetime.strptime(rl["resetAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        wait = max(5.0, (reset - datetime.now(timezone.utc)).total_seconds() + 5)
        print(f"  rate budget low ({rl['remaining']}); sleeping {int(wait)}s", file=sys.stderr)
        time.sleep(min(wait, 3700))
    return out


# Secret-shaped material in third-party READMEs (documentation examples of PEM keys, tokens)
# is never stored: scoring has no use for it and an evidence pack must not carry it. Same
# pattern set as the aiq-verifier-pack scanner; a PEM block is removed whole.
SECRET_SHAPES = (
    # A whole block when its END marker follows within 4,000 chars; otherwise only the header
    # and the base64 run after it (an unterminated example must not swallow the README).
    (re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----"
                r"(?:[\s\S]{0,4000}?-----END [A-Z ]*PRIVATE KEY-----|(?:\\n|[A-Za-z0-9+/=.\s])*)"), "private_key"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "github_token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b"), "github_token"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "anthropic_key"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b"), "openai_key"),
    (re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"), "slack_token"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws_access_key"),
    (re.compile(r"\blin_api_[A-Za-z0-9]{30,}"), "linear_key"),
)


def redact(text: str) -> tuple[str, dict]:
    counts = {}
    for rx, kind in SECRET_SHAPES:
        text, n = rx.subn(f"[REDACTED:{kind}]", text)
        if n:
            counts[kind] = counts.get(kind, 0) + n
    return text, counts


def _readme_name(rec: dict) -> str | None:
    entries = (((rec.get("repo") or {}).get("root") or {}).get("entries")) or []
    names = [e["name"] for e in entries if e.get("type") == "blob" and e["name"].lower().startswith("readme")]
    names.sort(key=lambda n: (n.lower() not in ("readme.md", "readme.markdown", "readme.rst", "readme"), n))
    return names[0] if names else None


def _fetch_readmes(http, keys: list, meta: dict, as_of: str) -> dict:
    out = {}
    for start in range(0, len(keys), 10):
        chunk = [k for k in keys[start:start + 10] if _readme_name(meta.get(k, {}))]
        if not chunk:
            continue
        parts = []
        for i, key in enumerate(chunk):
            owner, name = key.split("/")[1:3]
            expr = common.json.dumps("HEAD:" + _readme_name(meta[key]))
            parts.append(f'r{i}:repository(owner:{common.json.dumps(owner)},name:{common.json.dumps(name)})'
                         f'{{object(expression:{expr}){{... on Blob{{text isBinary byteSize}}}}}}')
        st, doc = http.call(GQL, {"query": "{" + " ".join(parts) + "}"}, auth=True, tries=3)
        data = (doc or {}).get("data") or {}
        for i, key in enumerate(chunk):
            blob = ((data.get(f"r{i}") or {}).get("object")) or {}
            if blob.get("text") is not None and not blob.get("isBinary"):
                text, red = redact(blob["text"][:README_MAX])
                out[key] = {"file": _readme_name(meta[key]), "byte_size": blob.get("byteSize"),
                            "observed_at": as_of, "text": text, "redactions": red,
                            "truncated": len(blob["text"]) > README_MAX}
            else:
                out[key] = {"file": _readme_name(meta[key]), "observed_at": as_of, "error": f"readme_{st}"}
    return out


def _history_row(key: str, rec: dict) -> dict:
    """The score-relevant observation for history: small, and enough to trend later."""
    r = rec.get("repo") or {}
    tgt = ((r.get("defaultBranchRef") or {}).get("target")) or {}
    return {"project": key, "observed_at": rec["observed_at"], "query": query_hash(),
            "error": rec.get("error"),
            "stars": r.get("stargazerCount"), "forks": r.get("forkCount"),
            "archived": r.get("isArchived"), "pushed_at": r.get("pushedAt"),
            "open_issues": (r.get("openIssues") or {}).get("totalCount"),
            "closed_issues": (r.get("closedIssues") or {}).get("totalCount"),
            "merged_prs": (r.get("mergedPRs") or {}).get("totalCount"),
            "releases": (r.get("releases") or {}).get("totalCount"),
            "commits_90d": (tgt.get("c90") or {}).get("totalCount"),
            "commits_365d": (tgt.get("c365") or {}).get("totalCount")}


def run(refresh: bool = False, only: list | None = None) -> dict:
    keys, replace = _targets()
    if only:
        keys = [k for k in keys if k in set(only)]
    prior = common.read_json(META) if META.exists() else {"records": {}}
    as_of = now_iso() if (refresh or not prior.get("as_of")) else prior["as_of"]
    records = {} if refresh else prior["records"]
    todo = [k for k in keys if k not in records or records[k].get("error")]
    win = windows(as_of)
    http = enrich.Http()
    print(f"score-harvest: {len(todo)} of {len(keys)} repositories to observe (as_of {as_of})", file=sys.stderr)
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]
    fresh = {}
    done = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for res in pool.map(lambda b: _ask(http, b, win, as_of), batches):
            fresh.update(res)
            records.update(res)
            done += 1
            if done % 25 == 0 or done == len(batches):
                print(f"  {done}/{len(batches)} batches", file=sys.stderr)
                common.write_json(META, {"as_of": as_of, "query": query_hash(), "windows": win, "records": records})
    common.write_json(META, {"as_of": as_of, "query": query_hash(), "windows": win, "records": records})

    readmes = common.read_json(READMES) if (READMES.exists() and not refresh) else {"records": {}}
    want = sorted(k for k in replace if k in records and not records[k].get("error")
                  and (k not in readmes["records"] or readmes["records"][k].get("error")))
    print(f"score-harvest: {len(want)} README texts to fetch", file=sys.stderr)
    readmes["records"].update(_fetch_readmes(http, want, records, as_of))
    readmes["as_of"] = as_of
    common.write_json(READMES, readmes)

    # History gets every observation made under this as_of that it does not already hold,
    # so a run that was interrupted and resumed loses nothing and duplicates nothing.
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    held = set()
    if HISTORY.exists():
        for h in common.read_jsonl(HISTORY):
            held.add((h["project"], h["observed_at"]))
    with open(HISTORY, "a", encoding="utf-8") as fh:
        for key in sorted(records):
            if records[key].get("observed_at") == as_of and (key, as_of) not in held:
                fh.write(common.dumps(_history_row(key, records[key])) + "\n")
    errors = sum(1 for k in keys if records.get(k, {}).get("error"))
    return {"as_of": as_of, "observed": len(fresh), "records": len(records), "errors": errors,
            "readmes": sum(1 for v in readmes["records"].values() if not v.get("error")),
            "http_calls": http.calls, "transient_failures": len(http.failures)}
