"""The Source MCP retrieval server (KEI-851, docs/MCP.md).

    python -m source_pipeline.mcp_server [--root DIR] [--now TS]
    python -m source_pipeline mcp-serve  [--root DIR] [--now TS]

A Model Context Protocol server on stdio (newline-delimited JSON-RPC 2.0); the same
engine is served over Streamable HTTP by source_pipeline/mcp_http.py. It answers from
the canonical Source read layer: data/canonical/ (MANIFEST.json, NOTICE.md,
saas_products.jsonl, oss_projects.jsonl, relationships.jsonl) and api/v1/ (index.json,
domains.json, saas/<saas_id>.json), approved for publication under gate-KEI-807-bfaf2354
and licensed CC BY 4.0 (DATA-LICENCE.md), plus the two published signal layers the daily
cycle commits beside it: data/momentum/ (Repository Momentum, docs/MOMENTUM.md) and
data/radar/ (Community Radar, docs/RADAR.md). Each signal layer is verified against its
own MANIFEST; when one is missing, malformed or stale the canonical tools keep serving
and every momentum answer says which and why. KEI-912 adds the public discovery corpus,
data/corpus/ (docs/CORPUS.md): every project the Discover lane found, verified against its
own MANIFEST and served as the `discovery` tier. Every project answer carries a tier derived
here from the canonical layer: `recommended` (a recommended relationship), `scored` (in the
canonical layer, scored, not recommended) or `discovery` (discovered, not scored). Discovery
projects are searchable, inspectable and comparable; they are never recommendations, and the
recommendation tools answer exactly as before unless a caller asks for a separate, labelled
`discovery_candidates` list. Nothing else in the repository is read: not the internal
discover-lane build, staging, live snapshots, history or intake. Hybrid retrieval applies the
KEI-844 semantic model (tfidf-sublinear/1, graph_retrieval.py) and graph traversal (SaaS
-REPLACES- project edges, shared categories) to the published layer, and, for search, to the
full corpus with its host descriptions and topics.

Standard library only, read-only, no network, no wall clock in any answer except
`freshness` in source_dataset_info (pin it with --now). The layer is verified before it
is served (every MANIFEST file hash, the dataset_version derived from them, and the same
dataset_version on every api/v1 file); a layer that fails verification is never served.
When the daily cycle replaces the files, the next request verifies and switches to the
new snapshot; if the new files do not verify, the previous snapshot keeps serving and
says so.

stdout carries protocol messages only. Diagnostics go to stderr. Queries are not logged.
Usage telemetry is aggregate counters only (tool, outcome, latency bucket, per UTC day);
no arguments, results, client identity or addresses are kept (Telemetry below).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

SERVER_NAME = "the-source"
SERVER_VERSION = "1.2.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first
API_VERSION = "v1"
CANONICAL_SCHEMA = "the-source.canonical/1"
LICENCE = "CC BY 4.0"
LICENCE_URL = "https://creativecommons.org/licenses/by/4.0/"
ATTRIBUTION = "The Source (github.com/keithdealwis-ui/the-source), by Keith de Alwis"
CANONICAL_FILES = ("NOTICE.md", "oss_projects.jsonl", "relationships.jsonl", "saas_products.jsonl")
STALE_AFTER_DAYS = 14  # docs/READ-API.md: show a staleness hint after about 14 days
MAX_LIMIT = 50
MAX_TEXT = 300
MAX_COMPARE = 5
SIGNAL_STALE_AFTER_HOURS = 48  # the daily cycle runs once a day; two missed runs is stale
SIGNALS = {"momentum": ("the-source.momentum/1", "docs/MOMENTUM.md"),
           "radar": ("the-source.radar/1", "docs/RADAR.md")}
MOMENTUM_LABELS = ("surging", "rising", "flat", "declining")
ACCELERATION_LABELS = ("accelerating", "steady", "decelerating")
LICENCE_FAMILIES = {  # SPDX id -> family; anything unlisted is "unknown" and never matches a family filter
    **dict.fromkeys(("MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense", "Zlib"),
                    "permissive"),
    **dict.fromkeys(("MPL-2.0", "LGPL-2.1", "LGPL-3.0", "EPL-1.0", "EPL-2.0"), "weak_copyleft"),
    **dict.fromkeys(("GPL-2.0", "GPL-3.0", "AGPL-3.0", "OSL-3.0", "EUPL-1.2"), "copyleft"),
}
# What the published layer cannot judge. A requirement like these is reported as not evaluated, never guessed.
NOT_EVALUABLE = ("API quality or coverage", "fit for a given team or company size", "SSO, compliance or support "
                 "terms", "hosting cost", "user experience")

# KEI-844's semantic model (source_pipeline/graph_retrieval.py, MODEL tfidf-sublinear/1), restated here because
# that module imports PyYAML and this one is standard library only. tests/test_mcp_server.py asserts the two
# tokenisers and the model id are identical.
SEMANTIC_MODEL = "tfidf-sublinear/1"
_SEM_WORD = re.compile(r"[a-z0-9][a-z0-9+#.]*[a-z0-9+#]|[a-z0-9]")
_SEM_STOP = set("""a an and are as at be but by for from has have in into is it its of on or our that the this
to was were will with you your we can not all any via using use used based built make makes more most
new one open source simple fast easy tool tools project projects app apps application""".split())
HYBRID_WEIGHTS = {"lexical": 0.45, "semantic": 0.35, "graph": 0.20}
CORPUS_SCHEMA = "the-source.corpus/1"  # KEI-912, docs/CORPUS.md
CORPUS_FILES = ("live.jsonl", "projects.jsonl")
CORPUS_STALE_AFTER_DAYS = 10  # used only when the MANIFEST does not state its own
TIERS = ("recommended", "scored", "discovery")
DISCOVERY_LABEL = "discovered, not scored \u2014 no Replacement Fit or Project Health"
TIER_MEANING = {
    "recommended": "in the canonical layer with at least one recommended relationship",
    "scored": "in the canonical layer with Replacement Fit / Project Health, but no recommended relationship",
    "discovery": DISCOVERY_LABEL + "; found by the Discover lane and published in data/corpus",
}

INSTRUCTIONS = (
    "The Source: evidence-backed open-source alternatives to SaaS products. Answers come from "
    "The Source's published read layer (CC BY 4.0; attribute \"" + ATTRIBUTION + "\"). "
    "Use source_get_alternatives for 'what can replace X', source_detect_saas for a URL, "
    "source_search to find products or projects, source_get_project and "
    "source_get_relationship for the evidence behind one project or pairing, source_compare_projects to "
    "compare projects, source_recommend for requirements (a need in words and/or hard constraints), "
    "source_emerging_projects and source_get_momentum for Repository and Community Momentum. Only "
    "relationships marked recommended are recommendations; Replacement Fit notes are "
    "machine-derived, not editorial. Every project carries a tier: recommended, scored, or discovery "
    "(discovered, not scored: never present a discovery project as a recommendation). "
    "Show live_checked_at or observed_as_of with any live figure."
)


class LayerError(Exception):
    """The read layer is missing, inconsistent or fails verification."""


class ToolError(Exception):
    """A tool call that cannot be answered as asked (reported with isError)."""


# --------------------------------------------------------------------------- layer


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _jsonl(path: Path) -> list:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _norm(text) -> str:
    """Lowercase, accents folded, every run of non-alphanumerics one space."""
    t = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", t))


def _tokens(text) -> list:
    out = []
    for t in _norm(text).split():
        out.append(t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t)
    return out


def _repo_key(value: str) -> str:
    """https://GitHub.com/Owner/Repo.git/ -> github.com/owner/repo"""
    v = str(value or "").strip().lower()
    v = re.sub(r"^[a-z][a-z0-9+.-]*://", "", v)
    v = v.split("?")[0].split("#")[0].rstrip("/")
    v = v[4:] if v.startswith("www.") else v
    return v[:-4] if v.endswith(".git") else v


def _sem_tokens(text) -> list:
    """graph_retrieval.tokens, verbatim."""
    return [t for t in _SEM_WORD.findall((text or "").lower()) if t not in _SEM_STOP and len(t) > 1]


def _sublinear_vectors(raw: dict):
    """graph_retrieval.RetrievalIndex.from_entities weighting: idf log((1+n)/(1+df))+1, tf 1+log(tf), L2."""
    df = {}
    for terms in raw.values():
        for t in terms:
            df[t] = df.get(t, 0) + 1
    n = len(raw) or 1
    idf = {t: math.log((1 + n) / (1 + d)) + 1 for t, d in df.items()}
    vecs = {}
    for key, terms in raw.items():
        w = {t: (1 + math.log(tf)) * idf[t] for t, tf in terms.items()}
        norm = math.sqrt(sum(v * v for v in w.values())) or 1.0
        vecs[key] = {t: v / norm for t, v in w.items()}
    return vecs, idf


def _counts(*texts) -> dict:
    c = {}
    for text in texts:
        for t in _sem_tokens(text):
            t = sys.intern(t)  # one copy of each term across every document (KEI-912: ~9.4k documents)
            c[t] = c.get(t, 0) + 1
    return c


def _interned_pairs(pairs):
    """object_pairs_hook: share keys and short values (ids, topics, SPDX ids, labels) across ~9.4k rows."""
    return {sys.intern(k): sys.intern(v) if isinstance(v, str) and len(v) <= 64 else v for k, v in pairs}


_INTERNING = json.JSONDecoder(object_pairs_hook=_interned_pairs)


def _jsonl_interned(path: Path) -> list:
    with open(path, encoding="utf-8") as fh:
        return [_INTERNING.decode(line) for line in fh if line.strip()]


