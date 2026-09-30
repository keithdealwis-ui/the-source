"""Knowledge graph (KEI-844) — the hybrid-retrieval representation and query engine.

A question is answered by three things working together:

  semantic   a vector per project over its name, description, host topics and resolved
             concepts. The reference model here is sublinear TF-IDF with cosine similarity,
             computed with the standard library so it is exact and reproducible. It is an
             adapter: `model` is recorded on every vector, and a dense embedding model can
             replace it by writing the same documents.jsonl shape with a different `model`.
  structured facets copied from canonical fields (lane, licence, maintenance, stars ...),
             each traceable to the field it came from (FACET_SOURCES).
  graph      edges from edges.jsonl, traversed with their confidence and evidence.

A query is a plan (config/graph_queries.yaml): a start set, then steps. Every result
carries the edges and facet values that admitted it, each pointing at a canonical source
record. Nothing is ranked by an opinion the result cannot show.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

from . import common
from .graph_model import RELATIONS, entity_type_of

MODEL = "tfidf-sublinear/1"
STOP = set("""a an and are as at be but by for from has have in into is it its of on or our that the this
to was were will with you your we can not all any via using use used based built make makes more most
new one open source simple fast easy tool tools project projects app apps application""".split())
SPECIFIC_DF_MAX = 250           # a term in more documents than this does not seed candidates
SIMILAR_TOP_K = 5
SIMILAR_MIN_COSINE = 0.35
SIMILAR_MIN_SHARED = 2

# facet -> the canonical field it is copied from (kei811:projects.jsonl record of the project)
FACET_SOURCES = {
    "lanes.replace": "lanes.replace", "lanes.discover": "lanes.discover",
    "outcome": "outcome", "recommendation_eligible": "recommendation_eligible",
    "licence_status": "quality.licence.status", "licence_spdx": "quality.licence.spdx",
    "maintenance_status": "quality.maintenance.status",
    "days_since_meaningful_activity": "quality.maintenance.days_since_meaningful_activity",
    "stars": "live.stars", "forks": "live.forks", "created_at": "live.created_at",
    "pushed_at": "live.pushed_at", "provider": "live.provider",
    "trend.status": "freshness.history (graph observations)",
    "trend.stars_per_30d": "freshness.history (graph observations)",
    "trend.commits_in_12m_window_per_30d": "freshness.history (graph observations)",
}

WORD = re.compile(r"[a-z0-9][a-z0-9+#.]*[a-z0-9+#]|[a-z0-9]")


def tokens(text: str) -> list[str]:
    return [t for t in WORD.findall((text or "").lower()) if t not in STOP and len(t) > 1]


def doc_terms(ent: dict, concept_names: dict, concepts: list[str]) -> Counter:
    a = ent["attrs"]
    c = Counter()
    for t in tokens(ent["name"].replace("-", " ").replace("_", " ")):
        c[t] += 2
    for t in tokens(a.get("description") or ""):
        c[t] += 1
    for tag in a.get("tags", []):
        c[f"tag:{tag}"] += 2
        for t in tokens(tag.replace("-", " ")):
            c[t] += 1
    for cid in concepts:
        c[f"concept:{cid}"] += 2
        for t in tokens(concept_names.get(cid, "")):
            c[t] += 1
    return c


class RetrievalIndex:
    def __init__(self, docs: dict, idf: dict):
        self.docs = docs          # id -> document
        self.idf = idf

    # -- construction

    @classmethod
    def from_entities(cls, entities: dict, edges: dict) -> "RetrievalIndex":
        ents = entities if isinstance(entities, dict) else {e["id"]: e for e in entities}
        edge_list = list(edges.values()) if isinstance(edges, dict) else edges
        concept_names = {k: v["name"] for k, v in ents.items()
                         if v["type"] in ("Capability", "UseCase", "Technology", "Category")}
        out = defaultdict(lambda: defaultdict(list))
        for e in edge_list:
            out[e["from"]][e["relation"]].append((e["to"], e["confidence"]))
            if RELATIONS[e["relation"]].get("symmetric"):
                out[e["to"]][e["relation"]].append((e["from"], e["confidence"]))
        projects = sorted(k for k, v in ents.items() if v["type"] == "Project")
        raw = {}
        for pid in projects:
            concepts = sorted({t for rel in ("HAS_CAPABILITY", "SERVES_USE_CASE", "USES_TECHNOLOGY",
                                             "BELONGS_TO_CATEGORY")
                               for t, conf in out[pid].get(rel, []) if conf >= 0.8})
            raw[pid] = doc_terms(ents[pid], concept_names, concepts)
        df = Counter()
        for terms in raw.values():
            df.update(terms.keys())
        n = len(raw) or 1
        idf = {t: round(math.log((1 + n) / (1 + d)) + 1, 6) for t, d in sorted(df.items())}
        docs = {}
        for pid in projects:
            ent = ents[pid]
            a = ent["attrs"]
            w = {t: (1 + math.log(tf)) * idf[t] for t, tf in raw[pid].items()}
            norm = math.sqrt(sum(v * v for v in w.values())) or 1.0
            graph = {rel: sorted(t for t, _ in lst) for rel, lst in sorted(out[pid].items())}
            docs[pid] = {
                "id": pid, "name": ent["name"],
                "text": " ".join(x for x in (ent["name"], a.get("description") or "", " ".join(a.get("tags", []))) if x),
                "facets": {"lanes": a["lanes"], "outcome": a["outcome"],
                           "recommendation_eligible": a["recommendation_eligible"],
                           "licence_status": a["licence_status"], "licence_spdx": a["licence_spdx"],
                           "maintenance_status": a["maintenance_status"],
                           "days_since_meaningful_activity": a["days_since_meaningful_activity"],
                           "stars": a["stars"], "forks": a["forks"], "provider": a["provider"],
                           "trend": a.get("trend")},
                "graph": graph,
                "vector": {"model": MODEL, "terms": {t: round(v / norm, 6) for t, v in sorted(w.items())}},
                "source": f"kei811:projects.jsonl#{pid}",
            }
        return cls(docs, idf)

    def write(self, path):
        common.write_jsonl(path / "documents.jsonl", [self.docs[k] for k in sorted(self.docs)])
        common.write_json(path / "model.json", {
            "model": MODEL, "documents": len(self.docs),
            "fields": {"name": 2, "description": 1, "host topic (as tag and words)": "2 / 1",
                       "resolved concepts with confidence >= 0.8 (as id and words)": "2 / 1"},
            "weighting": "sublinear tf x smoothed idf, L2-normalised; cosine similarity",
            "idf": self.idf,
            "replaceable_by": "any model that writes documents.jsonl with vector.model set to its own id; "
                              "the plan executor only calls RetrievalIndex.text_scores()",
        })

    @classmethod
    def load(cls, path) -> "RetrievalIndex":
        docs = {d["id"]: d for d in common.read_jsonl(path / "documents.jsonl")}
        return cls(docs, common.read_json(path / "model.json")["idf"])

    # -- use

    def query_vector(self, text: str) -> dict:
        tf = Counter()
        for t in tokens(text):
            tf[t] += 1
            tf[f"tag:{t}"] += 1
        for a, b in zip(tokens(text), tokens(text)[1:]):
            tf[f"tag:{a}-{b}"] += 1
        w = {t: (1 + math.log(n)) * self.idf[t] for t, n in tf.items() if t in self.idf}
        norm = math.sqrt(sum(v * v for v in w.values())) or 1.0
        return {t: v / norm for t, v in w.items()}

    def text_scores(self, text: str, ids=None) -> dict:
        q = self.query_vector(text)
        pool = ids if ids is not None else self.docs.keys()
        scores = {}
        for pid in pool:
            d = self.docs.get(pid)
            if not d:
                continue
            vec = d["vector"]["terms"]
            s = sum(v * vec.get(t, 0.0) for t, v in q.items())
            if s > 0:
                scores[pid] = s
        return scores

    def matched_terms(self, text: str, pid: str) -> list[str]:
        vec = self.docs[pid]["vector"]["terms"]
        return sorted(t for t in self.query_vector(text) if t in vec)

    def similar_pairs(self):
        """(a, b, cosine, shared specific terms) for each project's nearest profiles."""
        df = Counter()
        for d in self.docs.values():
            df.update(d["vector"]["terms"].keys())
        inverted = defaultdict(list)
        for pid, d in self.docs.items():
            for t, v in d["vector"]["terms"].items():
                if df[t] <= SPECIFIC_DF_MAX and df[t] >= 2:
                    inverted[t].append((pid, v))
        seen = set()
        out = []
        for pid in sorted(self.docs):
            vec = self.docs[pid]["vector"]["terms"]
            partial = defaultdict(float)
            shared = defaultdict(list)
            for t, v in vec.items():
                for other, w in inverted.get(t, ()):
                    if other != pid:
                        partial[other] += v * w
                        shared[other].append(t)
            cands = sorted(partial, key=lambda o: (-partial[o], o))[:30]
            scored = []
            for other in cands:
                if len(shared[other]) < SIMILAR_MIN_SHARED:
                    continue
            # exact cosine over full vectors
                ov = self.docs[other]["vector"]["terms"]
                cos = sum(v * ov.get(t, 0.0) for t, v in vec.items())
                if cos >= SIMILAR_MIN_COSINE:
                    scored.append((cos, other))
            for cos, other in sorted(scored, key=lambda x: (-x[0], x[1]))[:SIMILAR_TOP_K]:
                key = (min(pid, other), max(pid, other))
                if key in seen:
                    continue
                seen.add(key)
                out.append((key[0], key[1], cos, sorted(shared[other])[:12]))
        return out


