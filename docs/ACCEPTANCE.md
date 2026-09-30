# KEI-805 — acceptance criteria mapped to evidence

Producer evidence, prepared by the executor that did the work (`claude-heavy`). It is not
independent verification.

- Dataset as of: `2026-09-27T05:08:30Z`
- Validation: 41 of 41 checks pass
- Publication status: `internal_not_published`
- Dataset files and SHA-256: `dataset/MANIFEST.json` (9 files)

Paths below are relative to the evidence pack root.

## 1. Canonical schema exists for SaaS products, OSS projects and SaaS↔OSS relationship edges.

`schema/the-source.schema.json` defines `source`, `saas_product`, `oss_project` and `relationship`. `docs/SCHEMA.md` describes it. Every record in the dataset is validated against it on every run: 9 sources, 268 SaaS products, 590 OSS projects, 849 relationships.

Artefacts and tests: `TestBuiltDataset::test_records_conform_to_schema`

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `schema.source` | every source record conforms to the schema | pass | 0 |
| `schema.saas` | every SaaS product record conforms to the schema | pass | 0 |
| `schema.oss` | every OSS project record conforms to the schema | pass | 0 |
| `schema.edge` | every relationship record conforms to the schema | pass | 0 |

## 2. Provenance is mandatory at source/relationship level.

The schema requires at least one provenance entry on every relationship and every project, and each entry must name a registered source, its pinned 40-character commit and a locator (file and line, or file and key). A record without it fails validation.

Artefacts and tests: `TestProvenance` (3 tests, including mutation of a real edge to confirm the schema refuses it)

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `provenance.edge` | every relationship carries provenance naming a registered source, its pinned commit and a locator | pass | 0 |
| `provenance.oss` | every OSS project carries provenance to a registered source | pass | 0 |
| `provenance.imported_values` | every imported value names the source it was copied from | pass | 0 |

## 3. Initial upstream sources are licence-checked with allow/refuse rationale recorded.

`config/sources.yaml` and `docs/LICENCE-REVIEW.md`. 9 sources harvested, each with licence as read, decision, rationale, conditions and the SHA-256 of the licence file the decision was made about. 2 adjacent websites/APIs and 7 further lists were investigated and refused or found not applicable, with reasons. All five seeds named on the ticket are covered.

Artefacts and tests: `TestLicenceOfSources` (licence drift stops the harvest; a refused source is not parsed)

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `licence.sources_decided` | every upstream has a licence decision with rationale, pinned to a licence-file hash | pass | 0 |
| `licence.hash_verified_at_harvest` | the licence file read at harvest is the one the decision was made about | pass | 0 |
| `licence.refused_not_harvested` | no staged claim comes from a source that is not licence-cleared, and no refused repository was fetched or registered | pass | 0 |

## 4. Ingestion/normalisation can run repeatably rather than by hand.

`python -m source_pipeline run` performs harvest, live verification, build and validation in one command. `run --offline` rebuilds from pinned inputs with no network. Two consecutive offline runs produced the same `MANIFEST.json`; see `logs/determinism.txt`.

Artefacts and tests: `TestBuiltDataset::test_rebuild_is_byte_identical`, `TestNormalise::test_output_is_independent_of_input_order`

| Check | What it establishes | Result | Failures |
|---|---|---|---|

## 5. Duplicate products/repos/relationships are reconciled deterministically.

609 harvested repository keys reconciled to 590 projects; 19 were the same repository under an old name or URL and were merged on the host's own canonical identity. SaaS spellings resolve through the reviewed catalogue. One edge per (SaaS, OSS) pair, with each upstream statement kept as its own provenance entry. Rules are in `docs/SCHEMA.md`.

Artefacts and tests: `TestNormalise` 

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `dedupe.saas_ids_unique` | no duplicate saas_id | pass | 0 |
| `dedupe.oss_ids_unique` | no duplicate oss_id | pass | 0 |
| `dedupe.edge_ids_unique` | no duplicate edge_id | pass | 0 |
| `dedupe.one_record_per_repository` | no two project records point at the same repository on the host | pass | 0 |
| `dedupe.one_edge_per_pair` | one relationship per (SaaS, OSS) pair | pass | 0 |
| `integrity.edge_endpoints` | every relationship resolves to a SaaS record and an OSS record | pass | 0 |
| `dedupe.one_repository_per_product_per_saas` | within the launch set a SaaS lists each product once, even when the product has several repositories | pass | 0 |

