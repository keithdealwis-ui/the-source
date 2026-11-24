# Historical edition-check results

These are dated results, not current project recommendations. See the [current guide](../../LICENCE-EDITION-CHECK.md).

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
