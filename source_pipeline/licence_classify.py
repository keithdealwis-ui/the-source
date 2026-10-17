"""Project-level licence classification for flagged corpus rows (KEI-919 S1.1 / S1.7).

The Discover lane flags a repository whose licence it cannot recognise. This module
re-reads such a repository at a pinned commit (the recursive tree plus the text of
every root licence file and every licence file inside an enterprise-style directory)
and assigns ONE project-level class:

    open              licence_detect recognises the root licence (the strict second
                      opinion, unchanged) and no enterprise-style directory below the
                      root carries a licence of its own
    open_core         a recognised open licence text covers the core, a notice names it
                      as the default/rest-of-repository licence, and every part under
                      other terms is clearly delimited: a named directory, a directory
                      that carries its own non-open licence file, or an exact file-header
                      marker. The core SPDX and the excluded paths are recorded.
    source_available  source-available / use-restricted terms (Business Source, Elastic,
                      SSPL, FSL, Sustainable Use, Commons Clause, fair-code,
                      non-commercial) that are not confined to a delimited part
    restricted        a standard open licence text with prohibitive terms added
    missing           no licence file at the repository root
    unclear           anything else, including every case the classifier cannot read

Eligibility follows from the class and from nothing else:
    open        -> eligible
    open_core   -> conditional ("core only; edition check required before recommendation")
    otherwise   -> not_eligible
It FAILS CLOSED: any doubt about rights lands in `unclear`, which is not eligible.

This labels corpus / discovery eligibility only. It never relaxes the recommendation
gate: a project still reaches a recommendation only through the canonical build and,
for any licence issue, a named human entry in config/licence_exceptions.yaml.

Every classification carries provenance (S1.7): the API and files read, the commit
read, and when (UTC).

`classify()` is pure. `fetch()` reads GitHub (network) and caches under raw/.
"""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from . import common
from . import licence_detect as ld

METHOD = "the-source.licence-classify/1"
CLASSES = ("open", "open_core", "source_available", "restricted", "missing", "unclear")
ELIGIBILITY = {"open": "eligible", "open_core": "conditional"}
OPEN_CORE_CONDITION = "core only; edition check required before recommendation"
OPEN_CORE_FOLLOW_UP = "edition_check_required_before_recommendation"
CACHE = common.RAW / "licence_classify"

CORE_COVERAGE = 0.90
# Enterprise-style directory below the root (the root uses licence_detect.ENTERPRISE_DIR).
NESTED_ENTERPRISE_DIR = re.compile(
    r"^\.?(ee|enterprise|enterprise[-_]edition|commercial|premium|proprietary|ee[-_].+|.+[-_]ee|"
    r".+[-_]enterprise|.+[-_]licen[sc]e)$", re.I)
ENTERPRISE_FILE_NAME = re.compile(r"(enterprise|commercial|proprietary|[._-]ee([._-]|$))", re.I)
SUPPORTING_FILES = ("package.json", "Cargo.toml", "pyproject.toml")
MAX_DEPTH = 5

# Open licence names as they appear in notices (normalised text) -> SPDX family.
OSI_NAMES = [
    (re.compile(r"\b(agpl\w*|affero)\b"), "AGPL"),
    (re.compile(r"\b(lgpl\w*|lesser general public)\b"), "LGPL"),
    (re.compile(r"\b(gpl\w*|gnu general public)\b"), "GPL"),
    (re.compile(r"\b(mit|expat)\b"), "MIT"),
    (re.compile(r"\bapache\b"), "Apache"),
    (re.compile(r"\bbsd\b"), "BSD"),
    (re.compile(r"\b(mpl\w*|mozilla public)\b"), "MPL"),
    (re.compile(r"\bisc\b"), "ISC"),
    (re.compile(r"\b(epl\w*|eclipse public)\b"), "EPL"),
    (re.compile(r"\b(eupl\w*|european union public)\b"), "EUPL"),
    (re.compile(r"\bpostgresql licen[sc]e\b"), "PostgreSQL"),
]
# A notice that says the open licence is the default / covers the rest.
CORE_PHRASES = ("default", "rest of", "outside of", "outside the", "mostly licensed", "released under",
                "all other", "remainder", "everything else", "unless otherwise", "except for", "except as",
                "with the exception", "is licensed under", "are licensed under", "available under",
                "licensed under the terms", "is covered by", "under the terms of", "distributed under")
