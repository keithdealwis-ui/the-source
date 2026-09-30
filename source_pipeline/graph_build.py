"""Knowledge graph (KEI-844) — build the canonical graph from the corpus.

Pure. Reads the KEI-811 discovery corpus (data/discover/corpus/), its append-only
observation history, the KEI-805 Replace dataset (data/dataset/) with the live snapshot
it was judged on, and config/graph_ontology.yaml. Writes data/graph/. The clock is the
corpus MANIFEST's `as_of`; nothing reads the wall clock or the network, so the same
inputs produce the same bytes.

Outputs (data/graph/):
  entities.jsonl       one record per canonical entity, with the aliases resolved into it
  edges.jsonl          one record per relationship, each with assertion, confidence and evidence
  observations.jsonl   longitudinal metric observations, one per entity per as_of; never overwritten
  identity.jsonl       every alias key and the entity it resolves to, with the reason
  excluded_claims.jsonl  relationship claims that were seen and deliberately not made into edges
  retrieval/           the hybrid-retrieval representation (graph_retrieval.py)
  views/scoring_inputs.jsonl  the per-project view KEI-806 scoring consumes
  summary.json, MANIFEST.json

Evidence never points at the graph's own opinion. Every evidence item is a `ref` into a
canonical source record (dataset:file#key) plus the field and value it relied on, and
graph_build.resolve_ref() returns the record so anyone can check it.
"""
from __future__ import annotations


import re
from collections import defaultdict
from datetime import datetime

from . import common
from .graph_model import ENTITY_TYPES, RELATIONS, SCHEMA_ID, edge_id, entity_type_of, slug

CORPUS = common.DATA / "discover" / "corpus"
HISTORY = common.DATA / "discover" / "history" / "observations.jsonl"
KEI805_LIVE = common.LIVE / "snapshot.json"
ONTOLOGY = common.CONFIG / "graph_ontology.yaml"
GRAPH = common.DATA / "graph"

# ref prefix -> file. The key after '#' is the record's own id in that file.
DATASETS = {
    "kei811": CORPUS,
    "kei811-history": HISTORY.parent,
    "kei805": common.DATASET,
    "kei805-live": common.LIVE,
    "ontology": common.CONFIG,
}

TREND_MIN_SPAN_DAYS = 28


def ref(dataset: str, file: str, key: str) -> str:
    return f"{dataset}:{file}#{key}"


def ev(dataset, file, key, field=None, value=None, basis=None, as_of=None, **extra) -> dict:
    item = {"ref": ref(dataset, file, key)}
    for k, v in (("field", field), ("value", value), ("basis", basis), ("as_of", as_of)):
        if v is not None:
            item[k] = v
    item.update({k: v for k, v in extra.items() if v is not None})
    return item


# ---------------------------------------------------------------- inputs

def load_inputs() -> dict:
    return {
        "manifest": common.read_json(CORPUS / "MANIFEST.json"),
        "projects": common.read_jsonl(CORPUS / "projects.jsonl"),
        "nodes": common.read_jsonl(CORPUS / "nodes.jsonl"),
        "candidates": common.read_jsonl(CORPUS / "relationship_candidates.jsonl"),
        "history": common.read_jsonl(HISTORY),
        "kei805_manifest": common.read_json(common.DATASET / "MANIFEST.json"),
        "saas": common.read_jsonl(common.DATASET / "saas_products.jsonl"),
        "kei805_edges": common.read_jsonl(common.DATASET / "relationships.jsonl"),
        "kei805_oss": common.read_jsonl(common.DATASET / "oss_projects.jsonl"),
        "kei805_live": common.read_json(KEI805_LIVE),
        "ontology": common.load_yaml(ONTOLOGY),
    }


def input_digests() -> dict:
    files = [CORPUS / "MANIFEST.json", HISTORY, common.DATASET / "MANIFEST.json", KEI805_LIVE, ONTOLOGY]
    return {str(p.relative_to(common.ROOT)): common.sha256_file(p) for p in files}


# ---------------------------------------------------------------- ontology matching

class Ontology:
    """Compiled config/graph_ontology.yaml: topic / language / keyword / list -> concepts."""

    SECTIONS = {"technologies": ("Technology", "technology:", "USES_TECHNOLOGY"),
                "capabilities": ("Capability", "capability:", "HAS_CAPABILITY"),
                "use_cases": ("UseCase", "use-case:", "SERVES_USE_CASE")}

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.conf = cfg["confidence"]
        self.concepts = {}                       # entity id -> (section, spec)
        self.by_topic = defaultdict(list)
        self.by_language = defaultdict(list)
        self.by_list = defaultdict(list)
        self.keyword_rules = []                  # (entity id, compiled regex, pattern)
        for section, (_etype, prefix, _rel) in self.SECTIONS.items():
            for spec in cfg[section]:
                eid = prefix + spec["id"]
                if eid in self.concepts:
                    raise ValueError(f"ontology: duplicate concept {eid}")
                self.concepts[eid] = (section, spec)
                for t in spec.get("topics", []):
                    self.by_topic[t].append(eid)
                for lang in spec.get("languages", []):
                    self.by_language[lang].append(eid)
                for lst in spec.get("lists", []):
                    self.by_list[lst].append(eid)
                for pattern in spec.get("keywords", []):
                    self.keyword_rules.append((eid, re.compile(rf"(?<![\w-])(?:{pattern})(?![\w-])", re.I), pattern))
        self.saas_category_use_cases = cfg.get("saas_category_use_cases", {})
        self.integration_stoplist = set(cfg.get("integration_topic_stoplist", []))

    def relation_for(self, eid: str) -> str:
        return self.SECTIONS[self.concepts[eid][0]][2]

    def src(self, eid: str, route: str) -> str:
        section, spec = self.concepts[eid]
        return ref("ontology", "graph_ontology.yaml", f"{section}/{spec['id']}/{route}")


