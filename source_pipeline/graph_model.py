"""Knowledge graph (KEI-844) — the canonical model: entity types, relationship types, ids.

This module is the contract. The build, the validator, the retrieval layer, the docs
(docs/GRAPH.md) and the JSON schema (schema/the-source.graph.schema.json) all read the
registry below rather than restating it, so a relationship cannot mean one thing in the
data and another in the documentation.

The model is storage-independent: entities, edges and observations are plain records
with stable string ids. Nothing here assumes a graph database, a vector index or a
particular embedding model; those are adapters over these records (graph_retrieval.py,
graph_export.py) and can be replaced without touching this file or the canonical
Source schema.
"""
from __future__ import annotations

import re

SCHEMA_ID = "the-source.graph/1"

# Entity types. `prefix` is the id namespace; a Project keeps the id the discovery corpus
# gave it (github:<host repo id>, or kei805:<address> for hosts without numeric ids), so
# scoring and the corpus join on the same key without translation.
ENTITY_TYPES = {
    "Project": {"prefix": ("github:", "kei805:"),
                "meaning": "an open-source repository retained by the KEI-811 corpus"},
    "SaaSProduct": {"prefix": ("saas:",),
                    "meaning": "a commercially operated hosted product in the reviewed KEI-805 catalogue"},
    "Organisation": {"prefix": ("org:",),
                     "meaning": "a host account of organisation type that owns at least one retained project"},
    "Maintainer": {"prefix": ("maintainer:",),
                   "meaning": "a host account of user type that owns at least one retained project"},
    "Category": {"prefix": ("category:",),
                 "meaning": "a market category from a reviewed scheme (the SaaS catalogue, or an upstream list's own taxonomy)"},
    "Capability": {"prefix": ("capability:",),
                   "meaning": "something a project can do or be deployed as (config/graph_ontology.yaml)"},
    "UseCase": {"prefix": ("use-case:",),
                "meaning": "a job a user wants done (config/graph_ontology.yaml)"},
    "Technology": {"prefix": ("technology:",),
                   "meaning": "a language, framework, runtime, datastore or protocol (config/graph_ontology.yaml)"},
    "Licence": {"prefix": ("licence:",),
                "meaning": "a recognised licence, by SPDX identifier"},
    "Source": {"prefix": ("source:",),
               "meaning": "a licence-cleared upstream list or dataset that asserted something"},
}

# Assertion kinds, strongest first. `explicit` means a named source stated the fact
# directly; `inferred` means a documented rule derived it, and the edge then carries a
# confidence below 1.0 and names the rule.
ASSERTIONS = ("explicit", "inferred")