SOURCE_AVAILABLE_EXTRA = ("polyform", "confluent community", "redis source available", "server side public",
                          "business source", "busl", "elastic license", "functional source", "fsl 1",
                          "commons clause", "sustainable use", "fair code", "fair source",
                          "non commercial", "noncommercial", "source available")
PROHIBITIVE = ("may not", "shall not", "must not", "not permitted", "not allowed", "prohibited", "forbidden",
               "additional restriction", "additional restrictions", "you agree not to", "is not allowed")
HEADER_MARKER = re.compile(r"(/\*\s*@licen[sc]e[^*]*\*/|//\s*@licen[sc]e[^\n]*|@licen[sc]e\s+[A-Za-z][\w.-]*)")
PATH_TOKEN = re.compile(r"(?P<pre>[/\"'`]?)(?P<tok>[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*)(?P<post>/?)")
URL = re.compile(r"https?://\S+")
SPLIT = re.compile(r"(?<=[.;!?])\s+|\n\s*\n|\n\s*[-*•]\s+|\n\s*\(?(?:\d+|[ivx]+)[.)]\s+")


CODE_EXT = re.compile(r"\.(ts|tsx|js|jsx|mjs|cjs|go|py|rs|java|kt|cs|rb|php|c|h|cc|cpp|hpp|swift|scala|ex|exs|"
                      r"dart|lua|json|ya?ml|toml|svg|png|jpe?g|gif|html?|css|scss|snap|tmpl|sh)$", re.I)
NON_RUNTIME_DIRS = {"test", "tests", "e2e", "e2e-tests", "scripts", "examples", "example", "fixtures", "testdata"}


def _is_licence_file(name: str) -> bool:
    """A licence file by name; source files named license.ts or a LICENSE_FAQ.md are not."""
    return bool(ld.LICENCE_FILE.match(name)) and not CODE_EXT.search(name) and "faq" not in name.lower()


def _sa_markers() -> tuple[str, ...]:
    try:
        cfg = common.load_yaml(common.CONFIG / "licence_lane.yaml") or {}
    except OSError:
        cfg = {}
    return tuple(sorted(set(cfg.get("source_available_markers") or []) | set(SOURCE_AVAILABLE_EXTRA)))


def _family(spdx: str) -> str:
    return "BSD" if "BSD" in spdx else spdx.split("-")[0]


def _has(norm: str, phrases) -> list[str]:
    padded = f" {norm} "
    return sorted({p for p in phrases if f" {p} " in padded})


def open_texts(text: str) -> tuple[list[dict], str]:
    """Every reference open licence whose text appears (>= CORE_COVERAGE) in `text`.

    Returns ([{"spdx", "coverage"}], leftover) where leftover is the normalised text
    not covered by any of them. Several texts can share one file (e.g. Apache + AGPL)."""
    flat = ld._norm(text)
    words = flat.split()
    keep = [True] * len(words)
    found = []
    for spdx in ld._shortlist(flat, limit=4):
        ref = ld.references()[spdx]["words"]
        sm = SequenceMatcher(None, ref, words, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size >= ld.MIN_BLOCK_WORDS]
        cov = sum(b.size for b in blocks) / len(ref)
        if cov >= CORE_COVERAGE:
            found.append({"spdx": spdx, "coverage": round(cov, 4)})
            for b in blocks:
                for i in range(b.b, b.b + b.size):
                    keep[i] = False
    # BSD-3 contains BSD-2; GPL/LGPL/AGPL texts overlap. Keep the one with most coverage per family.
    best = {}
    for f in found:
        fam = _family(f["spdx"])
        if fam not in best or f["coverage"] > best[fam]["coverage"]:
            best[fam] = f
    found = sorted(best.values(), key=lambda f: f["spdx"])
    return found, " ".join(w for w, k in zip(words, keep) if k)