# ---------------------------------------------------------------- the build

class Builder:
    def __init__(self, inputs: dict):
        self.i = inputs
        self.as_of = inputs["manifest"]["as_of"]
        self.ont = Ontology(inputs["ontology"])
        self.entities: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}
        self.identity: dict[tuple, dict] = {}
        self.excluded: list[dict] = []
        self.alias_to_project: dict[str, str] = {}
        self.category_ids = self.resolve_categories()

    def resolve_categories(self) -> dict:
        """(scheme, label) -> canonical category id. Labels that mean the same category in
        different schemes resolve to one entity, named after the reviewed catalogue's label
        where the catalogue has it."""
        labels = {("catalogue", s["category"]) for s in self.i["saas"]}
        for o in self.i["kei805_oss"]:
            for c in o.get("imported", {}).get("categories", []):
                labels.add((c["source_id"], c["value"]))
        groups = defaultdict(list)
        for scheme, label in labels:
            groups[category_key(label)].append((scheme != "catalogue", scheme, label))
        out = {}
        for members in groups.values():
            _, _, preferred = sorted(members)[0]
            for _, scheme, label in members:
                out[(scheme, label)] = f"category:{slug(preferred)}"
        return out

    def category(self, scheme, label, record_ref):
        cid = self.category_ids[(scheme, label)]
        rec = self.entity(cid, label if scheme == "catalogue" or cid not in self.entities else self.entities[cid]["name"])
        if scheme == "catalogue":
            rec["name"] = label
        labels = rec["attrs"].setdefault("labels", [])
        if {"scheme": scheme, "label": label} not in labels:
            labels.append({"scheme": scheme, "label": label})
            labels.sort(key=lambda x: (x["scheme"], x["label"]))
        rec["attrs"]["schemes"] = sorted({x["scheme"] for x in labels})
        self.alias("Category", f"{scheme}:{label}", cid, "category label in this scheme (normalised match)")
        if record_ref not in rec["evidence"] and scheme == "catalogue":
            rec["evidence"].append(record_ref)
        return cid

    # -- primitives

    def entity(self, eid, name, attrs=None, evidence=None, aliases=()):
        etype = entity_type_of(eid)
        if etype is None:
            raise ValueError(f"no entity type for id {eid}")
        rec = self.entities.get(eid)
        if rec is None:
            rec = self.entities[eid] = {"id": eid, "type": etype, "name": name, "aliases": [],
                                        "attrs": {}, "evidence": []}
        rec["attrs"].update(attrs or {})
        for e in evidence or []:
            if e not in rec["evidence"]:
                rec["evidence"].append(e)
        for a in aliases:
            self.alias(etype, a, eid, "declared alias")
        return rec

    def alias(self, etype, key, eid, reason):
        k = (etype, key.lower())
        prior = self.identity.get(k)
        if prior and prior["entity"] != eid:
            raise ValueError(f"identity conflict: {etype} alias {key!r} -> {prior['entity']} and {eid}")
        if not prior:
            self.identity[k] = {"type": etype, "alias": key.lower(), "entity": eid, "reason": reason}
        rec = self.entities.get(eid)
        if rec is not None and key.lower() != eid and key.lower() not in rec["aliases"]:
            rec["aliases"].append(key.lower())

    def edge(self, relation, a, b, assertion, confidence, evidence, **attrs):
        spec = RELATIONS[relation]
        ta, tb = entity_type_of(a), entity_type_of(b)
        if ta not in spec["from"] or tb not in spec["to"]:
            raise ValueError(f"{relation}: {ta} -> {tb} is not allowed ({a} -> {b})")
        if not evidence:
            raise ValueError(f"{relation} {a} -> {b}: an edge needs evidence")
        eid = edge_id(relation, a, b)
        if spec.get("symmetric") and b < a:
            a, b = b, a
        rec = self.edges.get(eid)
        if rec is None:
            rec = self.edges[eid] = {"id": eid, "relation": relation, "from": a, "to": b,
                                     "assertion": assertion, "confidence": round(confidence, 3),
                                     "evidence": [], "attrs": {}}
        else:
            # several routes to one fact: keep every route's evidence, the strongest assertion
            # and the highest confidence, and say which route that was.
            if assertion == "explicit":
                rec["assertion"] = "explicit"
            rec["confidence"] = round(max(rec["confidence"], confidence), 3)
        for e in evidence:
            if e not in rec["evidence"]:
                rec["evidence"].append(e)
        rec["attrs"].update(attrs)
        return rec

    # -- stages

    @staticmethod
    def cross_host_merges(projects) -> dict:
        """The same owner/name on two hosts is one project (a mirror). The record with a
        numeric host id is kept, because that id survives renames; the other becomes its alias."""
        by_slug = defaultdict(list)
        for p in projects:
            by_slug["/".join(p["canonical"].lower().split("/")[1:])].append(p)
        merged = {}
        for group in by_slug.values():
            hosts = {p["canonical"].split("/", 1)[0] for p in group}
            if len(group) < 2 or len(hosts) < 2:
                continue
            primary = sorted(group, key=lambda p: (not p["id"].startswith("github:"), p["id"]))[0]
            for p in group:
                if p is not primary:
                    merged[p["id"]] = primary["id"]
        return merged

    def projects(self):
        oss_by_id = {o["oss_id"]: o for o in self.i["kei805_oss"]}
        self.merged = self.cross_host_merges(self.i["projects"])
        for p in sorted(self.i["projects"], key=lambda p: p["id"] in self.merged):
            pid = p["id"]
            if pid in self.merged:
                self.merge_project(p, self.merged[pid], oss_by_id)
                continue
            live, q = p["live"], p["quality"]
            key = pid
            src = lambda field, value=None: ev("kei811", "projects.jsonl", key, field, value)  # noqa: E731
            lic = q.get("licence") or {}
            maint = q.get("maintenance") or {}
            attrs = {
                "canonical": p["canonical"], "url": p["url"],
                "description": (p.get("descriptive") or {}).get("description"),
                "homepage": (p.get("descriptive") or {}).get("homepage"),
                "lanes": p["lanes"], "outcome": p["outcome"],
                "recommendation_eligible": p["recommendation_eligible"],
                "licence_status": lic.get("status"), "licence_spdx": lic.get("spdx"),
                "maintenance_status": maint.get("status"),
                "days_since_meaningful_activity": maint.get("days_since_meaningful_activity"),
                "stars": live.get("stars"), "forks": live.get("forks"),
                "created_at": live.get("created_at"), "pushed_at": live.get("pushed_at"),
                "tags": sorted(t.split(":", 1)[1] for t in p["entities"].get("topics", [])),
                "languages": [lang.split(":", 1)[1] for lang in p["entities"].get("languages", [])],
                "discovery_strategies": p["discovery"]["strategies"],
                "provider": live.get("provider"),
                "observed_as_of": p["freshness"].get("observed_as_of") or live.get("fetched_at"),
                "endorsed": False,
            }
            name = live.get("canonical_name") or p["canonical"].split("/", 1)[-1]
            self.entity(pid, name, attrs, [src("id", pid)])
            for addr in sorted(set(p["aliases"]) | {p["canonical"]}):
                self.alias("Project", addr, pid, "address of this repository (renames, forks and case folded by KEI-811)")
                self.alias_to_project[addr.lower()] = pid
            if p.get("replace"):
                oss_id = p["replace"]["kei805_oss_id"]
                self.alias("Project", oss_id, pid, "KEI-805 oss_id")
                self.alias_to_project[oss_id.lower()] = pid
                oss = oss_by_id.get(oss_id)
                for c in (oss or {}).get("imported", {}).get("categories", []):
                    self.upstream_category(pid, oss_id, c)
            self.project_edges(p, src)

    def merge_project(self, p, into, oss_by_id):
        reason = f"same owner/name on another host; merged into {into}"
        for addr in sorted(set(p["aliases"]) | {p["canonical"]}):
            self.alias("Project", addr, into, reason)
            self.alias_to_project[addr.lower()] = into
        self.alias("Project", p["id"], into, reason)
        rec = self.entities[into]
        rec["attrs"].setdefault("also_hosted_at", []).append(p["url"])
        rec["attrs"]["lanes"] = {k: rec["attrs"]["lanes"][k] or p["lanes"][k] for k in rec["attrs"]["lanes"]}
        rec["evidence"].append(ev("kei811", "projects.jsonl", p["id"], "canonical", p["canonical"], basis="cross_host_alias"))
        if p.get("replace"):
            oss_id = p["replace"]["kei805_oss_id"]
            self.alias("Project", oss_id, into, reason)
            self.alias_to_project[oss_id.lower()] = into
            for c in (oss_by_id.get(oss_id) or {}).get("imported", {}).get("categories", []):
                self.upstream_category(into, oss_id, c)

    def upstream_category(self, pid, oss_id, c):
        cid = self.category(c["source_id"], c["value"],
                            ev("kei805", "oss_projects.jsonl", oss_id, "imported.categories", c["value"]))
        self.edge("BELONGS_TO_CATEGORY", pid, cid, "explicit", 1.0,
                  [ev("kei805", "oss_projects.jsonl", oss_id, "imported.categories", c["value"],
                      basis="upstream_list_category", source_id=c["source_id"])])

    def project_edges(self, p, src):
        pid, live, lic = p["id"], p["live"], p["quality"].get("licence") or {}
        # OWNED_BY / MAINTAINED_BY
        owner = p["entities"].get("owner")
        if owner:
            login = owner["login"]
            host = live.get("provider") or "github"
            if owner.get("type") not in ("Organization", "User"):
                self.excluded.append({"claim": "OWNED_BY", "from": pid, "to": f"{host}:{login}",
                                      "reason": "owner_account_type_unknown",
                                      "evidence": [src("entities.owner.type", owner.get("type"))]})
                owner = None
        if owner:
            if owner.get("type") == "Organization":
                oid = f"org:{host}:{login.lower()}"
                self.entity(oid, login, {"host": host, "login": login})
            else:
                oid = f"maintainer:{host}:{login.lower()}"
                self.entity(oid, login, {"host": host, "login": login, "account_type": owner.get("type")})
            self.alias(entity_type_of(oid), f"{host}:{login}", oid, "host login (case-insensitive)")
            rel_ev = ev("kei811", "relationship_candidates.jsonl", f"{pid}|owned_by|{owner['id']}",
                        basis="host_api", as_of=self.as_of)
            self.edge("OWNED_BY", pid, oid, "explicit", 1.0, [rel_ev, src("entities.owner", login)])
            if oid.startswith("maintainer:"):
                self.edge("MAINTAINED_BY", pid, oid, "inferred", 0.7,
                          [src("entities.owner.type", owner.get("type"))],
                          rule="personal repository: the owning user account is its default maintainer")
        # LICENSED_UNDER — only a recognised licence; an unrecognised one stays unknown.
        if lic.get("recognised") and lic.get("spdx"):
            lid = f"licence:{lic['spdx']}"
            self.entity(lid, lic["spdx"], {"spdx": lic["spdx"]})
            self.edge("LICENSED_UNDER", pid, lid, "explicit", 1.0,
                      [src("quality.licence.spdx", lic["spdx"]) | {"basis": (lic.get("detection") or {}).get("method")}])
        # Ontology: languages
        for rank, lang_id in enumerate(p["entities"].get("languages", []), 1):
            lang = lang_id.split(":", 1)[1]
            for cid in self.ont.by_language.get(lang, []):
                cand = ev("kei811", "relationship_candidates.jsonl", f"{pid}|written_in|{lang_id}",
                          basis="host_api", value=lang, rank_by_size=rank)
                self.concept_edge(pid, cid, "explicit" if cid.startswith("technology:") else "inferred",
                                  self.ont.conf["host_language"] if cid.startswith("technology:") else self.ont.conf["host_topic"],
                                  [cand, {"ref": self.ont.src(cid, "languages")}])
        # topics
        for t_id in p["entities"].get("topics", []):
            topic = t_id.split(":", 1)[1]
            for cid in self.ont.by_topic.get(topic, []):
                self.concept_edge(pid, cid, "inferred", self.ont.conf["host_topic"],
                                  [ev("kei811", "relationship_candidates.jsonl", f"{pid}|has_topic|{t_id}",
                                      basis="host_api", value=topic),
                                   {"ref": self.ont.src(cid, "topics")}])
        # curated lists
        for prov in p.get("provenance", []):
            source = prov.get("source") or ""
            if not source.startswith("list:"):
                continue
            list_base = source.split("@", 1)[0]
            sid = f"source:{source}"
            self.entity(sid, list_base[5:], {"kind": "curated_list", "pinned": source})
            self.edge("LISTED_IN", pid, sid, "explicit", 1.0,
                      [ev("kei811", "relationship_candidates.jsonl", f"{pid}|listed_in|{source}",
                          field="locator", value=prov.get("locator"))])
            for cid in self.ont.by_list.get(list_base, []):
                self.concept_edge(pid, cid, "inferred", self.ont.conf["curated_list"],
                                  [ev("kei811", "projects.jsonl", pid, "provenance.source", source,
                                      locator=prov.get("locator")),
                                   {"ref": self.ont.src(cid, "lists")}])
        # description keywords
        desc = (p.get("descriptive") or {}).get("description") or ""
        if desc:
            for cid, rx, pattern in self.ont.keyword_rules:
                m = rx.search(desc)
                if m:
                    self.concept_edge(pid, cid, "inferred", self.ont.conf["description_keyword"],
                                      [src("descriptive.description", m.group(0)) | {"basis": "description_keyword"},
                                       {"ref": self.ont.src(cid, "keywords"), "value": pattern}])

    def concept_edge(self, pid, cid, assertion, confidence, evidence):
        section, spec = self.ont.concepts[cid]
        self.entity(cid, spec["name"], {"ontology_section": section})
        if section == "technologies":
            # Spellings of one technology resolve to it. For capabilities and use cases a topic
            # is evidence, not a name: one topic can support several of them.
            for t in spec.get("topics", []):
                if self.ont.by_topic[t] and [c for c in self.ont.by_topic[t] if c.startswith("technology:")] == [cid]:
                    self.alias("Technology", f"topic:{t}", cid, "ontology spelling of this technology")
            for lang in spec.get("languages", []):
                self.alias("Technology", f"language:{lang}", cid, "host language name of this technology")
        self.edge(self.ont.relation_for(cid), pid, cid, assertion, confidence, evidence)

    def saas(self):
        for s in self.i["saas"]:
            sid = f"saas:{s['saas_id']}"
            cat_id = self.category_ids[("catalogue", s["category"])]
            self.entity(sid, s["name"], {"domain": s["domain"], "category": s["category"],
                                         "record_origin": s.get("record_origin")},
                        [ev("kei805", "saas_products.jsonl", s["saas_id"], "saas_id", s["saas_id"])])
            names = {s["name"], *s.get("aliases", []), *s.get("imported", {}).get("spellings_seen", [])}
            for n in sorted(names):
                self.alias("SaaSProduct", slug(n), sid, "catalogue name, alias or upstream spelling (normalised)")
            self.category("catalogue", s["category"], ev("kei805", "saas_products.jsonl", s["saas_id"], "category", s["category"]))
            self.edge("BELONGS_TO_CATEGORY", sid, cat_id, "explicit", 1.0,
                      [ev("kei805", "saas_products.jsonl", s["saas_id"], "category", s["category"],
                          basis="reviewed_catalogue")])
            for uc in self.ont.saas_category_use_cases.get(s["category"], []):
                ucid = f"use-case:{uc}"
                if ucid not in self.ont.concepts:
                    raise ValueError(f"ontology saas_category_use_cases names unknown use case {uc}")
                self.entity(ucid, self.ont.concepts[ucid][1]["name"], {"ontology_section": "use_cases"})
                self.edge("SERVES_USE_CASE", sid, ucid, "inferred", 0.9,
                          [ev("kei805", "saas_products.jsonl", s["saas_id"], "category", s["category"]),
                           {"ref": ref("ontology", "graph_ontology.yaml", f"saas_category_use_cases/{slug(s['category'])}")}])

    @staticmethod
    def replaces_confidence(r: dict) -> float:
        n = r.get("source_count") or len(r.get("provenance", []))
        c = 0.7 if n <= 1 else 0.8 if n == 2 else 0.9
        if r["validation"].get("plausibility_review", {}).get("outcome") == "not reviewed":
            c -= 0.1
        return round(c, 3)

    def replaces(self):
        for r in self.i["kei805_edges"]:
            status = r["validation"]["status"]
            pid = self.alias_to_project.get(r["oss_id"].lower())
            sid = f"saas:{r['saas_id']}"
            reason = None
            if status not in ("validated", "flagged_licence_exception"):
                reason = f"kei805_status_{status}"
            elif pid is None:
                reason = "project_not_retained_in_corpus"
            elif sid not in self.entities:
                reason = "saas_not_in_catalogue"
            if reason:
                self.excluded.append({"claim": "REPLACES", "from": r["oss_id"], "to": sid, "reason": reason,
                                      "evidence": [ev("kei805", "relationships.jsonl", r["edge_id"], "validation.status", status)]})
                continue
            prov = [{"source_id": x["source_id"], "commit": x["source_commit"], "locator": x["locator"]}
                    for x in r["provenance"]]
            self.edge("REPLACES", pid, sid, "explicit", self.replaces_confidence(r),
                      [ev("kei805", "relationships.jsonl", r["edge_id"], "validation.status", status,
                          basis="upstream_claim+validation", as_of=r["validation"]["validated_as_of"],
                          upstream=prov)],
                      kei805_edge_id=r["edge_id"], kei805_status=status, kei805_lane=r["lane"],
                      recommendation_eligible=r["recommendation_eligible"], corroborated=r["corroborated"],
                      source_count=r["source_count"],
                      plausibility_review=r["validation"].get("plausibility_review", {}).get("outcome"))

    def derived_from_replaces(self):
        replaces = [e for e in self.edges.values() if e["relation"] == "REPLACES"]
        by_saas = defaultdict(list)
        for e in sorted(replaces, key=lambda e: e["id"]):
            by_saas[e["to"]].append(e)
            cat = self.entities[e["to"]]["attrs"]["category"]
            cat_id = self.category_ids[("catalogue", cat)]
            self.edge("BELONGS_TO_CATEGORY", e["from"], cat_id, "inferred", min(0.7, e["confidence"]),
                      [{"ref": ref("graph", "edges.jsonl", e["id"]), "basis": "replaces_saas_in_category"},
                       ev("kei805", "saas_products.jsonl", e["to"][5:], "category", cat)])
        for sid, group in by_saas.items():
            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    if a["from"] == b["from"]:
                        continue
                    self.edge("SIMILAR_TO", a["from"], b["from"], "inferred",
                              min(0.8, a["confidence"], b["confidence"]),
                              [{"ref": ref("graph", "edges.jsonl", a["id"])}, {"ref": ref("graph", "edges.jsonl", b["id"])},
                               ev("kei805", "relationships.jsonl", a["attrs"]["kei805_edge_id"]),
                               ev("kei805", "relationships.jsonl", b["attrs"]["kei805_edge_id"])],
                              rule="co_alternative")
                    self.edges[edge_id("SIMILAR_TO", a["from"], b["from"])]["attrs"].setdefault("shared_saas", [])
                    shared = self.edges[edge_id("SIMILAR_TO", a["from"], b["from"])]["attrs"]["shared_saas"]
                    if sid not in shared:
                        shared.append(sid)

    def integrations(self):
        saas_by_key = {e[5:]: e for e in self.entities if e.startswith("saas:")}
        replaced = {(e["from"], e["to"]) for e in self.edges.values() if e["relation"] == "REPLACES"}
        for p in self.i["projects"]:
            for t_id in p["entities"].get("topics", []):
                topic = t_id.split(":", 1)[1]
                sid = saas_by_key.get(topic)
                if not sid or topic in self.ont.integration_stoplist or (p["id"], sid) in replaced:
                    continue
                self.edge("INTEGRATES_WITH", p["id"], sid, "inferred", 0.7,
                          [ev("kei811", "relationship_candidates.jsonl", f"{p['id']}|has_topic|{t_id}",
                              basis="host_api", value=topic),
                           {"ref": ref("ontology", "graph_ontology.yaml", "integration_topic_stoplist"),
                            "basis": "topic equals catalogue key, not stoplisted, not an alternative"}])

    def links(self):
        for c in self.i["candidates"]:
            if c["relation"] != "readme_links_to":
                continue
            a, b = c["from"], c["to"]
            if a in self.entities and b in self.entities and a != b:
                self.edge("LINKS_TO", a, b, "explicit", 1.0,
                          [ev("kei811", "relationship_candidates.jsonl", f"{a}|readme_links_to|{b}",
                              field="locator", value=c["evidence"].get("locator"))])
            else:
                self.excluded.append({"claim": "LINKS_TO", "from": a, "to": b, "reason": "endpoint_not_retained",
                                      "evidence": [ev("kei811", "relationship_candidates.jsonl", f"{a}|readme_links_to|{b}")]})

    def similar_profiles(self, retrieval_index):
        """Profile similarity from the retrieval representation (graph_retrieval.py)."""
        for a, b, score, shared in retrieval_index.similar_pairs():
            self.edge("SIMILAR_TO", a, b, "inferred", min(0.75, score),
                      [{"ref": ref("graph", "retrieval/documents.jsonl", a), "basis": "profile_similarity"},
                       {"ref": ref("graph", "retrieval/documents.jsonl", b), "basis": "profile_similarity"}],
                      profile_similarity=round(score, 4), shared_terms=shared)

    # -- longitudinal

    def observations(self) -> list[dict]:
        rows = {}
        for h in self.i["history"]:
            pid = h["id"]
            if pid not in self.entities:
                continue
            key = (pid, h["as_of"])
            rows[key] = {"entity": pid, "as_of": h["as_of"], "source": "kei811-history",
                         "evidence": ev("kei811-history", "observations.jsonl", f"{pid}@{h['as_of']}"),
                         "metrics": {k: h.get(k) for k in ("stars", "forks", "commits_in_12m_window",
                                                            "issues_closed_12m", "pushed_at", "licence_spdx",
                                                            "archived", "latest_release")}}
        live = self.i["kei805_live"]
        for oss_id, obs in sorted(live["projects"].items()):
            pid = self.alias_to_project.get(oss_id.lower())
            if not pid or not obs.get("found"):
                continue
            as_of = obs.get("fetched_at") or live["as_of"]
            key = (pid, as_of)
            if key in rows:
                continue
            o = obs.get("observed") or {}
            rows[key] = {"entity": pid, "as_of": as_of, "source": "kei805-live",
                         "evidence": ev("kei805-live", "snapshot.json", oss_id),
                         "metrics": {"stars": obs.get("stars"), "forks": obs.get("forks"),
                                     "commits_in_12m_window": o.get("commits_in_12m_window"),
                                     "issues_closed_12m": o.get("issues_closed_12m"),
                                     "pushed_at": obs.get("pushed_at"), "licence_spdx": obs.get("licence_spdx"),
                                     "archived": obs.get("archived"), "latest_release": obs.get("latest_release")}}
        out = [rows[k] for k in sorted(rows)]
        for r in out:
            r["id"] = f"{r['entity']}@{r['as_of']}"
        return out

    def trends(self, observations):
        series = defaultdict(list)
        for o in observations:
            series[o["entity"]].append(o)
        for pid, rec in self.entities.items():
            if rec["type"] != "Project":
                continue
            pts = sorted(series.get(pid, []), key=lambda o: o["as_of"])
            trend = {"observations": len(pts), "first_observed": pts[0]["as_of"] if pts else None,
                     "last_observed": pts[-1]["as_of"] if pts else None}
            span = _days(pts[0]["as_of"], pts[-1]["as_of"]) if len(pts) >= 2 else 0
            trend["span_days"] = span
            if len(pts) < 2 or span < TREND_MIN_SPAN_DAYS:
                trend["status"] = "insufficient_history"
            else:
                first, last = pts[0]["metrics"], pts[-1]["metrics"]
                trend["status"] = "measured"
                for m in ("stars", "forks", "commits_in_12m_window"):
                    if first.get(m) is not None and last.get(m) is not None:
                        trend[f"{m}_delta"] = last[m] - first[m]
                        trend[f"{m}_per_30d"] = round((last[m] - first[m]) * 30 / span, 2)
            rec["attrs"]["trend"] = trend
            if pts:
                rec["attrs"]["first_observed"] = pts[0]["as_of"]

    # -- finish

    def finish(self):
        # every edge carries when the evidence behind it was observed, so a later build can
        # tell an edge that is new from one that has held across observations.
        for e in self.edges.values():
            src = self.entities[e["from"]]["attrs"]
            e["observed_as_of"] = src.get("observed_as_of") or self.as_of
            if src.get("first_observed"):
                e["first_observed"] = src["first_observed"]
        ents = [self.entities[k] for k in sorted(self.entities)]
        for e in ents:
            e["aliases"].sort()
        edges = [self.edges[k] for k in sorted(self.edges)]
        for e in edges:
            if "shared_saas" in e["attrs"]:
                e["attrs"]["shared_saas"].sort()
        identity = [self.identity[k] for k in sorted(self.identity)]
        return ents, edges, identity