def _load_signal(root: Path, name: str, canonical_version: str) -> dict:
    """One published signal layer, verified against its own MANIFEST, or a reason it is unavailable."""
    schema, doc = SIGNALS[name]
    base = root / "data" / name
    state = {"layer": f"data/{name}", "methodology": doc, "status": "unavailable", "reason": None,
             "as_of": None, "manifest_sha256": None, "rows": None}
    try:
        raw = (base / "MANIFEST.json").read_bytes()
        m = json.loads(raw)
    except (OSError, ValueError) as exc:
        state["reason"] = f"data/{name}/MANIFEST.json unreadable: {type(exc).__name__}"
        return state
    state["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    state["as_of"] = m.get("as_of")
    if m.get("schema") != schema:
        state["reason"] = f"schema is {m.get('schema')!r}, want {schema!r}"
        return state
    files = m.get("files") or {}
    for fname, digest in sorted(files.items()):
        if "/" in fname or fname.startswith("."):
            state["reason"] = f"MANIFEST names an unexpected path {fname!r}"
            return state
        path = base / fname
        if not path.is_file() or _sha256(path) != digest:
            state["reason"] = f"data/{name}/{fname} is missing or does not match its MANIFEST sha256"
            return state
    try:
        if name == "momentum":
            if "projects.jsonl" not in files:
                raise ValueError("MANIFEST does not list projects.jsonl")
            rows = _jsonl(base / "projects.jsonl")
            state["windows"] = m.get("windows")
            state["coverage"] = {w: {"status": c.get("status"), "measurable_from": c.get("measurable_from"),
                                     "acceleration_measurable_from": (c.get("acceleration") or {}).get("measurable_from"),
                                     "acceleration_status": (c.get("acceleration") or {}).get("status")}
                                 for w, c in sorted((m.get("coverage") or {}).items())}
            built_from = (m.get("history") or {}).get("dataset_version")
            state["built_from_dataset_version"] = built_from
            state["matches_canonical"] = built_from == canonical_version
            state["methodology_version"] = (m.get("methodology") or {}).get("version")
        else:
            if "radar.jsonl" not in files:
                raise ValueError("MANIFEST does not list radar.jsonl")
            rows = _jsonl(base / "radar.jsonl")
            state["window_days"] = m.get("window_days")
            state["sources"] = sorted(((m.get("collection") or {}).get("sources") or {}))
            state["source_status"] = {s: v.get("status") for s, v in
                                      sorted(((m.get("collection") or {}).get("sources") or {}).items())}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        state["reason"] = f"data/{name} unreadable: {exc}"
        return state
    state.update(status="ok", rows=rows)
    return state


def _load_corpus(root: Path) -> dict:
    """The public discovery corpus (KEI-912), verified against its own MANIFEST, or a reason it is unavailable."""
    base = root / "data" / "corpus"
    state = {"layer": "data/corpus", "methodology": "docs/CORPUS.md", "status": "unavailable", "reason": None,
             "as_of": None, "manifest_sha256": None, "rows": None}
    try:
        raw = (base / "MANIFEST.json").read_bytes()
        m = json.loads(raw)
    except (OSError, ValueError) as exc:
        state["reason"] = f"data/corpus/MANIFEST.json unreadable: {type(exc).__name__}"
        return state
    state["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    state["as_of"] = m.get("as_of")
    if m.get("schema") != CORPUS_SCHEMA:
        state["reason"] = f"schema is {m.get('schema')!r}, want {CORPUS_SCHEMA!r}"
        return state
    files = m.get("files") or {}
    if sorted(files) != sorted(CORPUS_FILES):
        state["reason"] = f"MANIFEST lists {sorted(files)}, want {sorted(CORPUS_FILES)}"
        return state
    for fname in CORPUS_FILES:
        path = base / fname
        if not path.is_file() or _sha256(path) != files[fname]:
            state["reason"] = f"data/corpus/{fname} is missing or does not match its MANIFEST sha256"
            return state
    if m.get("corpus_version") != hashlib.sha256(_dumps(files).encode()).hexdigest()[:16]:
        state["reason"] = "corpus_version is not the hash of the files"
        return state
    try:
        projects, live = _jsonl_interned(base / "projects.jsonl"), _jsonl_interned(base / "live.jsonl")
        if [p["id"] for p in projects] != [r["id"] for r in live]:
            raise ValueError("live.jsonl and projects.jsonl name different projects")
        if (m.get("counts") or {}).get("projects") != len(projects):
            raise ValueError(f"MANIFEST counts.projects is {(m.get('counts') or {}).get('projects')}, "
                             f"the files hold {len(projects)}")
        rows = [{**p, "live": r} for p, r in zip(projects, live)]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        state["reason"] = f"data/corpus unreadable: {exc}"
        return state
    refresh = m.get("refresh") or {}
    state.update(status="ok", rows=rows, corpus_version=m.get("corpus_version"), counts=m.get("counts"),
                 membership_as_of=(m.get("membership") or {}).get("as_of"),
                 live_observed_as_of=m.get("live_observed_as_of"),
                 last_refresh=(refresh.get("last") or {}).get("at"), cadence=refresh.get("cadence"),
                 stale_after_days=refresh.get("stale_after_days") or CORPUS_STALE_AFTER_DAYS,
                 publication=m.get("publication"))
    return state


def _bm25(docs):
    """BM25F-lite index over (kind, key, {field: (values, weight)}) documents: (docs, avg_len, idf)."""
    out, df, shared = [], {}, {}
    for kind, key, fields in docs:
        tf, matched_in, length = {}, {}, 0
        for field, (values, weight) in fields.items():
            for value in values:
                toks = _tokens(value)
                length += len(toks)
                for t in toks:
                    t = sys.intern(t)
                    tf[t] = tf.get(t, 0.0) + weight
                    matched_in.setdefault(t, set()).add(field)
        # One frozenset per distinct field combination, shared by every token and document that has it.
        matched_in = {t: shared.setdefault(frozenset(f), frozenset(f)) for t, f in matched_in.items()}
        exact = {_norm(v) for field in ("name", "aliases") if field in fields for v in fields[field][0]}
        exact |= {_norm(key)}
        out.append({"kind": kind, "key": key, "tf": tf, "fields": matched_in, "len": max(length, 1),
                    "exact": exact})
        for t in tf:
            df[t] = df.get(t, 0) + 1
    n = len(out)
    avg_len = sum(d["len"] for d in out) / max(n, 1)
    idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}
    return out, avg_len, idf


class Layer:
    """One verified snapshot of the read layer, indexed for retrieval."""

    def __init__(self, root: Path):
        self.root = root
        canon, api = root / "data" / "canonical", root / "api" / "v1"
        try:
            manifest_bytes = (canon / "MANIFEST.json").read_bytes()
            self.manifest = json.loads(manifest_bytes)
        except (OSError, ValueError) as exc:
            raise LayerError(f"data/canonical/MANIFEST.json unreadable: {exc}") from exc
        self.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        m = self.manifest
        if m.get("canonical_schema") != CANONICAL_SCHEMA:
            raise LayerError(f"canonical_schema is {m.get('canonical_schema')!r}, want {CANONICAL_SCHEMA!r}")
        files = m.get("files") or {}
        if sorted(files) != sorted(CANONICAL_FILES):
            raise LayerError(f"MANIFEST lists {sorted(files)}, want {sorted(CANONICAL_FILES)}")
        for name in CANONICAL_FILES:
            path = canon / name
            if not path.is_file():
                raise LayerError(f"data/canonical/{name} is missing")
            if _sha256(path) != files[name]:
                raise LayerError(f"data/canonical/{name} does not match its MANIFEST sha256")
        version = hashlib.sha256(_dumps(files).encode()).hexdigest()[:16]
        if version != m.get("dataset_version"):
            raise LayerError(f"dataset_version {m.get('dataset_version')!r} is not the hash of the files ({version})")
        self.dataset_version = version

        try:
            self.index = json.loads((api / "index.json").read_text(encoding="utf-8"))
            self.domains = json.loads((api / "domains.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LayerError(f"api/v1 index or domains unreadable: {exc}") from exc
        for label, doc in (("index.json", self.index), ("domains.json", self.domains)):
            if doc.get("dataset_version") != version or doc.get("api_version") != API_VERSION:
                raise LayerError(f"api/v1/{label} is dataset {doc.get('dataset_version')!r}, manifest is {version!r}")

        self.saas = {r["saas_id"]: r for r in _jsonl(canon / "saas_products.jsonl")}
        self.projects = {r["oss_id"]: r for r in _jsonl(canon / "oss_projects.jsonl")}
        self.relationships = _jsonl(canon / "relationships.jsonl")
        self.notice = (canon / "NOTICE.md").read_text(encoding="utf-8")

        # api/v1/saas/<id>.json, reached only through the index (never through caller input)
        self.surface = {}
        for row in self.index.get("supported_saas", []):
            sid = row["saas_id"]
            if row.get("path") != f"saas/{sid}.json" or sid not in self.saas:
                raise LayerError(f"index row for {sid!r} does not name its own file or a catalogued product")
            try:
                doc = json.loads((api / row["path"]).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise LayerError(f"api/v1/{row['path']} unreadable: {exc}") from exc
            if doc.get("dataset_version") != version or doc.get("saas", {}).get("saas_id") != sid:
                raise LayerError(f"api/v1/{row['path']} is not dataset {version} or not {sid}")
            self.surface[sid] = doc

        counts = m.get("counts", {})
        got = {"saas_products": len(self.saas), "oss_projects": len(self.projects),
               "relationships": len(self.relationships), "supported_saas": len(self.surface)}
        for k, v in got.items():
            if counts.get(k) != v:
                raise LayerError(f"MANIFEST counts.{k} is {counts.get(k)}, the files hold {v}")
        for r in self.relationships:
            if r["saas_id"] not in self.saas or r["oss_id"] not in self.projects:
                raise LayerError(f"relationship {r.get('edge_id')} names a product or project not in the layer")

        self.rel_by_saas, self.rel_by_project, self.rel_by_pair = {}, {}, {}
        for r in self.relationships:
            self.rel_by_saas.setdefault(r["saas_id"], []).append(r)
            self.rel_by_project.setdefault(r["oss_id"], []).append(r)
            self.rel_by_pair[(r["saas_id"], r["oss_id"])] = r
        self._build_names()
        self._build_search()
        self._build_semantic()
        self._load_signals()
        self._load_corpus()

    # -- signal layers (Repository Momentum, Community Radar)

    def _load_signals(self):
        self.signals = {name: _load_signal(self.root, name, self.dataset_version) for name in SIGNALS}
        self.momentum, self.radar = {}, {}
        mom = self.signals["momentum"]
        rows = mom.pop("rows") or []
        # A renamed repository has a row under its old key and one under its canonical id. Each row is indexed by
        # its own key first, so a canonical-id alias never shadows a real row; one row per project is listed,
        # preferring the row whose key is the canonical id.
        for row in rows:
            if row.get("key"):
                self.momentum.setdefault(_repo_key(row["key"]), row)
        for row in rows:
            if row.get("canonical_id"):
                self.momentum.setdefault(_repo_key(row["canonical_id"]), row)
        best = {}
        for row in rows:
            ident = _repo_key(row.get("canonical_id") or row.get("key") or "")
            current = best.get(ident)
            if current is None or (_repo_key(row.get("key") or "") == ident
                                   and _repo_key(current.get("key") or "") != ident):
                best[ident] = row
        self.momentum_projects = sorted(best.values(), key=lambda r: r.get("key") or "")
        rad = self.signals["radar"]
        for row in rad.pop("rows") or []:
            for key in [row.get("key"), row.get("oss_id"), *(row.get("link_keys") or [])]:
                if key:
                    self.radar.setdefault(_repo_key(key), row)
        self.radar_ranked = sorted({id(r): r for r in self.radar.values()}.values(),
                                   key=lambda r: (r.get("rank") or 10 ** 9, r.get("key") or ""))

    def signal_manifest_shas(self):
        return tuple(self.signals[n]["manifest_sha256"] for n in sorted(SIGNALS)) + (self.corpus_state["manifest_sha256"],)

    # -- KEI-912 discovery corpus and tiers

    def _load_corpus(self):
        self.corpus_state = _load_corpus(self.root)
        rows = self.corpus_state.pop("rows") or []
        canon_keys = {_repo_key(pid): pid for pid in self.projects}
        for pid, p in self.projects.items():
            for key in (p.get("repo_url"), p.get("github_url")):
                if key:
                    canon_keys.setdefault(_repo_key(key), pid)
        self.corpus, self.corpus_keys, self.corpus_names = {}, {}, {}
        self.canonical_corpus = {}  # canonical oss_id -> its corpus row, where the corpus holds it too
        for row in rows:
            keys = [row["id"], *row["aliases"]]
            pid = next((canon_keys[_repo_key(k)] for k in keys if _repo_key(k) in canon_keys), None)
            if pid:
                self.canonical_corpus.setdefault(pid, row)
                for k in keys:
                    self.corpus_keys.setdefault(_repo_key(k), pid)
                continue
            self.corpus[row["id"]] = row
            for k in keys:
                self.corpus_keys.setdefault(_repo_key(k), row["id"])
            self.corpus_names.setdefault(_norm(row["name"]), set()).add(row["id"])
        self.full = _FullIndex(self) if self.corpus_state["status"] == "ok" else None
        counts = {t: 0 for t in TIERS}
        for pid in self.projects:
            counts[self.tier_of(pid)] += 1
        counts["discovery"] = len(self.corpus)
        self.tier_counts = counts

    def tier_of(self, key: str):
        """recommended | scored (canonical layer) | discovery (corpus only) | None (unknown)."""
        if key in self.projects:
            return "recommended" if any(r["recommended"] for r in self.rel_by_project.get(key, [])) else "scored"
        if key in self.corpus:
            return "discovery"
        return None

    def resolve_any(self, value: str):
        """A project in the canonical layer or the discovery corpus -> (key, tier), canonical first."""
        try:
            pid = self.resolve_project(value)
            return pid, self.tier_of(pid)
        except ToolError as exc:
            v = str(value or "").strip()
            if "more than one project" in str(exc) or self.full is None:
                raise
        cid = self.corpus_keys.get(_repo_key(v))
        if cid is None:
            hit = self.corpus_names.get(_norm(v))
            if hit and len(hit) == 1:
                cid = next(iter(hit))
            elif hit:
                shown = sorted(hit)
                raise ToolError(f"{v!r} names more than one discovery project: {shown[:10]}"
                                + (f" and {len(shown) - 10} more" if len(shown) > 10 else "")
                                + "; pass the repository URL or id")
        if cid is None:
            raise ToolError(f"{v!r} is not a project The Source holds (canonical layer or discovery corpus); "
                            "try source_search")
        return cid, self.tier_of(cid)

    # -- KEI-844 semantic model over the published layer

    def _build_semantic(self):
        raw = {}
        for sid, s in sorted(self.saas.items()):
            raw[("saas", sid)] = _counts(s["name"], " ".join(s.get("aliases", [])), s["category"], s["category"],
                                         s["domain"].split(".")[0])
        for pid, p in sorted(self.projects.items()):
            recs = [r for r in self.rel_by_project.get(pid, []) if r["recommended"]]
            owner_repo = pid.split("/", 1)[1] if "/" in pid else pid
            replaced = [self.saas[r["saas_id"]] for r in recs]
            raw[("project", pid)] = _counts(
                p["name"], owner_repo.replace("/", " ").replace("-", " ").replace("_", " "),
                " ".join(s["name"] for s in replaced), " ".join(a for s in replaced for a in s.get("aliases", [])),
                " ".join(s["category"] for s in replaced), " ".join(s["category"] for s in replaced))
        self.vectors, self.sem_idf = _sublinear_vectors(raw)

    def semantic(self, query: str, kind: str):
        """Cosine between the query and every document under the KEI-844 weighting; (cos, kind, key, shared)."""
        q = {}
        for t in _sem_tokens(query):
            if t in self.sem_idf:
                q[t] = q.get(t, 0) + 1
        if not q:
            return []
        w = {t: (1 + math.log(tf)) * self.sem_idf[t] for t, tf in q.items()}
        norm = math.sqrt(sum(v * v for v in w.values())) or 1.0
        out = []
        for (k, key), vec in self.vectors.items():
            if kind != "any" and k != kind:
                continue
            shared = sorted(t for t in w if t in vec)
            if shared:
                cos = sum(w[t] / norm * vec[t] for t in shared)
                out.append((round(cos, 4), k, key, shared))
        out.sort(key=lambda h: (-h[0], h[1], h[2]))
        return out

    def hybrid(self, query: str, kind: str):
        """Lexical BM25 + KEI-844 semantic cosine + graph (REPLACES edges from matched products to projects,
        weighted by Replacement Fit). Every hit keeps its three components; nothing is ranked by a number the
        result cannot show."""
        # Lexical scoring sees the query without KEI-844 stopwords ("open source", "tool", "app" ...), which
        # otherwise match category names such as "Source control" and pull in unrelated products.
        content = " ".join(_sem_tokens(query)) or query
        lex = {(k, key): (s, why, exact) for s, k, key, why, exact in self.search(content, "any")}
        if _norm(query) != _norm(content):
            for s, k, key, why, exact in self.search(query, "any"):
                if exact:
                    lex[(k, key)] = (lex.get((k, key), (0,))[0] + 10.0, why, True)
        top_lex = max((v[0] for v in lex.values()), default=0.0) or 1.0
        sem = {(k, key): (c, shared) for c, k, key, shared in self.semantic(query, "any")}
        graph, via = {}, {}
        seeds = sorted(((max(lex.get(("saas", sid), (0,))[0] / top_lex, sem.get(("saas", sid), (0,))[0]), sid)
                        for sid in self.saas), reverse=True)
        top_seed = seeds[0][0] if seeds else 0
        seeds = [(strength, sid) for strength, sid in seeds[:3] if strength >= 0.3 and strength >= 0.8 * top_seed]
        for strength, sid in seeds:
            for r in self.rel_by_saas.get(sid, []):
                if not r["recommended"]:
                    continue
                g = round(strength * (r["replacement_fit"]["score"] or 0) / 100, 4)
                if g > graph.get(("project", r["oss_id"]), 0):
                    graph[("project", r["oss_id"])] = g
                    via[("project", r["oss_id"])] = {"saas_id": sid, "edge_id": r["edge_id"], "relation": "REPLACES",
                                                     "replacement_fit": r["replacement_fit"]["score"]}
        hits = []
        for key in set(lex) | set(sem) | set(graph):
            if kind != "any" and key[0] != kind:
                continue
            lx = round(lex[key][0] / top_lex, 4) if key in lex else 0.0
            sm = sem[key][0] if key in sem else 0.0
            gr = graph.get(key, 0.0)
            score = round(HYBRID_WEIGHTS["lexical"] * lx + HYBRID_WEIGHTS["semantic"] * sm
                          + HYBRID_WEIGHTS["graph"] * gr, 4)
            hits.append({"kind": key[0], "key": key[1], "score": score,
                         "components": {"lexical": lx, "semantic": sm, "graph": gr},
                         "matched": lex[key][1] if key in lex else {},
                         "semantic_terms": sem[key][1] if key in sem else [],
                         "graph_path": via.get(key), "exact": bool(lex.get(key, (0, 0, False))[2])})
        hits.sort(key=lambda h: (-h["score"], h["kind"], h["key"]))
        return hits, [{"saas_id": sid, "strength": round(st, 4)} for st, sid in seeds]

    # -- name resolution

    def _build_names(self):
        self.saas_names = {}
        for sid, s in sorted(self.saas.items()):
            for key in [sid, s["name"], *s.get("aliases", [])]:
                self.saas_names.setdefault(_norm(key), set()).add(sid)
        self.repo_keys = {}
        self.project_names = {}
        for pid, p in sorted(self.projects.items()):
            for key in (pid, p.get("repo_url"), p.get("github_url")):
                if key:
                    self.repo_keys[_repo_key(key)] = pid
            self.project_names.setdefault(_norm(p["name"]), set()).add(pid)

    # -- retrieval index: BM25 over weighted fields (BM25F-lite), deterministic

    def _build_search(self):
        docs = []
        for sid, s in sorted(self.saas.items()):
            docs.append(("saas", sid, {
                "name": ([s["name"]], 3.0), "aliases": (s.get("aliases", []) + [sid], 2.5),
                "category": ([s["category"]], 1.5), "domain": ([s["domain"]], 1.0)}))
        for pid, p in sorted(self.projects.items()):
            recs = [r for r in self.rel_by_project.get(pid, []) if r["recommended"]]
            owner_repo = pid.split("/", 1)[1] if "/" in pid else pid
            docs.append(("project", pid, {
                "name": ([p["name"]], 3.0), "repository": ([owner_repo], 2.0),
                "replaces": ([self.saas[r["saas_id"]]["name"] for r in recs]
                             + [a for r in recs for a in self.saas[r["saas_id"]].get("aliases", [])], 1.5),
                "category": (sorted({self.saas[r["saas_id"]]["category"] for r in recs}), 1.0),
                "licence": ([p["licence"]["spdx"] or ""], 0.5)}))
        self.docs, self.avg_len, self.idf = _bm25(docs)

    def search(self, query: str, kind: str):
        q = _tokens(query)
        qn = _norm(query)
        k1, b = 1.2, 0.75
        hits = []
        for d in self.docs:
            if kind != "any" and d["kind"] != kind:
                continue
            score, why = 0.0, {}
            for t in dict.fromkeys(q):
                f = d["tf"].get(t)
                if not f:
                    continue
                norm_tf = f * (k1 + 1) / (f + k1 * (1 - b + b * d["len"] / self.avg_len))
                score += self.idf[t] * norm_tf
                for field in sorted(d["fields"][t]):
                    why.setdefault(field, []).append(t)
            exact = bool(qn) and qn in d["exact"]
            if exact:
                score += 10.0
            if score > 0:
                hits.append((round(score, 4), d["kind"], d["key"], why, exact))
        hits.sort(key=lambda h: (-h[0], h[1], h[2]))
        return hits

    # -- lookups

    def resolve_saas(self, value: str):
        """saas_id, name, alias, domain or URL -> (saas_id, how) or ToolError."""
        v = str(value or "").strip()
        if not v:
            raise ToolError("saas is empty")
        hit = self.saas_names.get(_norm(v))
        if hit and len(hit) == 1:
            return next(iter(hit)), "name"
        if hit:
            raise ToolError(f"{v!r} names more than one product: {sorted(hit)}")
        if "/" in v or "." in v:
            sid = self.detect(v)
            if sid:
                return sid, "url"
            host = _host_path(v)[0]
            by_domain = sorted(s for s, r in self.saas.items() if r["domain"] == host)
            if len(by_domain) == 1:
                return by_domain[0], "domain"
            if by_domain:
                raise ToolError(f"{host!r} is the domain of more than one product: {by_domain}")
        raise ToolError(f"{v!r} is not a product in The Source catalogue; try source_search")

    def resolve_project(self, value: str):
        v = str(value or "").strip()
        if not v:
            raise ToolError("project is empty")
        pid = self.repo_keys.get(_repo_key(v))
        if pid:
            return pid
        hit = self.project_names.get(_norm(v))
        if hit and len(hit) == 1:
            return next(iter(hit))
        if hit:
            raise ToolError(f"{v!r} names more than one project: {sorted(hit)}; pass the oss_id")
        raise ToolError(f"{v!r} is not a project in The Source canonical layer; try source_search")

    def detect(self, url: str):
        host, path = _host_path(url)
        if host is None:
            return None
        first = None
        for rule in self.domains.get("rules", []):
            if "host" in rule:
                if host != rule["host"]:
                    continue
            elif "host_suffix" in rule:
                if not host.endswith(rule["host_suffix"]):
                    continue
            else:
                continue
            if "path_prefix" in rule:
                if path.startswith(rule["path_prefix"]):
                    return rule["saas_id"]
                continue
            if first is None:
                first = rule["saas_id"]
        return first


class _FullIndex:
    """Retrieval over the full corpus (KEI-912): the SaaS catalogue, every canonical project (with the host
    description and topics where the corpus holds them) and every discovery project. Ranking is Layer's own
    BM25F-lite, KEI-844 semantic cosine and graph hybrid, unchanged; only the documents differ. Used by search,
    emerging and the opt-in discovery candidates, never by a recommendation."""

    search, semantic, hybrid = Layer.search, Layer.semantic, Layer.hybrid

    def __init__(self, layer):
        self.saas, self.rel_by_saas = layer.saas, layer.rel_by_saas
        docs, raw = [], {}
        for sid, s in sorted(layer.saas.items()):
            docs.append(("saas", sid, {
                "name": ([s["name"]], 3.0), "aliases": (s.get("aliases", []) + [sid], 2.5),
                "category": ([s["category"]], 1.5), "domain": ([s["domain"]], 1.0)}))
            raw[("saas", sid)] = _counts(s["name"], " ".join(s.get("aliases", [])), s["category"], s["category"],
                                         s["domain"].split(".")[0])
        for pid, p in sorted(layer.projects.items()):
            recs = [r for r in layer.rel_by_project.get(pid, []) if r["recommended"]]
            owner_repo = pid.split("/", 1)[1] if "/" in pid else pid
            replaced = [layer.saas[r["saas_id"]] for r in recs]
            c = layer.canonical_corpus.get(pid) or {}
            topics = [t.replace("-", " ") for t in c.get("topics") or []]
            docs.append(("project", pid, {
                "name": ([p["name"]], 3.0), "repository": ([owner_repo], 2.0),
                "replaces": ([s["name"] for s in replaced] + [a for s in replaced for a in s.get("aliases", [])], 1.5),
                "category": (sorted({s["category"] for s in replaced}), 1.0),
                "topics": (topics, 1.2), "description": ([c.get("description") or ""], 1.0),
                "licence": ([p["licence"]["spdx"] or ""], 0.5)}))
            raw[("project", pid)] = _counts(
                p["name"], owner_repo.replace("/", " ").replace("-", " ").replace("_", " "),
                " ".join(s["name"] for s in replaced), " ".join(a for s in replaced for a in s.get("aliases", [])),
                " ".join(s["category"] for s in replaced), " ".join(s["category"] for s in replaced),
                " ".join(topics), c.get("description") or "")
        for cid, c in sorted(layer.corpus.items()):
            owner_repo = c["full_name"]
            topics = [t.replace("-", " ") for t in c["topics"]]
            docs.append(("project", cid, {
                "name": ([c["name"]], 3.0), "repository": ([owner_repo], 2.0), "topics": (topics, 1.2),
                "description": ([c["description"] or ""], 1.0), "languages": (c["languages"], 0.5),
                "licence": ([c["licence"]["spdx"] or ""], 0.5)}))
            raw[("project", cid)] = _counts(c["name"], owner_repo.replace("/", " ").replace("-", " ").replace("_", " "),
                                            " ".join(topics), c["description"] or "")
        self.docs, self.avg_len, self.idf = _bm25(docs)
        self.vectors, self.sem_idf = _sublinear_vectors(raw)


def _host_path(url: str):
    """The READ-API.md detection inputs: lowercase host without one leading www., and path.
    Only http(s); a bare host gets https://. Never fetched."""
    u = str(url or "").strip()
    if "://" not in u:
        u = "https://" + u
    try:
        parts = urlsplit(u)
        host = (parts.hostname or "").rstrip(".")
    except ValueError:
        return None, ""
    if parts.scheme not in ("http", "https") or not host:
        return None, ""
    host = host[4:] if host.startswith("www.") else host
    return host, parts.path or "/"


# --------------------------------------------------------------------------- tools


def _rank_key(rec):
    fit = rec.get("replacement_fit") or {}
    health = rec.get("project_health") or {}
    return (-(fit.get("score") or 0), -(health.get("score") or 0), -(rec.get("stars") or 0), rec["oss_id"])


def _saas_view(layer, sid):
    s = layer.saas[sid]
    out = {k: s[k] for k in ("saas_id", "name", "domain", "category", "aliases", "supported")}
    doc = layer.surface.get(sid)
    if doc:
        out["compare_path"] = doc["saas"]["compare_path"]
    return out


def _project_view(p):
    return {
        "oss_id": p["oss_id"], "name": p["name"], "repo_url": p["repo_url"], "github_url": p["github_url"],
        "host": p["host"], "licence_spdx": p["licence"]["spdx"],
        "stars": p["live"]["stars"], "forks": p["live"]["forks"], "archived": p["live"]["archived"],
        "latest_release": p["live"]["latest_release"], "pushed_at": p["live"]["pushed_at"],
        "maintenance": p["maintenance"], "project_health": p["project_health"],
        "live_checked_at": p["live"]["checked_at"],
    }


def _rel_summary(layer, r):
    fit = r["replacement_fit"] or {}
    return {
        "edge_id": r["edge_id"], "saas_id": r["saas_id"], "saas_name": layer.saas[r["saas_id"]]["name"],
        "oss_id": r["oss_id"], "recommended": r["recommended"],
        "replacement_fit": {k: fit.get(k) for k in ("score", "confidence", "credible_alternative", "publish",
                                                     "core_gate", "score_version")},
        "sources": r["sources"],
    }


def _canonical_rec(layer, r):
    """A recommendation row for a catalogued product that has no api/v1 page, built from the
    canonical layer only. It lacks self_hosting and note (those come from scoring detail that
    is not part of the read layer), and says so."""
    p = layer.projects[r["oss_id"]]
    fit = r["replacement_fit"]
    h = p["project_health"]
    return {
        "oss_id": p["oss_id"], "name": p["name"], "repo_url": p["repo_url"], "github_url": p["github_url"],
        "stars": p["live"]["stars"], "licence_spdx": p["licence"]["spdx"],
        "maintenance_status": p["maintenance"]["status"],
        "last_meaningful_activity_at": p["maintenance"]["last_meaningful_activity_at"],
        "latest_release": p["live"]["latest_release"],
        "replacement_fit": {k: fit[k] for k in ("score", "confidence", "score_version", "evaluated_at")},
        "project_health": None if h is None else {k: h[k] for k in ("score", "band", "trajectory", "confidence")},
        "sources": r["sources"], "live_checked_at": p["live"]["checked_at"],
    }


def _corpus_state(layer, server):
    """The discovery corpus's availability and freshness, for every answer that serves it."""
    s = dict(layer.corpus_state)
    for k in ("publication", "counts"):
        s.pop(k, None)
    age = None
    at = s.get("last_refresh") or s.get("as_of")
    if at:
        try:
            age = round((server.now() - datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
                        .total_seconds() / 86400, 2)
        except ValueError:
            pass
    s["age_days"] = age
    if s["status"] == "ok" and (age is None or age > s["stale_after_days"]):
        s["status"] = "stale"
        s["reason"] = (f"last refreshed {at}, more than {s['stale_after_days']} days ago; the scheduled corpus "
                       "refresh has not run")
    return s


def _licence_view(lic: dict, host_spdx):
    out = {"spdx": lic["spdx"], "status": lic["status"], "host_reports": host_spdx}
    if lic["status"] != "recognised":
        out["issue"] = lic["issue"]
    return out


def _discovery_view(layer, cid, full=False):
    """A discovery-tier project: facts only, labelled, never a recommendation."""
    c = layer.corpus[cid]
    live = c["live"]
    out = {"id": cid, "tier": "discovery", "label": DISCOVERY_LABEL, "name": c["name"], "full_name": c["full_name"],
           "repo_url": c["url"], "description": c["description"], "topics": c["topics"],
           "licence": _licence_view(c["licence"], live["licence_spdx"]),
           "stars": live["stars"], "archived": live["archived"], "pushed_at": live["pushed_at"],
           "maintenance_status": c["maintenance"]["status"], "observed_as_of": live["observed_as_of"]}
    if c["licence"]["status"] != "recognised":
        out["warning"] = (f"licence issue ({c['licence']['issue']['reason']}): not an open-source licence The Source "
                          "recognises; check the terms before any use")
    if not live["found"]:
        out["host_status"] = f"not found on the host at {live['observed_as_of']} (deleted, made private or moved)"
    if full:
        out.update(homepage=c["homepage"], languages=c["languages"], forks=live["forks"],
                   latest_release=live["latest_release"], aliases=c["aliases"], lanes=c["lanes"],
                   maintenance={**c["maintenance"], "evaluated_as_of": layer.corpus_state.get("membership_as_of")},
                   provenance=c["provenance"],
                   provenance_note="where the Discover lane found it: a licence-cleared curated list (pinned commit "
                                   "and line), a GitHub topic or search, an owner's other repositories, a README link, "
                                   "the Replace lane, or a maintainer seed. No list text is republished.")
    return out


def _corpus_extra(layer, pid):
    """What the corpus adds to a canonical project: host description, topics, provenance."""
    c = layer.canonical_corpus.get(pid)
    if not c:
        return None
    return {"description": c["description"], "topics": c["topics"], "observed_as_of": c["live"]["observed_as_of"]}


def _limit(args, default):
    v = args.get("limit", default)
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= MAX_LIMIT:
        raise ToolError(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return v


def tool_dataset_info(layer, args, server):
    m = layer.manifest
    newest = (m.get("live_checked_at") or {}).get("newest")
    oldest = (m.get("live_checked_at") or {}).get("oldest")
    now = server.now()
    age = None
    if oldest:
        age = round((now - datetime.strptime(oldest, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
                    .total_seconds() / 86400, 2)
    return {
        "dataset_version": layer.dataset_version, "canonical_schema": m["canonical_schema"],
        "api_version": API_VERSION, "data_as_of": m.get("data_as_of"),
        "live_checked_at": m.get("live_checked_at"), "counts": m.get("counts"),
        "export_guard": m.get("export_guard"), "sources": m.get("sources"),
        "freshness": {"now": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "oldest_live_check_age_days": age,
                      "stale": age is not None and age > STALE_AFTER_DAYS, "stale_after_days": STALE_AFTER_DAYS,
                      "newest_live_check": newest},
        "snapshot": server.snapshot_status(),
        "signals": {n: _signal_state(layer, server, n) for n in SIGNALS},
        "corpus": {**_corpus_state(layer, server), "counts": layer.corpus_state.get("counts"),
                   "publication": layer.corpus_state.get("publication"),
                   "projects_served": len(layer.corpus) + len(layer.canonical_corpus) if layer.full else 0,
                   "joined_to_canonical": len(layer.canonical_corpus)},
        "tiers": {"counts": layer.tier_counts, "meaning": TIER_MEANING,
                  "rule": "recommendation tools return recommended-tier projects only; search, project, compare, "
                          "emerging and momentum serve every tier and label each row"},
        "retrieval": {"lexical": "BM25F-lite", "semantic_model": SEMANTIC_MODEL,
                      "semantic_model_origin": "KEI-844 source_pipeline/graph_retrieval.py",
                      "graph": "SaaS -REPLACES-> project edges from data/canonical/relationships.jsonl",
                      "hybrid_weights": HYBRID_WEIGHTS},
        "serves": ["data/canonical/" + f for f in ("MANIFEST.json",) + CANONICAL_FILES]
                  + ["api/v1/index.json", "api/v1/domains.json", "api/v1/saas/<saas_id>.json",
                     "data/momentum/MANIFEST.json", "data/momentum/projects.jsonl",
                     "data/radar/MANIFEST.json", "data/radar/radar.jsonl",
                     "data/corpus/MANIFEST.json", "data/corpus/projects.jsonl", "data/corpus/live.jsonl"],
        "does_not_serve": "the internal Discover lane build (data/discover), staging, live snapshots, history, "
                          "intake and internal research data; relationships withheld by the export guard. Nothing "
                          "private to AIQ.",
    }


def tool_list_supported(layer, args, server):
    rows = []
    for row in layer.index["supported_saas"]:
        s = layer.saas[row["saas_id"]]
        rows.append({**row, "category": s["category"], "domain": s["domain"]})
    return {"supported_saas": rows, "count": len(rows),
            "catalogue_products": len(layer.saas),
            "note": "Supported products have a published api/v1 page. Other catalogued products can still have "
                    "recommended relationships in data/canonical; source_get_alternatives serves both and says which."}


def _include_discovery(args):
    v = args.get("include_discovery", False)
    if not isinstance(v, bool):
        raise ToolError("include_discovery must be true or false")
    return v


def _discovery_candidates(layer, server, query, limit, licences=None, family=None, status=None, min_stars=None,
                          max_stars=None):
    """Discovery-tier projects that match a query: a separate, labelled list, never recommendations. Only
    projects with a recognised licence that are live on their host and not archived are offered."""
    state = _corpus_state(layer, server)
    base = {"label": DISCOVERY_LABEL, "query": query, "corpus": state,
            "note": "Not recommendations. Discovered by the Discover lane and not scored: no Replacement Fit or "
                    "Project Health. Listed separately so they are never mistaken for the recommendations above."}
    if layer.full is None:
        return {**base, "status": "unavailable", "candidates": []}
    hits, _ = layer.full.hybrid(query, "project")
    rows, excluded = [], {}
    for h in hits:
        if h["key"] not in layer.corpus or h["score"] < 0.15:
            continue
        c = layer.corpus[h["key"]]
        live = c["live"]
        why_not = ("licence_issue" if c["licence"]["status"] != "recognised"
                   else "not_found_on_host" if not live["found"]
                   else "archived" if live["archived"]
                   else _licence_ok(c["licence"]["spdx"], licences, family)
                   or ("maintenance_status" if status and c["maintenance"]["status"] not in status else None)
                   or ("min_stars" if min_stars is not None and (live["stars"] or 0) < min_stars else None)
                   or ("max_stars" if max_stars is not None and (live["stars"] or 0) > max_stars else None))
        if why_not:
            excluded[why_not] = excluded.get(why_not, 0) + 1
            continue
        rows.append({**_discovery_view(layer, h["key"]),
                     "retrieval": {k: h[k] for k in ("score", "components", "semantic_terms")}})
    return {**base, "status": "ok" if rows else "no_matching_discovery_projects", "candidates": rows[:limit],
            "more_available": max(0, len(rows) - limit), "excluded": dict(sorted(excluded.items()))}


def tool_get_alternatives(layer, args, server):
    sid, how = layer.resolve_saas(args.get("saas"))
    limit = _limit(args, 5)
    include_discovery = _include_discovery(args)
    not_rec = [r for r in layer.rel_by_saas.get(sid, []) if not r["recommended"]]
    doc = layer.surface.get(sid)
    recs = sorted((_canonical_rec(layer, r) for r in layer.rel_by_saas.get(sid, []) if r["recommended"]),
                  key=_rank_key)
    for i, rec in enumerate(recs, 1):
        rec["rank"] = i
    if doc:
        page = doc["recommendations"]
        on_page = {r["oss_id"] for r in page}
        # Past the page: the same ranking over the canonical layer, flagged row by row.
        beyond = [{**r, "beyond_published_page": True} for r in recs if r["oss_id"] not in on_page]
        shown = (page + beyond)[:limit]
        out = {"saas": _saas_view(layer, sid), "resolved_by": how, "surface": f"api/v1/saas/{sid}.json",
               "status": doc["status"], "recommendations": shown,
               "more_available": max(0, len(page) + len(beyond) - limit),
               "last_checked": doc["last_checked"], "scoring": doc["scoring"], "provenance": doc["provenance"]}
        if len(shown) > len(page):
            out["caveat"] = (f"Rows after rank {len(page)} are beyond the published page: recommended "
                             "relationships from the canonical layer, without self-hosting evidence or a derived note.")
    else:
        shown = recs[:limit]
        checked = sorted(r["live_checked_at"] for r in shown)
        out = {"saas": _saas_view(layer, sid), "resolved_by": how, "surface": "data/canonical/relationships.jsonl",
               "status": ("ok" if len(shown) >= 3 else "insufficient_recommendations") if shown else "no_recommendations",
               "recommendations": shown, "more_available": max(0, len(recs) - limit),
               "last_checked": {"oldest": checked[0] if checked else None, "newest": checked[-1] if checked else None},
               "scoring": {"score_version": sorted({r["replacement_fit"]["score_version"] for r in shown}),
                           "methodology": "docs/METHODOLOGY.md"},
               "provenance": {"sources": sorted({x for r in shown for x in r["sources"]}),
                              "notice": "data/canonical/NOTICE.md"},
               "caveat": "This product has no published api/v1 page. These rows are its recommended relationships "
                         "in the canonical layer; they carry no self-hosting evidence or derived note."}
    out["not_recommended"] = {
        "count": len(not_rec),
        "why": "relationships held in the canonical layer whose Replacement Fit is not publishable or not a "
               "credible alternative; never presented as recommendations (see source_get_relationship)"}
    if include_discovery:
        s = layer.saas[sid]
        out["discovery_candidates"] = _discovery_candidates(layer, server, f"{s['name']} {s['category']}", limit)
    return out


def tool_detect_saas(layer, args, server):
    url = args.get("url")
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise ToolError("url must be a non-empty string of at most 2048 characters")
    host, path = _host_path(url)
    sid = layer.detect(url)
    return {"url_host": host, "url_path": path, "saas_id": sid,
            "saas": _saas_view(layer, sid) if sid else None,
            "match_semantics": layer.domains["match_semantics"],
            "note": "Matched locally against api/v1/domains.json. The URL is not fetched, stored or logged."}


def tool_search(layer, args, server):
    query = args.get("query")
    if not isinstance(query, str) or not _tokens(query) or len(query) > MAX_TEXT:
        raise ToolError(f"query must contain at least one letter or digit and be at most {MAX_TEXT} characters")
    kind = args.get("kind", "any")
    if kind not in ("any", "saas", "project"):
        raise ToolError("kind must be any, saas or project")
    limit = _limit(args, 10)
    lic = args.get("licence")
    status = args.get("maintenance_status")
    if status is not None and status not in ("active", "maintained"):
        raise ToolError("maintenance_status must be active or maintained")
    min_health = args.get("min_health")
    if min_health is not None and (isinstance(min_health, bool) or not isinstance(min_health, (int, float))
                                   or not 0 <= min_health <= 100):
        raise ToolError("min_health must be a number from 0 to 100")
    supported_only = args.get("supported_only", False)
    if not isinstance(supported_only, bool):
        raise ToolError("supported_only must be true or false")
    project_filters = lic is not None or status is not None or min_health is not None
    if project_filters and kind == "saas":
        raise ToolError("licence, maintenance_status and min_health filter projects; use kind project or any")
    mode = args.get("mode", "lexical")
    if mode not in ("lexical", "hybrid"):
        raise ToolError("mode must be lexical or hybrid")
    tier = args.get("tier", "any")
    if tier not in ("any",) + TIERS:
        raise ToolError("tier must be any, recommended, scored or discovery")
    if tier != "any" and kind == "saas":
        raise ToolError("tier filters projects; use kind project or any")
    index = layer.full or layer  # the full corpus when it verifies; the canonical layer alone otherwise

    if mode == "hybrid":
        hits, seeds = index.hybrid(query, kind)
        ranked = [(h["score"], h["kind"], h["key"], h["matched"], h["exact"]) for h in hits]
        extra = {(h["kind"], h["key"]): {"score_components": h["components"], "semantic_terms": h["semantic_terms"],
                                         "graph_path": h["graph_path"]} for h in hits}
    else:
        ranked, extra, seeds = index.search(query, kind), {}, None
    results = []
    for score, k, key, why, exact in ranked:
        if k == "saas":
            if project_filters or tier != "any":
                continue
            s = layer.saas[key]
            if supported_only and not s["supported"]:
                continue
            recs = [r for r in layer.rel_by_saas.get(key, []) if r["recommended"]]
            results.append({"kind": "saas", "id": key, "tier": "catalogue", "score": score,
                            "exact_name_match": exact, "matched": why,
                            "saas": _saas_view(layer, key), "recommended_alternatives": len(recs)})
        elif key in layer.corpus:
            c = layer.corpus[key]
            if tier not in ("any", "discovery") or min_health is not None:
                continue
            if lic is not None and (c["licence"]["spdx"] or "").lower() != str(lic).lower():
                continue
            if status is not None and c["maintenance"]["status"] != status:
                continue
            results.append({"kind": "project", "id": key, "tier": "discovery", "score": score,
                            "exact_name_match": exact, "matched": why, "project": _discovery_view(layer, key),
                            "recommended_for": []})
        else:
            p = layer.projects[key]
            if tier not in ("any", layer.tier_of(key)):
                continue
            if lic is not None and (p["licence"]["spdx"] or "").lower() != str(lic).lower():
                continue
            if status is not None and p["maintenance"]["status"] != status:
                continue
            h = p["project_health"]
            if min_health is not None and (h is None or h["score"] < min_health):
                continue
            rels = layer.rel_by_project.get(key, [])
            results.append({"kind": "project", "id": key, "tier": layer.tier_of(key), "score": score,
                            "exact_name_match": exact, "matched": why,
                            "project": {"oss_id": key, "name": p["name"], "repo_url": p["repo_url"],
                                        "licence_spdx": p["licence"]["spdx"], "stars": p["live"]["stars"],
                                        "maintenance_status": p["maintenance"]["status"],
                                        "project_health": None if h is None else {
                                            k2: h[k2] for k2 in ("score", "band", "trajectory")},
                                        "live_checked_at": p["live"]["checked_at"],
                                        "description": (_corpus_extra(layer, key) or {}).get("description")},
                            "recommended_for": sorted(r["saas_id"] for r in rels if r["recommended"])})
        if extra:
            results[-1].update(extra[(k, key)])
    total = len(results)
    out = {"query": query, "kind": kind, "mode": mode, "tier": tier, "total": total, "results": results[:limit],
           "more_available": max(0, total - limit),
           "searched": ("full corpus: SaaS catalogue, canonical projects and discovery projects (host descriptions "
                        "and topics)" if layer.full else "canonical layer only: the discovery corpus is unavailable"),
           "tiers": {t: TIER_MEANING[t] for t in TIERS}, "corpus": _corpus_state(layer, server)}
    if mode == "hybrid":
        out["graph_seeds"] = seeds
        out["ranking"] = (f"Hybrid: {HYBRID_WEIGHTS['lexical']} x lexical BM25 (normalised to the best hit) + "
                          f"{HYBRID_WEIGHTS['semantic']} x KEI-844 semantic cosine ({SEMANTIC_MODEL}) + "
                          f"{HYBRID_WEIGHTS['graph']} x graph (matched product -REPLACES-> project, weighted by "
                          "Replacement Fit). Components are returned per result; ties by kind then id.")
    else:
        out["ranking"] = ("BM25 over weighted fields (name, aliases or repository, products replaced, category, "
                          "topics, host description, languages, licence), +10 for an exact name match; ties by kind "
                          "then id. Lexical, deterministic.")
    return out


def tool_get_project(layer, args, server):
    pid, tier = layer.resolve_any(args.get("project"))
    if tier == "discovery":
        return {"project": _discovery_view(layer, pid, full=True), "tier": tier, "relationships": [],
                "corpus": _corpus_state(layer, server),
                "note": DISCOVERY_LABEL + ". The Source has not evaluated it as a replacement for any SaaS product; "
                        "it is not a recommendation."}
    rels = sorted(layer.rel_by_project.get(pid, []), key=lambda r: (not r["recommended"], r["saas_id"]))
    c = layer.canonical_corpus.get(pid)
    return {"project": {**_project_view(layer.projects[pid]), "tier": tier}, "tier": tier,
            "relationships": [_rel_summary(layer, r) for r in rels],
            "discovery_record": None if c is None else {
                "description": c["description"], "topics": c["topics"], "provenance": c["provenance"],
                "observed_as_of": c["live"]["observed_as_of"]},
            "note": "recommended is the publication gate: only recommended relationships are recommendations."}


def tool_get_relationship(layer, args, server):
    sid, how = layer.resolve_saas(args.get("saas"))
    pid = layer.resolve_project(args.get("project"))
    r = layer.rel_by_pair.get((sid, pid))
    if r is None:
        raise ToolError(f"The Source holds no relationship between {sid} and {pid} in its published layer "
                        "(none was claimed, or it was rejected or withheld)")
    fit = r["replacement_fit"]
    if r["recommended"]:
        verdict = "recommended: passes the export guard and its Replacement Fit is publishable and credible"
    else:
        reasons = []
        if fit.get("core_gate") == "failed":
            reasons.append("core use-case gate failed")
        if not fit.get("publish"):
            reasons.append("Replacement Fit not publishable")
        if not fit.get("credible_alternative"):
            reasons.append("not a credible alternative")
        verdict = "not recommended: " + ("; ".join(reasons) or "below the recommendation threshold")
    return {"relationship": r, "saas": _saas_view(layer, sid), "project": _project_view(layer.projects[pid]),
            "verdict": verdict,
            "evidence_note": "provenance names each upstream list, the pinned commit and the line the claim was read "
                             "from; licence, archive and maintenance were verified against the repository host."}


# -- signals: Repository Momentum and Community Radar


def _signal_state(layer, server, name):
    """The signal layer's availability and freshness, for every answer that uses it."""
    s = dict(layer.signals[name])
    s.pop("coverage", None)
    age_h = None
    if s.get("as_of"):
        try:
            then = datetime.strptime(s["as_of"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age_h = round((server.now() - then).total_seconds() / 3600, 2)
        except ValueError:
            pass
    s["age_hours"] = age_h
    s["stale"] = s["status"] == "ok" and (age_h is None or age_h > SIGNAL_STALE_AFTER_HOURS)
    if s["status"] == "ok" and s["stale"]:
        s["status"] = "stale"
        s["reason"] = f"as_of {s.get('as_of')} is more than {SIGNAL_STALE_AFTER_HOURS} h old; the daily cycle has not refreshed it"
    s["stale_after_hours"] = SIGNAL_STALE_AFTER_HOURS
    return s


def _window_view(win: dict):
    if not win:
        return {"status": "not_published"}
    if win.get("status") != "measured":
        return {"status": win.get("status"), "reason": win.get("reason")}
    acc = win.get("acceleration") or {}
    return {"status": "measured", "momentum": win.get("momentum"), "stars_now": win.get("stars_now"),
            "stars_delta": win.get("stars_delta"), "stars_per_day": win.get("stars_per_day"),
            "growth_pct_per_30d": win.get("growth_pct_per_30d"), "base_day": win.get("base_day"),
            "acceleration": ({"status": "measured", "label": acc.get("label"),
                              "stars_per_day_change": acc.get("stars_per_day_change"),
                              "explanation": acc.get("explanation")}
                             if acc.get("status") == "measured" else
                             {"status": acc.get("status") or "not_measured", "reason": acc.get("reason")}),
            "activity": win.get("activity"), "explanation": win.get("explanation")}


def _repository_momentum(layer, key):
    row = layer.momentum.get(_repo_key(key))
    if row is None:
        return {"status": "not_tracked", "reason": "this repository is not in data/momentum/projects.jsonl"}
    return {"status": "tracked", "key": row.get("key"), "on": row.get("on"), "stars": row.get("stars"),
            "windows": {w: _window_view((row.get("windows") or {}).get(w)) for w in ("1", "7", "30", "90")}}


def _community_momentum(layer, key, full=False):
    row = layer.radar.get(_repo_key(key))
    if row is None:
        return {"status": "no_mentions_in_window",
                "reason": "no counted mention of this repository in the Community Radar window"}
    out = {"status": "ranked", "rank": row.get("rank"), "score": row.get("score"), "key": row.get("key"),
           "sources": row.get("sources"), "first_mentioned_at": row.get("first_mentioned_at"),
           "last_mentioned_at": row.get("last_mentioned_at"), "explanation": row.get("explanation"),
           "radar_status": row.get("status"), "radar_status_reason": row.get("status_reason")}
    if full:
        out["mentions"] = [{k: m.get(k) for k in ("source", "permalink", "published_at", "points", "comments",
                                                    "contribution", "counted")} for m in row.get("mentions") or []]
    return out


MEASURES = {
    "project_health": "Is the project itself healthy (maintenance, releases, community, security posture)? "
                      "Scored per project, independent of any SaaS.",
    "replacement_fit": "How well does the project replace one specific SaaS product? Scored per pairing.",
    "repository_momentum": "Is the repository's star growth rising or falling, and accelerating, over 1/7/30/90 "
                           "days (data/momentum)? Measured from daily snapshots, not opinion.",
    "community_momentum": "Is the project being discussed now (Hacker News, DEV/Forem mentions in the Radar "
                          "window, data/radar)? Attention, not quality.",
}


def tool_get_momentum(layer, args, server):
    v = args.get("project")
    try:
        key, tier = layer.resolve_any(v)
        canonical = key in layer.projects
    except ToolError:
        key, canonical, tier = _repo_key(v), False, None
        if key not in layer.momentum and key not in layer.radar:
            raise
    return {"project": key, "in_canonical_layer": canonical, "tier": tier,
            "repository_momentum": _repository_momentum(layer, key),
            "community_momentum": _community_momentum(layer, key, full=True),
            "signals": {n: _signal_state(layer, server, n) for n in SIGNALS},
            "measures": {k: MEASURES[k] for k in ("repository_momentum", "community_momentum")}}


def tool_compare(layer, args, server):
    names = args.get("projects")
    if not isinstance(names, list) or not 2 <= len(names) <= MAX_COMPARE or \
            not all(isinstance(n, str) and n.strip() for n in names):
        raise ToolError(f"projects must be a list of 2 to {MAX_COMPARE} project names, oss_ids or repository URLs")
    pids = []
    for n in names:
        pid, _ = layer.resolve_any(n)
        if pid in pids:
            raise ToolError(f"{n!r} is the same project as an earlier entry ({pid})")
        pids.append(pid)
    rows, uncertainty = [], []
    for pid in pids:
        if pid in layer.corpus:
            d = _discovery_view(layer, pid, full=True)
            rows.append({
                "oss_id": pid, "tier": "discovery", "label": DISCOVERY_LABEL, "name": d["name"],
                "repo_url": d["repo_url"], "description": d["description"], "licence_spdx": d["licence"]["spdx"],
                "licence_family": LICENCE_FAMILIES.get(d["licence"]["spdx"] or "", "unknown"),
                "licence_status": d["licence"]["status"], "stars": d["stars"], "archived": d["archived"],
                "latest_release": d["latest_release"], "maintenance": d["maintenance"], "project_health": None,
                "observed_as_of": d["observed_as_of"], "replaces": [],
                "repository_momentum": _repository_momentum(layer, pid),
                "community_momentum": _community_momentum(layer, pid)})
            uncertainty.append(f"{pid}: discovery tier, not scored (no Project Health or Replacement Fit)")
            if "warning" in d:
                uncertainty.append(f"{pid}: {d['warning']}")
            continue
        p = layer.projects[pid]
        h = p["project_health"]
        recs = sorted((r for r in layer.rel_by_project.get(pid, []) if r["recommended"]), key=lambda r: r["saas_id"])
        rows.append({
            "oss_id": pid, "tier": layer.tier_of(pid), "name": p["name"], "repo_url": p["repo_url"],
            "licence_spdx": p["licence"]["spdx"],
            "licence_family": LICENCE_FAMILIES.get(p["licence"]["spdx"] or "", "unknown"),
            "stars": p["live"]["stars"], "archived": p["live"]["archived"],
            "latest_release": p["live"]["latest_release"], "maintenance": p["maintenance"],
            "project_health": h, "live_checked_at": p["live"]["checked_at"],
            "replaces": [{"saas_id": r["saas_id"], "saas_name": layer.saas[r["saas_id"]]["name"],
                          "replacement_fit": r["replacement_fit"]["score"],
                          "confidence": r["replacement_fit"]["confidence"]} for r in recs],
            "repository_momentum": _repository_momentum(layer, pid),
            "community_momentum": _community_momentum(layer, pid)})
        if h is None:
            uncertainty.append(f"{pid}: no Project Health score")
        elif h.get("evidence_insufficient"):
            uncertainty.append(f"{pid}: Project Health marked evidence_insufficient (confidence {h.get('confidence')})")
        w7 = rows[-1]["repository_momentum"].get("windows", {}).get("7", {})
        if w7.get("status") != "measured":
            uncertainty.append(f"{pid}: 7-day repository momentum {w7.get('status')}")
    shared = sorted(set.intersection(*[{r["saas_id"] for r in layer.rel_by_project.get(pid, [])} for pid in pids]))
    head_to_head = []
    for sid in shared:
        entry = {"saas_id": sid, "saas_name": layer.saas[sid]["name"], "by_project": {}}
        for pid in pids:
            r = layer.rel_by_pair[(sid, pid)]
            entry["by_project"][pid] = {"recommended": r["recommended"],
                                        "replacement_fit": r["replacement_fit"]["score"],
                                        "confidence": r["replacement_fit"]["confidence"]}
        best = max(pids, key=lambda pid: (entry["by_project"][pid]["recommended"],
                                          entry["by_project"][pid]["replacement_fit"] or 0))
        entry["higher_fit"] = best
        head_to_head.append(entry)
    def lead(field):
        vals = [(r["project_health"] or {}).get("score") if field == "health" else r["stars"] for r in rows]
        known = [(v, r["oss_id"]) for v, r in zip(vals, rows) if v is not None]
        return max(known)[1] if known else None
    return {"projects": rows, "shared_saas": head_to_head,
            "summary": {"higher_project_health": lead("health"), "more_stars": lead("stars"),
                        "shared_saas_products": len(shared)},
            "measures": MEASURES, "uncertainty": uncertainty,
            "signals": {n: _signal_state(layer, server, n) for n in SIGNALS},
            "note": "Each measure answers a different question; none is folded into another. A project can be "
                    "healthy and a poor replacement for one product, or trending and unproven."}


def _licence_ok(spdx, licences, family):
    if licences and (spdx or "").lower() not in {x.lower() for x in licences}:
        return "licence"
    if family and LICENCE_FAMILIES.get(spdx or "") != family:
        return "licence_family"
    return None


def _str_list(args, key, allowed=None):
    v = args.get(key)
    if v is None:
        return []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v) or len(v) > 20:
        raise ToolError(f"{key} must be a string or a list of up to 20 strings")
    if allowed and not set(v) <= set(allowed):
        raise ToolError(f"{key} values must be among {', '.join(allowed)}")
    return v


def _num(args, key, lo, hi):
    v = args.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise ToolError(f"{key} must be a number from {lo} to {hi}")
    return v


def tool_recommend(layer, args, server):
    need, saas = args.get("need"), args.get("saas")
    if need is None and saas is None:
        raise ToolError("give need (what the tool must do, in words) or saas (a product to replace), or both")
    if need is not None and (not isinstance(need, str) or not _tokens(need) or len(need) > MAX_TEXT):
        raise ToolError(f"need must contain words and be at most {MAX_TEXT} characters")
    licences = _str_list(args, "licence")
    family = args.get("licence_family")
    if family is not None and family not in ("permissive", "weak_copyleft", "copyleft"):
        raise ToolError("licence_family must be permissive, weak_copyleft or copyleft")
    self_host = args.get("self_hostable")
    if self_host is not None and not isinstance(self_host, bool):
        raise ToolError("self_hostable must be true or false")
    status = _str_list(args, "maintenance_status", ("active", "maintained"))
    momentum = _str_list(args, "repository_momentum_7d", MOMENTUM_LABELS)
    min_fit, min_health = _num(args, "min_fit", 0, 100), _num(args, "min_health", 0, 100)
    min_stars, max_stars = _num(args, "min_stars", 0, 10 ** 9), _num(args, "max_stars", 0, 10 ** 9)
    max_days = _num(args, "max_days_since_activity", 0, 3650)
    limit = _limit(args, 5)
    include_discovery = _include_discovery(args)

    matched, seeds = [], []
    if saas is not None:
        sid, how = layer.resolve_saas(saas)
        matched.append({"saas_id": sid, "name": layer.saas[sid]["name"], "matched_by": how, "strength": 1.0})
    if need is not None:
        hits, seeds = layer.hybrid(need, "any")
        # A product named outright in the need ("... like Notion") is the product meant; retrieval only fills in
        # when none is named.
        padded = f" {_norm(need)} "
        named = sorted({sid for key, sids in layer.saas_names.items() if len(key) > 2 and f" {key} " in padded
                        for sid in sids} - {m["saas_id"] for m in matched})
        for sid in named[:3]:
            matched.append({"saas_id": sid, "name": layer.saas[sid]["name"], "matched_by": "named_in_need",
                            "strength": 1.0})
        top_saas = max((h["score"] for h in hits if h["kind"] == "saas"), default=0)
        for h in ([] if named else hits):
            if h["kind"] == "saas" and h["score"] >= max(0.25, 0.8 * top_saas) and len(matched) < 3 and \
                    h["key"] not in {m["saas_id"] for m in matched}:
                matched.append({"saas_id": h["key"], "name": layer.saas[h["key"]]["name"],
                                "matched_by": "hybrid", "strength": h["score"], "components": h["components"]})
    proj_hits = {}
    if need is not None:
        for h in hits:
            if h["kind"] == "project" and h["score"] >= 0.15:
                proj_hits[h["key"]] = h
        if matched:  # a matched product's own alternatives come first; a direct hit must be strong to join them
            proj_hits = {k: h for k, h in proj_hits.items() if h["score"] >= 0.45}
    cands = {}
    for m in matched:
        for r in layer.rel_by_saas.get(m["saas_id"], []):
            if r["recommended"]:
                cur = cands.get(r["oss_id"])
                if cur is None or (r["replacement_fit"]["score"] or 0) > (cur["rel"]["replacement_fit"]["score"] or 0):
                    cands[r["oss_id"]] = {"rel": r, "via": "replaces " + m["saas_id"], "group": 0}
    for pid, h in proj_hits.items():
        if pid not in cands:
            recs = sorted((r for r in layer.rel_by_project.get(pid, []) if r["recommended"]),
                          key=lambda r: -(r["replacement_fit"]["score"] or 0))
            if recs:
                cands[pid] = {"rel": recs[0], "via": "matched the need directly", "group": 1 if matched else 0}
    excluded, out = {}, []
    page_rows = {(sid, r["oss_id"]): r for sid, doc in layer.surface.items() for r in doc["recommendations"]}
    for pid, c in cands.items():
        p, r = layer.projects[pid], c["rel"]
        h = p["project_health"]
        sh = (page_rows.get((r["saas_id"], pid)) or {}).get("self_hosting")
        mom = _repository_momentum(layer, pid)
        m7 = mom.get("windows", {}).get("7", {}) if mom["status"] == "tracked" else {}
        why_not = (_licence_ok(p["licence"]["spdx"], licences, family)
                   or ("maintenance_status" if status and p["maintenance"]["status"] not in status else None)
                   or ("min_fit" if min_fit is not None and (r["replacement_fit"]["score"] or 0) < min_fit else None)
                   or ("min_health" if min_health is not None and (h is None or h["score"] < min_health) else None)
                   or ("min_stars" if min_stars is not None and (p["live"]["stars"] or 0) < min_stars else None)
                   or ("max_stars" if max_stars is not None and (p["live"]["stars"] or 0) > max_stars else None)
                   or ("max_days_since_activity" if max_days is not None and
                       (p["maintenance"]["days_since_meaningful_activity"] is None or
                        p["maintenance"]["days_since_meaningful_activity"] > max_days) else None)
                   or ("self_hosting_unknown" if self_host is True and sh is None else None)
                   or ("not_self_hostable" if self_host is True and sh and not sh.get("self_hostable") else None)
                   or ("repository_momentum_7d" if momentum and m7.get("momentum") not in momentum else None))
        if why_not:
            excluded[why_not] = excluded.get(why_not, 0) + 1
            continue
        out.append({
            "oss_id": pid, "name": p["name"], "repo_url": p["repo_url"], "licence_spdx": p["licence"]["spdx"],
            "licence_family": LICENCE_FAMILIES.get(p["licence"]["spdx"] or "", "unknown"),
            "stars": p["live"]["stars"], "maintenance_status": p["maintenance"]["status"],
            "days_since_meaningful_activity": p["maintenance"]["days_since_meaningful_activity"],
            "for_saas": r["saas_id"], "via": c["via"], "edge_id": r["edge_id"], "_group": c["group"],
            "replacement_fit": {k: r["replacement_fit"].get(k) for k in ("score", "confidence", "score_version")},
            "project_health": None if h is None else {k: h.get(k) for k in ("score", "band", "trajectory",
                                                                             "confidence", "evidence_insufficient")},
            "self_hosting": sh if sh else {"self_hostable": None,
                                           "basis": "not evidenced in the published layer for this pairing"},
            "repository_momentum_7d": m7 or {"status": mom["status"]},
            "community_momentum": _community_momentum(layer, pid),
            "retrieval": ({k: proj_hits[pid][k] for k in ("score", "components", "semantic_terms")}
                          if pid in proj_hits else None),
            "sources": r["sources"], "live_checked_at": p["live"]["checked_at"]})
    out.sort(key=lambda x: (x["_group"], -(x["replacement_fit"]["score"] or 0),
                            -((x["project_health"] or {}).get("score") or 0), -(x["stars"] or 0), x["oss_id"]))
    for i, row in enumerate(out, 1):
        row["rank"] = i
        del row["_group"]
    constraints = {k: args[k] for k in ("licence", "licence_family", "self_hostable", "maintenance_status", "min_fit",
                                        "min_health", "min_stars", "max_stars", "max_days_since_activity",
                                        "repository_momentum_7d") if k in args}
    out = {"need": need, "saas": saas, "matched_products": matched, "graph_seeds": seeds,
            "constraints_applied": constraints, "candidates_considered": len(cands),
            "excluded_by_constraint": dict(sorted(excluded.items())),
            "recommendations": out[:limit], "more_available": max(0, len(out) - limit),
            "status": "ok" if out else ("no_products_matched" if not matched and not proj_hits
                                        else "no_recommended_alternatives" if not cands
                                        else "no_candidate_meets_constraints"),
            "ranking": "Alternatives to the matched products first, then strong direct matches of the need; within "
                       "each, Replacement Fit, then Project Health, then stars. Only recommended "
                       "relationships are candidates. Constraints are hard filters, applied exactly.",
            "not_evaluated": {"requirements": list(NOT_EVALUABLE),
                              "why": "The published layer holds no evidence for these; check them yourself. "
                                     "self_hostable is evidenced only on published api/v1 pages."},
            "signals": {n: _signal_state(layer, server, n) for n in SIGNALS}}
    if include_discovery:
        query = need or " ".join(f"{layer.saas[m['saas_id']]['name']} {layer.saas[m['saas_id']]['category']}"
                                 for m in matched)
        out["discovery_candidates"] = _discovery_candidates(layer, server, query, limit, licences, family, status,
                                                            min_stars, max_stars)
        out["discovery_candidates"]["constraints_not_applicable"] = sorted(
            k for k in ("self_hostable", "min_fit", "min_health", "max_days_since_activity", "repository_momentum_7d")
            if k in args)
    return out


def tool_emerging(layer, args, server):
    window = args.get("window", "7")
    if window not in ("1", "7", "30"):
        raise ToolError("window must be 1, 7 or 30 (days)")
    labels = _str_list(args, "momentum", MOMENTUM_LABELS) or ["surging", "rising"]
    accelerating = args.get("accelerating_only", False)
    if not isinstance(accelerating, bool):
        raise ToolError("accelerating_only must be true or false")
    min_stars, max_stars = _num(args, "min_stars", 0, 10 ** 9), _num(args, "max_stars", 0, 10 ** 9)
    query = args.get("query")
    if query is not None and (not isinstance(query, str) or not _tokens(query) or len(query) > MAX_TEXT):
        raise ToolError(f"query must contain words and be at most {MAX_TEXT} characters")
    limit = _limit(args, 10)
    mom_state, rad_state = _signal_state(layer, server, "momentum"), _signal_state(layer, server, "radar")
    in_scope = None
    if query is not None:
        hits, _ = (layer.full or layer).hybrid(query, "project")
        in_scope = {h["key"] for h in hits if h["score"] >= 0.15}

    cov = (layer.signals["momentum"].get("coverage") or {}).get(window, {})
    repo_rows = []
    for row in layer.momentum_projects:
        win = (row.get("windows") or {}).get(window) or {}
        if win.get("status") != "measured" or win.get("momentum") not in labels:
            continue
        acc = win.get("acceleration") or {}
        if accelerating and not (acc.get("status") == "measured" and acc.get("label") == "accelerating"):
            continue
        stars = win.get("stars_now")
        if min_stars is not None and (stars or 0) < min_stars or max_stars is not None and (stars or 0) > max_stars:
            continue
        pid = row.get("canonical_id")
        cid = layer.corpus_keys.get(_repo_key(row.get("canonical_id") or row.get("key") or ""))
        if in_scope is not None and pid not in in_scope and cid not in in_scope:
            continue
        p = layer.projects.get(pid) if pid else None
        c = layer.corpus.get(cid) if cid and not p else None
        repo_rows.append({
            "key": row.get("key"), "oss_id": pid if p else None,
            "name": p["name"] if p else c["name"] if c else row.get("canonical_name"),
            "tier": layer.tier_of(pid) if p else "discovery" if c else None,
            "description": (_corpus_extra(layer, pid) or {}).get("description") if p else c["description"] if c else None,
            "in_canonical_layer": bool(p), "in_corpus": bool(c) or pid in layer.canonical_corpus,
            "stars": stars, "momentum": win.get("momentum"),
            "growth_pct_per_30d": win.get("growth_pct_per_30d"), "stars_per_day": win.get("stars_per_day"),
            "acceleration": _window_view(win)["acceleration"], "explanation": win.get("explanation"),
            "recommended_for": sorted(r["saas_id"] for r in layer.rel_by_project.get(pid, []) if r["recommended"])
            if p else [],
            "community_momentum": _community_momentum(layer, row.get("key"))})
    repo_rows.sort(key=lambda x: (-(x["growth_pct_per_30d"] or 0), -(x["stars_per_day"] or 0), x["key"] or ""))
    community, unknown_stars = [], 0
    if in_scope is None:
        for r in layer.radar_ranked:
            mrow = layer.momentum.get(_repo_key(r.get("key") or ""))
            mwin = ((mrow or {}).get("windows") or {}).get(window) or {}
            stars = mwin.get("stars_now")
            if (min_stars is not None or max_stars is not None) and stars is None:
                unknown_stars += 1
                continue
            if min_stars is not None and stars < min_stars or max_stars is not None and stars > max_stars:
                continue
            community.append({"key": r.get("key"), "rank": r.get("rank"), "score": r.get("score"),
                              "sources": r.get("sources"), "last_mentioned_at": r.get("last_mentioned_at"),
                              "stars": stars, "repository_momentum": mwin.get("momentum") if mwin else None,
                              "explanation": r.get("explanation"), "radar_status": r.get("status")})
    both = sorted({x["key"] for x in repo_rows} & {x["key"] for x in community})
    degraded = []
    if mom_state["status"] != "ok":
        degraded.append(f"repository momentum {mom_state['status']}: {mom_state.get('reason')}")
    if rad_state["status"] != "ok":
        degraded.append(f"community momentum {rad_state['status']}: {rad_state.get('reason')}")
    if cov.get("status") not in (None, "measured"):
        degraded.append(f"{window}-day repository momentum is {cov.get('status')}; measurable from "
                        f"{cov.get('measurable_from')}")
    if cov.get("acceleration_status") not in (None, "measured"):
        degraded.append(f"{window}-day acceleration is {cov.get('acceleration_status')}; measurable from "
                        f"{cov.get('acceleration_measurable_from')}")
    if unknown_stars:
        degraded.append(f"{unknown_stars} Community Radar repositories have no star count in the momentum layer, so "
                        "a star filter cannot place them; they are left out, not guessed")
    if in_scope is not None:
        degraded.append(("query narrows to projects in the canonical layer and the discovery corpus" if layer.full
                         else "query narrows to projects in the canonical layer (the discovery corpus is "
                              "unavailable)") + "; Community Radar rows are not categorised, so the community list "
                                                "is omitted for a query")
    return {"window_days": int(window), "momentum_labels": labels, "accelerating_only": accelerating,
            "repository_momentum": repo_rows[:limit], "repository_momentum_total": len(repo_rows),
            "community_momentum": community[:limit], "community_momentum_total": len(community),
            "both_signals": both, "window_coverage": cov, "degraded": degraded,
            "signals": {"momentum": mom_state, "radar": rad_state}, "measures": {
                k: MEASURES[k] for k in ("repository_momentum", "community_momentum")},
            "corpus": _corpus_state(layer, server),
            "note": "Momentum is attention and growth, not quality or fit. tier says what The Source knows about each "
                    "row: discovery rows (and rows with no tier, outside both layers) have no Project Health or "
                    "Replacement Fit."}


def _obj(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT}
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

TOOLS = [
    ("source_dataset_info", "Dataset version and freshness",
     "Which snapshot of The Source is being served: dataset_version, data_as_of, when repositories were last "
     "checked (and whether that is stale), record counts, upstream sources, licence and attribution.",
     _obj({}), tool_dataset_info),
    ("source_list_supported_saas", "Supported SaaS products",
     "The SaaS products that have a published comparison page, with how many recommendations each has.",
     _obj({}), tool_list_supported),
    ("source_get_alternatives", "Open-source alternatives to a SaaS product",
     "Ranked, evidence-backed open-source alternatives to one SaaS product. 'saas' may be a saas_id, a name, "
     "an alias, a domain or a URL. For supported products this is exactly the published api/v1 page.",
     _obj({"saas": {"type": "string", "description": "saas_id, name, alias, domain or URL, e.g. 'Notion'"},
           "limit": {**LIMIT, "description": "how many recommendations (default 5)"},
           "include_discovery": {"type": "boolean", "description": "also return a separate, labelled "
                                 "discovery_candidates list (discovered, not scored; never recommendations)"}},
          ["saas"]),
     tool_get_alternatives),
    ("source_detect_saas", "Detect the SaaS product of a URL",
     "Which supported SaaS product a URL belongs to, using The Source's published domain rules. Matched "
     "locally; the URL is never fetched.",
     _obj({"url": {"type": "string", "description": "a page URL or host, e.g. https://acme.atlassian.net/jira/"}},
          ["url"]), tool_detect_saas),
    ("source_search", "Search products and projects",
     "Search The Source's SaaS catalogue and every open-source project it knows (about 9,400: the canonical layer "
     "plus the discovery corpus) by words: names, aliases, categories, products replaced, host descriptions and "
     "topics. Every project carries a tier (recommended, scored, discovery); filter by tier, licence, maintenance "
     "status and Project Health. Discovery projects are discovered, not scored, and never recommendations.",
     _obj({"query": {"type": "string", "description": "words to look for, e.g. 'password manager'"},
           "kind": {"type": "string", "enum": ["any", "saas", "project"], "description": "default any"},
           "licence": {"type": "string", "description": "SPDX id, e.g. AGPL-3.0 (projects only)"},
           "maintenance_status": {"type": "string", "enum": ["active", "maintained"]},
           "min_health": {"type": "number", "minimum": 0, "maximum": 100},
           "supported_only": {"type": "boolean", "description": "SaaS results: only products with a page"},
           "tier": {"type": "string", "enum": ["any"] + list(TIERS),
                    "description": "projects only: recommended, scored or discovery (default any)"},
           "mode": {"type": "string", "enum": ["lexical", "hybrid"],
                    "description": "lexical (default, BM25) or hybrid (BM25 + KEI-844 semantic + graph, with score "
                                   "components per result)"},
           "limit": {**LIMIT, "description": "default 10"}}, ["query"]), tool_search),
    ("source_get_project", "One open-source project",
     "One project's live facts (licence, stars, latest release, maintenance, Project Health, when checked), its "
     "tier and every SaaS relationship it has, recommended or not. Any project in the discovery corpus is served "
     "too, labelled discovery, with where it was found (list + pinned commit) and when it was observed. "
     "'project' may be an oss_id, a repository URL or a name.",
     _obj({"project": {"type": "string", "description": "e.g. github.com/docmost/docmost or Docmost"}},
          ["project"]), tool_get_project),
    ("source_get_relationship", "Evidence for one SaaS-to-project pairing",
     "The full evidence for one SaaS product / project pairing: upstream provenance (list, pinned commit, "
     "line), Replacement Fit with its dimensions, and why it is or is not recommended.",
     _obj({"saas": {"type": "string"}, "project": {"type": "string"}}, ["saas", "project"]),
     tool_get_relationship),
    ("source_compare_projects", "Compare open-source projects",
     "Side by side for 2-5 projects: licence, stars, maintenance, Project Health, Replacement Fit for every SaaS "
     "product they replace (head to head where they share one), Repository Momentum and Community Momentum, with "
     "the uncertainty in each. The four measures are kept separate. Discovery-tier projects can be compared too; "
     "they have no Project Health or Replacement Fit and say so.",
     _obj({"projects": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": MAX_COMPARE,
                        "description": "oss_ids, repository URLs or names, e.g. ['Docmost', 'AppFlowy']"}},
          ["projects"]), tool_compare),
    ("source_recommend", "Recommend projects against explicit requirements",
     "Recommended open-source projects for a need in words and/or a SaaS product to replace, filtered exactly by "
     "hard constraints (licence or licence family, self-hostable, maintenance, minimum Replacement Fit or Project "
     "Health, star range, days since activity, 7-day repository momentum). Says which products the need matched "
     "and how (lexical, KEI-844 semantic, graph), what each constraint excluded, and which requirements the data "
     "cannot judge.",
     _obj({"need": {"type": "string", "description": "e.g. 'team wiki and notes like Notion'"},
           "saas": {"type": "string", "description": "a product to replace: saas_id, name, alias, domain or URL"},
           "licence": {"type": "array", "items": {"type": "string"}, "description": "allowed SPDX ids"},
           "licence_family": {"type": "string", "enum": ["permissive", "weak_copyleft", "copyleft"]},
           "self_hostable": {"type": "boolean", "description": "true: only pairings with self-hosting evidence"},
           "maintenance_status": {"type": "array", "items": {"type": "string", "enum": ["active", "maintained"]}},
           "min_fit": {"type": "number", "minimum": 0, "maximum": 100},
           "min_health": {"type": "number", "minimum": 0, "maximum": 100},
           "min_stars": {"type": "integer", "minimum": 0}, "max_stars": {"type": "integer", "minimum": 0},
           "max_days_since_activity": {"type": "integer", "minimum": 0},
           "repository_momentum_7d": {"type": "array", "items": {"type": "string", "enum": list(MOMENTUM_LABELS)}},
           "limit": {**LIMIT, "description": "default 5"},
           "include_discovery": {"type": "boolean", "description": "also return a separate, labelled "
                                 "discovery_candidates list (discovered, not scored; never recommendations)"}}),
     tool_recommend),
    ("source_emerging_projects", "Emerging and accelerating projects",
     "Repositories whose star growth is surging or rising over a window (optionally only those whose growth is "
     "accelerating), and repositories the community is discussing now (Hacker News, DEV), filtered by star range "
     "and optionally narrowed to a topic (searched over the full corpus). Every row carries its tier. Says plainly "
     "when a window cannot be measured yet.",
     _obj({"window": {"type": "string", "enum": ["1", "7", "30"], "description": "days, default 7"},
           "momentum": {"type": "array", "items": {"type": "string", "enum": list(MOMENTUM_LABELS)},
                        "description": "default ['surging', 'rising']"},
           "accelerating_only": {"type": "boolean"},
           "min_stars": {"type": "integer", "minimum": 0}, "max_stars": {"type": "integer", "minimum": 0},
           "query": {"type": "string", "description": "narrow to a topic, e.g. 'developer tools'"},
           "limit": {**LIMIT, "description": "default 10"}}), tool_emerging),
    ("source_get_momentum", "Repository and community momentum for one project",
     "Repository Momentum (stars per day, growth, acceleration over 1/7/30/90 days, with the explanation) and "
     "Community Momentum (Radar rank and every counted mention with its permalink) for one repository, canonical "
     "or not, with the freshness of each signal.",
     _obj({"project": {"type": "string", "description": "oss_id, repository URL or name"}}, ["project"]),
     tool_get_momentum),
]
TOOL_BY_NAME = {t[0]: t for t in TOOLS}


def _check_args(schema, args):
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    extra = sorted(set(args) - set(schema["properties"]))
    if extra:
        raise ToolError(f"unknown argument(s): {', '.join(extra)}")
    missing = [k for k in schema["required"] if k not in args]
    if missing:
        raise ToolError(f"missing argument(s): {', '.join(missing)}")
    for k, v in args.items():
        prop = schema["properties"][k]
        want = prop.get("type")
        if want == "string" and (not isinstance(v, str) or len(v) > 2048):
            raise ToolError(f"{k} must be a string")
        if want == "string" and "enum" in prop and v not in prop["enum"]:
            raise ToolError(f"{k} must be one of {', '.join(prop['enum'])}")
        if want == "array" and (not isinstance(v, list) or len(v) > prop.get("maxItems", 20)
                                or not all(isinstance(x, str) and len(x) <= 2048 for x in v)):
            raise ToolError(f"{k} must be a list of at most {prop.get('maxItems', 20)} strings")
        if want == "boolean" and not isinstance(v, bool):
            raise ToolError(f"{k} must be true or false")


# --------------------------------------------------------------------------- server


def _file_sha(path: Path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class Telemetry:
    """Privacy-safe aggregate usage: per UTC day, per tool, per outcome, a count and a latency histogram.
    Nothing about the caller or the call is kept: no arguments, results, client name, session or address.
    The HTTP transport writes snapshot() to a file; stdio keeps it in memory only."""

    BUCKETS_MS = (10, 50, 200, 1000)

    def __init__(self, retain_days: int = 90):
        self.days, self.retain_days = {}, retain_days

    def count(self, tool: str, outcome: str, seconds: float, now: datetime):
        ms = seconds * 1000
        bucket = next((f"<{b}ms" for b in self.BUCKETS_MS if ms < b), f">={self.BUCKETS_MS[-1]}ms")
        day = self.days.setdefault(now.strftime("%Y-%m-%d"), {})
        row = day.setdefault(tool if tool in TOOL_BY_NAME else "unknown", {})
        cell = row.setdefault(outcome, {"count": 0, "latency": {}})
        cell["count"] += 1
        cell["latency"][bucket] = cell["latency"].get(bucket, 0) + 1
        cutoff = (now - timedelta(days=self.retain_days)).strftime("%Y-%m-%d")
        for d in [d for d in self.days if d < cutoff]:
            del self.days[d]

    def snapshot(self):
        return {"schema": "the-source.mcp-telemetry/1", "server_version": SERVER_VERSION,
                "fields": "per UTC day -> tool -> outcome (ok | tool_error | internal_error) -> count and latency "
                          "histogram. No arguments, results, client identity, sessions or addresses are recorded.",
                "retain_days": self.retain_days, "days": self.days}


class Server:
    def __init__(self, root: Path, now: datetime | None = None):
        self.root = Path(root)
        self._now = now
        self.layer = Layer(self.root)  # refuse to start on a layer that does not verify
        self.reload_error = None
        self.initialized = False
        self.protocol = None
        self.telemetry = Telemetry()

    def now(self):
        return self._now or datetime.now(timezone.utc).replace(microsecond=0)

    def snapshot_status(self):
        return {"serving_dataset_version": self.layer.dataset_version,
                "current": self.reload_error is None,
                "reload_error": self.reload_error}

    def refresh(self):
        """Switch to a newer layer when the MANIFEST changes and the new layer verifies."""
        try:
            sha = hashlib.sha256((self.root / "data" / "canonical" / "MANIFEST.json").read_bytes()).hexdigest()
        except OSError as exc:
            self.reload_error = f"MANIFEST unreadable: {exc}"
            return
        signal_shas = tuple(_file_sha(self.root / "data" / n / "MANIFEST.json") for n in sorted(SIGNALS) + ["corpus"])
        if sha == self.layer.manifest_sha256 and signal_shas == self.layer.signal_manifest_shas():
            return
        try:
            self.layer = Layer(self.root)
            self.reload_error = None
            print(f"the-source-mcp: now serving dataset {self.layer.dataset_version}", file=sys.stderr)
        except Exception as exc:  # any malformed update: keep serving the verified snapshot
            self.reload_error = f"newer files did not verify, still serving {self.layer.dataset_version}: {exc}"
            print(f"the-source-mcp: {self.reload_error}", file=sys.stderr)

    def meta(self):
        m = self.layer.manifest
        return {"dataset_version": self.layer.dataset_version, "data_as_of": m.get("data_as_of"),
                "api_version": API_VERSION, "licence": LICENCE, "licence_url": LICENCE_URL,
                "attribution": ATTRIBUTION, "notice": "data/canonical/NOTICE.md",
                "snapshot_current": self.reload_error is None}

    # -- JSON-RPC

    def handle(self, msg):
        """One parsed message -> one response object, or None for a notification."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
                return None  # a response to nothing we sent; ignore
            return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
        method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        is_note = "id" not in msg
        if is_note:
            if method == "notifications/initialized":
                self.initialized = True
            return None
        if not isinstance(params, dict):
            return _err(mid, -32602, "params must be an object")
        if method == "initialize":
            return _ok(mid, self._initialize(params))
        if method == "ping":
            return _ok(mid, {})
        if self.protocol is None:
            return _err(mid, -32600, "Server not initialized")
        if method == "tools/list":
            return _ok(mid, {"tools": [_tool_desc(t) for t in TOOLS]})
        if method == "tools/call":
            return self._call(mid, params)
        if method == "resources/list":
            self.refresh()
            return _ok(mid, {"resources": self._resources()})
        if method == "resources/templates/list":
            return _ok(mid, {"resourceTemplates": [{
                "uriTemplate": "source://api/v1/saas/{saas_id}", "name": "saas-page",
                "title": "Published read-API page for one supported SaaS product", "mimeType": "application/json"}]})
        if method == "resources/read":
            self.refresh()
            return self._read(mid, params)
        if method == "prompts/list":
            return _ok(mid, {"prompts": []})
        return _err(mid, -32601, f"Method not found: {method}")

    def _initialize(self, params):
        asked = params.get("protocolVersion")
        self.protocol = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {"protocolVersion": self.protocol,
                "capabilities": {"tools": {"listChanged": False},
                                 "resources": {"subscribe": False, "listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "The Source", "version": SERVER_VERSION},
                "instructions": INSTRUCTIONS}

    def _call(self, mid, params):
        name, args = params.get("name"), params.get("arguments")
        if name not in TOOL_BY_NAME:
            return _err(mid, -32602, f"Unknown tool: {name}")
        self.refresh()
        _, _, _, schema, fn = TOOL_BY_NAME[name]
        args = {} if args is None else args
        started = time.monotonic()
        try:
            _check_args(schema, args)
            payload = {"result": fn(self.layer, args, self), "meta": self.meta()}
        except ToolError as exc:
            self.telemetry.count(name, "tool_error", time.monotonic() - started, self.now())
            return _ok(mid, {"content": [{"type": "text", "text": f"{name}: {exc}"}], "isError": True})
        except Exception:
            self.telemetry.count(name, "internal_error", time.monotonic() - started, self.now())
            raise
        self.telemetry.count(name, "ok", time.monotonic() - started, self.now())
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=1)
        result = {"content": [{"type": "text", "text": text}], "isError": False}
        if self.protocol >= "2025-06-18":
            result["structuredContent"] = payload
        return _ok(mid, result)

    def _resources(self):
        out = [
            {"uri": "source://data/canonical/MANIFEST.json", "name": "manifest", "mimeType": "application/json",
             "title": "Canonical manifest: dataset_version, counts, file hashes, sources"},
            {"uri": "source://data/canonical/NOTICE.md", "name": "notice", "mimeType": "text/markdown",
             "title": "Upstream attribution notices"},
            {"uri": "source://api/v1/index.json", "name": "index", "mimeType": "application/json",
             "title": "Read API index of supported products"},
            {"uri": "source://api/v1/domains.json", "name": "domains", "mimeType": "application/json",
             "title": "Read API domain rules"},
        ]
        for row in self.layer.index["supported_saas"]:
            out.append({"uri": f"source://api/v1/saas/{row['saas_id']}", "name": f"saas-{row['saas_id']}",
                        "mimeType": "application/json", "title": f"{row['name']}: published alternatives"})
        return out

    def _read(self, mid, params):
        uri = params.get("uri")
        L = self.layer
        texts = {
            "source://data/canonical/MANIFEST.json": ("application/json", _pretty(L.manifest)),
            "source://data/canonical/NOTICE.md": ("text/markdown", L.notice),
            "source://api/v1/index.json": ("application/json", _pretty(L.index)),
            "source://api/v1/domains.json": ("application/json", _pretty(L.domains)),
        }
        for sid, doc in L.surface.items():
            texts[f"source://api/v1/saas/{sid}"] = ("application/json", _pretty(doc))
        if not isinstance(uri, str) or uri not in texts:
            return _err(mid, -32002, f"Resource not found: {uri}")
        mime, text = texts[uri]
        return _ok(mid, {"contents": [{"uri": uri, "mimeType": mime, "text": text}]})

    def serve(self, stdin, stdout):
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                out = _err(None, -32700, "Parse error")
            else:
                if isinstance(msg, list):
                    out = _err(None, -32600, "Batches are not supported")
                else:
                    try:
                        out = self.handle(msg)
                    except Exception as exc:  # never die on one bad request
                        print(f"the-source-mcp: internal error: {type(exc).__name__}", file=sys.stderr)
                        out = _err(msg.get("id") if isinstance(msg, dict) else None, -32603, "Internal error")
            if out is not None:
                stdout.write(json.dumps(out, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n")
                stdout.flush()


def _pretty(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def _tool_desc(t):
    name, title, desc, schema, _ = t
    return {"name": name, "title": title, "description": desc, "inputSchema": schema,
            "annotations": {"title": title, **READ_ONLY}}


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="the-source-mcp", description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parent.parent),
                    help="repository root holding data/canonical/ and api/v1/ (default: this checkout)")
    ap.add_argument("--now", help="pin the clock used for freshness (YYYY-MM-DDTHH:MM:SSZ)")
    ap.add_argument("--check", action="store_true", help="verify the read layer, print a summary and exit")
    a = ap.parse_args(argv)
    now = None
    if a.now:
        now = datetime.strptime(a.now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    try:
        server = Server(Path(a.root), now)
    except Exception as exc:  # a layer that cannot be loaded and verified is never served
        print(f"the-source-mcp: refusing to serve: {exc}", file=sys.stderr)
        return 2
    if a.check:
        print(json.dumps({"ok": True, "dataset_version": server.layer.dataset_version,
                          "counts": server.layer.manifest.get("counts")}, sort_keys=True))
        return 0
    print(f"the-source-mcp: serving dataset {server.layer.dataset_version} on stdio", file=sys.stderr)
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    server.serve(sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
