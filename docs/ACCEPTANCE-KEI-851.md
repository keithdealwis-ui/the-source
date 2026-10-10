# KEI-851 acceptance map: The Source MCP retrieval server

Mapped to the ticket's own twelve acceptance criteria (the earlier version of this file
mapped a reconstruction, because the first dispatch omitted them).

- **Endpoint:** `https://source-mcp.de-alwis.com/mcp` (Streamable HTTP). It is public, anonymous
  and read-only, approved in gate-KEI-851-20c71990.
- **Code:**
  - `source_pipeline/mcp_server.py`: the engine. It uses the standard library only, opens no
    network connections and writes nothing.
  - `source_pipeline/mcp_http.py`: the transport.
  - `deploy/the-source-mcp/`: the hosted deployment.
- **Commits:**
  - `695303b`: the source stage, built by linux-claude and independently verified by Alpha
    (`aiq-pack-generic` PASS, packs `KEI-851-mcp-retrieval` and `-r2`).
  - `fc7ea58` and `a361712`: the remaining scope, built by attended Claude Code.
- **Data served:** dataset `a6e5bef0eaf47a24` (cycle `2026-10-10-schedule-a1`), refreshed hourly
  from public `main`.
- **Evidence:** pack `KEI-851-mcp-v1` holds the commit bundle, the live client transcripts and
  the checks.

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | A remote/usable MCP server is demonstrated against the live canonical Source intelligence | Met | The hosted endpoint serves the verified commit over TLS, behind nginx. Its data is a sparse clone of public `main` (`data/canonical`, `api/v1`, `data/momentum`, `data/radar`), pulled every hour; the server re-verifies every snapshot before serving it. `/healthz` reports `serving_dataset_version`. Both client transcripts were recorded against this URL. |
| 2 | At least the six V1 capability groups are exposed through stable schemas | Met | discovery: `source_search` (lexical or hybrid); alternatives: `source_get_alternatives`; project intelligence: `source_get_project`, `source_get_relationship`, `source_get_momentum`; comparison: `source_compare_projects`; recommendations against explicit requirements: `source_recommend`; emerging/accelerating: `source_emerging_projects`. 11 tools in all. Every schema has `additionalProperties: false` and is read-only annotated. `test_six_capability_groups_have_tools`. |
| 3 | Representative natural-language agent tasks resolve to structured Source-backed results | Met | Claude Code was given the ticket's own prompts in plain English (alternatives to Notion; self-hosted, maintained, copyleft Notion replacements for a 20-person company with a strong API; Docmost vs AppFlowy; projects under 10,000 stars with surging/rising 7-day momentum). It chose the tools itself and answered from `structuredContent`, citing the dataset version (`claude-code-hosted.jsonl`). Every tool result is JSON with a `meta` block. |
| 4 | Results include canonical project identifiers, evidence/provenance and freshness/version metadata | Met | Every result carries `oss_id` / `saas_id` and `edge_id`, upstream `sources` (with a pinned commit and line in `source_get_relationship`), and `live_checked_at`. Every answer's `meta` has `dataset_version`, `data_as_of`, licence and attribution. Momentum answers carry each signal's `as_of`, `age_hours` and `status`. |
| 5 | Replacement, constrained recommendation, comparison and emerging-project queries demonstrated end to end | Met | Each was run against the hosted endpoint by the MCP Python SDK client (`sdk-client-hosted.json`: `source_get_alternatives`, `source_recommend` with licence family, self-hostable and maintenance constraints, `source_compare_projects`, `source_emerging_projects`) and by Claude Code (above). `test_recommend_applies_every_constraint_exactly` recomputes every constraint. |
| 6 | Retrieval uses KEI-844 semantic/graph capability where it materially improves the result | Met, with residue | Hybrid mode combines lexical BM25, KEI-844's semantic model (`tfidf-sublinear/1`; the same tokeniser, asserted by `test_semantic_tokeniser_is_kei844s`) and SaaS -REPLACES-> project graph traversal weighted by Replacement Fit, with the components returned. It materially improves results. "open source password manager": lexical returns Open Web Analytics, Passbolt, Gogs; hybrid returns Passbolt only, via the graph path. "customer support helpdesk": hybrid ranks Zammad, osTicket, FreeScout by fit through Zendesk's edges. `source_recommend` maps a need in words to products only through hybrid retrieval. **Residue (NON_BLOCKING):** the full KEI-844 graph is built from the discover corpus, which is not published, so the server applies the KEI-844 model and the published relationship graph to the published layer. |
| 7 | Repository Momentum and Community Momentum are independently available once their upstream tickets are complete | Met, with residue | Repository Momentum (`data/momentum`, 625 repositories, windows 1/7/30/90) and Community Radar (`data/radar`, HN + DEV) are served as separate measures by `source_get_momentum`, `source_emerging_projects` and `source_compare_projects`, and never folded into Health or Fit. **Residue (NON_BLOCKING, upstream history):** 7-day acceleration becomes measurable on 2026-10-16 and 30-day momentum on 2026-11-01. Until then the server says so and gives the date, instead of inventing a figure. |
| 8 | Failure/degraded states are explicit when an upstream surface is unavailable or stale | Met | Each signal layer is verified against its MANIFEST and reported as `ok`, `stale` (over 48 h) or `unavailable` with the reason, and the canonical tools keep serving (`test_missing_signals_degrade_explicitly...`, `test_tampered_signal_is_not_served`, `test_old_signal_is_reported_stale`). A canonical layer that fails verification is never served. A half-written update keeps the last good snapshot, marked `snapshot_current: false`. Windows that cannot be measured are listed in `degraded`. |
| 9 | No private AIQ/vault data is exposed through the public interface | Met | The server reads only the four published directories of the public repository; an audit-hook test records every file opened. The hosted data directory is a sparse clone of public GitHub `main`. It has no write tools and no credentials, and nothing from the vault, Bravo control plane or discover corpus is on the path. |
| 10 | Setup and example prompts documented and proven from at least two independent MCP-capable clients | Met | `docs/MCP.md` gives setup for Claude Code and the MCP Python SDK, plus example prompts. Proven against the hosted URL: (1) the official MCP Python SDK 1.30.0 `streamablehttp_client` (initialize 2025-06-18, 11 tools, 7/7 calls); (2) Claude Code on the Mac via `--mcp-config` (connected, 6 tools called, four prompts answered). |
| 11 | Aggregate usage telemetry is privacy-safe and retains no user prompt/content beyond what is required and documented | Met | The server keeps per-day, per-tool, per-outcome counts and a latency histogram for 90 days (`the-source.mcp-telemetry/1`). It keeps no arguments, results, client identity, sessions or addresses. nginx `access_log off`. Rate-limit addresses are held in memory for one minute only. `test_telemetry_is_aggregate_only` and `test_telemetry_file_holds_counts_only` plant a query and an address and check that neither is written. A live sample is in the pack. |
| 12 | Independent verification confirms the MCP is a read/retrieval product surface, not an alternate source of truth | Pending Alpha r3 + Draper | Alpha `aiq-pack-generic` already passed `695303b` (r2). Pack `KEI-851-mcp-v1` checks over stdio, importing no producer code: reads only the published layer; no write or network events; files unchanged after a session; every tool read-only; answers equal the source files. Draper maps this table. |

## Operations

- **Bounds:** nginx allows 60 requests/min per address with a burst of 20 (429 beyond that) and a
  64 KB body cap. The server allows 120 requests/min per address, 8 in flight and 64 KB
  bodies. Live check: 90 rapid requests got 24 × 200 and 66 × 429.
- **Rollback:**
  - `systemctl disable --now the-source-mcp the-source-mcp-refresh.timer`
  - remove `/etc/nginx/sites-enabled/source-mcp.de-alwis.com` and reload nginx
  - delete A record `source-mcp` (recordId `fed99011-fd15-425d-9d2b-c4b294359e85`)
- **CI and merge:** pushing the branch is Keith's action (`engineering.repo.push_to_remote` is
  denied to agents). The push triggers `ci.yml` on the branch, which runs the full test set
  including the three MCP test files (147 passed locally). `main` then moves only through
  `aiq-merge-verified` after an Alpha PASS of this exact commit.