# The relationship registry. Direction is always from -> to as written in `reads`.
RELATIONS = {
    "REPLACES": {
        "from": ["Project"], "to": ["SaaSProduct"],
        "reads": "Project REPLACES SaaSProduct",
        "semantics": "the project is a validated open-source alternative to the SaaS product",
        "assertion": "explicit",
        "evidence": "a KEI-805 relationship record (upstream claim with source commit and locator) "
                    "whose validation status is validated or flagged_licence_exception. Rejected "
                    "KEI-805 edges never become REPLACES.",
        "confidence": "0.70 for one upstream source, 0.80 for two, 0.90 for three or more; minus 0.10 "
                      "when the plausibility review has not been done. Licence status is carried as "
                      "an edge attribute, not folded into confidence.",
    },
    "SIMILAR_TO": {
        "from": ["Project"], "to": ["Project"], "symmetric": True,
        "reads": "Project SIMILAR_TO Project",
        "semantics": "the two projects serve substantially overlapping purposes",
        "assertion": "inferred",
        "evidence": "either both are validated alternatives to the same SaaS product (co_alternative), "
                    "or their descriptions and host topics are close under the retrieval model and they "
                    "share at least two specific terms (profile_similarity). The shared SaaS or terms are "
                    "recorded on the edge.",
        "confidence": "co_alternative: the lower of the two REPLACES confidences, capped at 0.80. "
                      "profile_similarity: the cosine similarity, capped at 0.75. Stored once per "
                      "unordered pair (from < to).",
    },
    "INTEGRATES_WITH": {
        "from": ["Project"], "to": ["SaaSProduct"],
        "reads": "Project INTEGRATES_WITH SaaSProduct",
        "semantics": "the project works with the SaaS product (connector, client, MCP server, plugin)",
        "assertion": "inferred",
        "evidence": "a host topic equal to the product's catalogue key, outside the ontology's "
                    "stoplist, on a project that is not an alternative to that product.",
        "confidence": "0.70",
    },
    "DEPENDS_ON": {
        "from": ["Project"], "to": ["Project"],
        "reads": "Project DEPENDS_ON Project",
        "semantics": "the project declares the other as a build or runtime dependency",
        "assertion": "explicit",
        "evidence": "a dependency manifest or SBOM entry read from the repository at a pinned commit. "
                    "The corpus does not yet read manifests, so no DEPENDS_ON edge exists; the type is "
                    "defined so that adding the evidence adds edges without a schema change.",
        "confidence": "1.0 when present",
    },
    "OWNED_BY": {
        "from": ["Project"], "to": ["Organisation", "Maintainer"],
        "reads": "Project OWNED_BY Organisation|Maintainer",
        "semantics": "the host account under which the repository lives",
        "assertion": "explicit",
        "evidence": "the host API's owner object at live.fetched_at",
        "confidence": "1.0",
    },
    "MAINTAINED_BY": {
        "from": ["Project"], "to": ["Maintainer"],
        "reads": "Project MAINTAINED_BY Maintainer",
        "semantics": "a person who keeps the project going",
        "assertion": "inferred",
        "evidence": "the repository is owned by a user account (a personal repository), so its owner "
                    "is its default maintainer. Commit authorship is not inspected, so organisation-owned "
                    "projects get no MAINTAINED_BY edge: their maintainers are unknown, not absent.",
        "confidence": "0.70",
    },
    "BELONGS_TO_CATEGORY": {
        "from": ["Project", "SaaSProduct"], "to": ["Category"],
        "reads": "Project|SaaSProduct BELONGS_TO_CATEGORY Category",
        "semantics": "the entity is in that market category",
        "assertion": "explicit or inferred",
        "evidence": "SaaS: the reviewed catalogue (explicit). Project: an upstream list's own category "
                    "for it (explicit), or the category of a SaaS product it validly REPLACES (inferred).",
        "confidence": "explicit 1.0; inferred = min(0.70, the REPLACES confidence)",
    },
    "USES_TECHNOLOGY": {
        "from": ["Project"], "to": ["Technology"],
        "reads": "Project USES_TECHNOLOGY Technology",
        "semantics": "the project is built with or on the technology",
        "assertion": "explicit or inferred",
        "evidence": "host language breakdown (explicit, 1.0), an owner-applied host topic mapped by the "
                    "ontology (inferred, 0.9), or a description keyword (inferred, 0.6)",
        "confidence": "the strongest route that produced it; every route is kept as evidence",
    },
    "HAS_CAPABILITY": {
        "from": ["Project"], "to": ["Capability"],
        "reads": "Project HAS_CAPABILITY Capability",
        "semantics": "the project offers the capability (self-hosting, an API, an MCP server ...)",
        "assertion": "inferred",
        "evidence": "an owner-applied host topic (0.9), inclusion in a curated list whose admission "
                    "criterion is that capability (0.8), host language (Dockerfile -> container "
                    "deployment, 0.9), or a description keyword (0.6)",
        "confidence": "the strongest route; every route is kept as evidence",
    },
    "SERVES_USE_CASE": {
        "from": ["Project", "SaaSProduct"], "to": ["UseCase"],
        "reads": "Project|SaaSProduct SERVES_USE_CASE UseCase",
        "semantics": "the entity is used to get that job done",
        "assertion": "inferred",
        "evidence": "Project: host topic (0.9) or description keyword (0.6) mapped by the ontology. "
                    "SaaS: its reviewed catalogue category mapped by the ontology (0.9).",
        "confidence": "the strongest route; every route is kept as evidence",
    },
    "LICENSED_UNDER": {
        "from": ["Project"], "to": ["Licence"],
        "reads": "Project LICENSED_UNDER Licence",
        "semantics": "the licence the host reports for the repository",
        "assertion": "explicit",
        "evidence": "host API licence detection, or the repository's licence file where the KEI-805/811 "
                    "licence lane recognised it. An unrecognised licence produces no edge: it is unknown.",
        "confidence": "1.0",
    },
    "LISTED_IN": {
        "from": ["Project"], "to": ["Source"],
        "reads": "Project LISTED_IN Source",
        "semantics": "a licence-cleared curated list names the project",
        "assertion": "explicit",
        "evidence": "the list at a pinned commit, with the line that names the project",
        "confidence": "1.0",
    },
    "LINKS_TO": {
        "from": ["Project"], "to": ["Project"],
        "reads": "Project LINKS_TO Project",
        "semantics": "the project's README links to the other project",
        "assertion": "explicit",
        "evidence": "the README line, recorded at discovery",
        "confidence": "1.0",
    },
}


def slug(text: str) -> str:
    """Normalised key for names: lower-case, runs of anything else become one hyphen."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def entity_type_of(entity_id: str) -> str | None:
    for etype, spec in ENTITY_TYPES.items():
        if entity_id.startswith(spec["prefix"]):
            return etype
    return None


def edge_id(relation: str, a: str, b: str) -> str:
    if RELATIONS[relation].get("symmetric") and b < a:
        a, b = b, a
    return f"{a}|{relation}|{b}"
