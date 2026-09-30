# The Source — knowledge graph and retrieval model (KEI-844)

**Status: internal. Nothing here is published, recommended or endorsed.** Graph membership
and edges are evidence about projects, not verdicts on them.

The graph sits between discovery and scoring. KEI-811 discovers and normalises the universe
(`data/discover/corpus/`); this layer resolves entities and relationships over it
(`data/graph/`); KEI-806 scores against the result (`data/graph/views/scoring_inputs.jsonl`).

## Run it

```bash
.venv/bin/python -m source_pipeline graph-build      # build data/graph/ from the corpus, then validate
.venv/bin/python -m source_pipeline graph-validate   # re-run the query suite and acceptance checks only
.venv/bin/python -m source_pipeline graph-query q01  # run one question (id or plan file); prints evidence
.venv/bin/python -m pytest tests/test_graph.py -q
python3 scripts/graph_docs.py                        # regenerate this file's reference section
python3 scripts/graph_acceptance.py                  # regenerate docs/GRAPH-ACCEPTANCE.md
```

The build reads no network and no wall clock. Its clock is the corpus `as_of`; the same
inputs give the same bytes (`rebuild_is_byte_identical` checks this on every validation).

## Inputs

| Input | What it contributes |
|---|---|
| `data/discover/corpus/` (KEI-811) | retained projects, owners, topics, languages, list memberships, README links |
| `data/discover/history/observations.jsonl` | append-only observations, one per verify run |
| `data/dataset/` (KEI-805) | the reviewed SaaS catalogue and validated SaaS↔OSS claims |
| `data/live/snapshot.json` (KEI-805) | an earlier observation of every Replace-lane project |
| `config/graph_ontology.yaml` | the reviewed vocabulary that turns topics, languages and keywords into concepts |

The two upstream datasets are read, never written. Validation checks both still match their
own manifests.

## Outputs (`data/graph/`)

| File | Contents |
|---|---|
| `entities.jsonl` | one canonical record per entity: `id`, `type`, `name`, `aliases`, `attrs`, `evidence` |
| `edges.jsonl` | one record per relationship: `relation`, `from`, `to`, `assertion`, `confidence`, `evidence`, `attrs`, `observed_as_of` |
| `observations.jsonl` | longitudinal metrics, keyed `entity@as_of`; later builds add rows, they never overwrite them |
| `identity.jsonl` | every alias key and the entity it resolves to, with the reason |
| `excluded_claims.jsonl` | claims seen and deliberately not made into edges, with the reason |
| `retrieval/documents.jsonl`, `retrieval/model.json` | the hybrid-retrieval representation |
| `views/scoring_inputs.jsonl` | the per-project view KEI-806 consumes |
| `export/property_graph/` | the same graph as property-graph CSV (a storage adapter, not an input) |
| `reports/query_results.json`, `reports/validation_report.json` | the query suite and acceptance checks |
| `summary.json`, `MANIFEST.json` | counts, pinned input digests, and a SHA-256 of every file |

## Identity resolution

One real-world thing is one entity. Every alias that resolves into an entity is recorded in
`identity.jsonl`, and the build refuses to finish if one alias would resolve to two entities.

- **Projects** keep the corpus id (`github:<numeric host id>`), which survives renames and
  transfers. Every address a project was ever listed under, and its KEI-805 `oss_id`, is an
  alias. The same owner/name on two hosts (a mirror) is one project; the record with the
  numeric host id is kept and the other becomes its alias.
- **Organisations and maintainers** are host accounts, case-insensitive
  (`org:github:<login>`, `maintainer:github:<login>`). An account whose type the host did not
  report is not guessed at; its ownership claim goes to `excluded_claims.jsonl`.
- **Categories** from the reviewed SaaS catalogue and from each upstream list's own taxonomy
  resolve by normalised label (case, punctuation, `&`/`and`, plural endings), so "CRM" from five
  schemes is one entity. The catalogue's label names it; every scheme's label is kept.
