# Checking open-core editions

A project's public repository may include an open core alongside components with different terms. Edition checks inspect the stated core licence, excluded paths and evidence of a core-only self-hosting route.

An edition check is limited evidence, not a blanket licence or feature-equivalence guarantee. Ambiguous results remain explicit. The sections below describe the checks and how results affect eligibility. Maintainers run `python -m source_pipeline corpus-edition-check`; the implementation is `source_pipeline/edition_check.py`.

## What it confirms

At the commit the licence classification was made (`licence.classification.provenance.commit`), so
the excluded parts are exactly the ones the classification named:

1. **Core licence.** The row is `open_core` with a core SPDX and named excluded paths or markers.
   This is re-checked from the classification, not re-derived.
2. **A core-only self-hosting path.** A docker-compose file or Helm chart in the repository that runs
   the project itself (a service that builds it, or an image named for the repository) and does not
   need an excluded part. That means no build context, Dockerfile, volume, env file or chart inside an
   excluded path; no image, value or switch carrying an enterprise marker (`enterprise`, `ee`, `pro`,
   `commercial`, `premium`, `proprietary`); no file named for an enterprise edition; and no Dockerfile
   it builds that copies an excluded path. Development, test, benchmark and demo stacks, and files
   inside the source tree rather than at the root or under a deployment directory, are recorded but
   never the path that passes.
3. **What the excluded parts cover.** Each excluded path, why it is excluded, what it contains (from
   the tree) and its README's first line. This becomes the disclosure.

Provenance has the classification's shape: `{source: {api, files}, commit, checked_at}`.

## Outcomes (fail closed)

| Result | Row eligibility | Exception |
|---|---|---|
| `pass` | `eligible`, condition = the disclosure | entry generated in `config/licence_exceptions.generated.yaml` |
| `fail` | stays `conditional`, `reason_code` set | none |
| `ambiguous` | stays `conditional`, `reason_code` set | none |

Reason codes: `core_only_deployment`; `deployment_requires_excluded_parts`;
`only_development_deployments`; `no_deployment_runs_the_project`; `no_deployment_files_in_repository`;
`deployment_file_unreadable`; `repository_unreadable`; `tree_truncated`; `no_classification_commit`;
`not_classified_open_core`.

## Where it shows

- **Corpus** (`data/corpus/projects.jsonl`): `licence.edition_check`, plus `licence.listed_alternative_to`
  where the evaluation dataset lists the project as replacing a product (licence-cleared, non-share-alike
  sources only). `corpus-validate` refuses a pass without its disclosure, a pass at a commit other than
  the classification's, and any eligibility a non-pass check would imply.
- **Discovery** (MCP): every open-core row carries `licence_label: "open_core"` and an `open_core` block
  (core SPDX, disclosure, edition-check result). Conditional rows are listed, not excluded as
  `licence_issue`. A product-driven query also lists open-core rows the dataset names as its
  alternatives (so Slack's list holds Mattermost and Rocket.Chat). Discovery rows are never
  recommendations.
- **Recommendations** (MCP): every row that names a canonical open-core project carries
  `licence_disclosure`, in every tool. The disclosure comes from the canonical record (an edition-check
  exception) or else from the project's corpus classification. A relationship to an open-core project
  that cannot be disclosed is not served as a recommendation. `canonical-validate` checks
  `open_core_recommendations_disclosed`.

## The switch

Generated exceptions reach the build only when `config/licence_lane.yaml` → `edition_check.generated_exceptions_feed_the_build` is `true`. It is currently `false`; generated results are validated whether or not they feed the build. Explicit reviewed exceptions and current recommendation policy remain separate controls.

Changes to that switch require evaluation against the current policy, including catalogue serving and preservation of already served valid relationships. The effects measured during the original launch are historical, not a forecast for today's dataset.

[Historical October 2026 results](archive/decisions/EDITION-CHECK-2026-10-17.md) · [Current methodology](METHODOLOGY.md)
