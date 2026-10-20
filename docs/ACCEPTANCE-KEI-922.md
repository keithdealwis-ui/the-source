# KEI-922: The Source V2.1 WP5, the demand-led 500-product catalogue (acceptance map and task log)

Executor: linux-claude on aiq-exec-1, as continuation owner while the Mac executors are unroutable (travel mode).
Demand resume (Option A, 2026-10-19): attended Claude Code on the Mac. See "Demand resume" below.
Method: [`CATALOGUE.md`](CATALOGUE.md).

## Demand resume (Option A, dr-KEI-922-c2ed1cb7), 2026-10-19

- **Source.** This tree was reconstructed from the sealed pack `KEI-922-wp5-catalogue-source` (tarball `5100ac65`, 503/503 files matching `TREE-SHA256`). It was then changed only by importing demand and rerunning the pipeline.
- **Demand evidence.** Two aiq-kwi Google Ads `generateKeywordHistoricalMetrics` runs on Bravo (US, en, API v25). Both are free keyword-planning calls; no spend and no new credential.
  - The attended run of 2026-10-18T03:41Z: 1,060 products × 4 alternative-intent phrases, imported as `kwi-5a6edbf0d39a.json`.
  - One bounded run on 2026-10-19 for the 60 candidates it did not cover (79 names and aliases, 312 phrases), imported as `kwi-5bce57bcbc3a.json`.
- **Coverage.** 600 of the 602 candidates now have measured demand. Every row's `demand.status` is `measured` or `not_covered`; none says `unavailable`.
- **Effect on selection.** Demand changed 69 of the 500 selected products and moved ranks; statuses did not change. Products that came in include Google Sheets, Google Calendar, Databricks, Gong, Greenhouse and Expensify. Lower-demand cloud primitives dropped out, for example Amazon SQS and SNS, Azure Blob Storage, and the HubSpot sub-hubs.
- **Effect on criterion 2: none.** Status counts are 49 ok, 65 partial, 386 none_yet, the same as before demand. Demand only orders the selection; ok comes only from full evaluations inside the launch cap. The literal ≥250 threshold remains unmet and is not manufactured.
- **Checks.**
  - Every validator passes: canonical (38), catalogue, quick, momentum, radar, corpus, obs-golden and obs-lineage.
  - pytest on the CI list plus `test_catalogue.py` and `test_mcp_http.py` (loopback is up on the Mac): 341 passed, and the same 4 known failures.
  - The extension tests pass 32/32.

## Recovery and baseline

- **Former work.** None was available. Origin has no `kei-922` or WP5 branch (`git ls-remote origin`), and no local branch, worktree or commit mentions KEI-922 or WP5. The former Mac-local worktree was not reachable and was not assumed. Nothing was recovered, so nothing could be overwritten or duplicated.
- **Baseline.** `origin/main` at `88df71ae4a454b47c24ddd7177708a66d02e92c3` (cycle `manual-20261018T024719Z-manual-a1`, dataset `1e1edfdb836490f2`).
- **Workspace.** Task-isolated worktree `the-source-kei-922` on local branch `kei-922-wp5-catalogue`. The main workspace was left on `kei-918-wp1-observability`, clean. Nothing has been committed or pushed.

## Acceptance

