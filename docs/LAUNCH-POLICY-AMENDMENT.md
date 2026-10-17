# Launch-policy amendment

**Status:** **approved by Keith on 2026-10-17**, together with the three Databend corrections, with
pair-level eligibility enforced. Edition-check exceptions stay **off**
(`config/licence_lane.yaml` `edition_check.generated_exceptions_feed_the_build: false`). `licence_unclear`
is not cleared, and the 17 new relationships are not activated. Turning either on needs its own decision.
With the switch off, the rebuild keeps all 339 served relationships and all 301 recommendations
(regression against `main` b304f29: 301 retained, 0 added, 0 removed, 0 changed).

## The six requirements and how they are met

| # | Requirement | Mechanism | Evidence |
|---|---|---|---|
| 1 | Keep the served SaaS set; adding projects never silently removes products or recommendations | `policy.yaml` `launch.preserve_served: true`; `build.select_launch` keeps every relationship in `data/canonical` unless it stops validating, and records why if it does (`summary.launch.preserve_served.not_kept`) | With the switch on: 301 of 301 recommendations retained, 0 removed, 0 changed |
| 2 | 250 stays a minimum, not a reason to replace coverage | Served products join the top-100 floor; the per-product cap limits additions only, never served relationships; `validate.launch.ranking_unchanged` amended to "top of the ranking plus products already served" | 116 launch SaaS, 262 OSS, 383 edges (≤ 400) with the switch on |
| 3 | Fail closed on ambiguous edition rights; a project-level pass is not enough when the job needs enterprise parts | Pair-level check `edition_rights_cover_the_job` (`edition_check.pair_rights`): a pair is served only if no defining phrase of the product's job (`saas_jobs.yaml`) names an excluded part. `unknown` (unlisted marker files, no job) fails. The project check also goes ambiguous when the excluded part is the project's own runtime (`excluded_part_is_a_runtime_component`) | FullStory→PostHog and Hotjar→PostHog withheld (session recording sits in PostHog's `ee/`). AFFiNE (`packages/backend/`) and Stirling-PDF (`engine/`) moved from pass to ambiguous |
| 4 | Score the new pairs before serving | Activation re-scores every edition-check project through the cycle's single-project path (`cycle._rescore`) | 14 projects re-scored on 2026-10-17; results below |
| 5 | Before/after regression with reasons | `scripts/launch_regression.py --before <rev> --after <root>` | `docs/launch-amendment/regression-*.json` |
| 6 | Investigate Databend | Below | The current disclosure is **not sufficient** |

## Regression (2026-10-17, against `main` a5e38ee)

| | Served now | Switch on, no amendment | Switch on, with amendment |
|---|---|---|---|
| Recommended relationships | 301 | 278 (**23 removed**) | **301 (0 removed, 0 changed)** |
| Canonical relationships | 339 | 336 | 356 |
| SaaS products with relationships | 114 | 99 (15 removed) | 115 (+SurveyMonkey) |
| New relationships recommended | — | 0 | 0 of 17 |

Removed without the amendment: Circle, Fivetran, Gmail, Loggly, Mode (×2), MongoDB Atlas, OneSignal (×2),
Papertrail, Pingdom (×2), Power BI, Render, SAP Business One (×2), Snowflake, Splunk (×2), Twitch (×2),
Wix (×2). Every one of them "not in the launch set": they were displaced, not invalidated.

### The 17 new relationships (amendment on): scored, none served

| Pair | Fit | Edition rights | Served? | Reason |
|---|---|---|---|---|
| amplitude → posthog/posthog | 79.0 | covered (session) | no | not publishable: project carries a hard disqualifier: licence_unclear |
| auth0 → goauthentik/authentik | 78.1 | covered | no | not publishable: project carries a hard disqualifier: licence_unclear |
| docusign → opensignlabs/opensign | 76.8 | covered | no | not publishable: project carries a hard disqualifier: licence_unclear |
| fullstory → highlight/highlight | 66.3 | covered | no | not publishable: project carries a hard disqualifier: licence_unclear |
| google-analytics → posthog/posthog | 67.9 | covered (api) | no | not publishable: project carries a hard disqualifier: licence_unclear |
| hubspot → twentyhq/twenty | 59.3 | covered (api, email, report) | no | not publishable: project carries a hard disqualifier: licence_unclear |
| launchdarkly → growthbook/growthbook | 77.3 | covered | no | not publishable: project carries a hard disqualifier: licence_unclear |
| looker → lightdash/lightdash | 71.4 | covered | no | not publishable: project carries a hard disqualifier: licence_unclear |
| mixpanel → posthog/posthog | 79.0 | covered (session) | no | not publishable: project carries a hard disqualifier: licence_unclear |
| okta → goauthentik/authentik | 78.1 | covered | no | not publishable: project carries a hard disqualifier: licence_unclear |
| openai-api → berriai/litellm | 72.3 | covered | no | not publishable: project carries a hard disqualifier: licence_unclear |
| pipedrive → twentyhq/twenty | 59.3 | covered (api, email, report) | no | not publishable: project carries a hard disqualifier: licence_unclear |
| salesforce → twentyhq/twenty | 62.6 | covered (api, email, report) | no | not publishable: project carries a hard disqualifier: licence_unclear |
| surveymonkey → formbricks/formbricks | 60.6 | covered | no | not publishable: project carries a hard disqualifier: licence_unclear |
| tailscale → gravitl/netmaker | 55.6 | covered | no | not publishable: project carries a hard disqualifier: licence_unclear |
| typeform → formbricks/formbricks | 60.6 | covered | no | not publishable: project carries a hard disqualifier: licence_unclear |
| zapier → activepieces/activepieces | 60.8 | covered (api) | no | not publishable: project carries a hard disqualifier: licence_unclear |

**Finding for requirement 4:** none of the 17 can be published. Project Health applies the hard
disqualifier `licence_unclear` to any licence status other than `recognised`
(`score_build.disqualifiers`), and an approved exception is `exception_approved`. That fails closed. So
switching the exceptions on today adds these relationships to the canonical layer as **not recommended**,
and serves none of them. Serving them needs a separate, explicit change. One option: treat
`exception_approved` with an edition-check exception as usable in that disqualifier, and keep the
disclosure. This is a scoring-gate change, not a weight change. It is not made here. If approved, the
Replacement Fit scores above (55.6–79.0) would then decide each pair under the unchanged publish gate.

## Databend (requirement 6)

- **The canonical layer recognises it as Apache-2.0.** The dataset licence check is the host API alone
  (`detection.method: host_api`), and the host reports Apache-2.0. It is recommended for Snowflake today.
- **`LICENSING.md`** (8b1dc207 and e9b49fde) puts these paths under the **Elastic License 2.0**, unlocked with
  a paid key: `src/query/ee/`, `src/meta/ee/`, `src/bendsave/`, `src/binaries/query/ee_main.rs`,
  `src/binaries/meta/ee_main.rs`. The last of these does not exist at either commit (`src/binaries/` has
  only `query/` and `tool/`).
- **What the Elastic parts implement** (`src/query/ee/src/lib.rs` modules, `src/bendsave/`): attach table,
  data masking, data sharing, fail-safe, materialized views, resource management, row access policies,
  storage encryption, streams, table refs, and backup/restore.
- **Deployment:** the official `docker/Dockerfile` ships the default `databend-query` and
  `databend-meta` binaries, which include the Elastic code behind a licence key. Only the
  `databend-query-oss` and `databend-meta-oss` binaries are built without it. The edition check is
  ambiguous (`no_deployment_runs_the_project`).

### The three corrections (approved 2026-10-17, implemented here)

1. **Excluded files.** The classifier now reads file paths in a licence notice, not only directories
   (`licence_classify._file_tokens`). A file counts when it is in the tree, or below a directory the tree
   has deeper than it was read. Databend now excludes `src/binaries/query/ee_main.rs`. The listed
   `src/binaries/meta/ee_main.rs` is not in the repository, so it is not recorded.
2. **The core-only build.** A notice naming a build without the excluded parts is recorded as
   `classification.core_build` (`licence_classify.core_build`). The edition check also records whether
   the repository's own image is that build (`edition_check.core_build`). The disclosure names it, and
   says so when the default image ships the full build. Databend's disclosure now ends: "core-only build:
   databend-meta-oss, databend-query-oss (the repository's default image, docker/Dockerfile, ships the
   full build)".
3. **Recognised with a carve-out.** A project the host reports under a recognised licence, but which
   the corpus classifies as open core, stays `recognised` for its core and is treated as open core
   (`build.carve_out`):
   - every pair must pass `edition_rights_cover_the_job`;
   - the canonical licence carries `open_core` with the disclosure, and every recommendation carries
     `licence_disclosure`;
   - with no edition check (so no excluded scope), the pair is `unknown` and not validated.

   A served recommendation is preserved only while it still validates (`select_launch` keeps validated
   edges only), so an enterprise-only job is never kept as an OSS recommendation.

   **Result:** Databend is the only served project affected. Snowflake's job (data warehouse / OLAP; SQL,
   query, database) is not named by any excluded part, so the pair is `covered`. The recommendation stays,
   with the full disclosure. Materialized views is the only Snowflake feature among the enterprise parts.
   It is listed, and does not block.