## 6. Launch dataset targets 100 high-value SaaS products, 250+ unique OSS projects, and approximately 300–400 validated SaaS↔OSS replacement relationships.

**100 SaaS is read as a floor**, by strategy decision `dr-KEI-805-507e4f70` (Option A, 2026-09-27T05:37:42Z). At exactly 100 SaaS the unchanged quality bar gave 231 distinct projects. The same ranking was continued, one product at a time, until 250 was reached: 21 SaaS products were added, and 9 of the launch SaaS products are named by a single source. **121 SaaS products, 251 OSS repositories (250 distinct products), 371 validated relationships.** All three targets are met. 136 of the relationships are stated by two or more independent sources. Every validated relationship was read in a desk review of plausibility, which rejected 39 upstream claims with reasons (`config/edge_review.yaml`). "High value" is decided by how many independent sources name the SaaS product, then by validated alternatives; never by stars. The licence-exception lane is not counted in any of these figures. The full recommendable corpus behind the launch set is 214 SaaS products, 365 recommendable projects and 483 validated relationships.

Artefacts and tests: `TestBuiltDataset::test_launch_holds_only_recommendable_projects`; the size targets are asserted by the `launch.targets` validation check, not by the test suite

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `launch.only_validated_edges` | every launch relationship is validated | pass | 0 |
| `launch.only_recommendable_projects` | every launch relationship points at a recommendable project | pass | 0 |
| `launch.ranking_unchanged` | the launch SaaS products are exactly the first N of the declared ranking (independent sources, then validated alternatives, then name): nothing was skipped or promoted | pass | 0 |
| `launch.targets` | launch corpus size against the ticket's targets (100 SaaS read as a floor per dr-KEI-805-507e4f70) | pass | 0 |

## 7. Publicly recommended OSS projects must have meaningful activity within the previous 12 months. Repositories with no meaningful activity for >12 months are excluded by default.

Of 590 projects verified live, 79 were classed stale and excluded. Every recommended project carries its dated evidence in `derived.maintenance.evidence`.

Artefacts and tests: `TestMeaningfulActivity::test_class_boundaries`

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `activity.recommended_within_12_months` | every recommended project has meaningful activity within 12 months | pass | 0 |

## 8. "Meaningful activity" must not be satisfied by trivial README/docs-only churn alone.

Qualifying evidence is a commit or merged pull request made by a person and touching at least one path that is neither documentation nor housekeeping, or a release or tag whose name is a version and which ships more than documentation, judged from the diff against the previous version. Not counted: documentation, images, translations, CI definitions, dependency manifests, lockfiles, automation accounts, floating tags such as `nightly`. Patterns are declared in `config/policy.yaml`. What was looked at and rejected is recorded under `derived.maintenance.disregarded` with the reason. Closed issues are recorded as supporting only. The validation check re-derives every piece of evidence from the raw host observations rather than trusting the build's summary.

Artefacts and tests: `TestMeaningfulActivity`

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `activity.evidence_is_not_docs_only` | every piece of evidence relied on exists in the raw snapshot and, judged from its full path list, is human-authored code or a version release that ships more than documentation | pass | 0 |

## 9. Archived repositories are excluded automatically.

19 archived repositories were found among the candidates; none is recommended and each carries the `archived` exclusion reason. Archive state is read from the host, not from any list.

Artefacts and tests: `TestEligibility::test_archived_is_excluded_even_if_recently_active`

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `archived.excluded` | no archived repository is recommended | pass | 0 |
| `archived.reason_recorded` | every archived repository carries the exclusion reason | pass | 0 |

## 10. Repositories without a recognised/usable open-source licence are excluded from normal recommendations unless explicitly reviewed and approved as an exception.