| # | Criterion | Result |
|---|---|---|
| 1 | 500 catalogued products | **Met.** 602 candidates (268 existing plus 334 added and reviewed), tagged b2b 416, b2c 82, both 104, each with a category and detection rules. 500 selected: 20 already served, 4 named by the ticket, 90 with a fully evaluated recommendation, 386 by rank. `api/v1/catalogue.json` publishes all 500. |
| 2 | ≥250 `ok`, only if genuinely supported | **Not met, and not manufactured.** 49 are `ok`, 65 `partial`, 386 `none_yet`. Every status is recomputed by `canonical-validate` from the canonical relationships; ok needs ≥3 recommendations that passed the full evaluation. See "Why there are 49, not 250". |
| 3 | Slack, Salesforce, Zapier, 1Password evaluated | **Met.** All four are in the catalogue and have support records. Salesforce is **ok** (3 full evaluations: warpdrive 90.4, idurar-erp-crm 77.1, erpnext 63.2). Slack is **partial**: Zulip is recommended (86.7); Element is scored but not a credible alternative (45.7); Spacebar failed the core gate; Colanode and Huly are withheld as share-alike-only; Mattermost, Mezon and Rocket.Chat wait on a licence decision. Zapier is **partial**: Huginn is recommended (80.9); n8n, Activepieces, Automatisch, Pipedream and Tracardi wait on a licence decision. 1Password is **partial**: Passbolt is recommended (71.5); Bitwarden (clients and server) and KeePassXC wait on a licence decision. |
| 4 | Detection tests cover every catalogue entry | **Met.** Every one of the 1,197 rules across all 602 candidates resolves its own URLs to its own product among every candidate's rules. The 82 examples hold, covering HubSpot hubs on `app.hubspot.com`, Confluence, Jira and JSM on `*.atlassian.net`, Google docs and maps, Microsoft SharePoint and OneDrive, AWS console paths, GitHub, Figma and Salesforce, plus negatives. This is checked in `catalogue-validate`, `canonical-validate` (`catalogue_entries_detectable`), `tests/test_catalogue.py`, and in JavaScript (`extension/test/match.test.mjs`, which also checks JS and Python agree over 5,000+ URLs). |
| 5 | Transparent none_yet and partial | **Met.** Every entry has `reasons` (codes) and `reason` (a sentence), plus evidence counts: awaiting evaluation, evaluated but not recommended, outside the launch selection, share-alike only, licence review, corpus pairings, and whether the category has a job definition. |
| 6 | No external publishing | **Met.** No commit, push, deploy or publish was done. The de-alwis.com comparison cluster (KEI-809) was not touched. `compare_path` is only a field, given to ok products. |

## Why there are 49, not 250

1. **The launch policy caps the published layer.** KEI-805 decision dr-KEI-805-507e4f70 sets `config/policy.yaml` `launch: edges_max: 400`. That keeps the canonical layer to about 116 products and 345 relationships, and only the canonical layer can carry a recommendation. 122 selected products have validated relationships outside it. Even with the cap lifted, the full evaluations that exist would make 54 products ok (60 counting share-alike-only relationships; 55 and 61 before demand reordered the selection). Changing the policy is not this executor's call.
2. **Nothing has been found for many products.** 194 selected products have no relationship evidence at all. Upstream lists and corpus self-descriptions name no open-source alternative for them yet.
3. **Some categories cannot be evaluated.** 23 categories among the selected products have no job definition in `config/saas_jobs.yaml`, for example Payments, Point of sale, Application security and SEO. The full evaluation cannot score their pairs.
4. **No new full evaluations here.** This executor has no GitHub token. Full evaluation needs the GitHub API, so the 45 relationship candidates that admitting the new products surfaced (31 recommendable) remain unevaluated. Today all 31 are outside the launch selection anyway.
5. **Demand does not create support.** Measured demand now covers 600 of the 602 candidates (see "Demand resume"). It changes which products are selected and in what order, and the ok count stays at 49. KEI-918 telemetry has no per-product dimension by design.

## What changed

- **New:** `config/catalogue.yaml`, `source_pipeline/catalogue.py`, `source_pipeline/detect.py`, `tests/test_catalogue.py`, `docs/CATALOGUE.md`, `docs/ACCEPTANCE-KEI-922.md`, `data/catalogue/{candidates.jsonl,selection.json}`, `api/v1/catalogue.json`, and 94 new support records `api/v1/saas/*.json`.
- **Code:**
  - `source_pipeline/canonical.py`: catalogue states, support records for ok and partial products, `catalogue.json`, an evidence block per recommendation.
  - `source_pipeline/canonical_validate.py`: KEI-922 checks, and the served set taken from the index.
  - `source_pipeline/mcp_server.py`: `source_detect_saas` uses the shared matcher.
  - `source_pipeline/cli.py`: the `catalogue-*` commands.
  - `extension/src/match.js`: whole-segment paths, most specific rule wins, segment-exact Chrome filters.
