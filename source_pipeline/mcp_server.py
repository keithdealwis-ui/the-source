"""The Source MCP retrieval server (KEI-851, docs/MCP.md).

    python -m source_pipeline.mcp_server [--root DIR] [--now TS]
    python -m source_pipeline mcp-serve  [--root DIR] [--now TS]

A Model Context Protocol server on stdio (newline-delimited JSON-RPC 2.0). It answers
from the canonical Source read layer only: data/canonical/ (MANIFEST.json, NOTICE.md,
saas_products.jsonl, oss_projects.jsonl, relationships.jsonl) and api/v1/ (index.json,
domains.json, saas/<saas_id>.json). These are the files approved for publication under
gate-KEI-807-bfaf2354 and licensed CC BY 4.0 (DATA-LICENCE.md). Nothing else in the
repository is read: not the graph, the discover lane, staging, live snapshots, momentum
or radar.

Standard library only, read-only, no network, no wall clock in any answer except
`freshness` in source_dataset_info (pin it with --now). The layer is verified before it
is served (every MANIFEST file hash, the dataset_version derived from them, and the same
dataset_version on every api/v1 file); a layer that fails verification is never served.
When the daily cycle replaces the files, the next request verifies and switches to the
new snapshot; if the new files do not verify, the previous snapshot keeps serving and
says so.

stdout carries protocol messages only. Diagnostics go to stderr. Queries are not logged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SERVER_NAME = "the-source"
SERVER_VERSION = "1.0.0"
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

INSTRUCTIONS = (
    "The Source: evidence-backed open-source alternatives to SaaS products. Answers come from "
    "The Source's published read layer (CC BY 4.0; attribute \"" + ATTRIBUTION + "\"). "
    "Use source_get_alternatives for 'what can replace X', source_detect_saas for a URL, "
    "source_search to find products or projects, source_get_project and "
    "source_get_relationship for the evidence behind one project or pairing. Only "
    "relationships marked recommended are recommendations; Replacement Fit notes are "
    "machine-derived, not editorial. Show live_checked_at with any live figure."
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
        self.docs = []
        df = {}
        for kind, key, fields in docs:
            tf, matched_in, length = {}, {}, 0
            for field, (values, weight) in fields.items():
                for value in values:
                    toks = _tokens(value)
                    length += len(toks)
                    for t in toks:
                        tf[t] = tf.get(t, 0.0) + weight
                        matched_in.setdefault(t, set()).add(field)
            exact = {_norm(v) for field in ("name", "aliases") if field in fields for v in fields[field][0]}
            exact |= {_norm(key)}
            self.docs.append({"kind": kind, "key": key, "tf": tf, "fields": matched_in, "len": max(length, 1),
                              "exact": exact})
            for t in tf:
                df[t] = df.get(t, 0) + 1
        n = len(self.docs)
        self.avg_len = sum(d["len"] for d in self.docs) / max(n, 1)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

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
        "serves": ["data/canonical/" + f for f in ("MANIFEST.json",) + CANONICAL_FILES]
                  + ["api/v1/index.json", "api/v1/domains.json", "api/v1/saas/<saas_id>.json"],
        "does_not_serve": "the knowledge graph, the discover lane, staging, live snapshots, history, momentum, "
                          "radar, intake and internal research data; relationships withheld by the export guard",
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


def tool_get_alternatives(layer, args, server):
    sid, how = layer.resolve_saas(args.get("saas"))
    limit = _limit(args, 5)
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

    results = []
    for score, k, key, why, exact in layer.search(query, kind):
        if k == "saas":
            if project_filters:
                continue
            s = layer.saas[key]
            if supported_only and not s["supported"]:
                continue
            recs = [r for r in layer.rel_by_saas.get(key, []) if r["recommended"]]
            results.append({"kind": "saas", "id": key, "score": score, "exact_name_match": exact, "matched": why,
                            "saas": _saas_view(layer, key), "recommended_alternatives": len(recs)})
        else:
            p = layer.projects[key]
            if lic is not None and (p["licence"]["spdx"] or "").lower() != str(lic).lower():
                continue
            if status is not None and p["maintenance"]["status"] != status:
                continue
            h = p["project_health"]
            if min_health is not None and (h is None or h["score"] < min_health):
                continue
            rels = layer.rel_by_project.get(key, [])
            results.append({"kind": "project", "id": key, "score": score, "exact_name_match": exact, "matched": why,
                            "project": {"oss_id": key, "name": p["name"], "repo_url": p["repo_url"],
                                        "licence_spdx": p["licence"]["spdx"], "stars": p["live"]["stars"],
                                        "maintenance_status": p["maintenance"]["status"],
                                        "project_health": None if h is None else {
                                            k2: h[k2] for k2 in ("score", "band", "trajectory")},
                                        "live_checked_at": p["live"]["checked_at"]},
                            "recommended_for": sorted(r["saas_id"] for r in rels if r["recommended"])})
    total = len(results)
    return {"query": query, "kind": kind, "total": total, "results": results[:limit],
            "more_available": max(0, total - limit),
            "ranking": "BM25 over weighted fields (name, aliases or repository, products replaced, category, "
                       "licence), +10 for an exact name match; ties by kind then id. Lexical, deterministic."}


def tool_get_project(layer, args, server):
    pid = layer.resolve_project(args.get("project"))
    rels = sorted(layer.rel_by_project.get(pid, []), key=lambda r: (not r["recommended"], r["saas_id"]))
    return {"project": _project_view(layer.projects[pid]),
            "relationships": [_rel_summary(layer, r) for r in rels],
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
           "limit": {**LIMIT, "description": "how many recommendations (default 5)"}}, ["saas"]),
     tool_get_alternatives),
    ("source_detect_saas", "Detect the SaaS product of a URL",
     "Which supported SaaS product a URL belongs to, using The Source's published domain rules. Matched "
     "locally; the URL is never fetched.",
     _obj({"url": {"type": "string", "description": "a page URL or host, e.g. https://acme.atlassian.net/jira/"}},
          ["url"]), tool_detect_saas),
    ("source_search", "Search products and projects",
     "Search The Source's SaaS catalogue and open-source projects by words (names, aliases, categories, "
     "products replaced). Projects can be filtered by licence, maintenance status and Project Health.",
     _obj({"query": {"type": "string", "description": "words to look for, e.g. 'password manager'"},
           "kind": {"type": "string", "enum": ["any", "saas", "project"], "description": "default any"},
           "licence": {"type": "string", "description": "SPDX id, e.g. AGPL-3.0 (projects only)"},
           "maintenance_status": {"type": "string", "enum": ["active", "maintained"]},
           "min_health": {"type": "number", "minimum": 0, "maximum": 100},
           "supported_only": {"type": "boolean", "description": "SaaS results: only products with a page"},
           "limit": {**LIMIT, "description": "default 10"}}, ["query"]), tool_search),
    ("source_get_project", "One open-source project",
     "One project's live facts (licence, stars, latest release, maintenance, Project Health, when checked) and "
     "every SaaS relationship it has, recommended or not. 'project' may be an oss_id, a repository URL or a name.",
     _obj({"project": {"type": "string", "description": "e.g. github.com/docmost/docmost or Docmost"}},
          ["project"]), tool_get_project),
    ("source_get_relationship", "Evidence for one SaaS-to-project pairing",
     "The full evidence for one SaaS product / project pairing: upstream provenance (list, pinned commit, "
     "line), Replacement Fit with its dimensions, and why it is or is not recommended.",
     _obj({"saas": {"type": "string"}, "project": {"type": "string"}}, ["saas", "project"]),
     tool_get_relationship),
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
        want = schema["properties"][k].get("type")
        if want == "string" and (not isinstance(v, str) or len(v) > 2048):
            raise ToolError(f"{k} must be a string")


# --------------------------------------------------------------------------- server


class Server:
    def __init__(self, root: Path, now: datetime | None = None):
        self.root = Path(root)
        self._now = now
        self.layer = Layer(self.root)  # refuse to start on a layer that does not verify
        self.reload_error = None
        self.initialized = False
        self.protocol = None

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
        if sha == self.layer.manifest_sha256:
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
        try:
            _check_args(schema, args)
            payload = {"result": fn(self.layer, args, self), "meta": self.meta()}
        except ToolError as exc:
            return _ok(mid, {"content": [{"type": "text", "text": f"{name}: {exc}"}], "isError": True})
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
