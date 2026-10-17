# Licence edition check (open-core projects)

`python -m source_pipeline corpus-edition-check` (`source_pipeline/edition_check.py`) replaces the hand
step that every `open_core` project needed before it could be recommended: reading the repository
and writing an entry in `config/licence_exceptions.yaml`.

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
  where the KEI-805 dataset lists the project as replacing a product (licence-cleared, non-share-alike
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

Generated exceptions reach the dataset build only when `config/licence_lane.yaml`
`edition_check.generated_exceptions_feed_the_build` is `true`. It ships `false`. Under the launch
policy (`policy.yaml` launch, decision dr-KEI-805-507e4f70: 100 SaaS is a floor, extended only to reach
`oss_minimum`), the 15 dataset projects this would admit let the launch set reach 250 OSS projects with
100 SaaS products instead of 115. On 2026-10-17 that would add 20 relationships (scored by the next
cycle) and drop 23 recommended ones across 16 products (Splunk, Snowflake, Pingdom, Power BI, Twitch,
Wix and others). That trade is a launch-policy decision. Generated entries are validated whether or not
the switch is on.

## Run of 2026-10-17: 38 open-core projects

**23 pass, 0 fail, 15 ambiguous.**

| Project | Core | Result | Reason code | Deployment / reason |
|---|---|---|---|---|
| activepieces/activepieces | MIT | pass | `core_only_deployment` | docker-compose.yml |
| agenta-ai/agenta | MIT | pass | `core_only_deployment` | hosting/kubernetes/helm |
| berriai/litellm | MIT | pass | `core_only_deployment` | helm/litellm |
| betterdb-inc/monitor | MIT | pass | `core_only_deployment` | charts/betterdb-monitor |
| chatwoot/chatwoot | MIT | pass | `core_only_deployment` | docker-compose.production.yaml |
| elie222/inbox-zero | AGPL-3.0 | pass | `core_only_deployment` | docker-compose.yml |
| formbricks/formbricks | AGPL-3.0 | pass | `core_only_deployment` | docker/docker-compose.yml |
| goauthentik/authentik | MIT | pass | `core_only_deployment` | lifecycle/container/compose.yml |
| gravitl/netmaker | Apache-2.0 | pass | `core_only_deployment` | compose/docker-compose.yml |
| growthbook/growthbook | MIT | pass | `core_only_deployment` | docker-compose.yml |
| highlight/highlight | Apache-2.0 | pass | `core_only_deployment` | docker/compose.hobby.yml |
| infisical/infisical | MIT | pass | `core_only_deployment` | docker-compose.prod.yml |
| keephq/keep | MIT | pass | `core_only_deployment` | docker-compose.yml |
| kensaurus/mushi-mushi | MIT | pass | `core_only_deployment` | deploy/docker-compose.yml |
| langfuse/langfuse | MIT | pass | `core_only_deployment` | docker-compose.yml |
| lightdash/lightdash | MIT | pass | `core_only_deployment` | docker-compose.yml |
| onyx-dot-app/onyx | MIT | pass | `core_only_deployment` | deployment/helm/charts/onyx |
| opensignlabs/opensign | AGPL-3.0 | pass | `core_only_deployment` | docker-compose.yml |
| phasehq/console | MIT | pass | `core_only_deployment` | docker-compose.yml |
| posthog/posthog | MIT | pass | `core_only_deployment` | docker-compose.hobby.yml |
| stirling-tools/stirling-pdf | MIT | pass | `core_only_deployment` | docker/compose/docker-compose-unified-backend.yml |
| toeverything/affine | MIT | pass | `core_only_deployment` | .docker/selfhost/compose.yml |
| twentyhq/twenty | AGPL-3.0 | pass | `core_only_deployment` | packages/twenty-docker/docker-compose.yml |
| bitwarden/clients | GPL-3.0 | ambiguous | `no_deployment_files_in_repository` | no docker-compose file or Helm chart in the repository; any deployment lives elsewhere |
| darrylmorley/whatcable | MIT | ambiguous | `no_deployment_files_in_repository` | no docker-compose file or Helm chart in the repository; any deployment lives elsewhere |
| dyad-sh/dyad | Apache-2.0 | ambiguous | `no_deployment_files_in_repository` | no docker-compose file or Helm chart in the repository; any deployment lives elsewhere |
| openreplay/openreplay | AGPL-3.0 AND MIT | ambiguous | `no_deployment_files_in_repository` | no docker-compose file or Helm chart in the repository; any deployment lives elsewhere |
| papermark/papermark | AGPL-3.0 | ambiguous | `no_deployment_files_in_repository` | no docker-compose file or Helm chart in the repository; any deployment lives elsewhere |
| signoz/signoz | MIT | ambiguous | `no_deployment_files_in_repository` | no docker-compose file or Helm chart in the repository; any deployment lives elsewhere |
| supertokens/supertokens-core | Apache-2.0 | ambiguous | `no_deployment_files_in_repository` | no docker-compose file or Helm chart in the repository; any deployment lives elsewhere |
| webiny/webiny-js | MIT | ambiguous | `no_deployment_files_in_repository` | no docker-compose file or Helm chart in the repository; any deployment lives elsewhere |
| amplication/amplication | Apache-2.0 | ambiguous | `no_deployment_runs_the_project` | 1 deployment file(s) found, none runs the project itself (dependencies only) |
| databendlabs/databend | Apache-2.0 | ambiguous | `no_deployment_runs_the_project` | 1 deployment file(s) found, none runs the project itself (dependencies only) |
| tolgee/tolgee-platform | Apache-2.0 | ambiguous | `no_deployment_runs_the_project` | 5 deployment file(s) found, none runs the project itself (dependencies only) |
| bitwarden/server | AGPL-3.0 | ambiguous | `only_development_deployments` | the only deployments that run the project are development, test or demo stacks: dev/docker-compose.yml |
| chatbotxio/chatbotx | MIT | ambiguous | `only_development_deployments` | the only deployments that run the project are development, test or demo stacks: docker-compose.dev.yml |
| mattermost/mattermost | AGPL-3.0 | ambiguous | `only_development_deployments` | the only deployments that run the project are development, test or demo stacks: server/build/docker-compose.co |
| rocketchat/rocket.chat | MIT | ambiguous | `only_development_deployments` | the only deployments that run the project are development, test or demo stacks: docker-compose-local.yml |
