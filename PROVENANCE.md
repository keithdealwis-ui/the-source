# Where The Source gets its evidence

Every useful recommendation should be traceable. The Source records where a claim came from, the repository evidence used to assess it and when that evidence was checked.

Imported claims, live host observations and derived assessments are distinct. Preserve their source and date when using a result. The walkthrough below explains how to trace one recommendation; for interactive inspection, ask your assistant to use `source_get_relationship` through [MCP](docs/MCP.md).

## Tracing a recommendation

Take one row of `api/v1/saas/notion.json`:

1. `sources` names the evidence sources behind the relationship, which may be upstream lists, intake or a derived job match. `data/canonical/relationships.jsonl`
   has the full `provenance` for the edge: `source_id`, `source_commit` and `locator`,
   which is the file and line or record in the upstream at that commit.
2. `config/sources.yaml` holds the licence decision for each source, pinned to the SHA-256
   of the licence file the decision was made about. `data/state/source_pins.json` records
   any newer commit the daily research moved to, and proves the licence file was
   unchanged. `data/canonical/NOTICE.md` carries the attributions each licence requires.
3. `live_checked_at` says when the host was asked. The full observation, including which
   commits, pull requests and releases counted as meaningful activity and which were
   disregarded and why, is in `data/dataset/oss_projects.jsonl` under `derived.maintenance`.
4. `replacement_fit` and `project_health` carry `score_version` and `evaluated_at`.
   `data/scores/history.jsonl` is the append-only history. `scoring_path` says whether a
   score came from the full scoring lane or the single-project path.
5. `runs/<run_id>.json` records the cycle that produced this version, and the
   `cycle/<run_id>` tag is the commit.
6. Momentum and acceleration (`data/momentum/`) are computed only from the dated
   history snapshots. Each value names its base and current days. The manifest records the
   sha256 of every history day it read and the methodology version and parameter hash
   ([`docs/MOMENTUM.md`](docs/MOMENTUM.md)).

## Manual intake

Repositories admitted through intake have `source_id: intake`. The locator points at the
admitting request (`data/intake/admitted/<request_id>.json`). The outcome record
`data/intake/outcomes/<request_id>.json` keeps the matched evidence. The intake source's
"licence file" is `docs/INTAKE-POLICY.md`, pinned by hash like any upstream licence.

## What is never imported

Upstream list descriptions, taglines, comparisons, pros and cons, pricing, setup estimates, referral
links and logos. The discovery corpus separately carries truncated descriptions and topics
reported by repository hosts, labelled as host metadata. The Source keeps facts only: names, repository URLs, declared licence
identifiers, short category labels and relationship claims. See
[`docs/LICENCE-REVIEW.md`](docs/LICENCE-REVIEW.md).

## Upstream services

The daily cycle asks independent services to corroborate what the repository hosts say,
and to suggest candidate repositories. None of them is a source of any published value:
canonical facts come from the repository host only. The decision record, with licences,
limits, freshness and fallbacks, is [`docs/BUILD-VS-INGEST.md`](docs/BUILD-VS-INGEST.md).

| Service | Role | Licence of its data | What The Source keeps |
|---|---|---|---|
| GitHub, GitLab, Codeberg APIs | Primary: the repository facts themselves | Host terms (aggregate metadata only, no personal data) | The facts, with `fetched_at` |
| [ecosyste.ms](https://ecosyste.ms) (repos, awesome) | Corroboration; discovery seed | CC BY-SA 4.0, attribution "ecosyste.ms" | Agreement statistics, disagreeing keys, and candidate repository addresses. No ecosyste.ms values |
| [deps.dev](https://deps.dev) (Open Source Insights) | Corroboration | CC BY 4.0 | Agreement statistics and disagreeing keys |
| [Hacker News API](https://github.com/HackerNews/API), [Forem API](https://developers.forem.com/api/v1) (dev.to) | Community Radar : which repositories are being discussed | Site terms; authored content belongs to its authors | Item id, its own address, publication time, public points and comment counts, linked repository addresses. No titles, text or usernames. Never a canonical value ([`docs/RADAR.md`](docs/RADAR.md)) |

Corroboration data from ecosyste.ms and deps.dev is used under their licences; attribution:
"ecosyste.ms" (CC BY-SA 4.0) and "Open Source Insights, deps.dev" (CC BY 4.0).