def _sentences(text: str, refs: list[str]) -> list[dict]:
    """Notice sentences: the parts of a licence file that are not standard licence text."""
    ref_flats = [" " + " ".join(ld.references()[s]["words"]) + " " for s in refs]
    out = []
    for raw in SPLIT.split(text.replace("\r\n", "\n")):
        if not raw or not raw.strip():
            continue
        clean = URL.sub(" ", raw)
        norm = ld._norm(clean)
        if len(norm.split()) < 3:
            continue
        if any(f" {norm} " in rf for rf in ref_flats):
            continue
        out.append({"raw": raw.strip(), "clean": clean, "norm": norm})
    return out


def _path_tokens(clean: str, dirs: set[str]) -> tuple[list[str], bool]:
    """(existing directories named in a sentence, whether it names any path at all)."""
    named, pathy = [], False
    for m in PATH_TOKEN.finditer(clean):
        tok = m.group("tok").strip("./")
        if not tok:
            continue
        slashy = bool(m.group("pre") in ("/", '"', "'", "`") or m.group("post") or "/" in tok)
        if not (slashy or NESTED_ENTERPRISE_DIR.match(tok.split("/")[-1])):
            continue
        if tok in dirs:
            named.append(tok)
            pathy = True
        elif "/" in tok and not re.search(r"\.\w{1,4}$", tok):
            pathy = True  # a path the tree does not have: still a path-scoped sentence
    return sorted(set(named)), pathy


def _osi_families(norm: str) -> set[str]:
    return {fam for rx, fam in OSI_NAMES if rx.search(norm)}


def self_hosting_evidence(paths: list[tuple[str, str]]) -> list[str]:
    ev = []
    for path, kind in paths:
        parts = path.split("/")
        name = parts[-1].lower()
        depth = len(parts)
        if any(p.startswith(".") or p.lower() in NON_RUNTIME_DIRS for p in parts[:-1]):
            continue
        if kind == "blob" and depth <= 2 and (name == "dockerfile" or name.startswith("dockerfile.")
                                             or name.endswith(".dockerfile")):
            ev.append(path)
        elif kind == "blob" and depth <= 3 and re.match(r"^(docker-)?compose([.-][\w.-]+)?\.ya?ml$", name) \
                and not re.search(r"[.-](ci|test|e2e)([.-]|$)", name):
            ev.append(path)
        elif kind == "blob" and depth <= 4 and name == "chart.yaml":
            ev.append(path)
        elif depth <= 4 and re.search(r"self[-_ ]?host", name) and (depth == 1 or parts[0].lower() in
                                                                      ("docs", "doc", "documentation", "website")):
            ev.append(path)
    return sorted(ev)[:10]


def _supporting(texts: dict[str, str]) -> dict:
    out = {}
    pj = texts.get("package.json")
    if pj:
        try:
            v = json.loads(pj).get("license")
            if isinstance(v, str):
                out["package.json"] = v
        except (ValueError, AttributeError):
            pass
    for name in ("Cargo.toml", "pyproject.toml"):
        t = texts.get(name)
        if t:
            m = re.search(r'^\s*licen[sc]e\s*=\s*"([^"]+)"', t, re.M)
            if m:
                out[name] = m.group(1)
    return out


