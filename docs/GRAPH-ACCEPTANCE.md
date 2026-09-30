# KEI-844 — acceptance map

Graph `as_of` 2026-09-29T10:34:38Z (the corpus clock). Schema `the-source.graph/1`.
Validation: **42/42 checks pass.**
Query suite: **24 questions, 11 multi-hop, all pass.**
Status: internal, not published, nothing endorsed.

Regenerate with `python3 scripts/graph_acceptance.py` after `python -m source_pipeline graph-build`.

## The graph

| Entity type | Count |
|---|---|
| Capability | 17 |
| Category | 322 |
| Licence | 21 |
| Maintainer | 4,212 |
| Organisation | 3,245 |
| Project | 9,363 |
| SaaSProduct | 268 |
| Source | 6 |
| Technology | 49 |
| UseCase | 22 |

| Relationship | explicit | inferred |
|---|---|---|
| `BELONGS_TO_CATEGORY` | 1,074 | 361 |
| `HAS_CAPABILITY` | 0 | 16,790 |
| `INTEGRATES_WITH` | 0 | 755 |
| `LICENSED_UNDER` | 7,970 | 0 |
| `LINKS_TO` | 457 | 0 |
| `LISTED_IN` | 4,104 | 0 |
| `MAINTAINED_BY` | 0 | 5,047 |
| `OWNED_BY` | 9,359 | 0 |
| `REPLACES` | 690 | 0 |
| `SERVES_USE_CASE` | 0 | 7,747 |
| `SIMILAR_TO` | 0 | 4,726 |
| `USES_TECHNOLOGY` | 23,256 | 10,216 |

Defined with no edges yet (the evidence is not collected): `DEPENDS_ON`.
Identity aliases resolved: 17,855. Observations: 9,816.

Claims seen and not made into edges:

| Claim : reason | Count |
|---|---|
| `LINKS_TO:endpoint_not_retained` | 40 |
| `OWNED_BY:owner_account_type_unknown` | 4 |
| `REPLACES:kei805_status_rejected` | 158 |

## Criterion by criterion

### AC1 — the retained corpus ingests deterministically
- `corpus_matches_its_manifest` **PASS** — `{"changed": [], "files": 8}`
- `kei805_dataset_matches_its_manifest` **PASS** — `{"files": 9}`
- `every_retained_project_ingested` **PASS** — `{"corpus_projects": 9364, "missing": [], "project_entities": 9363}`
- `rebuild_is_byte_identical` **PASS** — `{"edges": "0a4ea0f93d1466dd", "entities": "f9b31f13e8660d3f", "note": "graph rebuilt in-process from the same inputs and compared record by record"}`
- `build_inputs_are_pinned` **PASS** — `{"config/graph_ontology.yaml": "2a089ea516a3e38e83609447099525ba0ef94ed430130ecb71f91a3b9f466048", "data/dataset/MANIFEST.json": "ec37ae4adb012f5c669d759666fb99e300f1e2b8915235f1c1779b0a4d8c3672", "data/discover/corpus/MANIFEST.json": "d39f`

### AC2 — canonical identities; no obvious duplicates
- `no_duplicate_projects_by_owner_name` **PASS** — `{"cross_host_merges": [{"alias": "gitlab.com/baserow/baserow", "entity": "github:281073868", "reason": "same owner/name on another host; merged into github:281073868", "type": "Project"}, {"alias": "kei805:gitlab.com/baserow/baserow", "enti`
- `project_ids_unique` **PASS** — `{"projects": 9363}`
- `renamed_repositories_resolve_to_one_project` **PASS** — `{"projects_with_several_addresses": 197}`
- `no_duplicate_category_names` **PASS** — `{"duplicates": [], "entities": 322}`
- `no_duplicate_organisation_names` **PASS** — `{"duplicates": [], "entities": 3245}`
- `no_duplicate_maintainer_names` **PASS** — `{"duplicates": [], "entities": 4212}`
- `no_duplicate_technology_names` **PASS** — `{"duplicates": [], "entities": 49}`
- `no_duplicate_saasproduct_names` **PASS** — `{"duplicates": [], "entities": 268}`
- `category_labels_merged_across_schemes` **PASS** — `{"categories": 322, "labels_folded": 395, "multi_scheme": 47}`
- `technology_spellings_resolve` **PASS** — `{"technology_aliases": 131}`
- `saas_spellings_resolve` **PASS** — `{"saas_aliases": 310}`