**Excluded from recommendations: all of them.** 121 projects whose only shortcoming is the licence are retained in the `licence_exception` lane, with 208 relationships, under Keith's direction (Linear KEI-805 comment 258b38dd). Every one is `recommendation_eligible: false`, none is in the launch set or the export, and each carries an issue record: category, exact reason, what was detected, upstream claims, evidence, review status. By category: conflicting 2, missing 8, open_core 64, restricted 7, source_available 37, undetermined 1, unrecognised 2. A further 18 projects have a licence issue and another failing (archived, stale) and are in the `rejected` lane, also with the issue recorded. **Named exceptions approved for recommendation: 0.** The pipeline refuses an exception whose approver is an executor or is missing. Where the host's detector reports "Other", the licence file text is matched against pinned reference texts and admitted only if it is a plain standard licence with no added terms, no carve-outs and no enterprise directory.

Artefacts and tests: `TestLicenceLane`, `TestLicenceTextDetection`, `TestEligibility::test_unrecognised_licence_is_excluded`, `test_licence_exception_needs_a_human_approver`

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `licence.recommended_have_recognised_licence` | every recommended project has a recognised open-source licence or an approved exception | pass | 0 |
| `licence_lane.every_licence_issue_is_described` | every verified project without a recognised licence carries an issue record: category, exact reason, what was detected, upstream claims, evidence and review status | pass | 0 |
| `licence_lane.retained_not_discarded` | every project whose only shortcoming is its licence is retained in the licence-exception lane, and nothing else is in that lane | pass | 0 |
| `licence_lane.never_recommended` | no licence-exception record, and no relationship pointing at one, is recommendation-eligible, validated, or in the launch set | pass | 0 |
| `lanes.consistent` | lane, recommendation_eligible and the underlying verdict agree on every record | pass | 0 |
| `licence.exceptions_are_approved` | every licence exception in use names its approver | pass | 0 |

## 11. Maintenance classification is explicit: Active, Maintained, Stale.

`derived.maintenance.status` on every verified project: active 453, maintained 43, stale 79, unverified 15. "unverified" are repositories that could not be found or whose host cannot be queried; they are excluded.

Artefacts and tests: `TestMeaningfulActivity::test_class_boundaries` (90/91 and 365/366 day boundaries)

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `maintenance.classification_matches_definition` | status, recomputed from the dates of the evidence itself: active <= 90 days, maintained <= 365 days, otherwise stale | pass | 0 |
| `maintenance.stale_excluded` | no stale project is recommended | pass | 0 |

## 12. Stars are a discovery/adoption signal only and are never sufficient on their own to qualify a project.

Stars are stored under `live` and are not an input to eligibility, edge validation or launch ordering. 92 projects with 10,000 or more stars were rejected, for example github.com/n8n-io/n8n (206069 stars: no_recognised_open_source_licence); github.com/langgenius/dify (157298 stars: no_recognised_open_source_licence); github.com/open-webui/open-webui (153283 stars: no_recognised_open_source_licence).

Artefacts and tests: `TestEligibility::test_stars_never_qualify_a_project`, `test_stars_never_disqualify_a_project`, `test_stars_are_not_an_input_to_any_decision`

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `stars.do_not_change_any_verdict` | every eligibility verdict is unchanged when stars are set to 0 or to 10,000,000 | pass | 0 |
| `stars.rejected_despite_stars` | projects with 10,000+ stars that were rejected, and launch projects with fewer than 500 | recorded figure, asserts nothing | n/a |

## 13. The ingestion strategy should harvest broadly and reject aggressively.

2402 upstream entries and 1522 relationship claims read from 9 sources. 590 candidate projects; 365 recommendable, 225 rejected. 849 candidate relationships; 483 validated, 158 rejected. Every rejection is recorded with its reason in `rejections.jsonl` (460 records).

Artefacts and —

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `strategy.every_rejection_is_recorded` | every rejected project has a rejection record stating why | pass | 0 |
| `strategy.harvest_broad_reject_aggressive` | harvested volume against accepted volume | recorded figure, asserts nothing | n/a |

## 14. Imported data is clearly distinguished from independently refreshed/live fields.

Every value on a project sits under `imported` (tagged with its source, never refreshed), `live` (dated by `fetched_at`, read from the host) or `derived` (computed, dated by `evaluated_as_of`). There are no top-level licence, star or activity fields. An upstream's licence claim and the verified licence are both kept, separately.