def classify(paths: list[tuple[str, str]], texts: dict[str, str], *, commit: str | None,
             checked_at: str, source: str = "github", found: bool = True, error: str | None = None) -> dict:
    """Classify one repository.

    paths: [(path, "blob"|"tree")] for the tree at `commit`, as far as it was read
    texts: path -> text for the licence files (and supporting manifests) that were read
    """
    files_read = sorted(p for p in texts if p not in SUPPORTING_FILES)
    prov = {"source": {"api": f"{source}: GraphQL tree at commit (3 levels; enterprise-style directories 4) + Blob.text",
                       "files": files_read},
            "commit": commit, "checked_at": checked_at}

    def done(cls, reason, **extra):
        out = {"class": cls, "reason": reason, "method": METHOD, **extra, "provenance": prov}
        return out

    if not found or commit is None:
        return done("unclear", f"repository could not be read ({error or 'not found'}); rights unknown",
                    self_hosting=[])
    supporting = _supporting(texts)
    selfhost = self_hosting_evidence(paths)
    dirs = {p for p, k in paths if k == "tree"}
    root = [{"name": p, "type": "file" if k == "blob" else "dir"} for p, k in paths if "/" not in p]
    root_lic = sorted(e["name"] for e in root if e["type"] == "file" and _is_licence_file(e["name"])
                      and "faq" not in e["name"].lower())
    base = {"self_hosting": selfhost, "supporting": supporting}
    if not root_lic:
        if any(e["type"] == "dir" and e["name"].lower() in ("licenses", "licences", "license", "licence")
               for e in root):
            return done("unclear", "licences are kept in a LICENSES/ directory, not a root licence file; "
                                   "which licence covers what is not established", **base)
        return done("missing", "no licence file at the repository root"
                    + (f" (manifests declare {supporting}; supporting evidence only)" if supporting else ""), **base)
    unread = [n for n in root_lic if n not in texts]
    if unread:
        return done("unclear", f"root licence file(s) could not be read: {unread}", **base)

    # 1. The strict second opinion: recognised over the whole repository.
    det = ld.detect([e for e in root if e["type"] == "dir" or e["name"] in root_lic],
                    {n: texts[n] for n in root_lic})
    nested_lic = sorted(p for p, k in paths if k == "blob" and "/" in p and _is_licence_file(p.split("/")[-1])
                        and any(NESTED_ENTERPRISE_DIR.match(d) for d in p.split("/")[:-1])
                        and len(p.split("/")) <= MAX_DEPTH + 1)
    nested_non_open = {}
    for p in nested_lic:
        t = texts.get(p)
        if t is None or not ld.match_text(t)["matched"]:
            nested_non_open[p.rsplit("/", 1)[0]] = p
    if det["recognised"] and not nested_non_open:
        return done("open", f"root licence recognised as {det['spdx']} over the whole repository",
                    spdx=det["spdx"], **base)

    # 2. Per root licence file: which open texts it carries, and its notice sentences.
    sa = _sa_markers()
    info = {}
    for n in root_lic:
        found_refs, leftover = open_texts(texts[n])
        info[n] = {"open": found_refs, "sentences": _sentences(texts[n], [f["spdx"] for f in found_refs])}
    all_raw = "\n".join(texts[n] for n in root_lic)
    enterprise_files = []
    for n in root_lic:
        if info[n]["open"]:
            continue
        others = "\n".join(texts[m] for m in root_lic if m != n)
        if ENTERPRISE_FILE_NAME.search(n.split(".", 1)[-1] if "." in n else "") or ENTERPRISE_FILE_NAME.search(n) \
                or n in others:
            enterprise_files.append(n)
    core_side = [n for n in root_lic if n not in enterprise_files]
    core_texts = sorted({f["spdx"] for n in core_side for f in info[n]["open"]})
    core_sents = [s for n in core_side for s in info[n]["sentences"]]

    excluded, markers = {}, []
    for d, lic in nested_non_open.items():
        excluded[d] = f"directory carries its own non-open licence file {lic}"
    statements, sa_hits, prohibitive = [], [], []
    for s in core_sents:
        named, pathy = _path_tokens(s["clean"], dirs)
        fams = _osi_families(s["norm"])
        if named and not fams and re.search(r"\b(licen[sc]\w*|enterprise|commercial|proprietary)\b", s["norm"]):
            for d in named:
                excluded.setdefault(d, f"named under other terms in the root licence: \"{s['raw'][:160]}\"")
        for m in HEADER_MARKER.findall(s["raw"]):
            if re.search(r"\b(marked|header|comment)\b", s["norm"]):
                markers.append(m.strip())
        core_fams = {_family(x) for x in core_texts}
        if fams & core_fams and not pathy and _has(s["norm"], CORE_PHRASES):
            statements.append(s["raw"][:240])
        hits = _has(s["norm"], sa)
        if hits and not pathy and not HEADER_MARKER.search(s["raw"]):
            sa_hits.append({"markers": hits, "text": s["raw"][:200]})
        if _has(s["norm"], PROHIBITIVE):
            prohibitive.append(s["raw"][:200])
    # Root enterprise directories must be accounted for: named in the text or carrying a licence.
    root_ent = [e["name"] for e in root if e["type"] == "dir" and ld.ENTERPRISE_DIR.match(e["name"])]
    for d in root_ent:
        if d not in excluded and re.search(rf"(^|[\s\"'`(/]){re.escape(d)}(/|[\s\"'`).,;:]|$)", all_raw):
            excluded[d] = "enterprise directory named in the root licence"
    unaccounted_ent = [d for d in root_ent if d not in excluded]
    unaccounted_files = [n for n in core_side if not info[n]["open"] and not info[n]["sentences"]]
    # A core-side file that is neither open text nor a notice wrapper around one: a foreign licence.
    foreign = [n for n in core_side if not info[n]["open"]
               and len(ld._norm(texts[n])) > 2500]
    implicit = not core_sents and bool(core_texts)  # pure standard text at the root, parts licensed separately
    exclusions = {"paths": sorted(excluded), "markers": sorted(set(markers))}
    evidence = {"core_texts": core_texts, "core_statements": statements[:3],
                "enterprise_licence_files": enterprise_files,
                "excluded_paths": [{"path": p, "why": excluded[p]} for p in sorted(excluded)],
                "header_markers": sorted(set(markers))}

    if (core_texts and (excluded or markers) and (statements or implicit) and not sa_hits
            and not unaccounted_ent and not foreign and not unaccounted_files):
        stmt_fams = set().union(*(_osi_families(ld._norm(t)) for t in statements)) if statements else \
            {_family(x) for x in core_texts}
        core = sorted(x for x in core_texts if _family(x) in stmt_fams) or core_texts
        return done("open_core", f"core under {' AND '.join(core)}; other terms confined to "
                                 f"{', '.join(exclusions['paths'] + exclusions['markers'])}",
                    core_spdx=" AND ".join(core), open_components=core_texts, excluded=exclusions,
                    evidence=evidence, **base)

    non_open_sa = [n for n in root_lic if not info[n]["open"] and n not in enterprise_files
                   and _has(ld._norm(texts[n]), sa)]
    if sa_hits or non_open_sa:
        return done("source_available", "source-available terms not confined to a delimited part: "
                    + "; ".join(sorted({m for h in sa_hits for m in h["markers"]}
                                       | {f"{n} carries source-available terms" for n in non_open_sa})),
                    evidence={**evidence, "source_available": sa_hits[:3]}, **base)
    if core_texts and prohibitive and not (excluded or markers):
        return done("restricted", "a standard open licence with prohibitive terms added",
                    evidence={**evidence, "prohibitive": prohibitive[:3]}, **base)
    why = []
    if not core_texts:
        why.append("no root licence file carries a recognised open licence text")
    if core_texts and (excluded or markers) and not (statements or implicit):
        why.append("parts are licensed separately but no notice names the open licence as the default")
    if unaccounted_ent:
        why.append(f"enterprise directory {unaccounted_ent} has no stated licence")
    if foreign:
        why.append(f"{foreign} is a licence text that matches no reference open licence")
    if core_texts and not (excluded or markers):
        why.append(f"open licence text present, but the notices around it ({det['refusal_reasons']}) "
                   "are not a recognised standard text")
    return done("unclear", "; ".join(why) or "rights could not be established", evidence=evidence, **base)


