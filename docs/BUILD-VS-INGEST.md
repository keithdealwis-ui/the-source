# Build vs ingest: raw discovery and metadata layers (KEI-848)

Decision date 2026-10-02. Evidence: live probes of every upstream on 2026-10-02 between
15:08Z and 15:15Z, plus two end-to-end daily cycles run against live data the same day.
This record covers the raw layers only: discovering repositories and observing their
metadata. The Source keeps ownership of everything above them: the canonical entity
model, provenance, reconciliation, licence and maintenance verdicts, scoring, history and
the read surface.

## The decision in one table

| Upstream | Verdict | Trusted for | Not trusted for | Wired into |
|---|---|---|---|---|
| GitHub GraphQL + REST (incl. search, `stargazers/history`) | **Adopt: primary** | Every field we track for github.com repositories; discovery via search | Personal data (never collected) | `daily.light_refresh`, `enrich.observe`, `discovery_search` (`github_search`) |
| GitLab REST, Codeberg (Forgejo) REST | **Adopt: primary** for those hosts | The handful of non-GitHub repositories | n/a | `daily._light_rest`, `enrich.gitlab`/`codeberg` |
| repos.ecosyste.ms (GitHub host) | **Adopt: corroborate** | Existence, archive state, licence, repository id, stars to a tolerance | Anything canonical; any non-GitHub host (Codeberg last synced 2023, GitLab Jan 2026) | `corroborate` (`ecosystems`) |
| deps.dev v3 projects | **Adopt: corroborate** | Existence, SPDX licence, stars to a tolerance | Anything canonical; Codeberg (HTTP 400) | `corroborate` (`depsdev`) |
| awesome.ecosyste.ms | **Adopt: discovery seed** | "This repository is on awesome-selfhosted" (address and id only) | Any metric | `discovery_search` (`ecosystems_awesome`) |
| Hacker News official API, Forem v1 API (KEI-850) | **Adopt: community signal** | "This item, with these public counts, links this repository" | Any repository fact; any canonical value; any authored content (titles, text, usernames are never stored) | `radar` (`community_radar` cycle step), see [`RADAR.md`](RADAR.md) |
| packages.ecosyste.ms | Evaluated, not adopted now | Package downloads and releases (fresh) | n/a | none: downloads are not a KEI-848 field; candidate for future enrichment |
| Software Heritage | Evaluated, not adopted now | Proof a repository existed, last archived snapshot (covers Codeberg/GitLab) | Any metric (it has none) | none: a fallback for "upstream 404s" provenance; recorded as follow-on residue |
| OSS Insight | **Not adopted** | n/a | Stars: Aider 36,841 vs 49,335 live (−25%); n8n series frozen at 2025-02-19 | none |
| GH Archive / BigQuery | **Not adopted** (future option) | Whole-GitHub event firehose, ~65 min latency | A daily need we have: candidates would still each need a GitHub lookup | none |
| Libraries.io | **Not adopted** | n/a | Needs an API key; repository endpoints no longer documented; same lineage and licence as ecosyste.ms | none |
| ecosyste.ms topics / timeline / `created_after` | **Not adopted** | n/a | Broken on probe: topics return 0 even for `react`; timeline and `created_after` return 500 | none |

## Why the core refresh is not ingested

Criterion 11 asks that commodity ingestion is not rebuilt where an upstream meets the
requirement. For the daily metadata refresh, no upstream does, and the "build" is not a
build at all: it is one batched GraphQL query per 50 repositories against the origin.

- **Freshness.** GitHub answers live. ecosyste.ms GitHub records were 1-3 days old; deps.dev
  trailed by days (Aider −136 stars); every aggregator is months or years stale for
  Codeberg and GitLab. A daily cycle fed from a 1-3 day old mirror is not daily.
- **Cost.** 611 repositories took 33 HTTP calls (13 GraphQL batches plus a few REST calls
  for non-GitHub hosts and rename resolution) and ~30 s in the live run of 2026-10-02.
  GraphQL batches of 50 cost 1 point each against the 1,000 points/hour `GITHUB_TOKEN`
  budget. No aggregator is cheaper than that.
- **Exactness and identity.** Only the host gives the current name after a rename or
  transfer and the repository id that ties the two together; reconciliation depends on it.

So the light tier calls the origin, and upstreams do what they are good at: independent
corroboration (two of them, different lineages: ecosyste.ms crawls GitHub itself, deps.dev
is Google's) and discovery seeding. Nothing was rebuilt that an upstream provides: we do
not run an event warehouse, a star-history store or a topic index of our own. Star history,
when wanted, is GitHub's own `GET /repos/{o}/{r}/stargazers/history` (shipped 2026-09-04,
exact to within 1 star on probe), not a re-implementation.

