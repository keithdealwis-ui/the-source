# MCP retrieval server (KEI-851)

The Source as a [Model Context Protocol](https://modelcontextprotocol.io) server, so an AI
client (Claude Desktop, Claude Code, Cursor, an Agent SDK app) can ask it what replaces a
SaaS product, read the evidence and cite it.

```bash
python -m source_pipeline mcp-serve                 # stdio server over this checkout's read layer
python -m source_pipeline.mcp_server --root DIR     # the same, without loading the pipeline CLI
python -m source_pipeline.mcp_server --check        # verify the read layer, print a summary, exit
python -m pytest tests/test_mcp_server.py -q
```

Client configuration (stdio):

```json
{"mcpServers": {"the-source": {
  "command": "python3",
  "args": ["-S", "-m", "source_pipeline.mcp_server", "--root", "/path/to/the-source"],
  "env": {"PYTHONPATH": "/path/to/the-source"}}}}
```

## What it reads

Only the canonical read layer, the files approved for publication (gate-KEI-807-bfaf2354,
CC BY 4.0, see `DATA-LICENCE.md`):

| File | Used for |
|---|---|
| `data/canonical/MANIFEST.json` | dataset_version, counts, file hashes, sources |
| `data/canonical/saas_products.jsonl` | the catalogue: names, aliases, domains, categories |
| `data/canonical/oss_projects.jsonl` | live project facts, maintenance, Project Health |
| `data/canonical/relationships.jsonl` | SaaS↔OSS relationships, provenance, Replacement Fit, `recommended` |
| `data/canonical/NOTICE.md` | upstream attribution |
| `api/v1/index.json`, `api/v1/domains.json`, `api/v1/saas/<saas_id>.json` | the published pages and domain rules |

It reads nothing else: not the knowledge graph, the discover lane, staging, `data/live/`,
history, momentum, radar or intake. A test runs the server against a directory holding only
these two folders, with an audit hook recording every file it opens and every socket event.

## Guarantees

- **Verified before served.** At start the server checks every canonical file against its
  MANIFEST sha256, recomputes `dataset_version` from those hashes, checks the counts, and
  checks every `api/v1` file carries the same `dataset_version`. If anything fails it exits 2
  with a message on stderr and serves nothing.
- **Follows the daily cycle safely.** Each request checks whether `MANIFEST.json` changed. A
  new layer that verifies is switched to; one that does not (for example, a half-written
  update) is ignored, the previous snapshot keeps serving, and every answer says
  `snapshot_current: false`, with the reason in `source_dataset_info`.
- **Publication gate.** Only relationships with `recommended: true` are returned as
  recommendations. For a supported product the first rows are exactly its published
  `api/v1` page, byte for byte. Further rows continue the same ranking over the canonical
  layer and are flagged `beyond_published_page`. Non-recommended relationships are counted,
  and `source_get_relationship` explains why each one was not recommended. Relationships the
  export guard withholds are not in the layer, so the server cannot serve them.
- **Attribution travels with the data.** Every tool result has a `meta` block:
  `dataset_version`, `data_as_of`, `licence` (CC BY 4.0), `attribution`, `notice`.
- **Read-only, local, private.** Standard library only. No network, no writes, no
  telemetry. Queries and URLs are not logged. `source_detect_saas` matches a URL against
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
| `source_get_alternatives` | `saas` (id, name, alias, domain or URL), `limit` 1-50 (default 5): ranked recommendations with status, last checked, scoring version and provenance |
| `source_detect_saas` | `url`: the supported product it belongs to, by the READ-API.md rules |
| `source_search` | `query`, `kind` (any, saas, project), `licence`, `maintenance_status`, `min_health`, `supported_only`, `limit`: lexical retrieval over the catalogue and projects |
| `source_get_project` | `project` (oss_id, repository URL or name): live facts and every relationship, recommended or not |
| `source_get_relationship` | `saas`, `project`: the full relationship with upstream provenance (list, pinned commit, line), Replacement Fit dimensions and the reason it is or is not recommended |

Resources: `source://data/canonical/MANIFEST.json`, `source://data/canonical/NOTICE.md`,
`source://api/v1/index.json`, `source://api/v1/domains.json`, and
`source://api/v1/saas/{saas_id}` for each supported product.

### Retrieval

`source_search` ranks with BM25 over weighted fields. For a SaaS product the fields are
name ×3, aliases and id ×2.5, category ×1.5 and domain ×1. For a project they are name ×3,
owner/repository ×2, the names and aliases of the products it is recommended for ×1.5,
their categories ×1 and licence ×0.5. An exact name match adds 10. Ties sort by kind, then
id. Every hit reports which query terms matched which fields. Retrieval is lexical and
exact. Like the graph layer's TF-IDF model, it could be replaced by a dense index without
changing the tool contract.

### Errors

Protocol errors use JSON-RPC codes: -32700 for a parse error, -32600 for an invalid request
(including a batch, or a request before `initialize`), -32601 for an unknown method, -32602
for an unknown tool and -32002 for an unknown resource. A call the server cannot answer
returns `isError: true` with a message naming the problem: a bad or unknown argument, a
`limit` out of range, an unknown or ambiguous product or project, or no such relationship.
An unexpected exception becomes -32603, and the server keeps running.

## Protocol

Newline-delimited JSON-RPC 2.0 on stdio. Protocol versions `2025-06-18` (with
`structuredContent`), `2025-03-26` and `2024-11-05` (text content only). A client asking for
another version gets `2025-06-18`. Capabilities: `tools`, `resources`. Neither list changes
during a session.

Not provided: an HTTP transport, authentication or a hosted endpoint. Exposing the server
beyond a local process is a publication decision. It is outside this server.