def eligibility(cls: dict) -> dict:
    status = ELIGIBILITY.get(cls["class"], "not_eligible")
    out = {"status": status, "condition": None, "follow_up": None,
           "self_hostable": bool(cls.get("self_hosting")) or None,
           "self_hosting_evidence": cls.get("self_hosting") or [],
           "recommendation_gate": "unchanged: recommendation still requires the canonical build and, for any "
                                  "licence issue, a named entry in config/licence_exceptions.yaml"}
    if status == "conditional":
        out["condition"] = OPEN_CORE_CONDITION
        out["follow_up"] = OPEN_CORE_FOLLOW_UP
    return out


def row_fields(cls: dict) -> dict:
    """The compact form published on a corpus row's `licence` object."""
    c = {k: cls[k] for k in ("class", "reason", "method", "provenance")}
    for k in ("spdx", "core_spdx", "excluded", "supporting"):
        if cls.get(k):
            c[k] = cls[k]
    return {"classification": c, "eligibility": eligibility(cls)}


# -- network


LIST_DEPTH = 3  # tree levels read in one query; enterprise-style directories are then opened one level more


def _entries(depth: int) -> str:
    inner = "name type"
    for _ in range(depth - 1):
        inner = f"name type object{{... on Tree{{entries{{{inner}}}}}}}"
    return f"... on Tree{{entries{{{inner}}}}}"


