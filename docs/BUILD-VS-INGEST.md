# Data sources and verification

The Source combines discovery from reviewed sources with facts checked against repository hosts. Its own rules handle identity, licensing, maintenance, scoring and the published read layer.

| Source | Role | Boundary |
|---|---|---|
| GitHub, GitLab and Codeberg | Primary observations for repositories on those hosts | Evidence is dated; a failed request does not invent a value. |
| Licence-cleared project lists | Candidate discovery and relationship claims | Claims retain source and commit references and require independent evaluation. |
| ecosyste.ms | Discovery seeds and corroboration | Disagreements are recorded; corroboration does not silently replace host facts. |
| deps.dev | Corroboration | Supporting signals remain distinguishable from primary facts. |
| Hacker News, Forem and Bluesky | Community attention | Discussion is a discovery signal, not a recommendation. |

See [provenance](../PROVENANCE.md) for tracing a result, [licence review](LICENCE-REVIEW.md) for source reuse decisions, [operations](OPERATIONS.md) for refresh behaviour and [Community Radar](RADAR.md) for mention handling.

The [October 2026 provider review](archive/decisions/BUILD-VS-INGEST.md) retains the original comparisons and live probes. Those observations are dated; provider terms, costs and capacities must be rechecked before a new integration decision.