- **Technologies** resolve host language names and topic spellings (`golang` and `Go`,
  `postgres` and `postgresql`) through the ontology.
- **SaaS products** resolve every catalogue alias and upstream spelling.

## Evidence, assertion and confidence

Every edge carries at least one evidence item. Each item is a `ref` in the form
`<dataset>:<file>#<key>` pointing at a canonical source record, plus the field and value it
relied on. `graph_build.resolve_ref()` returns that record, and validation resolves every
ref in the graph.

`assertion` is `explicit` when a named source stated the fact (the host API, a curated list
at a pinned commit, a validated KEI-805 claim), and `inferred` when a documented rule derived
it. An inferred edge always has confidence below 1.0 and names its rule or ontology entry.
When several routes support one edge, all of them are kept as evidence, and the edge takes the
strongest assertion and the highest confidence.

**Unknown stays unknown.** No edge is written to make the graph look complete:
`DEPENDS_ON` is defined but has no edges because the corpus does not read dependency
manifests yet; organisation-owned projects have no `MAINTAINED_BY` because commit authorship
is not inspected; an unrecognised licence produces no `LICENSED_UNDER`.

## Replace and Discover lanes

`REPLACES` comes only from KEI-805 claims whose validation status is `validated` or
`flagged_licence_exception`. The 158 rejected claims are listed in `excluded_claims.jsonl`.
Every project records its lanes. A Discover-only project has no `REPLACES` edge, and nothing in
the build creates one: the semantic layer can surface Discover projects that look like
alternatives (query q24) without writing an edge.

## Longitudinal observations

`observations.jsonl` holds one row per project per observation time, from KEI-811's
append-only history and KEI-805's earlier snapshot. Each project's `trend` summarises its
series. A trend is reported only when there are at least two observations at least
28 days apart. Until then its status is `insufficient_history`: the model supports trend
questions, but the current data cannot yet answer them, and they are not answered.

## Retrieval

`retrieval/documents.jsonl` holds one document per project:

- `vector`: sublinear TF-IDF over the name, description, host topics and resolved concepts
  (confidence ≥ 0.8), L2-normalised, `model: tfidf-sublinear/1`. It is exact, reproducible and
  standard-library-only. A dense embedding model can replace it by writing the same document
  shape with its own `model` id; the plan executor only calls `RetrievalIndex.text_scores()`.
- `facets`: structured filters copied from canonical fields (lane, outcome, licence,
  maintenance, stars, trend). The field each one comes from is listed in `FACET_SOURCES`.
- `graph`: the entity's neighbours by relation.

A question compiles to a **plan**: a start set, then steps (`where`, `require`, `exclude`,
`traverse`, `text`, `group`, `rank`). Semantic similarity, graph constraints and structured
filters combine in one plan, and every result returns the edges, facet values and semantic
match that admitted it. `config/graph_queries.yaml` holds 24 representative questions, each
compiled by hand. Compiling questions into plans automatically is later product work. Parts of
a question the evidence cannot answer (price, contributor growth) are listed as `limitations`
and reported with the results, not quietly dropped.

## Storage independence

The canonical form is the three JSONL files above, validated by
`schema/the-source.graph.schema.json`. Nothing in them assumes a particular store.
`graph_export.py` writes the same graph as property-graph CSV (Neo4j bulk-import headers), and
validation reads it back and checks the round trip is exact. Moving to a graph database or a
vector index means writing another adapter. The canonical Source schema does not change.

## For KEI-806 scoring

`views/scoring_inputs.jsonl` holds one row per project: lanes, licence, maintenance, the
observation series and trend, and, with confidence and edge ids, its `REPLACES` (with KEI-805
status and licence lane), `INTEGRATES_WITH`, capabilities, use cases, technologies, categories,
owner, maintainers and nearest similar projects. `replacement_fit_applicable` is true only
when a `REPLACES` edge exists. Nothing is marked endorsed.


## Reference (generated by scripts/graph_docs.py; do not edit by hand)

### Entity types