def _flatten(entries, prefix="", out=None):
    out = [] if out is None else out
    for e in entries or []:
        path = f"{prefix}{e['name']}"
        kind = "tree" if e["type"] == "tree" else "blob" if e["type"] == "blob" else None
        if kind:
            out.append((path, kind))
        sub = ((e.get("object") or {}).get("entries")) if kind == "tree" else None
        if sub:
            _flatten(sub, path + "/", out)
    return out


class Fetcher:
    """Reads one repository at its default-branch commit through GitHub GraphQL: the tree
    to LIST_DEPTH levels (enterprise-style directories one level further) and the text of
    the licence files and supporting manifests. Responses are cached under raw/ (gitignored)."""

    def __init__(self, api=None, cache: Path = CACHE):
        from .discover import Api

        self.api = api or Api()
        self.cache = cache
        cache.mkdir(parents=True, exist_ok=True)

    def _path(self, slug):
        return self.cache / (slug.replace("/", "__") + ".json")

    def _repo(self, slug: str, body: str):
        owner, name = slug.split("/", 1)
        st, doc = self.api.gql(f"query{{repository(owner:{json.dumps(owner)}, name:{json.dumps(name)}){{{body}}}}}")
        if st != 200 or not isinstance(doc, dict):
            return st, None, f"graphql_http_{st}"
        if doc.get("data") is None or (doc.get("data") or {}).get("repository") is None:
            kinds = {e.get("type") for e in doc.get("errors") or []}
            return st, None, "not_found" if "NOT_FOUND" in kinds else f"graphql_error_{sorted(kinds)}"
        return st, doc["data"]["repository"], None

    def one(self, slug: str, use_cache: bool = True) -> dict:
        cp = self._path(slug)
        if use_cache and cp.exists():
            return common.read_json(cp)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rec = {"slug": slug, "found": False, "commit": None, "error": None, "fetched_at": now,
               "paths": [], "texts": {}, "list_depth": LIST_DEPTH}
        _, repo, err = self._repo(slug, "nameWithOwner defaultBranchRef{target{oid}} "
                                        f"object(expression:\"HEAD:\"){{{_entries(LIST_DEPTH)}}}")
        if err:
            rec["error"] = err  # found stays False: classify() then says unclear, never open
            return self._save(cp, rec, err)
        rec["found"] = True
        if not repo.get("defaultBranchRef"):
            rec["error"] = "empty_repository"
            return self._save(cp, rec)
        commit = repo["defaultBranchRef"]["target"]["oid"]
        rec["commit"] = commit
        paths = _flatten((repo.get("object") or {}).get("entries"))
        # Open enterprise-style directories at the deepest listed level one level more.
        deep = [p for p, k in paths if k == "tree" and p.count("/") == LIST_DEPTH - 1
                and NESTED_ENTERPRISE_DIR.match(p.split("/")[-1])][:10]
        want = [p for p, k in paths if k == "blob" and (
            ("/" not in p and (_is_licence_file(p) or p in SUPPORTING_FILES))
            or ("/" in p and _is_licence_file(p.split("/")[-1])
                and any(NESTED_ENTERPRISE_DIR.match(d) for d in p.split("/")[:-1])))][:25]
        fields = [f"d{i}: object(expression:{json.dumps(commit + ':' + d)}){{... on Tree{{entries{{name type}}}}}}"
                  for i, d in enumerate(deep)]
        fields += [f"f{i}: object(expression:{json.dumps(commit + ':' + p)}){{... on Blob{{text isBinary}}}}"
                   for i, p in enumerate(want)]
        if fields:
            _, got, err = self._repo(slug, " ".join(fields))
            if err:
                rec["error"] = f"blobs_{err}"
                got = {}
            extra_want = []
            for i, d in enumerate(deep):
                for e in ((got.get(f"d{i}") or {}).get("entries") or []):
                    kind = "tree" if e["type"] == "tree" else "blob"
                    paths.append((f"{d}/{e['name']}", kind))
                    if kind == "blob" and _is_licence_file(e["name"]):
                        extra_want.append(f"{d}/{e['name']}")
            for i, p in enumerate(want):
                o = got.get(f"f{i}")
                if o and o.get("text") is not None and not o.get("isBinary"):
                    rec["texts"][p] = o["text"][:200000]
            if extra_want:
                f2 = " ".join(f"f{i}: object(expression:{json.dumps(commit + ':' + p)}){{... on Blob{{text isBinary}}}}"
                              for i, p in enumerate(extra_want))
                _, got2, err2 = self._repo(slug, f2)
                for i, p in enumerate(extra_want):
                    o = (got2 or {}).get(f"f{i}")
                    if o and o.get("text") is not None and not o.get("isBinary"):
                        rec["texts"][p] = o["text"][:200000]
        rec["paths"] = sorted(paths)
        return self._save(cp, rec)

    def _save(self, cp, rec, err=None):
        # Transport failures are not cached: the next run retries them.
        if not (err and err.startswith("graphql_http")):
            common.write_json(cp, rec)
        return rec


