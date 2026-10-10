# MCP retrieval server (KEI-851)

The Source as a [Model Context Protocol](https://modelcontextprotocol.io) server, so an AI
client (Claude Code, Claude Desktop, Cursor, ChatGPT, an Agent SDK app) can ask it what
replaces a SaaS product, compare projects, recommend against explicit requirements, find
emerging projects, read the evidence and cite it.

## Connect

Hosted, public, read-only, no account: **`https://source-mcp.de-alwis.com/mcp`**
(Streamable HTTP). Proven clients (evidence in the KEI-851 pack):

**Claude Code**

```bash
claude mcp add --transport http the-source https://source-mcp.de-alwis.com/mcp
# or for one run, without changing your config:
echo '{"mcpServers":{"the-source":{"type":"http","url":"https://source-mcp.de-alwis.com/mcp"}}}' > the-source.json
claude -p "What are the best self-hosted Notion replacements?" --mcp-config the-source.json
```

**MCP Python SDK** (`pip install mcp`)

```python
import asyncio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

async def main():
    async with streamablehttp_client("https://source-mcp.de-alwis.com/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("source_recommend", {"saas": "Notion", "self_hostable": True})
            print(res.structuredContent["result"]["recommendations"][0]["name"])

asyncio.run(main())
```

Any other Streamable-HTTP client (Claude Desktop and claude.ai custom connectors, Cursor,
ChatGPT connectors) takes the same URL. No authentication; requests are rate-limited
(60/min per address at the edge, 120/min in the server).

Example prompts:

- *Find credible open-source alternatives to Notion.*
- *Find self-hosted Notion replacements that are actively maintained and copyleft-licensed, for a 20-person company with a strong API.* (the answer names what the data cannot judge: API quality, team-size fit)
- *Compare Docmost and AppFlowy.*
- *Show open-source projects under 10,000 stars whose repository momentum is surging this week, and what the community is discussing.*
- *Is Vaultwarden recommended as a 1Password replacement, and why?*

## Run it yourself

```bash
python -m source_pipeline mcp-serve                 # stdio server over this checkout's read layer
python -m source_pipeline.mcp_server --root DIR     # the same, without loading the pipeline CLI
python -m source_pipeline.mcp_server --check        # verify the read layer, print a summary, exit
python -m source_pipeline.mcp_http --port 8851  # Streamable HTTP on 127.0.0.1:8851/mcp
python -m pytest tests/test_mcp_server.py tests/test_mcp_v1_tools.py tests/test_mcp_http.py -q
```

The hosted deployment is `deploy/the-source-mcp/` (systemd units, nginx, installer).

Client configuration (stdio):

```json
{"mcpServers": {"the-source": {
  "command": "python3",
  "args": ["-S", "-m", "source_pipeline.mcp_server", "--root", "/path/to/the-source"],
  "env": {"PYTHONPATH": "/path/to/the-source"}}}}
```

## What it reads

The canonical read layer, the files approved for publication (gate-KEI-807-bfaf2354,
CC BY 4.0, see `DATA-LICENCE.md`), and the two published signal layers beside it:

| File | Used for |
|---|---|
| `data/canonical/MANIFEST.json` | dataset_version, counts, file hashes, sources |
| `data/canonical/saas_products.jsonl` | the catalogue: names, aliases, domains, categories |
| `data/canonical/oss_projects.jsonl` | live project facts, maintenance, Project Health |
| `data/canonical/relationships.jsonl` | SaaS↔OSS relationships, provenance, Replacement Fit, `recommended` |
| `data/canonical/NOTICE.md` | upstream attribution |
| `api/v1/index.json`, `api/v1/domains.json`, `api/v1/saas/<saas_id>.json` | the published pages and domain rules |
| `data/momentum/MANIFEST.json`, `projects.jsonl` | Repository Momentum: star growth and acceleration over 1/7/30/90 days ([MOMENTUM.md](MOMENTUM.md)) |
| `data/radar/MANIFEST.json`, `radar.jsonl` | Community Momentum: ranked Hacker News / DEV / Bluesky (and, where configured, X and Instagram) mentions in the Radar window ([RADAR.md](RADAR.md)) |
| `data/corpus/MANIFEST.json`, `projects.jsonl`, `live.jsonl` | the discovery corpus: every project the Discover lane found, served as the `discovery` tier ([CORPUS.md](CORPUS.md), KEI-912, gate-KEI-912-dba84c8b) |

It reads nothing else: not the internal Discover lane build (`data/discover/`), staging,
`data/live/`, history or intake, and nothing private to AIQ. A test runs the server against a directory holding only
these folders, with an audit hook recording every file it opens and every socket event.

Each signal layer is verified against its own MANIFEST sha256s. A signal layer that is
missing, fails verification, or is older than 48 hours is reported in every answer that uses
it (`signals.<name>.status`: `ok`, `stale` or `unavailable`, with the reason), and the
canonical tools keep serving. The discovery corpus is verified the same way and reported as
`corpus.status` (`ok`, `stale` after 10 days without a refresh, or `unavailable` with the
reason); without it, search covers the canonical layer only and says so. A window that cannot be measured yet (for example 7-day
acceleration before 14 days of history exist) is reported with the date it becomes
measurable, never filled in.

## Guarantees

- **Verified before served.** At start the server checks every canonical file against its
  MANIFEST sha256, recomputes `dataset_version` from those hashes, checks the counts, and
  checks every `api/v1` file carries the same `dataset_version`. If anything fails it exits 2
  with a message on stderr and serves nothing.
- **Follows the daily cycle safely.** Each request checks whether `MANIFEST.json` changed. A
  new layer that verifies is switched to; one that does not (for example, a half-written
  update) is ignored, the previous snapshot keeps serving, and every answer says
  `snapshot_current: false`, with the reason in `source_dataset_info`.
- **Tiers.** Every project in an answer carries `tier`: `recommended`, `scored` or `discovery`
  ([CORPUS.md](CORPUS.md)). Discovery projects are labelled "discovered, not scored" and are
  never recommendations. `source_get_alternatives` and `source_recommend` answer byte for byte
  as before; `include_discovery: true` adds a separate, labelled `discovery_candidates` list.
- **Publication gate.** Only relationships with `recommended: true` are returned as
  recommendations. For a supported product the first rows are exactly its published
  `api/v1` page, byte for byte. Further rows continue the same ranking over the canonical
  layer and are flagged `beyond_published_page`. Non-recommended relationships are counted,
  and `source_get_relationship` explains why each one was not recommended. Relationships the
  export guard withholds are not in the layer, so the server cannot serve them.
- **Attribution travels with the data.** Every tool result has a `meta` block:
  `dataset_version`, `data_as_of`, `licence` (CC BY 4.0), `attribution`, `notice`.
- **Read-only and private.** Standard library only. The engine opens no sockets and writes
  nothing; the HTTP transport adds only the listener. Queries, URLs and client addresses are
  never logged or stored.
- **Aggregate telemetry only.** Per UTC day, per tool, per outcome (`ok`, `tool_error`,
  `internal_error`): a count and a latency histogram (`<10ms` ... `>=1000ms`), kept 90 days.
  No arguments, results, client names, sessions or addresses. The hosted server writes it to
  `/var/lib/the-source-mcp/telemetry.json` (schema `the-source.mcp-telemetry/1`). `source_detect_saas` matches a URL against
  `domains.json` and never fetches it. stdout carries only protocol messages.
- **Deterministic.** The same layer and the same `--now` give byte-identical responses.
  The clock is used only for `freshness` in `source_dataset_info`.

## Tools

All tools are annotated `readOnlyHint: true`, `openWorldHint: false`. Their input schemas
set `additionalProperties: false`.

| Tool | Answers |
|---|---|
| `source_dataset_info` | which snapshot is served, when repositories were last checked (stale after 14 days), counts, sources, licence |
| `source_list_supported_saas` | the products with a published page |
| `source_get_alternatives` | `saas` (id, name, alias, domain or URL), `limit` 1-50 (default 5), `include_discovery`: ranked recommendations with status, last checked, scoring version and provenance; optionally a separate `discovery_candidates` list |
| `source_detect_saas` | `url`: the supported product it belongs to, by the READ-API.md rules |
| `source_search` | `query`, `kind` (any, saas, project), `tier` (any, recommended, scored, discovery), `licence`, `maintenance_status`, `min_health`, `supported_only`, `mode`, `limit`: retrieval over the catalogue and every project in the canonical layer and the discovery corpus, each result with its `tier` |
| `source_get_project` | `project` (oss_id, repository URL or name, canonical or discovery): live facts, tier and every relationship, recommended or not; a discovery project with its provenance (list + pinned commit + line) and `observed_as_of` |
| `source_get_relationship` | `saas`, `project`: the full relationship with upstream provenance (list, pinned commit, line), Replacement Fit dimensions and the reason it is or is not recommended |
| `source_compare_projects` | `projects` (2-5, any tier): licence and family, stars, maintenance, Project Health, Replacement Fit per product replaced (head to head on shared products), Repository and Community Momentum, and the uncertainty in each |
| `source_recommend` | `need` (words) and/or `saas`, plus hard constraints: `licence`, `licence_family` (permissive, weak_copyleft, copyleft), `self_hostable`, `maintenance_status`, `min_fit`, `min_health`, `min_stars`, `max_stars`, `max_days_since_activity`, `repository_momentum_7d`, `limit`, `include_discovery`. Returns the products matched and how, every constraint applied, counts excluded per constraint, and `not_evaluated`: requirements the data cannot judge |
| `source_emerging_projects` | `window` (1, 7, 30 days), `momentum` labels, `accelerating_only`, `min_stars`, `max_stars`, `query`, `limit`: repositories by star growth, and the Community Radar ranking, with window coverage and every degradation stated |
| `source_get_momentum` | `project` (canonical or any tracked repository): every momentum window with its explanation, and each counted community mention with its permalink |

The four measures are never folded into one another: **Project Health** (is the project
healthy), **Replacement Fit** (does it replace this product), **Repository Momentum** (is its
star growth rising or accelerating) and **Community Momentum** (is it being discussed now).

Resources: `source://data/canonical/MANIFEST.json`, `source://data/canonical/NOTICE.md`,
`source://api/v1/index.json`, `source://api/v1/domains.json`, and
`source://api/v1/saas/{saas_id}` for each supported product.

### Retrieval

`source_search` ranks with BM25 over weighted fields. For a SaaS product the fields are
name ×3, aliases and id ×2.5, category ×1.5 and domain ×1. For a project they are name ×3,
owner/repository ×2, the names and aliases of the products it is recommended for ×1.5,
their categories ×1, topics ×1.2, host description ×1, languages ×0.5 and licence ×0.5
(topics, description and languages come from the discovery corpus, KEI-912). With the corpus
available, search ranks over the full corpus; recommendations always rank over the canonical
layer alone, so the corpus cannot move them. An exact name match adds 10. Ties sort by kind, then
id. Every hit reports which query terms matched which fields. This is `mode: "lexical"`, the default.

`mode: "hybrid"` (and `source_recommend`, `source_emerging_projects` with `query`) combine
three signals, each returned per result as `score_components`:

- **lexical**: the BM25 above on the query without KEI-844's stopwords, normalised to the best hit;
- **semantic**: cosine similarity under the KEI-844 model (`tfidf-sublinear/1`,
  `source_pipeline/graph_retrieval.py`; the tokeniser is the same, and a test asserts it)
  over each product's name, aliases and category and each project's name, repository, the
  products it replaces and their categories;
- **graph**: traversal of `SaaS -REPLACES-> project` edges from the best-matched products,
  weighted by Replacement Fit, reported as `graph_path`.

Score = 0.45 lexical + 0.35 semantic + 0.20 graph. A product named outright in a `need`
("... like Notion") is taken as the product meant. For search, the semantic
model is fitted over the full corpus, including host descriptions and topics; for
recommendations, over the canonical layer, exactly as before.

### Errors

Protocol errors use JSON-RPC codes: -32700 for a parse error, -32600 for an invalid request
(including a batch, or a request before `initialize`), -32601 for an unknown method, -32602
for an unknown tool and -32002 for an unknown resource. A call the server cannot answer
returns `isError: true` with a message naming the problem: a bad or unknown argument, a
`limit` out of range, an unknown or ambiguous product or project, or no such relationship.
An unexpected exception becomes -32603, and the server keeps running.

## Protocol

Newline-delimited JSON-RPC 2.0 on stdio, or Streamable HTTP (`source_pipeline/mcp_http.py`):
`POST /mcp` with one JSON-RPC message, answered as `application/json`; notifications get 202.
Stateless (no `Mcp-Session-Id`); each request is answered under its `MCP-Protocol-Version`
header (2025-03-26 when absent). `GET /mcp` and `DELETE /mcp` are 405, batches 400, bodies
over 64 KB 413, a browser `Origin` not on the allow-list 403, over the rate 429. `GET /healthz`
returns the dataset version served. Protocol versions `2025-06-18` (with
`structuredContent`), `2025-03-26` and `2024-11-05` (text content only). A client asking for
another version gets `2025-06-18`. Capabilities: `tools`, `resources`. Neither list changes
during a session.

Hosting: `https://source-mcp.de-alwis.com/mcp`, approved as a public, anonymous, read-only
endpoint in gate-KEI-851-20c71990. No authentication by design; the data is already public.