# ---------------------------------------------------------------- the plan executor

class Graph:
    def __init__(self, entities: list[dict], edges: list[dict]):
        self.ent = {e["id"]: e for e in entities}
        self.edge = {e["id"]: e for e in edges}
        self.out = defaultdict(lambda: defaultdict(list))
        self.inc = defaultdict(lambda: defaultdict(list))
        for e in edges:
            self.out[e["from"]][e["relation"]].append(e)
            self.inc[e["to"]][e["relation"]].append(e)
            if RELATIONS[e["relation"]].get("symmetric"):
                self.out[e["to"]][e["relation"]].append(e)
                self.inc[e["from"]][e["relation"]].append(e)

    @classmethod
    def load(cls, base=None):
        from .graph_build import GRAPH
        base = base or GRAPH
        return cls(common.read_jsonl(base / "entities.jsonl"), common.read_jsonl(base / "edges.jsonl"))

    def neighbours(self, eid, relation, direction="out"):
        side = self.out if direction == "out" else self.inc
        for e in side[eid].get(relation, []):
            other = e["to"] if e["from"] == eid else e["from"]
            if direction == "in" and not RELATIONS[relation].get("symmetric"):
                other = e["from"]
            yield other, e


def get_path(obj, dotted):
    for part in dotted.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def match(value, cond: dict) -> bool:
    for op, want in cond.items():
        if op == "eq" and value != want:
            return False
        if op == "ne" and value == want:
            return False
        if op == "in" and value not in want:
            return False
        if op == "gte" and (value is None or value < want):
            return False
        if op == "lte" and (value is None or value > want):
            return False
        if op == "exists" and (value is not None) != want:
            return False
        if op == "contains" and (value is None or want not in value):
            return False
    return True


