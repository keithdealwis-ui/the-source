# KEI-912 acceptance map

The Source 08: serve the full discover corpus via MCP as a discovery tier. Revision deployed to
`https://source-mcp.de-alwis.com/mcp`: `d5afedb` (served code identical to the verified `15d043d`,
plus an installer fix). Publication gate: `gate-KEI-912-dba84c8b` (Keith, option A).

| # | Criterion | Evidence |
|---|---|---|
| 1 | Every publishable corpus project retrievable by id, name or URL on the hosted endpoint; served count = MANIFEST | `data/corpus/MANIFEST.json` counts.projects 9,364; hosted `source_dataset_info` corpus.projects_served 9,364 (9,133 discovery + 231 joined to canonical); pack check C5/C6: all 9,364 retrieved by id with the expected tier, a sample by URL; ambiguous names answer with the candidates |
| 2 | Every result carries `tier`; no discovery or scored project in any recommendation list, tested over the whole layer | `tests/test_corpus.py::test_no_discovery_or_scored_project_is_ever_recommended`; pack C7 (every catalogued product, `source_get_alternatives` and `source_recommend`, incl. `discovery_candidates` rules) and C8 (search, every mode and tier filter) |
| 3 | Recommendations byte for byte unchanged | `scripts/recommendation_parity.py`: 256 answers identical to `origin/main` 54eb2cb; `test_the_corpus_cannot_change_a_recommendation`; pack C10 |
| 4 | Discovery rows carry provenance (list + pinned commit) and `observed_as_of`; licence-issue projects flagged | 1,393 flagged with `licence.issue` and a served warning (pack C4, C9); curated routes carry `list:owner/repo`, a 40-hex commit and `line N` (C3) |
| 5 | Full-corpus hybrid search materially better than canonical-only, with score components | `scripts/corpus_search_compare.py`: e.g. "browser automation for AI agents" → browser-use, agent-browser (was Huginn and two transcription apps); "terminal file manager" → nnn, yazi (was ownCloud, Syncthing); components per hit |
| 6 | Refreshes on schedule without the Mac; stale/unavailable explicit | `.github/workflows/corpus-refresh.yml` (daily wake-up, weekly refresh, 50 per GraphQL request, ~190 requests, budget ceiling, keep-previous on failure); live refresh 224 requests / 7m49s / 0 unresolved; `corpus.status` ok → stale after 10 days (C12), unavailable on a bad MANIFEST (C11) |
| 7 | p95 latency and memory within bounds on Bravo at full size | Bound: server-side p95 ≤ 500 ms per tool, peak ≤ 60% of the unchanged 512M MemoryMax. Bravo (python 3.12, service user): RSS 142 MB, 232 MB peak during a reload, worst tool p95 320 ms (search); service MemoryCurrent 132 MiB after deploy; hosted end to end from the Mac p95 436 ms |
| 8 | No private AIQ/vault data, no republished list text; licence review recorded | Seed routes published as `maintainer_seed` only; validator and pack C2 refuse seed markers and any locator other than `line N`; `docs/LICENCE-REVIEW.md` KEI-912 section; `DATA-LICENCE.md` |
| 9 | Two independent MCP clients against the hosted URL | MCP Python SDK 1.30.0 (`evidence/hosted/sdk-client-hosted-912.json`, 9/9 calls, `tier` filter in the schema) and Claude Code over HTTP (`evidence/hosted/claude-code-hosted.json`) |
| 10 | Independent verification (Alpha pack + Draper) before Done | Pre-serve pack KEI-912-corpus-v1: Draper PASS (orch-KEI-912-c1d8e892); governed merge 54eb2cb → 15d043d, 200 snapshot files matched; post-serve pack KEI-912-corpus-v2 |

Residue (non-blocking): membership changes only when the Discover lane is rebuilt where the private
seed list lives; search has no popularity prior, so a small repository with exact wording can
outrank a flagship; the 2026-10-10 12:40Z manual daily cycle failed closed on GitLab API 403s
(unrelated to KEI-912, first failure in the run history).