def fetch_all(slugs: list[str], workers: int = 6, api=None) -> dict[str, dict]:
    from .discover import log

    f = Fetcher(api)
    log(f"licence-classify: reading {len(slugs)} repositories (GraphQL, cached under {f.cache})")
    out = {}
    with ThreadPoolExecutor(workers) as pool:
        for n, (slug, rec) in enumerate(zip(slugs, pool.map(f.one, slugs)), 1):
            out[slug] = rec
            if n % 100 == 0:
                log(f"licence-classify: {n}/{len(slugs)} read, {f.api.http.calls} requests")
    log(f"licence-classify: {f.api.http.calls} requests, {len(f.api.http.failures)} transport failures")
    return out



def classify_record(rec: dict) -> dict:
    texts = rec.get("texts") or {}
    return classify([tuple(p) for p in rec.get("paths") or []], texts, commit=rec.get("commit"),
                    checked_at=rec["fetched_at"], found=bool(rec.get("found")) and bool(rec.get("paths")),
                    error=rec.get("error"))


def run(out: Path | None = None, workers: int = 8, api=None) -> dict:
    """Classify every flagged corpus row (network), write the fields, re-validate, promote."""
    from . import corpus

    out = out or corpus.OUT
    m = common.read_json(out / "MANIFEST.json")
    pre = corpus.validate(out)
    if pre["failed_checks"]:
        raise corpus.CorpusError(f"data/corpus does not validate before classification: {pre['failed_checks']}")
    projects = common.read_jsonl(out / "projects.jsonl")
    live = {r["id"]: r for r in common.read_jsonl(out / "live.jsonl")}
    flagged = [p for p in projects if p["licence"]["status"] == "issue"]
    slugs = {}
    for p in flagged:
        if p["id"].startswith("github.com/"):
            slugs[p["id"]] = (live[p["id"]].get("full_name") or p["full_name"]).lower()
    recs = fetch_all(sorted(set(slugs.values())), workers=workers, api=api)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    before = {}
    after = {}
    for p in flagged:
        before[p["licence"]["issue"]["category"]] = before.get(p["licence"]["issue"]["category"], 0) + 1
        if p["id"] in slugs:
            cls = classify_record(recs[slugs[p["id"]]])
        else:
            host = p["id"].split("/", 1)[0]
            cls = classify([], {}, commit=None, checked_at=stamp, source=host, found=False,
                           error=f"host {host} is not read by the classifier")
        p["licence"].update(row_fields(cls))
        after[cls["class"]] = after.get(cls["class"], 0) + 1
    extra = {k: m[k] for k in ("membership", "publication", "refresh") if k in m}
    extra["licence_classification"] = {
        "method": METHOD, "run_at": stamp, "scope": "rows with licence.status == 'issue'",
        "rules": "source_pipeline/licence_classify.py; docs/ACCEPTANCE-KEI-919-S1.1.md",
        "recommendation_gate": "unchanged"}
    manifest = corpus.write_layer(out, projects, list(live.values()), extra)
    return {"classified": len(flagged), "before": dict(sorted(before.items())),
            "after": dict(sorted(after.items())), "corpus_version": manifest["corpus_version"],
            "counts": manifest["counts"]}


def main() -> int:
    from . import corpus

    try:
        print(json.dumps(run(), indent=2))
    except corpus.CorpusError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    return 0