- **Config:**
  - `config/saas_catalogue.yaml`: 334 products added; Otter.ai gains the alias "Otter".
  - `config/supported_saas.yaml`: detection rules moved to `catalogue.yaml`.
  - `schema/the-source.canonical.schema.json`.
  - `.github/workflows/ci.yml`: adds `catalogue-validate` and `tests/test_catalogue.py`.
- **Regenerated, offline, the way the cycle does it:**
  - `data/dataset/` (`build`): 45 new relationship candidates from upstream claims that now name catalogued products; `saas_not_in_catalogue` rejections fell from 197 to 152.
  - `data/quick/` (`quick-score`, incremental).
  - `data/canonical/`, `api/v1/` (`canonical-build`).
  - `data/radar/MANIFEST.json` (`radar-build`; input hashes only).
- **Tests and docs:** `tests/test_canonical.py`, `tests/test_mcp_server.py` and `extension/test/match.test.mjs` updated for the new semantics. `README.md`, `docs/READ-API.md` and `extension/README.md` updated.

## Commands run (worktree, `THE_SOURCE_NO_NETWORK=1` where the CI sets it)

| Command | Result |
|---|---|
| `python -m source_pipeline build` | ok, 43 checks |
| `python -m source_pipeline quick-score`, then `quick-validate` | ok, 0 API calls |
| `python -m source_pipeline catalogue-rank` (repeated until stable) | 500 selected; a second run is byte-identical |
| `python -m source_pipeline canonical-build`, then `canonical-validate` | ok, 38 checks including the byte-identical rebuild |
| `python -m source_pipeline catalogue-validate` | ok, 12 checks |
| `radar-build`, `radar-validate`, `momentum-validate`, `corpus-validate` | ok |
| `obs-golden --baseline observability/golden-baseline.json --check --summary` | ok; precision@5 0.7801, recall@5 0.7122, the same as the base |
| `obs-lineage verify` | ok |
| `pytest` on the CI list plus `tests/test_catalogue.py` | 341 passed, 4 failed (below; the same 4 pass on the base) |
| `cd extension && node --test test/*.test.mjs` | 32 passed |

## Known failures and follow-ups for Draper

- `tests/test_observability.py::test_intake_report_follows_the_funnel_of_the_live_run` and `::test_dataset_info_reports_the_last_run` fail on this branch by construction. They require the committed data to be the newest cycle's output, and this branch rebuilds data outside a cycle; the newest run record names dataset `1e1edfdb836490f2`. They should pass after the first daily cycle on the merged result. No run record was fabricated, and the tests were not weakened.
- `tests/test_kei919_correctness.py::TestLiveLayer::test_no_integrations_in_supported_product_discovery` and `::test_golden_set_targets` (`integrations_in_top5` 1) fail because Zapier is now served. Its discovery candidates include `activepieces/activepieces`, a genuine Zapier alternative that upstream lists name and that comes through the open-core "listed" path. The KEI-919 integration heuristic classifies it as `mcp_server` from its description. This is a pre-existing false positive, exposed rather than caused here. It needs its own ticket.
- **Policy decision:** whether to raise the launch cap (`edges_max: 400`, dr-KEI-805-507e4f70) so that catalogue products beyond about 116 can be served.
- **Demand:** done on 2026-10-19 (see "Demand resume").
- **Raw Google Ads keyword volumes (f2), resolved 2026-10-20:** Keith chose to keep them private. `data/catalogue/demand/` is not committed, and `data/catalogue/candidates.jsonl` carries a demand band (high/medium/low/none) instead of `monthly_volume` and the rank score.
- **Evaluation coverage:** add job definitions for the 23 uncovered categories, and let the cycle (which has a GitHub token) score the new relationships once they are in the published layer.
- **Extension:** the surface the extension downloads grows from about 170 KB (20 products) to about 0.9 MB (114 products, with evidence blocks). Releasing the updated matcher (`extension/src/match.js`) to the store is a separate step. The store version still reads the new `domains.json` with its older first-match matcher; it lacks only the whole-segment and most-specific refinements.