**Why only the light tier.** The deep tier (commit and PR evidence with the files they
touched, release diffs, licence-file probes) is The Source's own differentiated evidence
for its maintenance verdict. No upstream offers it. It already existed (KEI-805/807) and
is now run only when something warrants it.

## Per-upstream detail

### GitHub (primary)

- **API.** GraphQL `repository(owner:,name:)` aliased 50 per query (`r0` … `r49`) for
  `nameWithOwner databaseId isArchived isDisabled stargazerCount forkCount pushedAt
  licenseInfo{spdxId} issues(states:OPEN){totalCount} latestRelease{tagName publishedAt}
  defaultBranchRef{target{oid committedDate}}`. REST `GET /repos/{o}/{r}` resolves renames
  that GraphQL reports as NOT_FOUND. Search: `GET /search/repositories?q=…`.
- **Limits.** GraphQL: 1,000 points/h per repository for `GITHUB_TOKEN` (5,000 for a user
  token); minimum 1 point a query; `RESOURCE_LIMITS_EXCEEDED` returns partial data if a
  query is too heavy (seen at 100 aliases with history fields, not at 50). REST: 1,000/h
  for `GITHUB_TOKEN`. Search: 30/min authenticated, 1,000 results per query, no OR
  between qualifiers (422).
- **Terms.** [GitHub ToS §H](https://docs.github.com/en/site-policy/github-terms-of-service)
  and the [Acceptable Use Policies](https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies):
  no excessive requests, no token sharing to exceed limits, no personal data resale. The
  Source stores aggregate repository metadata only (counts, dates, licence, names) and no
  user lists or e-mail addresses.
- **Change in 2026.** Since 2026-06-30 stargazer *listing* is admin/collaborator only
  ([changelog](https://github.blog/changelog/2026-06-30-upcoming-access-restrictions-to-public-api-endpoints-and-ui-views/));
  the replacement is the star-history endpoint
  ([changelog](https://github.blog/changelog/2026-09-04-new-api-endpoint-provides-privacy-safe-star-history-data/)).
  Nothing in The Source pages stargazers (checked: no `stargazers`/`starredAt` use).
- **Failure behaviour.** A batch-level HTTP failure marks every key in the batch
  transient; per-alias errors other than NOT_FOUND are transient too. Transient failures
  are retried (`refresh.retry_failed_lookups`, 2) and, if any remain, **the cycle fails and
  promotes nothing**: the light tier is the daily truth, and a lookup failure looks exactly
  like a project with nothing to show.
- **Data-quality risks.** `pushed_at` moves on any branch push (bots included), so it only
  ever *triggers* a deep look near a maintenance boundary, never decides a class.
  `latestRelease` excludes prereleases while the deep observation's newest release can be a
  prerelease: comparing the two invented 99 "changes" in the first live run, so the light
  tier now compares only with its own previous answer (fixed before commit, with a test).

### GitLab and Codeberg (primary for their hosts)

`https://gitlab.com/api/v4/projects/{path}?license=true` and
`https://codeberg.org/api/v1/repos/{owner}/{repo}`, one call per repository (6 in the
corpus). Aggregators are unusable here: ecosyste.ms last synced Codeberg forgejo/forgejo
on 2023-08-08 (690 stars against 5,575 live); deps.dev rejects Codeberg with HTTP 400.

### repos.ecosyste.ms (corroborate)

- **API.** `https://repos.ecosyste.ms/api/v1/hosts/GitHub/repositories/{owner}/{name}`
  (`stargazers_count`, `archived`, `license` as a lowercase key, `uuid` = GitHub
  databaseId, `last_synced_at`).
- **Licence.** Data is CC BY-SA 4.0 ([ecosyste.ms/api](https://ecosyste.ms/api)); the
  [terms](https://ecosyste.ms/terms) require attributing "ecosyste.ms" when published
  alongside other data, and share-alike applies to published values. **The Source
  publishes no ecosyste.ms value.** The run record keeps only agreement statistics, the
  names of fields that disagreed and the keys involved, so the CC BY 4.0 canonical files
  stay clean. Attribution is still given (PROVENANCE.md, "Upstream services") because the comparison is ours to
  report. The terms' clause against "data mining, robots" reads as the website, not the
  documented API; the cycle asks ~120 repositories a day at most (well inside the
  anonymous 5,000/h), and `config/corroboration.yaml: mailto` joins the polite pool
  (15,000/h) when an operator address is set.
- **Freshness.** 1-3 days for GitHub; responses are cached for a day behind Cloudflare.
- **Fallback.** None needed: it is a check, not a source. If it is down, the provider is
  marked `degraded` in the run record and job summary, and the cycle carries on.

### deps.dev (corroborate)

- **API.** `https://api.deps.dev/v3/projects/{url-encoded github.com/owner/name}`
  (`starsCount`, `forksCount`, `license` in SPDX, `scorecard` when present).
- **Licence.** CC BY 4.0 ([docs](https://docs.deps.dev/api/v3/)): attribution, no
  share-alike. No documented rate limit; responses cached an hour.
- **Freshness.** Days to weeks behind on counts; licence is its most useful check (6 of 110
  covered repositories disagreed with GitHub's licence detection on 2026-10-02, worth a
  human look, never an automatic change).

### awesome.ecosyste.ms (discovery seed)

`https://awesome.ecosyste.ms/api/v1/lists/lookup?url=https://github.com/awesome-selfhosted/awesome-selfhosted`,
then the `projects_url` it returns. awesome-selfhosted parsed: 1,406 projects, synced
2026-09-20. Our own pinned harvest of the same list (licence-cleared, KEI-805) keeps only
entries that name a SaaS product; this lane surfaces the listed projects the corpus does
not yet hold. Only the repository address and id are carried forward; CC BY-SA values
(stars, descriptions) are not stored.

### Hacker News and Forem (community signal, KEI-850)

Added 2026-10-06 and probed live that day (HN: 303 calls, 300 stories read, 28 linking a
repository; Forem: 37 calls, 33 articles read, 3 removed since listing, 20 linking a
repository). Each source is used only through the interface its operator publishes for
programmatic access: HN's official Firebase API (YC's terms forbid scraping the site, and
the API is the means they made available) and Forem's documented public v1 API. No key is
needed for either. Neither offers a better ingestible alternative for "what is being
discussed now", so nothing is rebuilt. The radar keeps only facts and addresses, never
authored content, and its output never enters `data/canonical/` or `api/v1/`. Its
repository facts still come from GitHub. Not adopted: the Algolia HN search API (a
third-party index of the same data, so the official API is preferred) and Reddit (its
Data API needs registered OAuth access under its own terms, which is a decision for the
owner, not an engineering default).

### Not adopted

- **OSS Insight.** Star counts materially wrong and series frozen (see table). Beta API,
  600 requests/h, no explicit data licence and PingCAP's ToS restrict automated scraping.
  GitHub's own star history makes it unnecessary.
- **GH Archive / BigQuery.** ~0.5 GB/day compressed, ~65 min latency, BigQuery costs GBs
  per query for star aggregation, no topic metadata in events. Worth revisiting only for
  "fastest-rising repositories across all of GitHub".
- **Libraries.io.** API key required; repository endpoints gone from the docs; nothing
  beyond ecosyste.ms (same lineage, CC BY-SA 4.0).
- **packages.ecosyste.ms, Software Heritage.** Sound, but they serve needs this outcome
  does not have (downloads; existence proof after deletion). Recorded as candidates.

## How provider independence is kept (criterion 12)

1. **One canonical answer per field, from the origin.** The snapshot, dataset and
   canonical files take repository facts from the host only. `corroborate.py` has no
   write path to them (`canonical_effect: none` in every provider block of the run record),
   and a test asserts the light observations are byte-identical before and after
   corroboration.
2. **Disagreement is detected, not obeyed.** Per provider and day: `coverage` (does the
   provider know the repository), `agreement` (of those, how many match within tolerance:
   stars ±max(200, 5%), archive state, licence, repository id), and the disagreeing keys
   with the fields that differ.
3. **Degradation is visible.** A provider with more than 20% errors or under 80% agreement
   is `degraded`, listed in `degraded_providers`, and shown in the job summary. The cycle
   still succeeds: losing a second opinion is not losing the data.
4. **Origin outages fail the cycle instead.** If the host itself cannot be asked
   (transient light-refresh failures after retries), nothing is promoted and the
   `cycle-failure` issue opens: the last known-good dataset stays as it was.
5. **Discovery cannot create canonical entities.** Search and seed lanes only add to the
   candidate pool; entering the corpus still needs a licence-cleared relationship claim
   (an upstream list at a reviewed pin, or accepted intake), exactly as under KEI-807.

## Observed on 2026-10-02 (live cycle, scratch copy)

| Provider | Asked | Coverage | Agreement | Disagreements |
|---|---|---|---|---|
| ecosyste.ms | 120 | 1.00 | 0.983 | 1 archive state, 1 stars |
| deps.dev | 120 | 0.917 | 0.946 | 6 licence, 1 stars |

## Revisit when

- ecosyste.ms or deps.dev agreement drops below 0.8 for a week (the provider block says so);
- GitHub's limits change, or the corpus grows past ~25,000 repositories (≈500 light batches);
- star *velocity* becomes a product need (adopt `stargazers/history`, already verified);
- a "trending across GitHub" product need appears (GH Archive / BigQuery).