def category_key(label: str) -> str:
    """Matching key for category labels across schemes: case, punctuation, '&' vs 'and' and
    plural word endings do not make two categories different."""
    words = re.sub(r"[^a-z0-9]+", " ", label.lower().replace("&", " and ")).split()
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "is", "us")) else w
                    for w in words)


def _days(a: str, b: str) -> int:
    fa = datetime.fromisoformat(a.replace("Z", "+00:00"))
    fb = datetime.fromisoformat(b.replace("Z", "+00:00"))
    return int((fb - fa).total_seconds() // 86400)


def build(inputs: dict, with_similarity: bool = True) -> dict:
    from . import graph_retrieval

    b = Builder(inputs)
    b.projects()
    b.saas()
    b.replaces()
    b.derived_from_replaces()
    b.integrations()
    b.links()
    observations = b.observations()
    b.trends(observations)
    index = graph_retrieval.RetrievalIndex.from_entities(b.entities, b.edges)
    if with_similarity:
        b.similar_profiles(index)
        index = graph_retrieval.RetrievalIndex.from_entities(b.entities, b.edges)
    entities, edges, identity = b.finish()
    return {"as_of": b.as_of, "entities": entities, "edges": edges, "identity": identity,
            "observations": observations, "excluded": sorted(b.excluded, key=lambda x: (x["claim"], x["from"], x["to"])),
            "index": index}


def scoring_inputs(entities: list[dict], edges: list[dict], observations: list[dict]) -> list[dict]:
    """The per-project view KEI-806 consumes: no raw discovery records needed."""
    out_edges = defaultdict(list)
    for e in edges:
        out_edges[e["from"]].append(e)
        if RELATIONS[e["relation"]].get("symmetric"):
            out_edges[e["to"]].append(e)
    obs = defaultdict(list)
    for o in observations:
        obs[o["entity"]].append({"as_of": o["as_of"], "ref": o["id"], **o["metrics"]})
    rows = []
    for ent in entities:
        if ent["type"] != "Project":
            continue
        a = ent["attrs"]
        mine = out_edges[ent["id"]]

        def rel(name):
            return sorted(({"to": e["to"] if e["from"] == ent["id"] else e["from"], "edge": e["id"],
                            "confidence": e["confidence"], "assertion": e["assertion"]} for e in mine
                           if e["relation"] == name), key=lambda x: (-x["confidence"], x["to"]))

        replaces = [dict(r, **{k: v for k, v in next(e for e in mine if e["id"] == r["edge"])["attrs"].items()
                               if k in ("kei805_status", "kei805_lane", "recommendation_eligible", "source_count",
                                        "plausibility_review")}) for r in rel("REPLACES")]
        rows.append({
            "project": ent["id"], "name": ent["name"], "canonical": a["canonical"], "url": a["url"],
            "lanes": a["lanes"], "replacement_fit_applicable": bool(replaces),
            "outcome": a["outcome"], "recommendation_eligible": a["recommendation_eligible"],
            "licence": {"status": a["licence_status"], "spdx": a["licence_spdx"]},
            "maintenance": {"status": a["maintenance_status"], "days_since_meaningful_activity": a["days_since_meaningful_activity"]},
            "observations": sorted(obs[ent["id"]], key=lambda o: o["as_of"]),
            "trend": a.get("trend"),
            "replaces": replaces,
            "integrates_with": rel("INTEGRATES_WITH"),
            "capabilities": rel("HAS_CAPABILITY"), "use_cases": rel("SERVES_USE_CASE"),
            "technologies": rel("USES_TECHNOLOGY"), "categories": rel("BELONGS_TO_CATEGORY"),
            "owner": rel("OWNED_BY"), "maintainers": rel("MAINTAINED_BY"),
            "similar_to": rel("SIMILAR_TO")[:10],
            "endorsed": False,
        })
    return rows


def run() -> dict:
    from . import graph_export

    inputs = load_inputs()
    out = build(inputs)
    GRAPH.mkdir(parents=True, exist_ok=True)
    common.write_jsonl(GRAPH / "entities.jsonl", out["entities"])
    common.write_jsonl(GRAPH / "edges.jsonl", out["edges"])
    common.write_jsonl(GRAPH / "observations.jsonl", out["observations"])
    common.write_jsonl(GRAPH / "identity.jsonl", out["identity"])
    common.write_jsonl(GRAPH / "excluded_claims.jsonl", out["excluded"])
    out["index"].write(GRAPH / "retrieval")
    common.write_jsonl(GRAPH / "views" / "scoring_inputs.jsonl",
                       scoring_inputs(out["entities"], out["edges"], out["observations"]))
    graph_export.property_graph_csv(out["entities"], out["edges"], GRAPH / "export" / "property_graph")
    summary = summarise(out)
    summary["inputs"] = input_digests()
    common.write_json(GRAPH / "summary.json", summary)
    return summary


def summarise(out) -> dict:
    by_type = defaultdict(int)
    for e in out["entities"]:
        by_type[e["type"]] += 1
    by_rel = defaultdict(lambda: defaultdict(int))
    for e in out["edges"]:
        by_rel[e["relation"]][e["assertion"]] += 1
    excl = defaultdict(int)
    for x in out["excluded"]:
        excl[f"{x['claim']}:{x['reason']}"] += 1
    trend = defaultdict(int)
    for e in out["entities"]:
        if e["type"] == "Project":
            trend[e["attrs"]["trend"]["status"]] += 1
    return {"schema": SCHEMA_ID, "as_of": out["as_of"], "publication_status": "internal_not_published",
            "endorsement": "none: graph membership and edges imply no recommendation or endorsement",
            "counts": {"entities": dict(sorted(by_type.items())),
                       "edges": {k: dict(v) for k, v in sorted(by_rel.items())},
                       "relations_defined_without_edges": sorted(set(RELATIONS) - set(by_rel)),
                       "entity_types_defined_without_entities": sorted(set(ENTITY_TYPES) - set(by_type)),
                       "identity_aliases": len(out["identity"]),
                       "observations": len(out["observations"]),
                       "excluded_claims": dict(sorted(excl.items())),
                       "project_trend": dict(sorted(trend.items()))}}


# ---------------------------------------------------------------- evidence resolution

_INDEX_CACHE: dict = {}


def _index(dataset: str, file: str) -> dict:
    key = (dataset, file)
    if key in _INDEX_CACHE:
        return _INDEX_CACHE[key]
    base = GRAPH if dataset == "graph" else DATASETS[dataset]
    path = base / file
    idx = {}
    if file == "relationship_candidates.jsonl":
        for r in common.read_jsonl(path):
            idx[f"{r['from']}|{r['relation']}|{r['to']}"] = r
    elif file == "observations.jsonl" and dataset == "kei811-history":
        for r in common.read_jsonl(path):
            idx[f"{r['id']}@{r['as_of']}"] = r
    elif file == "projects.jsonl" or file == "documents.jsonl" or file.endswith("/documents.jsonl") or file == "entities.jsonl":
        for r in common.read_jsonl(path):
            idx[r["id"]] = r
    elif file == "edges.jsonl":
        for r in common.read_jsonl(path):
            idx[r["id"]] = r
    elif file == "relationships.jsonl":
        for r in common.read_jsonl(path):
            idx[r["edge_id"]] = r
    elif file == "saas_products.jsonl":
        for r in common.read_jsonl(path):
            idx[r["saas_id"]] = r
    elif file == "oss_projects.jsonl":
        for r in common.read_jsonl(path):
            idx[r["oss_id"]] = r
    elif file == "snapshot.json":
        idx = common.read_json(path)["projects"]
    elif file == "graph_ontology.yaml":
        idx = {"__doc__": common.load_yaml(path)}
    else:
        raise KeyError(f"no resolver for {dataset}:{file}")
    _INDEX_CACHE[key] = idx
    return idx


def resolve_ref(r: str):
    """Return the canonical source record a ref points at, or raise KeyError."""
    head, key = r.split("#", 1)
    dataset, file = head.split(":", 1)
    idx = _index(dataset, file)
    if file == "graph_ontology.yaml":
        doc = idx["__doc__"]
        parts = key.split("/")
        if parts[0] in ("technologies", "capabilities", "use_cases"):
            spec = next(s for s in doc[parts[0]] if s["id"] == parts[1])
            return {k: spec[k] for k in spec if len(parts) < 3 or k in ("id", "name", parts[2])}
        if parts[0] == "saas_category_use_cases":
            return {c: v for c, v in doc[parts[0]].items() if slug(c) == parts[1]} or _missing(r)
        return {parts[0]: doc[parts[0]]}
    if key not in idx:
        _missing(r)
    return idx[key]


def _missing(r):
    raise KeyError(f"unresolvable evidence ref {r}")