**Limitation:** the carve-out rule applies where a corpus row has a licence classification. 94 dataset
projects recognised by the host have no corpus classification, because classification only runs on rows
the corpus flags. A `LICENSING.md` carve-out in one of them would not be seen yet.

## What switching on would change, with this amendment (measured before the Databend corrections; re-run before activation)

- **Dataset:** 15 projects move to recommendable. 17 relationships join the launch set; 2 more are
  withheld at pair level.
- **Canonical:** 17 relationships added, none recommended (until the scoring gate is decided). All 301
  served recommendations are unchanged.
- **MCP:** the added relationships appear as not recommended. Every open-core recommendation carries
  `licence_disclosure` (already true on `main` once #5 merges).

## Known verification limitations

- `tests/test_pipeline.py`: 3 tests fail on the Mac because the pinned upstream checkouts (`raw/`) are not
  present: `test_every_built_edge_traces_to_a_pinned_commit`,
  `test_offline_harvest_and_build_run_with_network_forbidden` and `test_rebuild_is_byte_identical`. They
  are not in CI's test set and fail the same way on `main`. This is a known gap, not a pass.
- The pair-level check is lexical: phrases from `saas_jobs.yaml` against excluded paths, module names
  and marker-file directories. It catches a job that names an excluded module. It cannot see an
  enterprise feature implemented under a neutral name.
- Marker files are listed through GitHub code search on the default branch at check time, not at the
  classification commit. An incomplete result is treated as unlisted (fail closed).
