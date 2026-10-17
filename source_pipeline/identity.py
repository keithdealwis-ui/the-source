"""Identity resolution for the discovery corpus (KEI-919 S1.8): one real project, one entity.

A deterministic step over data/corpus/ rows, the canonical project keys
(data/canonical/oss_projects.jsonl) and GitHub's own identity facts
(data/identity/github.jsonl, refreshed in bounded GraphQL batches by `corpus-identity --refresh`):

  renames    GitHub answers a moved repository under its current name (GraphQL follows renames,
             REST follows transfers). The current name is recorded as `identity.renamed_to` and
             as an alias, so the old and the new address resolve to one row. Two rows that turn
             out to be one repository (a shared address, or the same host repository id) are
             collapsed into one, the other id kept as an alias (`identity.merged`).
  forks      a fork (`isFork`, `parent`) is marked `identity.fork_of`; `fork_parent_in_corpus`
             says whether the parent is a project here, in which case the fork must never be
             presented as a separate replacement candidate for it. Forks are not collapsed: a
             fork can be a real successor.
  mirrors    a repository GitHub flags as a mirror, or whose own description says it mirrors
             another, is marked `identity.mirror`, with `mirror_of` the upstream it names. When
             the upstream is a project here (including the same owner/name on another host, or
             a canonical project), the mirror is collapsed into it. A mirror of an upstream The
             Source does not hold (postgres/postgres mirrors git.postgresql.org) stays: it is the
             only representation of that project.
  siblings   one owner, one project split across repositories (owner/server + owner/clients,
             owner/project + owner/project-docs): `identity.siblings` lists the others and
             `identity.sibling_primary` names the one a name lookup should land on.

Nothing here is estimated: every mark traces to a fact held (a name, a description, a GitHub
flag). Rows with nothing to say carry `identity: null`.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import common

FACTS_NAME = "github.jsonl"
FACTS_FIELDS = {"id", "asked", "observed_at", "found", "error", "name_with_owner", "database_id", "is_fork", "parent",
                "is_mirror", "mirror_url"}
GQL_FIELDS = "nameWithOwner databaseId isFork isMirror mirrorUrl parent{nameWithOwner databaseId}"
IDENTITY_KEYS = ("renamed_to", "fork_of", "fork_parent_in_corpus", "mirror", "mirror_of", "merged", "siblings",
                 "sibling_primary")
HOSTS = ("github.com", "gitlab.com", "codeberg.org")

# A repository that says it is a mirror. "mirroring", "mirrord" and "a mirror service" are products, not mirrors.
MIRROR_SAYS = re.compile(
    r"\(\s*mirror\s*\)|\bmirror\s+(?:of|from)\b|\bis\s+(?:just\s+)?a\s+\*?mirror\*?\b|^\s*mirror\b"
    r"|\b(?:read-only|readonly|official(?:\s+github)?|github|public(?:\s+publish)?|publish|archived|source\s+code"
    r"|codebase|ce)\s+mirror\b", re.I)
UPSTREAM = re.compile(r"(?:mirror\s+(?:of|from)|main\s+repo:?|maintained\s+at)\s+(?:the\s+)?(https?://[^\s,;)]+|"
                      r"@?[A-Za-z0-9][\w.-]*/[\w.-]+)", re.I)

SIBLING_PARTS = ("server", "client", "clients", "web", "webapp", "frontend", "backend", "api", "app", "desktop",
                 "mobile", "android", "ios", "docs", "doc", "documentation", "website", "site", "www", "cli", "sdk",
                 "helm", "helm-charts", "charts", "docker", "deploy", "ui", "core", "io")
PRIMARY_PARTS = ("core", "server", "backend", "app")
_PARTS_BY_LEN = sorted(SIBLING_PARTS, key=lambda s: (-len(s), s))


def data_dir(out: Path) -> Path:
    return out.parent


def load_facts(path: Path) -> dict:
    return {r["id"]: r for r in common.read_jsonl(path)} if path.is_file() else {}


def load_canonical(path: Path) -> list[dict]:
    return common.read_jsonl(path) if path.is_file() else []


def _key(url: str | None) -> str | None:
    """host/owner/name for a repository address (URL or key), lower case, no .git; other URLs as host/path."""
    if not url:
        return None
    u = re.sub(r"^https?://", "", url.strip().lower()).rstrip("/.")
    u = re.sub(r"^www\.", "", u)
    u = re.sub(r"\.git$", "", u).rstrip("/")
    parts = u.split("/")
    if len(parts) < 3 or not parts[1] or not parts[2]:
        return u or None
    return "/".join(parts[:3]) if parts[0] in HOSTS else u


def says_mirror(desc: str | None) -> bool:
    return bool(desc and MIRROR_SAYS.search(desc))


def upstream_of(desc: str | None, host: str) -> tuple[str | None, bool]:
    """(the upstream a mirror's description names, whether it was a full URL). A bare owner/name
    ("mirror of monepla/report-mcp") is only trusted when it names a repository held here."""
    m = UPSTREAM.search(desc or "")
    if not m:
        return None, False
    ref = m.group(1).rstrip(".")
    if ref.startswith("@"):  # an npm package, not a repository
        return None, False
    if "://" not in ref:
        return (f"{host}/{ref.lower()}", False) if ref.count("/") == 1 else (None, False)
    return _key(ref), True


def stem(owner: str, name: str) -> str:
    """The project a repository is a part of: owner/server -> owner, owner/x-docs -> x."""
    n = name.lower()
    if n in SIBLING_PARTS:
        return owner.lower()
    for part in _PARTS_BY_LEN:
        for sep in "-_.":
            tail = sep + part
            if n.endswith(tail) and len(n) > len(tail):
                return n[:-len(tail)]
    return n


class _Union:
    def __init__(self, ids):
        self.p = {i: i for i in ids}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def join(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _why(m: str, prim: str, info: dict, rows: dict, reason: dict) -> str:
    """Why row m is the same project as its primary, from the facts that joined them."""
    a, b = info[m], info[prim]
    if a["mirror"] or (m.split("/", 1)[1] == prim.split("/", 1)[1] and a["host"] != b["host"]):
        return "mirror"
    if {m, a["current"], *rows[m]["aliases"]} & {prim, b["current"], *rows[prim]["aliases"]}:
        return "renamed"
    if a["hid"] and a["hid"] == b["hid"] and a["host"] == b["host"]:
        return "same_repository"
    return reason.get(m) if reason.get(m) in ("renamed", "same_repository", "mirror") else "same_canonical_project"


def _routes_key(r):
    return (r["depth"] if r["depth"] is not None else 9, r["strategy"] or "", r["source"] or "", r["locator"] or "")


def resolve(projects: list[dict], live: list[dict], facts: dict | None = None,
            canonical: list[dict] | None = None) -> tuple[list[dict], list[dict], dict]:
    """(projects, live, report) with identity resolved. Pure, deterministic, idempotent."""
    facts = facts or {}
    live_by = {r["id"]: r for r in live}
    rows = {p["id"]: {**p, "aliases": list(p["aliases"])} for p in projects}
    prior_merged = {pid: list(((p.get("identity") or {}).get("merged")) or []) for pid, p in rows.items()}
    canon_of: dict[str, str] = {}  # any address of a canonical project -> its oss_id
    for c in canonical or []:
        for u in (c.get("oss_id"), c.get("repo_url"), c.get("github_url")):
            k = _key(u)
            if k:
                canon_of.setdefault(k, c["oss_id"])
    canonical_ids = {c["oss_id"] for c in canonical or []}

    # -- facts per row: current name, host repository id, fork, mirror
    info = {}
    for pid, p in rows.items():
        host = pid.split("/", 1)[0]
        f = facts.get(pid) or {}
        found = f.get("found")
        current = (f.get("name_with_owner") if found else None) or (live_by.get(pid) or {}).get("full_name") \
            or p["full_name"]
        cur_key = f"{host}/{current.lower()}"
        hid = (f.get("database_id") if found else None) or p.get("host_repo_id")
        mirror = bool(found and f.get("is_mirror")) or says_mirror(p.get("description"))
        upstream, explicit = (_key(f.get("mirror_url")), True) if found and f.get("mirror_url") else \
            upstream_of(p.get("description"), host) if mirror else (None, False)
        info[pid] = {"host": host, "current": cur_key, "hid": hid, "mirror": mirror, "upstream": upstream,
                     "upstream_explicit": explicit,
                     "fork_parent": (f.get("parent") or {}).get("slug") if found and f.get("is_fork") else None}

    # -- one repository, one row: shared addresses, the same host repository id, mirrors of a held upstream
    uf = _Union(sorted(rows))
    reason: dict[str, str] = {}
    by_addr: dict[str, str] = {}
    for pid in sorted(rows):
        for k in sorted({pid, info[pid]["current"], *rows[pid]["aliases"]}):
            if k in by_addr and uf.find(by_addr[k]) != uf.find(pid):
                uf.join(by_addr[k], pid)
                reason.setdefault(pid, "renamed")
            by_addr.setdefault(k, pid)
    by_hid: dict[tuple, str] = {}
    for pid in sorted(rows):
        h = info[pid]["hid"]
        if h:
            first = by_hid.setdefault((info[pid]["host"], h), pid)
            if first != pid and uf.find(first) != uf.find(pid):
                uf.join(first, pid)
                reason.setdefault(pid, "same_repository")
    by_canon: dict[str, str] = {}
    for pid in sorted(rows):
        oid = next((canon_of[k] for k in sorted({pid, info[pid]["current"], *rows[pid]["aliases"]}) if k in canon_of),
                   None)
        if oid:
            first = by_canon.setdefault(oid, pid)
            if first != pid and uf.find(first) != uf.find(pid):
                uf.join(first, pid)
                reason.setdefault(pid, "same_canonical_project")
    # the same owner/name on two hosts, one of them saying it mirrors: one project
    by_path: dict[str, list[str]] = {}
    for pid in sorted(rows):
        by_path.setdefault(pid.split("/", 1)[1], []).append(pid)
    for path, ids in sorted(by_path.items()):
        if len({i.split("/", 1)[0] for i in ids}) > 1 and any(info[i]["mirror"] or i in canonical_ids
                                                              or canon_of.get(i) for i in ids):
            for i in ids[1:]:
                if uf.find(ids[0]) != uf.find(i):
                    uf.join(ids[0], i)
                    reason.setdefault(i, "mirror")
    for pid in sorted(rows):
        up = info[pid]["upstream"]
        tgt = by_addr.get(up) if up else None
        if tgt and uf.find(tgt) != uf.find(pid):
            uf.join(tgt, pid)
            reason[pid] = "mirror"

    groups: dict[str, list[str]] = {}
    for pid in sorted(rows):
        groups.setdefault(uf.find(pid), []).append(pid)

    def primary_rank(pid):
        inf = info[pid]
        return (0 if pid in canonical_ids else 1, 1 if inf["mirror"] else 0,
                0 if inf["current"] == pid else 1, 0 if inf["host"] == "github.com" else 1, pid)

    merged_into: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        prim = min(members, key=primary_rank)
        p = rows[prim]
        aliases = set(p["aliases"])
        provenance = {common.dumps(r): r for r in p["provenance"]}
        lanes = set(p["lanes"])
        merged = prior_merged.get(prim, [])
        for m in sorted(members):
            if m == prim:
                continue
            q = rows[m]
            merged_into[m] = prim
            aliases |= {m, *q["aliases"]}
            provenance.update({common.dumps(r): r for r in q["provenance"]})
            lanes |= set(q["lanes"])
            merged = merged + [{"id": m, "reason": _why(m, prim, info, rows, reason)}] + prior_merged.get(m, [])
        p.update(aliases=sorted(aliases - {prim}), provenance=sorted(provenance.values(), key=_routes_key),
                 lanes=sorted(lanes))
        prior_merged[prim] = merged
    for m in merged_into:
        del rows[m]

    # -- addresses that resolve to a row, after collapsing
    addr: dict[str, str] = {}
    for pid in sorted(rows):
        for k in [pid, *rows[pid]["aliases"]]:
            addr.setdefault(k, pid)

    out_ident: dict[str, dict] = {}
    for pid in sorted(rows):
        inf = info[pid]
        renamed = inf["current"] if inf["current"] != pid else None
        if renamed and renamed not in rows[pid]["aliases"] and addr.get(renamed, pid) == pid:
            rows[pid]["aliases"] = sorted({*rows[pid]["aliases"], renamed})
            addr[renamed] = pid
        parent = f"github.com/{inf['fork_parent']}" if inf["fork_parent"] else None
        fork_of = addr.get(parent, parent) if parent else None
        upstream = inf["upstream"]
        mirror_of = addr.get(upstream, upstream if inf["upstream_explicit"] else None) if upstream else None
        out_ident[pid] = {"renamed_to": renamed, "fork_of": fork_of,
                          "fork_parent_in_corpus": (fork_of in rows) if fork_of else None,
                          "mirror": inf["mirror"], "mirror_of": mirror_of,
                          "merged": sorted(prior_merged.get(pid, []), key=lambda m: m["id"]),
                          "siblings": [], "sibling_primary": None}

    # -- siblings: one owner, one project split across repositories
    stems: dict[tuple, list[str]] = {}
    for pid in sorted(rows):
        host_owner, name = pid.rsplit("/", 1)
        owner = host_owner.split("/", 1)[1]
        stems.setdefault((host_owner, stem(owner, name)), []).append(pid)
    for (host_owner, st), members in sorted(stems.items()):
        if len(members) < 2:
            continue
        stars = {m: (live_by.get(m) or {}).get("stars") or 0 for m in members}

        def sib_rank(m):
            name = m.rsplit("/", 1)[1]
            return (0 if m in canonical_ids else 1, 0 if name == st else 1, -stars[m],
                    PRIMARY_PARTS.index(name) if name in PRIMARY_PARTS else len(PRIMARY_PARTS), m)
        prim = min(members, key=sib_rank)
        for m in members:
            out_ident[m]["siblings"] = [x for x in members if x != m]
            out_ident[m]["sibling_primary"] = prim

    result = []
    for pid in sorted(rows):
        ident = out_ident[pid]
        empty = not any(ident[k] for k in IDENTITY_KEYS if k != "fork_parent_in_corpus")
        result.append({**rows[pid], "identity": None if empty else ident})
    live_out = [live_by[pid] for pid in sorted(rows) if pid in live_by]
    return result, live_out, counts(result, merged_into)


def counts(projects: list[dict], merged_now: dict | None = None) -> dict:
    ids = [p.get("identity") or {} for p in projects]
    merged = [m for i in ids for m in i.get("merged") or []]
    prim = {i["sibling_primary"] for i in ids if i.get("sibling_primary")}
    return {"with_identity": sum(1 for i in ids if i),
            "renamed": sum(1 for i in ids if i.get("renamed_to")),
            "forks": sum(1 for i in ids if i.get("fork_of")),
            "forks_of_corpus_projects": sum(1 for i in ids if i.get("fork_parent_in_corpus")),
            "mirrors": sum(1 for i in ids if i.get("mirror")),
            "mirrors_of_corpus_projects": sum(1 for i in ids if i.get("mirror") and i.get("mirror_of")
                                              in {p["id"] for p in projects}),
            "collapsed": len(merged),
            "collapsed_by_reason": {r: sum(1 for m in merged if m["reason"] == r)
                                    for r in sorted({m["reason"] for m in merged})},
            "sibling_groups": len(prim),
            "sibling_repositories": sum(1 for i in ids if i.get("sibling_primary"))}


def check(projects: list[dict]) -> list[str]:
    """Referential checks over resolved rows (corpus-validate)."""
    bad = []
    ids = {p["id"] for p in projects}
    by = {p["id"]: p for p in projects}
    for p in projects:
        i = p.get("identity")
        if i is None:
            continue
        if sorted(i) != sorted(IDENTITY_KEYS):
            bad.append(f"{p['id']}: identity fields {sorted(set(i) ^ set(IDENTITY_KEYS))}")
            continue
        if i["fork_parent_in_corpus"] and i["fork_of"] not in ids:
            bad.append(f"{p['id']}: fork_of {i['fork_of']} is not a corpus project")
        if i["fork_of"] == p["id"] or i["mirror_of"] == p["id"]:
            bad.append(f"{p['id']}: is its own parent or upstream")
        for m in i["merged"]:
            if m["id"] in ids or m["id"] not in p["aliases"]:
                bad.append(f"{p['id']}: merged {m['id']} is still a row or not an alias")
        if i["renamed_to"] and i["renamed_to"] not in p["aliases"] and i["renamed_to"] not in ids:
            bad.append(f"{p['id']}: renamed_to {i['renamed_to']} resolves nowhere")
        if i["sibling_primary"]:
            sp = by.get(i["sibling_primary"])
            if not sp or (sp.get("identity") or {}).get("sibling_primary") != i["sibling_primary"] \
                    or any(s not in ids for s in i["siblings"]) or not i["siblings"]:
                bad.append(f"{p['id']}: sibling group does not close")
            elif p["id"].rsplit("/", 1)[0] != i["sibling_primary"].rsplit("/", 1)[0]:
                bad.append(f"{p['id']}: sibling of another owner")
    return bad


# -- bounded GitHub refresh of identity facts (network)


def refresh_facts(out: Path, api=None, now: datetime | None = None, size: int = 100, max_requests: int = 150,
                  max_rest: int = 60) -> dict:
    """Ask GitHub, in GraphQL batches of `size`, for every github.com corpus project's current name,
    id, fork parent and mirror flag; follow a not-found through REST (transfers). Writes
    data/identity/github.jsonl. Bounded: refuses more than max_requests batches."""
    from .discover import Api, log

    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    projects = common.read_jsonl(out / "projects.jsonl")
    live = {r["id"]: r for r in common.read_jsonl(out / "live.jsonl")}
    slugs = {p["id"]: ((live.get(p["id"]) or {}).get("full_name") or p["full_name"]).lower()
             for p in projects if p["id"].startswith("github.com/")}
    order = sorted(set(slugs.values()))
    chunks = [order[i:i + size] for i in range(0, len(order), size)]
    if len(chunks) > max_requests:
        raise ValueError(f"{len(chunks)} batches would exceed max_requests {max_requests}")
    api = api or Api()
    log(f"identity: {len(order)} repositories in {len(chunks)} GraphQL batches")
    got: dict[str, dict] = {}

    def one(chunk):
        parts = [f"r{i}: repository(owner:{json.dumps(s.split('/', 1)[0])}, name:{json.dumps(s.split('/', 1)[1])})"
                 f"{{{GQL_FIELDS}}}" for i, s in enumerate(chunk)]
        st, doc = api.gql("query{" + "\n".join(parts) + "}")
        if st != 200 or not isinstance(doc, dict) or not isinstance(doc.get("data"), dict):
            if len(chunk) > 1:
                half = len(chunk) // 2
                return {**one(chunk[:half]), **one(chunk[half:])}
            return {chunk[0]: {"found": None, "error": f"graphql_http_{st}"}}
        nf = {(e.get("path") or [None])[0] for e in doc.get("errors") or [] if e.get("type") == "NOT_FOUND"}
        res = {}
        for i, s in enumerate(chunk):
            r = doc["data"].get(f"r{i}")
            if r:
                par = r.get("parent")
                res[s] = {"found": True, "error": None, "name_with_owner": r["nameWithOwner"],
                          "database_id": r.get("databaseId"), "is_fork": bool(r.get("isFork")),
                          "parent": {"slug": par["nameWithOwner"].lower(), "database_id": par.get("databaseId")}
                          if par else None, "is_mirror": bool(r.get("isMirror")), "mirror_url": r.get("mirrorUrl")}
            else:
                res[s] = {"found": False if f"r{i}" in nf else None,
                          "error": "not_found" if f"r{i}" in nf else "missing_from_response"}
        return res

    for n, chunk in enumerate(chunks, 1):
        got.update(one(chunk))
        if n % 20 == 0:
            log(f"  identity {n}/{len(chunks)}")
    rest = 0
    for s in sorted(k for k, v in got.items() if v.get("error") == "not_found"):
        if rest >= max_rest:
            break
        rest += 1
        st, doc = api.rest(f"repos/{s}")
        moved = (doc or {}).get("full_name") if st == 200 else None
        if moved:
            par = (doc or {}).get("parent")
            got[s] = {"found": True, "error": None, "name_with_owner": moved, "database_id": doc.get("id"),
                      "is_fork": bool(doc.get("fork")),
                      "parent": {"slug": par["full_name"].lower(), "database_id": par.get("id")} if par else None,
                      "is_mirror": bool(doc.get("mirror_url")), "mirror_url": doc.get("mirror_url")}
    rows = []
    for pid, s in sorted(slugs.items()):
        g = got.get(s) or {"found": None, "error": "not_asked"}
        rows.append({"id": pid, "asked": s, "observed_at": stamp, "found": g.get("found"), "error": g.get("error"),
                     "name_with_owner": g.get("name_with_owner"), "database_id": g.get("database_id"),
                     "is_fork": g.get("is_fork"), "parent": g.get("parent"), "is_mirror": g.get("is_mirror"),
                     "mirror_url": g.get("mirror_url")})
    # a transient failure keeps the previous answer for that repository rather than forgetting it
    path = data_dir(out) / "identity" / FACTS_NAME
    prev = load_facts(path)
    rows = [prev[r["id"]] if r["found"] is None and prev.get(r["id"], {}).get("found") is not None else r
            for r in rows]
    common.write_jsonl(path, rows)
    return {"observed_at": stamp, "repositories": len(rows), "requests": api.http.calls, "batches": len(chunks),
            "rest_follow": rest, "found": sum(1 for r in rows if r["found"]),
            "not_found": sum(1 for r in rows if r["found"] is False),
            "unanswered": sum(1 for r in rows if r["found"] is None)}


def apply(out: Path, refresh: bool = False, api=None) -> dict:
    """corpus-identity: (optionally refresh facts, then) re-resolve data/corpus and promote it if it validates."""
    from . import corpus

    stats = refresh_facts(out, api) if refresh else None
    m = common.read_json(out / "MANIFEST.json")
    before = m["counts"]["projects"]
    projects = common.read_jsonl(out / "projects.jsonl")
    live = common.read_jsonl(out / "live.jsonl")
    extra = {k: v for k, v in m.items() if k in ("membership", "publication", "refresh")}
    manifest = corpus.write_layer(out, projects, live, extra)
    return {"facts_refresh": stats, "projects_before": before, "projects_after": manifest["counts"]["projects"],
            "identity": manifest["counts"]["identity"], "corpus_version": manifest["corpus_version"]}


def main(argv: list[str]) -> int:
    from . import corpus

    try:
        print(json.dumps(apply(corpus.OUT, refresh="--refresh" in argv), indent=2))
    except (corpus.CorpusError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    return 0
