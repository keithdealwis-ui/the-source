# KEI-851 acceptance map: The Source MCP retrieval server

**Read this first.** The dispatch for KEI-851 said "satisfy the twelve acceptance criteria
above", but the criteria themselves were not in the text the executor received. The twelve
below are the executor's reconstruction from the objective ("build and demonstrate The
Source MCP retrieval server against the canonical Source read layer ... return independent
verification evidence ... respect all existing security, privacy and publication gates")
and from the sibling acceptance maps (KEI-807, KEI-848). Draper must re-map this evidence to
the ticket's own wording before treating any criterion as met.

Server: `source_pipeline/mcp_server.py`. Docs: `docs/MCP.md`. Tests:
`tests/test_mcp_server.py`. Independent checks: the evidence pack's `verify_kei851.py`,
which imports no producer code. Producer: linux-claude, 2026-10-10. Read layer at the base:
dataset `a6e5bef0eaf47a24` (cycle `2026-10-10-schedule-a1`).

| # | Criterion (reconstructed) | Status | Evidence |
|---|---|---|---|
| 1 | A working MCP server: stdio JSON-RPC 2.0, `initialize` with version negotiation, `tools/list`, `tools/call`, `resources/list` and `resources/read`, `ping` | Met | Protocol versions 2025-06-18, 2025-03-26 and 2024-11-05. Tests `test_initialize_negotiates_protocol` and `test_tools_list_is_read_only_and_strict`. |
| 2 | Interoperates with a standard MCP client | Met (SDK); Claude Code blocked by host policy | The official MCP Python SDK 1.30.0 client (`ClientSession` over `stdio_client`, which validates every response against the MCP schema) initialised, listed 7 tools and 24 resources, read NOTICE and made 10 calls: 9 answered, plus 1 intended error. Transcript in the pack. On this executor, Claude Code refuses ad-hoc MCP servers ("enterprise MCP config is present"). Registering the server there is a managed-policy decision. |
| 3 | Answers only from the canonical Source read layer | Met | It reads `data/canonical/` and `api/v1/` only. An audit-hook test runs the server on a directory holding only those two folders and checks every file it opened. Nothing in the graph, discover, live, staging, history, momentum, radar or intake layers is read. |
| 4 | Retrieval: alternatives by product name, alias, domain or URL; search with filters; project and relationship lookups | Met | 7 tools (`docs/MCP.md`). Search is BM25 over weighted fields, with licence, maintenance and Project Health filters, and it reports which terms matched which field. Resolution covers ids, names, aliases, domains, URLs and repository URLs. Ambiguous input is refused with the candidates named. |
| 5 | Results are consistent with the published read API | Met | For all 20 supported products, `source_get_alternatives` returns exactly the `api/v1/saas/<id>.json` recommendations, status, provenance and last_checked. Rows past the page continue the same ranking and are flagged `beyond_published_page`. The verifier recomputes this from the files. |
| 6 | Publication gate: only recommended relationships are recommendations; withheld or stale material is never served | Met | Across all 118 catalogued products, the set served as recommendations equals the `recommended: true` set exactly (310 pairs), and every served project is active or maintained and not archived. The 39 non-recommended pairs are counted, and `source_get_relationship` explains each one. Relationships the export guard withholds are not in the layer. |
| 7 | Provenance, licence and attribution travel with every answer | Met | Every tool result has `meta`: `dataset_version`, `data_as_of`, `licence` CC BY 4.0, `attribution`, `notice`. Relationships carry upstream provenance (source, pinned commit, locator). NOTICE.md is a resource. |
| 8 | Integrity: a layer that does not verify is never served | Met | Checked at start: MANIFEST hashes, the recomputed dataset_version, counts, and the same dataset_version on every api/v1 file. Tamper tests (one flipped byte, an api version mismatch, a missing page, a wrong count) give exit 2, an empty stdout and "refusing to serve". |
| 9 | Freshness and degradation follow READ-API.md | Met | The server switches to a new verified snapshot when the cycle updates the files. If an update is half-written or malformed, the old snapshot keeps serving with `snapshot_current: false` and the reason. `source_dataset_info` reports live-check age and `stale` after 14 days. |
| 10 | Security and privacy: read-only, no network, no telemetry, bounded and validated input | Met | Standard library only, checked by AST. An audit hook records no socket events, and the read-layer files are byte-identical after a session. All tools are `readOnlyHint`. Schemas set `additionalProperties: false`. `limit` is 1-50 and text at most 300 characters. File paths are never built from caller input: pages are reached through the index, and resources through a fixed map. URLs are matched locally and never fetched or logged. stdout carries protocol messages only. |
| 11 | Deterministic, robust error handling | Met | The same layer and `--now` give byte-identical transcripts (tested twice in-process, and again in the pack). Parse, batch, unknown-method, unknown-tool and unknown-resource requests get JSON-RPC errors. Bad arguments get `isError` results. The server survives every case. |
| 12 | Tests, documentation, CI and an independent evidence pack | Met | `tests/test_mcp_server.py` has 43 tests and is added to `ci.yml`. `docs/MCP.md`, a README link and the `mcp-serve` CLI verb are added. On the branch, 134 tests pass in the CI test set and `canonical-validate` is ok. The aiq-verifier-pack/1 pack is `KEI-851-mcp-retrieval`. |

## Not done here (gates)

- No HTTP or remote transport, authentication or hosted endpoint. Exposing the server
  beyond a local process is a publication decision (Keith/Draper).
- No Claude Code, Claude Desktop or Claude.ai registration on any host. On this executor it
  is blocked by managed MCP policy.
- The branch has not been merged to `main`.