### AC3 — relationship types defined with direction, semantics and evidence requirements
Defined in `source_pipeline/graph_model.py` and documented in `docs/GRAPH.md` (reference section generated from it).
- `relation_registry_complete` **PASS** — `{"relations": ["BELONGS_TO_CATEGORY", "DEPENDS_ON", "HAS_CAPABILITY", "INTEGRATES_WITH", "LICENSED_UNDER", "LINKS_TO", "LISTED_IN", "MAINTAINED_BY", "OWNED_BY", "REPLACES", "SERVES_USE_CASE", "SIMILAR_TO", "USES_TECHNOLOGY"]}`
- `edges_respect_declared_direction_and_types` **PASS** — `{"violations": []}`
- `every_relation_documented` **PASS** — `{"missing": []}`
- `ticket_relations_all_defined` **PASS** — `{"defined_without_edges": ["DEPENDS_ON"]}`
- `entity_types_registered` **PASS** — `{}`

### AC4 — provenance on every material edge; confidence where inferred
- `every_edge_has_evidence` **PASS** — `{"edges": 92552, "without": []}`
- `inferred_edges_carry_confidence_below_one` **PASS** — `{"violations": []}`
- `every_evidence_ref_resolves` **PASS** — `{"refs": 230814, "unresolved": []}`
- `explicit_and_inferred_are_distinguished` **PASS** — `{"BELONGS_TO_CATEGORY:explicit": 1074, "BELONGS_TO_CATEGORY:inferred": 361, "HAS_CAPABILITY:inferred": 16790, "INTEGRATES_WITH:inferred": 755, "LICENSED_UNDER:explicit": 7970, "LINKS_TO:explicit": 457, "LISTED_IN:explicit": 4104, "MAINTAINE`

### AC5 — Replace and Discover lanes stay distinguishable
- `replaces_edges_only_on_replace_lane` **PASS** — `{"violations": []}`
- `discover_only_projects_have_no_saas_replacement` **PASS** — `{"discover_only_projects": 8878}`
- `rejected_replace_claims_excluded` **PASS** — `{"excluded": 158}`

### AC6 — traversal across project, organisation, maintainer, category, capability, use case, technology and licence
- `all_entity_types_connected` **PASS** — `{"Project-BELONGS_TO_CATEGORY->Category": 1167, "Project-HAS_CAPABILITY->Capability": 16790, "Project-INTEGRATES_WITH->SaaSProduct": 755, "Project-LICENSED_UNDER->Licence": 7970, "Project-LINKS_TO->Project": 457, "Project-LISTED_IN->Source"`

### AC7 — longitudinal observations, not latest-state overwrite
- `observations_are_keyed_by_time_not_overwritten` **PASS** — `{"entities_with_two_or_more": 453, "observations": 9816, "sources": {"kei805-live": 457, "kei811-history": 9359}}`
- `edges_carry_observation_time` **PASS** — `{}`
- `trend_status_is_explicit` **PASS** — `{"insufficient_history": 9363}`

The model keeps every observation. The current data has at most two observations per project, less than 28 days apart, so every trend is `insufficient_history`: trend questions are supported but not yet answerable, and are reported as such.

### AC8 — a retrieval representation for hybrid search
- `retrieval_document_per_project` **PASS** — `{"documents": 9363}`
- `retrieval_documents_are_hybrid` **PASS** — `{"model": "tfidf-sublinear/1"}`