| Type | Id prefix | Meaning |
|---|---|---|
| `Project` | `github:`, `kei805:` | an open-source repository retained by the KEI-811 corpus |
| `SaaSProduct` | `saas:` | a commercially operated hosted product in the reviewed KEI-805 catalogue |
| `Organisation` | `org:` | a host account of organisation type that owns at least one retained project |
| `Maintainer` | `maintainer:` | a host account of user type that owns at least one retained project |
| `Category` | `category:` | a market category from a reviewed scheme (the SaaS catalogue, or an upstream list's own taxonomy) |
| `Capability` | `capability:` | something a project can do or be deployed as (config/graph_ontology.yaml) |
| `UseCase` | `use-case:` | a job a user wants done (config/graph_ontology.yaml) |
| `Technology` | `technology:` | a language, framework, runtime, datastore or protocol (config/graph_ontology.yaml) |
| `Licence` | `licence:` | a recognised licence, by SPDX identifier |
| `Source` | `source:` | a licence-cleared upstream list or dataset that asserted something |

## Relationship types

Direction is always `from -> to` as written.

### `REPLACES`

*Project REPLACES SaaSProduct*

- **Meaning:** the project is a validated open-source alternative to the SaaS product
- **Assertion:** explicit
- **Evidence required:** a KEI-805 relationship record (upstream claim with source commit and locator) whose validation status is validated or flagged_licence_exception. Rejected KEI-805 edges never become REPLACES.
- **Confidence:** 0.70 for one upstream source, 0.80 for two, 0.90 for three or more; minus 0.10 when the plausibility review has not been done. Licence status is carried as an edge attribute, not folded into confidence.

### `SIMILAR_TO`

*Project SIMILAR_TO Project* (symmetric: stored once per pair)

- **Meaning:** the two projects serve substantially overlapping purposes
- **Assertion:** inferred
- **Evidence required:** either both are validated alternatives to the same SaaS product (co_alternative), or their descriptions and host topics are close under the retrieval model and they share at least two specific terms (profile_similarity). The shared SaaS or terms are recorded on the edge.
- **Confidence:** co_alternative: the lower of the two REPLACES confidences, capped at 0.80. profile_similarity: the cosine similarity, capped at 0.75. Stored once per unordered pair (from < to).

### `INTEGRATES_WITH`

*Project INTEGRATES_WITH SaaSProduct*

- **Meaning:** the project works with the SaaS product (connector, client, MCP server, plugin)
- **Assertion:** inferred
- **Evidence required:** a host topic equal to the product's catalogue key, outside the ontology's stoplist, on a project that is not an alternative to that product.
- **Confidence:** 0.70

### `DEPENDS_ON`

*Project DEPENDS_ON Project*

- **Meaning:** the project declares the other as a build or runtime dependency
- **Assertion:** explicit
- **Evidence required:** a dependency manifest or SBOM entry read from the repository at a pinned commit. The corpus does not yet read manifests, so no DEPENDS_ON edge exists; the type is defined so that adding the evidence adds edges without a schema change.
- **Confidence:** 1.0 when present

### `OWNED_BY`

*Project OWNED_BY Organisation|Maintainer*

- **Meaning:** the host account under which the repository lives
- **Assertion:** explicit
- **Evidence required:** the host API's owner object at live.fetched_at
- **Confidence:** 1.0

### `MAINTAINED_BY`

*Project MAINTAINED_BY Maintainer*

- **Meaning:** a person who keeps the project going
- **Assertion:** inferred
- **Evidence required:** the repository is owned by a user account (a personal repository), so its owner is its default maintainer. Commit authorship is not inspected, so organisation-owned projects get no MAINTAINED_BY edge: their maintainers are unknown, not absent.
- **Confidence:** 0.70

### `BELONGS_TO_CATEGORY`

*Project|SaaSProduct BELONGS_TO_CATEGORY Category*

- **Meaning:** the entity is in that market category
- **Assertion:** explicit or inferred
- **Evidence required:** SaaS: the reviewed catalogue (explicit). Project: an upstream list's own category for it (explicit), or the category of a SaaS product it validly REPLACES (inferred).
- **Confidence:** explicit 1.0; inferred = min(0.70, the REPLACES confidence)

### `USES_TECHNOLOGY`

*Project USES_TECHNOLOGY Technology*

- **Meaning:** the project is built with or on the technology
- **Assertion:** explicit or inferred
- **Evidence required:** host language breakdown (explicit, 1.0), an owner-applied host topic mapped by the ontology (inferred, 0.9), or a description keyword (inferred, 0.6)
- **Confidence:** the strongest route that produced it; every route is kept as evidence

### `HAS_CAPABILITY`

*Project HAS_CAPABILITY Capability*

- **Meaning:** the project offers the capability (self-hosting, an API, an MCP server ...)
- **Assertion:** inferred
- **Evidence required:** an owner-applied host topic (0.9), inclusion in a curated list whose admission criterion is that capability (0.8), host language (Dockerfile -> container deployment, 0.9), or a description keyword (0.6)
- **Confidence:** the strongest route; every route is kept as evidence

### `SERVES_USE_CASE`

*Project|SaaSProduct SERVES_USE_CASE UseCase*

- **Meaning:** the entity is used to get that job done
- **Assertion:** inferred
- **Evidence required:** Project: host topic (0.9) or description keyword (0.6) mapped by the ontology. SaaS: its reviewed catalogue category mapped by the ontology (0.9).
- **Confidence:** the strongest route; every route is kept as evidence

### `LICENSED_UNDER`

*Project LICENSED_UNDER Licence*

- **Meaning:** the licence the host reports for the repository
- **Assertion:** explicit
- **Evidence required:** host API licence detection, or the repository's licence file where the KEI-805/811 licence lane recognised it. An unrecognised licence produces no edge: it is unknown.
- **Confidence:** 1.0

### `LISTED_IN`

*Project LISTED_IN Source*

- **Meaning:** a licence-cleared curated list names the project
- **Assertion:** explicit
- **Evidence required:** the list at a pinned commit, with the line that names the project
- **Confidence:** 1.0

### `LINKS_TO`

*Project LINKS_TO Project*

- **Meaning:** the project's README links to the other project
- **Assertion:** explicit
- **Evidence required:** the README line, recorded at discovery
- **Confidence:** 1.0

### Ontology concepts (`config/graph_ontology.yaml`, reviewed 2026-09-29)

| Section | Count | Concepts |
|---|---|---|
| technologies | 49 | typescript, javascript, python, rust, go, java, kotlin, csharp, cpp, c, php, ruby, swift, dart, elixir, shell, nodejs, react, vue, svelte, angular, tailwind, electron, tauri, django, fastapi, postgresql, mysql, sqlite, mongodb, redis, docker, kubernetes, wasm, playwright, puppeteer, ffmpeg, pytorch, langchain, ollama, openai-api, anthropic-api, gemini-api, mcp, graphql, opentelemetry, tree-sitter, cloudflare-workers, blockchain |
| capabilities | 17 | self-hosting, container-deployment, exposes-api, mcp-server, mcp-client, agent-skill, ai-agent-integration, local-first, privacy-focused, cli, desktop-app, mobile-app, browser-extension, sso-auth, multi-tenant, realtime-collaboration, plugin-extensible |
| use_cases | 22 | ai-coding-assistance, agent-memory, retrieval-rag, web-scraping, workflow-automation, observability, security-testing, knowledge-management, marketing-seo, finance-trading, document-processing, speech-audio, video-media, devops-infra, data-analytics, team-chat, project-management, ecommerce, email, crm-sales, browser-testing, password-secrets |

| Route | Confidence |
|---|---|
| `host_language` | 1.0 |
| `host_topic` | 0.9 |
| `curated_list` | 0.8 |
| `description_keyword` | 0.6 |
| `replaced_saas_category` | 0.7 |
