"""Knowledge graph (KEI-844) — validate the graph against the ticket's acceptance boundary.

Each check names the criterion it evidences. Writes data/graph/reports/validation_report.json,
then MANIFEST.json over every graph file.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict

import jsonschema

from . import common, graph_export, graph_queries
from .graph_build import (CORPUS, GRAPH, Builder, build, category_key, load_inputs, resolve_ref,
                          scoring_inputs)
from .graph_model import ENTITY_TYPES, RELATIONS, SCHEMA_ID, entity_type_of


def _digest(rows) -> str:
    h = hashlib.sha256()
    for r in rows:
        h.update(common.dumps(r).encode())
        h.update(b"\n")
    return h.hexdigest()


def write_manifest(as_of):
    files = {}
    for path in sorted(GRAPH.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.json":
            files[str(path.relative_to(GRAPH))] = common.sha256_file(path)
    manifest = {"as_of": as_of, "schema": SCHEMA_ID, "publication_status": "internal_not_published",
                "sha256": files}
    common.write_json(GRAPH / "MANIFEST.json", manifest)
    return manifest


def run(queries: dict | None = None) -> dict:
    entities = common.read_jsonl(GRAPH / "entities.jsonl")
    edges = common.read_jsonl(GRAPH / "edges.jsonl")
    observations = common.read_jsonl(GRAPH / "observations.jsonl")
    identity = common.read_jsonl(GRAPH / "identity.jsonl")
    docs = common.read_jsonl(GRAPH / "retrieval" / "documents.jsonl")
    scoring = common.read_jsonl(GRAPH / "views" / "scoring_inputs.jsonl")
    summary = common.read_json(GRAPH / "summary.json")
    corpus = common.read_jsonl(CORPUS / "projects.jsonl")
    ent = {e["id"]: e for e in entities}
    projects = [e for e in entities if e["type"] == "Project"]
    checks = []

    def check(cid, criterion, ok, detail):
        checks.append({"id": cid, "criterion": criterion, "ok": bool(ok), "detail": detail})

    # AC1 — deterministic ingestion of the whole retained corpus.
    inputs = load_inputs()
    corpus_manifest = inputs["manifest"]
    now = {f: common.sha256_file(CORPUS / f) for f in corpus_manifest["sha256"]}
    check("corpus_matches_its_manifest", "AC1", now == corpus_manifest["sha256"],
          {"files": len(now), "changed": sorted(f for f in now if now[f] != corpus_manifest["sha256"][f])})
    kei805_manifest = inputs["kei805_manifest"]
    now805 = {f: common.sha256_file(common.DATASET / f) for f in kei805_manifest["sha256"]}
    check("kei805_dataset_matches_its_manifest", "AC1", now805 == kei805_manifest["sha256"],
          {"files": len(now805)})
    alias_map = {(i["type"], i["alias"]): i["entity"] for i in identity}
    missing = [p["id"] for p in corpus if p["id"] not in ent and ("Project", p["id"].lower()) not in alias_map]
    check("every_retained_project_ingested", "AC1", not missing and len(corpus) == len(projects) + sum(
        1 for p in corpus if p["id"] not in ent),
          {"corpus_projects": len(corpus), "project_entities": len(projects), "missing": missing[:10]})
    rebuilt = build(inputs)
    same = all(_digest(rebuilt[k]) == _digest(v) for k, v in
               (("entities", entities), ("edges", edges), ("observations", observations), ("identity", identity)))
    check("rebuild_is_byte_identical", "AC1", same,
          {"entities": _digest(entities)[:16], "edges": _digest(edges)[:16],
           "note": "graph rebuilt in-process from the same inputs and compared record by record"})
    check("build_inputs_are_pinned", "AC1", summary.get("inputs") and all(summary["inputs"].values()),
          summary.get("inputs"))

    # AC2 — canonical identity: no obvious duplicates.
    slugs = Counter("/".join(e["attrs"]["canonical"].lower().split("/")[1:]) for e in projects)
    check("no_duplicate_projects_by_owner_name", "AC2", all(n == 1 for n in slugs.values()),
          {"duplicates": [s for s, n in slugs.items() if n > 1][:10],
           "cross_host_merges": [i for i in identity if i["reason"].startswith("same owner/name")][:5]})
    host_ids = Counter(e["id"] for e in projects)
    check("project_ids_unique", "AC2", all(n == 1 for n in host_ids.values()), {"projects": len(host_ids)})
    renamed = [p for p in corpus if len(p["aliases"]) > 1]
    resolved = all(alias_map.get(("Project", a.lower())) in ent for p in renamed for a in p["aliases"])
    check("renamed_repositories_resolve_to_one_project", "AC2", resolved,
          {"projects_with_several_addresses": len(renamed)})
    for etype, keyf in (("Category", lambda e: category_key(e["name"])),
                        ("Organisation", lambda e: e["attrs"]["login"].lower()),
                        ("Maintainer", lambda e: e["attrs"]["login"].lower()),
                        ("Technology", lambda e: e["name"].lower()),
                        ("SaaSProduct", lambda e: e["name"].lower())):
        keys = Counter(keyf(e) for e in entities if e["type"] == etype)
        check(f"no_duplicate_{etype.lower()}_names", "AC2", all(n == 1 for n in keys.values()),
              {"entities": sum(keys.values()), "duplicates": [k for k, n in keys.items() if n > 1][:10]})
    cats = [e for e in entities if e["type"] == "Category"]
    check("category_labels_merged_across_schemes", "AC2", any(len(c["attrs"]["schemes"]) > 1 for c in cats),
          {"categories": len(cats), "multi_scheme": sum(1 for c in cats if len(c["attrs"]["schemes"]) > 1),
           "labels_folded": sum(len(c["attrs"]["labels"]) for c in cats)})
    langs = [i for i in identity if i["type"] == "Technology" and i["alias"].startswith(("topic:", "language:"))]
    check("technology_spellings_resolve", "AC2",
          alias_map.get(("Technology", "topic:golang")) == alias_map.get(("Technology", "language:go")) == "technology:go"
          and alias_map.get(("Technology", "topic:postgres")) == "technology:postgresql",
          {"technology_aliases": len(langs)})
    saas_alias = [i for i in identity if i["type"] == "SaaSProduct"]
    check("saas_spellings_resolve", "AC2", len(saas_alias) >= len([e for e in entities if e["type"] == "SaaSProduct"]),
          {"saas_aliases": len(saas_alias)})

    # AC3 — relationship types defined.
    fields = ("from", "to", "reads", "semantics", "assertion", "evidence", "confidence")
    check("relation_registry_complete", "AC3", all(all(spec.get(f) for f in fields) for spec in RELATIONS.values()),
          {"relations": sorted(RELATIONS)})
    bad_ends = [e["id"] for e in edges if entity_type_of(e["from"]) not in RELATIONS[e["relation"]]["from"]
                or entity_type_of(e["to"]) not in RELATIONS[e["relation"]]["to"]]
    check("edges_respect_declared_direction_and_types", "AC3", not bad_ends, {"violations": bad_ends[:10]})
    doc = (common.ROOT / "docs" / "GRAPH.md").read_text() if (common.ROOT / "docs" / "GRAPH.md").exists() else ""
    check("every_relation_documented", "AC3", all(f"`{r}`" in doc for r in RELATIONS),
          {"missing": [r for r in RELATIONS if f"`{r}`" not in doc]})
    check("ticket_relations_all_defined", "AC3",
          {"REPLACES", "SIMILAR_TO", "INTEGRATES_WITH", "DEPENDS_ON", "MAINTAINED_BY", "OWNED_BY",
           "BELONGS_TO_CATEGORY", "USES_TECHNOLOGY", "SERVES_USE_CASE"} <= set(RELATIONS),
          {"defined_without_edges": summary["counts"]["relations_defined_without_edges"]})

    # AC4 — evidence and confidence on every edge; every ref resolves.
    no_ev = [e["id"] for e in edges if not e["evidence"]]
    check("every_edge_has_evidence", "AC4", not no_ev, {"edges": len(edges), "without": no_ev[:10]})
    bad_conf = [e["id"] for e in edges if not (0 < e["confidence"] <= 1)
                or (e["assertion"] == "inferred" and e["confidence"] >= 1)]
    check("inferred_edges_carry_confidence_below_one", "AC4", not bad_conf, {"violations": bad_conf[:10]})
    unresolved, n = [], 0
    for e in edges:
        for item in e["evidence"]:
            n += 1
            try:
                resolve_ref(item["ref"])
            except (KeyError, StopIteration) as err:
                unresolved.append(str(err))
    for o in observations:
        n += 1
        try:
            resolve_ref(o["evidence"]["ref"])
        except KeyError as err:
            unresolved.append(str(err))
    check("every_evidence_ref_resolves", "AC4", not unresolved, {"refs": n, "unresolved": unresolved[:10]})
    by_assert = Counter((e["relation"], e["assertion"]) for e in edges)
    check("explicit_and_inferred_are_distinguished", "AC4", by_assert[("REPLACES", "explicit")] > 0
          and by_assert[("SIMILAR_TO", "inferred")] > 0,
          {f"{r}:{a}": c for (r, a), c in sorted(by_assert.items())})

    # AC5 — Replace and Discover lanes distinct; no forced SaaS edge.
    rep_from = defaultdict(int)
    for e in edges:
        if e["relation"] == "REPLACES":
            rep_from[e["from"]] += 1
    wrong_lane = [p for p in rep_from if not ent[p]["attrs"]["lanes"]["replace"]]
    check("replaces_edges_only_on_replace_lane", "AC5", not wrong_lane, {"violations": wrong_lane[:10]})
    discover_only = [p for p in projects if p["attrs"]["lanes"]["discover"] and not p["attrs"]["lanes"]["replace"]]
    check("discover_only_projects_have_no_saas_replacement", "AC5",
          discover_only and not any(p["id"] in rep_from for p in discover_only),
          {"discover_only_projects": len(discover_only)})
    rejected = [x for x in common.read_jsonl(GRAPH / "excluded_claims.jsonl") if x["claim"] == "REPLACES"]
    check("rejected_replace_claims_excluded", "AC5", rejected and all(x["reason"].startswith("kei805_status") or
                                                                     x["reason"] == "project_not_retained_in_corpus"
                                                                     for x in rejected),
          {"excluded": len(rejected)})

    # AC6 — traversal across all entity types.
    type_pairs = Counter((entity_type_of(e["from"]), e["relation"], entity_type_of(e["to"])) for e in edges)
    reachable = {t for (a, _, b) in type_pairs for t in (a, b)}
    wanted = {"Project", "Organisation", "Maintainer", "Category", "Capability", "UseCase", "Technology", "Licence",
              "SaaSProduct"}
    check("all_entity_types_connected", "AC6", wanted <= reachable,
          {f"{a}-{r}->{b}": c for (a, r, b), c in sorted(type_pairs.items())})

    # AC7 — longitudinal.
    obs_ids = Counter(o["id"] for o in observations)
    per = Counter(o["entity"] for o in observations)
    check("observations_are_keyed_by_time_not_overwritten", "AC7",
          all(n == 1 for n in obs_ids.values()) and max(per.values()) >= 2,
          {"observations": len(observations), "entities_with_two_or_more": sum(1 for v in per.values() if v >= 2),
           "sources": dict(Counter(o["source"] for o in observations))})
    check("edges_carry_observation_time", "AC7", all(e.get("observed_as_of") for e in edges), {})
    check("trend_status_is_explicit", "AC7", all(p["attrs"].get("trend", {}).get("status") for p in projects),
          summary["counts"]["project_trend"])

    # AC8 — retrieval representation.
    check("retrieval_document_per_project", "AC8", {d["id"] for d in docs} == {p["id"] for p in projects},
          {"documents": len(docs)})
    check("retrieval_documents_are_hybrid", "AC8",
          all(d["vector"]["terms"] and d["vector"]["model"] and d["facets"] and "graph" in d for d in docs),
          {"model": docs[0]["vector"]["model"] if docs else None})

    # AC9 / AC10 — query suite.
    q = queries or graph_queries.run()
    check("at_least_20_queries_all_pass", "AC9", q["queries"] >= 20 and q["ok"],
          {"queries": q["queries"], "failed": [r["id"] for r in q["report"] if not r["ok"]]})
    check("multi_hop_queries_exercised", "AC9", q["multi_hop"] >= 5, {"multi_hop": q["multi_hop"]})
    check("query_results_resolve_to_source_records", "AC10",
          all(not r["unresolved"] for r in q["report"]) and sum(r["evidence_refs_resolved"] for r in q["report"]) > 0,
          {"refs_resolved": sum(r["evidence_refs_resolved"] for r in q["report"])})

    # AC11 — implementation independence.
    schema = common.read_json(common.SCHEMA / "the-source.graph.schema.json")
    errs = []
    for kind, rows in (("entity", entities), ("edge", edges), ("observation", observations)):
        # no $id: older jsonschema releases resolve a local $ref against $id as if it were a URL
        v = jsonschema.Draft202012Validator({**{k: x for k, x in schema.items() if k != "$id"},
                                             "$ref": f"#/$defs/{kind}"})
        for r in rows:
            for err in v.iter_errors(r):
                errs.append(f"{kind} {r.get('id')}: {err.message}")
                break
            if len(errs) > 10:
                break
    check("records_validate_against_storage_independent_schema", "AC11", not errs, {"errors": errs[:10]})
    rt_ents, rt_edges = graph_export.read_property_graph_csv(GRAPH / "export" / "property_graph")
    strip = lambda rows: [{k: v for k, v in r.items() if k not in ("observed_as_of", "first_observed")} for r in rows]  # noqa: E731
    check("property_graph_export_round_trips", "AC11",
          _digest(rt_ents) == _digest(entities) and _digest(rt_edges) == _digest(strip(edges)),
          {"nodes": len(rt_ents), "relationships": len(rt_edges)})

    # AC12 — scoring can consume the graph.
    expected = scoring_inputs(entities, edges, observations)
    check("scoring_view_covers_every_project", "AC12", _digest(expected) == _digest(scoring)
          and len(scoring) == len(projects), {"rows": len(scoring)})
    check("replacement_fit_only_where_a_saas_edge_exists", "AC12",
          all(r["replacement_fit_applicable"] == bool(r["replaces"]) for r in scoring)
          and all(x["confidence"] and x["edge"] for r in scoring for x in r["replaces"]),
          {"applicable": sum(r["replacement_fit_applicable"] for r in scoring),
           "not_applicable": sum(not r["replacement_fit_applicable"] for r in scoring)})
    check("nothing_endorsed", "AC12", not any(r["endorsed"] for r in scoring) and
          not any(p["attrs"].get("endorsed") for p in projects), {})

    check("entity_types_registered", "AC3", {e["type"] for e in entities} <= set(ENTITY_TYPES), {})
    report = {"schema": SCHEMA_ID, "as_of": summary["as_of"], "ok": all(c["ok"] for c in checks), "checks": checks}
    common.write_json(GRAPH / "reports" / "validation_report.json", report)
    write_manifest(summary["as_of"])
    return report


__all__ = ["run", "Builder"]