Artefacts and tests: `TestBuiltDataset::test_imported_and_live_fields_are_separate`, `test_upstream_licence_claims_never_decide_eligibility`

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `origin.no_unlabelled_fields` | a project has no top-level field outside the identity fields and the imported, live and derived blocks, and no field name appears in both imported and live | pass | 0 |
| `origin.live_matches_snapshot` | every live field equals what the host returned in the snapshot | pass | 0 |
| `origin.imported_values_are_facts_not_prose` | no imported value is longer than 80 characters | pass | 0 |
| `origin.live_is_dated` | every live block is dated | pass | 0 |

## 15. No copied dataset is published in a way that violates upstream licence/terms.

**Nothing has been published.** The dataset is marked `internal_not_published`. No repository was pushed and no external service was written to. The export guard builds a publishable view locally with the notices each licence requires and withholds the 24 launch relationships that rest only on share-alike material. Open questions for Keith before any publication are in `docs/LICENCE-REVIEW.md`.

Artefacts and tests: `TestBuiltDataset::test_export_withholds_share_alike_and_publishes_nothing`

| Check | What it establishes | Result | Failures |
|---|---|---|---|
| `licence_lane.never_exported` | the local export contains no project that is not recommendation-eligible | pass | 0 |
| `publication.guard` | dataset marked internal; no git remote configured; any local export is unpublished and withholds share-alike-only relationships | pass | 0 |
| `export.local_dry_run` | whether a local export built from THIS dataset was present to be checked | recorded figure, asserts nothing | n/a |

## Findings recorded by the producer

All are residue that the producer judges NON_BLOCKING. They are listed so the verifier can disagree.

| Finding |
|---|
| 121 well-known projects are open-core or source-available and are excluded under criterion 10 pending any exception Keith chooses to approve. |
| "Validated" includes a desk review of plausibility but not a test of fitness. 235 of 371 launch relationships rest on a single upstream's claim. Testing and scoring are out of scope. |
| Reaching 250 projects took 21 SaaS products beyond the first 100, under the floor reading strategy approved. Those are less well attested than the first 100; 9 launch SaaS products are named by one source only. The launch set sits exactly at the 250 minimum, so a single project going stale would take it below. |
| The five seeds alone did not reach the launch targets at this quality bar. Four further licence-checked sources and a reviewed table of repository overrides were added. |
| 24 launch relationships rest only on CC BY-SA 3.0 material and are withheld from a public export by default. Where a relationship is also stated by a permissive source, the export cites the permissive source; the project name in that row may still have been first read from the share-alike list. |
| altstack-data states two different licences for itself. Both permit reuse and both sets of terms are met. |
| GitHub mirrors of projects whose home is elsewhere (for example MediaWiki, WordPress, Moodle) are verified as the mirror. The mirror flag is recorded under `live` and is not used to exclude. |
| Live verification inspects the 15 newest commits and the 10 most recently UPDATED merged pull requests; GitHub cannot order pull requests by merge date. A project whose recent history is all automation is judged on releases alone, which can understate it. |
| GitLab and Codeberg projects (a handful) are verified on commits, releases and tags only; merged requests are not read there. |
| Identity-drift rejection is conservative: it also excludes a few projects that were genuinely renamed (for example redkubes/otomi-core to linode/apl-core). They are listed in `rejections.jsonl`. |
| Open-core projects whose licence the host reports as a single recognised licence (for example one with an `ee/` directory but an AGPL root licence detected by GitHub) are admitted on the host's word; the enterprise-directory check runs only where the host reports no recognised licence. |
| The work is not under version control. Strategy approved a dedicated private repository; creating it and committing is left to a sanctioned executor. |

## Producer-side adversarial review

Before this pack was frozen the producer commissioned an adversarial review of its own work by a separate agent.
It is producer evidence, not independent verification. It found defects that were then fixed, including projects
admitted on documentation-only tags and CI churn, a licence matcher that accepted added restriction clauses, a
repository repurposed since listing, and validation checks that could not fail. The fixes and their regression
tests are in this pack; the account is in the outcome ledger under the SELF-CORRECTION entries.

## What was not done

- Nothing was published, pushed, or sent to any external service.
- No licence exception was approved.
- No project was tested for functional fitness.
- No repository was created and no commit was made.