### AC9 — at least 20 natural-language questions exercised, including multi-hop
- `at_least_20_queries_all_pass` **PASS** — `{"failed": [], "queries": 24}`
- `multi_hop_queries_exercised` **PASS** — `{"multi_hop": 11}`

| # | Question | Kind | Multi-hop | Results | Evidence refs resolved | |
|---|---|---|---|---|---|---|
| q01 | Find actively maintained open-source tools that replace expensive SaaS products, support self-hosting, expose APIs, fit AI-agent workflows and show growing contributor activity. | multi-constraint (the ticket's reference question) |  | 4 | 64 | PASS |
| q02 | What are the open-source alternatives to Notion? | single-hop reverse |  | 12 | 30 | PASS |
| q03 | Which self-hostable Slack alternatives are licensed under AGPL or GPL? | multi-constraint with licence |  | 1 | 5 | PASS |
| q04 | Which SaaS categories have the most validated open-source alternatives? | two-hop aggregate | yes | 44 | 110 | PASS |
| q05 | Show MCP servers that let an AI agent work with Notion, Slack or GitHub. | capability + integration |  | 25 | 85 | PASS |
| q06 | Find Discover-lane projects for agent long-term memory that run locally. | lane + use case + capability |  | 86 | 89 | PASS |
| q07 | Which projects are similar to Plausible Analytics? | similarity traversal |  | 15 | 96 | PASS |
| q08 | Which organisations own three or more actively maintained MCP servers? | aggregate over owners | yes | 14 | 160 | PASS |
| q09 | What else do the teams behind the open-source Trello alternatives build? | three-hop (SaaS <- REPLACES - project - OWNED_BY -> owner <- OWNED_BY - project) | yes | 1 | 9 | PASS |
| q10 | Rust-based tools that replace a SaaS observability product. | technology + two-hop SaaS category | yes | 3 | 29 | PASS |
| q11 | Open-source web analytics that respect privacy. | hybrid semantic + graph |  | 10 | 54 | PASS |
| q12 | Which SaaS products have no retained open-source alternative in the corpus yet? | gap analysis (anti-join) |  | 14 | 14 | PASS |
| q13 | Password-manager alternatives that ship a container deployment. | two-hop category through SaaS + capability | yes | 2 | 18 | PASS |
| q14 | Find a headless browser an AI agent can drive for web scraping. | semantic + use case + capability |  | 102 | 125 | PASS |
| q15 | Which maintainers personally run projects in more than one of the curated agent/MCP lists? | aggregate across sources (project - LISTED_IN -> source, grouped by maintainer) | yes | 28 | 100 | PASS |
| q16 | Which open-source CRMs are in both the Replace and Discover lanes? | lane intersection | yes | 5 | 35 | PASS |
| q17 | Which workflow-automation projects are built around n8n? | use case + semantic |  | 119 | 68 | PASS |
| q18 | Which open-source tools for Retrieval-Augmented Generation use PostgreSQL? | use case + technology |  | 31 | 91 | PASS |
| q19 | Alternatives to Zapier that are recommendation-eligible and have a recognised licence. | single-hop with quality facets | yes | 1 | 6 | PASS |
| q20 | Which alternatives to Google Analytics are also similar to each other (a competitive cluster)? | multi-hop cluster (SaaS <- REPLACES - p1 - SIMILAR_TO - p2 - REPLACES -> SaaS) | yes | 14 | 108 | PASS |
| q21 | Desktop apps for local AI speech-to-text transcription. | hybrid semantic + capability |  | 23 | 116 | PASS |
| q22 | Which SaaS products does an AI-agent tool most often integrate with? | aggregate over integrations | yes | 25 | 160 | PASS |
| q23 | Which projects replace Jira and what categories do they fall into upstream? | two-hop out via upstream taxonomy | yes | 5 | 26 | PASS |
| q24 | Discover-only projects that look like an alternative to a catalogued SaaS product but have no validated REPLACES edge (candidates for the Replace lane). | semantic lane-bridging without forcing an edge |  | 6 | 18 | PASS |

### AC10 — results carry evidence back to canonical source records
- `query_results_resolve_to_source_records` **PASS** — `{"refs_resolved": 1616}`

**q01: Find actively maintained open-source tools that replace expensive SaaS products, support self-hosting, expose APIs, fit AI-agent workflows and show growing contributor activity.**

> Limitation reported with the result: 'expensive': the reviewed SaaS catalogue carries no pricing, so price is not a constraint yet
> Limitation reported with the result: 'growing contributor activity': contributor counts are not observed and every project has fewer than 28 days of history, so trend status is reported (insufficient_history), not filtered on

- **open-webui/open-webui** (`github:701547123`)
  - `REPLACES` → `saas:chatgpt` · explicit · confidence 0.7 · evidence `kei805:relationships.jsonl#chatgpt--github.com/open-webui/open-webui` (`validation.status` = `flagged_licence_exception`)
  - `HAS_CAPABILITY` → `capability:self-hosting` · inferred · confidence 0.9 · evidence `kei811:relationship_candidates.jsonl#github:701547123|has_topic|github-topic:self-hosted`
  - `HAS_CAPABILITY` → `capability:exposes-api` · inferred · confidence 0.9 · evidence `kei811:relationship_candidates.jsonl#github:701547123|has_topic|github-topic:openapi`
  - `HAS_CAPABILITY` → `capability:ai-agent-integration` · inferred · confidence 0.9 · evidence `kei811:relationship_candidates.jsonl#github:701547123|has_topic|github-topic:mcp`
  - facet `maintenance_status` = `active` from `kei811:projects.jsonl#github:701547123` (`quality.maintenance.status`)
  - facet `trend.status` = `insufficient_history` from `kei811:projects.jsonl#github:701547123` (`freshness.history (graph observations)`)
- **LibreChat-AI/LibreChat** (`github:600596928`)
  - `REPLACES` → `saas:chatgpt` · explicit · confidence 0.8 · evidence `kei805:relationships.jsonl#chatgpt--github.com/librechat-ai/librechat` (`validation.status` = `validated`)
  - `HAS_CAPABILITY` → `capability:self-hosting` · inferred · confidence 0.6 · evidence `kei811:projects.jsonl#github:600596928` (`descriptive.description` = `self-hosting`)
  - `HAS_CAPABILITY` → `capability:exposes-api` · inferred · confidence 0.6 · evidence `kei811:projects.jsonl#github:600596928` (`descriptive.description` = `OpenAPI`)
  - `HAS_CAPABILITY` → `capability:ai-agent-integration` · inferred · confidence 0.9 · evidence `kei811:relationship_candidates.jsonl#github:600596928|has_topic|github-topic:mcp`
  - facet `maintenance_status` = `active` from `kei811:projects.jsonl#github:600596928` (`quality.maintenance.status`)
  - facet `trend.status` = `insufficient_history` from `kei811:projects.jsonl#github:600596928` (`freshness.history (graph observations)`)
- **webiny/webiny-js** (`github:116817549`)
  - `REPLACES` → `saas:contentful` · explicit · confidence 0.7 · evidence `kei805:relationships.jsonl#contentful--github.com/webiny/webiny-js` (`validation.status` = `flagged_licence_exception`)
  - `HAS_CAPABILITY` → `capability:self-hosting` · inferred · confidence 0.6 · evidence `kei811:projects.jsonl#github:116817549` (`descriptive.description` = `self-hosted`)
  - `HAS_CAPABILITY` → `capability:exposes-api` · inferred · confidence 0.9 · evidence `kei811:relationship_candidates.jsonl#github:116817549|has_topic|github-topic:graphql`
  - `HAS_CAPABILITY` → `capability:ai-agent-integration` · inferred · confidence 0.9 · evidence `kei811:relationship_candidates.jsonl#github:116817549|has_topic|github-topic:mcp`
  - facet `maintenance_status` = `active` from `kei811:projects.jsonl#github:116817549` (`quality.maintenance.status`)
  - facet `trend.status` = `insufficient_history` from `kei811:projects.jsonl#github:116817549` (`freshness.history (graph observations)`)

**q20: Which alternatives to Google Analytics are also similar to each other (a competitive cluster)?**

- **PostHog/posthog** (`github:235901813`)
  - `REPLACES` → `saas:google-analytics` · explicit · confidence 0.9 · evidence `kei805:relationships.jsonl#google-analytics--github.com/plausible/analytics` (`validation.status` = `validated`)
  - `SIMILAR_TO` → `github:235901813` · inferred · confidence 0.7 · evidence `graph:edges.jsonl#github:160427405|REPLACES|saas:google-analytics`
  - `REPLACES` → `saas:google-analytics` · explicit · confidence 0.6 · evidence `kei805:relationships.jsonl#google-analytics--github.com/posthog/posthog` (`validation.status` = `flagged_licence_exception`)
- **umami-software/umami** (`github:280363952`)
  - `REPLACES` → `saas:google-analytics` · explicit · confidence 0.9 · evidence `kei805:relationships.jsonl#google-analytics--github.com/matomo-org/matomo` (`validation.status` = `validated`)
  - `SIMILAR_TO` → `github:280363952` · inferred · confidence 0.8 · evidence `graph:edges.jsonl#github:1548202|REPLACES|saas:google-analytics`
  - `REPLACES` → `saas:google-analytics` · explicit · confidence 0.9 · evidence `kei805:relationships.jsonl#google-analytics--github.com/umami-software/umami` (`validation.status` = `validated`)

**q11: Open-source web analytics that respect privacy.**

- **plausible/analytics** (`github:160427405`)
  - `HAS_CAPABILITY` → `capability:privacy-focused` · inferred · confidence 0.9 · evidence `kei811:relationship_candidates.jsonl#github:160427405|has_topic|github-topic:privacy`
  - semantic 0.3408 (tfidf-sublinear/1) on alternative, analytics, friendly, google, privacy, tag:analytics
- **electerious/Ackee** (`github:18831920`)
  - `HAS_CAPABILITY` → `capability:privacy-focused` · inferred · confidence 0.9 · evidence `kei811:relationship_candidates.jsonl#github:18831920|has_topic|github-topic:privacy`
  - semantic 0.3406 (tfidf-sublinear/1) on analytics, google, privacy, tag:analytics, tag:google-analytics, tag:privacy

### AC11 — implementation-independent
- `records_validate_against_storage_independent_schema` **PASS** — `{"errors": []}`
- `property_graph_export_round_trips` **PASS** — `{"nodes": 17525, "relationships": 92552}`

### AC12 — KEI-806 scoring consumes graph entities, edges and confidence
- `scoring_view_covers_every_project` **PASS** — `{"rows": 9363}`
- `replacement_fit_only_where_a_saas_edge_exists` **PASS** — `{"applicable": 402, "not_applicable": 8961}`
- `nothing_endorsed` **PASS** — `{}`

`data/graph/views/scoring_inputs.jsonl`: one row per project. `replacement_fit_applicable` is true only where a `REPLACES` edge exists.

## Known limits (recorded, not hidden)

- `DEPENDS_ON` has no edges: dependency manifests are not read yet.
- `MAINTAINED_BY` covers only personal repositories. For organisation-owned projects the maintainers are unknown.
- Trends need a second observation at least 28 days after the first. The next verify run starts that series.
- Price ("expensive SaaS") is not in the reviewed catalogue, so no query can constrain on it yet.
- Questions are compiled into plans by hand (`config/graph_queries.yaml`). Automatic compilation is later work.
- Category near-synonyms that differ by more than form ("Password manager" vs "Password management") are separate entities. The ontology can fold them when someone reviews them.