class Row:
    __slots__ = ("id", "via", "edges", "facets", "score", "notes", "members")

    def __init__(self, eid, via=None, edges=None, facets=None, score=0.0):
        self.id, self.via = eid, via
        self.edges = list(edges or [])
        self.facets = list(facets or [])
        self.score = score
        self.notes = []
        self.members = []


class Executor:
    def __init__(self, graph: Graph, index: RetrievalIndex):
        self.g, self.ix = graph, index

    def entity_facet(self, eid, field):
        ent = self.g.ent[eid]
        if field in ("id", "name", "type"):
            return ent[field]
        return get_path(ent["attrs"], field)

    def facet_evidence(self, eid, field, value):
        ent = self.g.ent[eid]
        if ent["type"] == "Project":
            return {"entity": eid, "facet": field, "value": value,
                    "ref": f"kei811:projects.jsonl#{eid}", "source_field": FACET_SOURCES.get(field, field)}
        return {"entity": eid, "facet": field, "value": value,
                "ref": (ent["evidence"][0]["ref"] if ent["evidence"] else f"graph:entities.jsonl#{eid}")}

    def edge_ok(self, e, step):
        return e["confidence"] >= step.get("min_confidence", 0.0) and \
            (not step.get("assertion") or e["assertion"] == step["assertion"])

    def path_exists(self, eid, path, depth=0):
        """Return the edge chain for the first satisfying walk along `path`, or None."""
        if depth == len(path):
            return []
        hop = path[depth]
        for other, e in sorted(self.g.neighbours(eid, hop["relation"], hop.get("direction", "out")),
                               key=lambda x: (-x[1]["confidence"], x[0])):
            if not self.edge_ok(e, hop):
                continue
            if "to" in hop and other not in (hop["to"] if isinstance(hop["to"], list) else [hop["to"]]):
                continue
            if "to_type" in hop and self.g.ent.get(other, {}).get("type") != hop["to_type"]:
                continue
            if "where" in hop and not all(match(self.entity_facet(other, f), c) for f, c in hop["where"].items()):
                continue
            rest = self.path_exists(other, path, depth + 1)
            if rest is not None:
                return [e] + rest
        return None

    def run(self, plan: dict) -> dict:
        start = plan["start"]
        if "ids" in start:
            rows = [Row(i) for i in start["ids"] if i in self.g.ent]
        else:
            rows = [Row(i) for i, e in sorted(self.g.ent.items()) if e["type"] == start["type"]]
        trace = [{"step": "start", "rows": len(rows)}]
        for step in plan.get("steps", []):
            op = step["op"]
            if op == "where":
                keep = []
                for r in rows:
                    v = self.entity_facet(r.id, step["facet"])
                    if match(v, step["cond"]):
                        r.facets.append(self.facet_evidence(r.id, step["facet"], v))
                        keep.append(r)
                rows = keep
            elif op in ("require", "exclude"):
                keep = []
                for r in rows:
                    chain = self.path_exists(r.id, step["path"])
                    if (chain is not None) == (op == "require"):
                        if chain:
                            r.edges.extend(chain)
                        keep.append(r)
                rows = keep
            elif op == "traverse":
                nxt = {}
                for r in rows:
                    for other, e in self.g.neighbours(r.id, step["relation"], step.get("direction", "out")):
                        if not self.edge_ok(e, step):
                            continue
                        if "to_type" in step and self.g.ent.get(other, {}).get("type") != step["to_type"]:
                            continue
                        if other in step.get("exclude_ids", []):
                            continue
                        prev = nxt.get(other)
                        cand = Row(other, via=r.id, edges=r.edges + [e], facets=r.facets, score=r.score)
                        if prev is None or e["confidence"] > prev.edges[-1]["confidence"]:
                            cand.members = (prev.members if prev else []) + [r.id]
                            nxt[other] = cand
                        else:
                            prev.members.append(r.id)
                rows = [nxt[k] for k in sorted(nxt)]
            elif op == "text":
                scores = self.ix.text_scores(step["query"], [r.id for r in rows])
                keep = []
                for r in rows:
                    s = scores.get(r.id, 0.0)
                    if s >= step.get("min_score", 0.0):
                        r.score = s
                        r.notes.append({"semantic_score": round(s, 4), "model": MODEL,
                                        "matched_terms": self.ix.matched_terms(step["query"], r.id)[:12],
                                        "ref": f"graph:retrieval/documents.jsonl#{r.id}"})
                        keep.append(r)
                rows = keep
            elif op == "group":
                groups = defaultdict(list)
                for r in rows:
                    for other, e in self.g.neighbours(r.id, step["relation"], step.get("direction", "out")):
                        if self.edge_ok(e, step):
                            groups[other].append((r, e))
                rows = []
                for gid, members in sorted(groups.items()):
                    if len(members) >= step.get("min_count", 1):
                        g = Row(gid, score=len(members))
                        g.members = sorted(m.id for m, _ in members)
                        g.edges = [e for _, e in members][: step.get("evidence_per_group", 5)]
                        rows.append(g)
            elif op == "rank":
                key = step["by"]
                if key == "score":
                    rows.sort(key=lambda r: (-r.score, r.id))
                elif key in ("id", "name"):
                    rows.sort(key=lambda r: (self.entity_facet(r.id, key), r.id))
                else:
                    rows.sort(key=lambda r: (-(self.entity_facet(r.id, key) or 0), r.id))
            else:
                raise ValueError(f"unknown plan op {op}")
            trace.append({"step": op, "rows": len(rows)})
        limit = plan.get("limit", 10)
        return {"total": len(rows), "trace": trace, "results": [self.render(r) for r in rows[:limit]]}

    def render(self, r: Row) -> dict:
        ent = self.g.ent[r.id]
        out = {"id": r.id, "type": ent["type"], "name": ent["name"], "score": round(r.score, 4),
               "evidence": {
                   "edges": [{"edge": e["id"], "relation": e["relation"], "assertion": e["assertion"],
                              "confidence": e["confidence"], "sources": e["evidence"]} for e in r.edges],
                   "facets": r.facets, "semantic": r.notes}}
        if r.members:
            out["members"] = r.members[:20]
            out["member_count"] = len(r.members)
        if ent["type"] == "Project":
            out["url"] = ent["attrs"]["url"]
            out["record"] = f"kei811:projects.jsonl#{r.id}"
        elif ent["evidence"]:
            out["record"] = ent["evidence"][0]["ref"]
        return out


def entity_type(eid):
    return entity_type_of(eid)
