# The 25,000+ project discovery catalogue

The discovery catalogue brings together 25,000+ public software projects. It is the broad starting point for search and research. Selected projects receive deeper assessment; discovery entries retain their own evidence, dates and warnings.

Explore the catalogue through [your AI assistant](https://the-source.dev/agents/#setup). For a replacement task, [start with a paid product](https://the-source.dev/#explore). Current counts are in [the corpus manifest](../data/corpus/MANIFEST.json); the corpus and assessed layer overlap.

`data/corpus/` publishes discovery facts beside the canonical assessed layer. Quick scores are lightweight indicators, not the full Project Health/Replacement Fit assessment and never sufficient for recommendation.

## Tiers

The export carries no tier. The MCP server derives it at load time from the canonical layer it
serves beside, so a tier can never disagree with the recommendations being served.

| Tier | Meaning | Recommended? |
|---|---|---|
| `recommended` | in the canonical layer with at least one recommended relationship | yes, when the recommendation rules pass |
| `scored` | in the canonical layer, with Replacement Fit / Project Health, no recommended relationship | no |
| `discovery` | not fully assessed for recommendation; may have separately labelled quick scores | never |

Projects whose licence The Source does not recognise (for example no licence, `NOASSERTION`,
added or removed terms, or a licence that is not an open-source software licence) are published
only as `discovery`, with `licence.status: "issue"`, the reason, and a plain warning. They are
never offered, even as `discovery_candidates`, by the recommendation tools. A project that is
also in the canonical layer takes its tier from the canonical layer.

## Files

| File | Holds |
|---|---|
| `MANIFEST.json` | schema `the-source.corpus/1`, sha256 of each file, `corpus_version` (hash of the hashes), counts, membership and refresh records, publication record |
| `projects.jsonl` | one row per project, sorted by `id`: identity, host metadata, licence, maintenance, lanes, provenance |
| `live.jsonl` | one row per project, same order: the live facts the weekly refresh re-reads |

`projects.jsonl` row:

| Field | |
|---|---|
| `id` | `host/owner/repo`, lower case: the key the project was first published under |
| `host_repo_id` | the host's numeric repository id |
| `name`, `full_name`, `url`, `aliases` | as the host reports them now; `aliases` holds earlier keys (renames, transfers) |
| `description`, `homepage`, `topics`, `languages` | host-reported metadata; descriptions truncated to 300 characters |
| `licence` | `spdx`, `status` (`recognised` or `issue`), `issue` (`category`, `reason`) when there is one; on `issue` rows also `classification` and `eligibility` (below) |
| `maintenance` | `status` (`active` or `maintained`) and `last_meaningful_activity_at`, as the Discover lane evaluated them at `MANIFEST.membership.as_of` |
| `lanes` | `discover` and/or `replace` |
| `provenance` | every route that found it: `strategy`, `source`, `commit`, `locator`, `depth`, `via` |
| `identity` | `null` when there is nothing to say; see [Identity](#identity-renames-forks-mirrors-siblings) |

Provenance names a curated list with its pinned commit and line (`list:owner/repo`, `commit`,
`line N`), a GitHub topic (`github-topic:x`), a recorded search (`search:id`), an owner's other
repositories (`github-owner:x`), a README link (`readme:owner/repo`), or the Replace lane.
Routes from the maintainer's private seed list are published as `maintainer_seed` with no file,
line or other detail.

**Licence classification.** Every `issue` row also carries a project-level class read
from the repository itself at a pinned commit (`python -m source_pipeline corpus-licence-classify`,
network): `licence.classification` = `class` (`open`, `open_core`, `source_available`,
`restricted`, `missing`, `unclear`), `reason`, `method`, for `open` the `spdx`, for `open_core`
the `core_spdx` and the `excluded` `paths`/`markers`, any `supporting` manifest licence fields
(evidence only), and `provenance` (`source`, `commit`, `checked_at`). `licence.eligibility` =
`status` (`eligible` for `open`, `conditional` for `open_core`, otherwise `not_eligible`),
`condition` and `follow_up` (open_core: "core only; edition check required before
recommendation"), and `self_hostable` (`true` with `self_hosting_evidence`, or `null`).
`status` stays `issue` and `issue` is kept for back-compatibility. The classification labels
discovery eligibility only; the recommendation gate is unchanged. Rules and proof:
`docs/ACCEPTANCE-KEI-919-S1.1.md`.

`live.jsonl` row: `found`, `full_name`, `stars`, `forks`, `pushed_at`, `archived`,
`latest_release`, `licence_spdx` (as the host reports it now), `observed_as_of`, and
`unresolved` (why the last refresh could not read it, if it could not).

## Building and refreshing

```bash
python -m source_pipeline corpus-export     # data/corpus/ from data/discover/corpus/ (internal); no network
python -m source_pipeline corpus-refresh    # re-read every project's live facts from the host (network)
python -m source_pipeline corpus-validate   # MANIFEST, schema and publication-rule checks
python -m source_pipeline corpus-due        # is a scheduled refresh due
python -m source_pipeline corpus-licence-classify  # classify every licence-issue row (network)
python -m source_pipeline corpus-admit      # admit new discovery-pool candidates (network; also a cycle step)
```

**Membership** changes two ways. A rebuild of the Discover lane followed by `corpus-export` (the
lane's harvest reads the maintainer's private seed list, so it runs where that list lives); and,
every daily cycle, **admission** (`source_pipeline/corpus_admit.py`, the `corpus_admission`
strategy). Admission takes the discovery pool (`data/discovery/candidates.jsonl`: the daily search
lanes, the awesome-selfhosted seed and Community Radar), drops whatever the corpus already holds
by id, alias or host repository id (before and after asking the host, so a rename is caught),
reads the rest from GitHub in GraphQL batches and applies the Discover lane's bar unchanged: the
`discover.prefilter` checks (found, public, not archived, disabled, empty, a fork or a mirror;
pushed within `policy.yaml` `maintained_days`) and the recognised-licence list. A row whose licence
is not recognised is admitted as `retained`, with its issue named, exactly as the export does.
Maintenance status is read from `pushed_at`, not from commits, tags and releases as the Discover
lane does; `MANIFEST.membership.admission` says so. A re-export keeps admitted rows.

Each cycle's run record (`runs/<run_id>.json`) carries a `corpus` block (`corpus_added`,
`corpus_removed`, `corpus_collapsed`, `membership_before`, `membership_after`,
`candidates_found_by_strategy`, `top_new_by_stars`, what was dropped and why) and `anomalies`:
`zero_corpus_additions_consecutive` (this and the previous scheduled run admitted nothing),
`corpus_manifest_unchanged` (the run left `data/corpus/MANIFEST.json` byte-identical) and
`scheduled_run_missing` (a cycle key in the cadence has no scheduled record). The MCP server's
`last_run` serves both.

**Live facts** are refreshed by automation by `.github/workflows/corpus-refresh.yml`: a daily
wake-up that refreshes when `corpus-due` says so (weekly: due after 6 days,
`config/corpus.yaml`). One GraphQL request carries 50 repositories and costs one rate-limit
point, so a pass over the ~25k corpus is ~500 requests and, measured in Actions, about 30
minutes, plus a REST call for each rename and a few calls per GitLab or Codeberg member.
Consult the current hosting plan for execution quotas and costs.

A pass is resumable. Rows are read in id order, `chunk_size` (2,500) at a time, and
every chunk read in full is promoted, each row with the moment it was actually read as its
`observed_as_of`. A run stops on its own clock at `max_runtime_minutes` (40, inside the job's
60-minute timeout) and records the unfinished pass in `MANIFEST.refresh.in_progress`
(`started_at`, `cursor`, `done`/`total`, running totals, why it stopped); `corpus-due` is true
while a pass is in progress, so the next daily wake-up carries on from the cursor.
`MANIFEST.refresh.last`, which `corpus-due` and the MCP server's staleness read, advances only
when a pass has covered the whole corpus, and is dated from the pass's start (`at`, with
`completed_at` and `runs`), so the layer never claims to be fresher than its oldest row.

No back-off wait is longer than `max_wait_seconds` (120): a host that asks for longer is
recorded as unresolved for that row (it keeps its last known facts and says why, e.g.
`http_429`), not waited on. Every wait of 10 seconds or more is logged, as is each chunk. A
refresh stops and keeps the previous layer if it would exceed `max_graphql_points`; a pass that
cannot read more than 2% of the corpus stops at the chunk that would cross that bar. A
repository that has gone (deleted, made private) stays listed with `found: false` and its last
known facts. A renamed one keeps its `id`; the new key is added to `aliases`, so both resolve.
A failed or cancelled run opens or comments on a `corpus-refresh-failure` issue, as the daily
cycle does, and every wake-up, due or not, opens a `corpus-stale` issue (and an error in the
job summary) when the last full refresh is older than `stale_after_days`.

## Identity: renames, forks, mirrors, siblings

One real project is one row. Every write of the layer (`corpus-export`,
`corpus-refresh`, `corpus-identity`) runs `source_pipeline/identity.py`, a deterministic,
idempotent step over the rows, the canonical project keys (`data/canonical/oss_projects.jsonl`:
`oss_id`, `repo_url`, `github_url`) and GitHub's own identity facts in
`data/identity/github.jsonl` (current name, repository id, `isFork` and `parent`, `isMirror` and
`mirrorUrl`). Those facts are re-read by `corpus-identity --refresh`: GraphQL batches of 100
(the number of requests depends on corpus size), REST only to follow a transfer GraphQL does not, refused
above 150 batches. A repository GitHub cannot answer keeps its previous facts.

```bash
python -m source_pipeline corpus-identity            # re-resolve from the facts held; no network
python -m source_pipeline corpus-identity --refresh  # re-read GitHub's identity facts first (network)
```

| `identity` field | |
|---|---|
| `renamed_to` | the current address when `id` is an earlier name; also an alias, so both resolve |
| `merged` | rows collapsed into this one, each with a `reason`: `renamed` (a shared address), `same_repository` (same host repository id), `same_canonical_project`, `mirror`; each id is now an alias |
| `fork_of`, `fork_parent_in_corpus` | a fork's parent (its corpus id when held). Forks are marked, not collapsed (a fork can be the maintained successor), but a fork whose parent is held must never be offered as a separate replacement candidate for it |
| `mirror`, `mirror_of` | GitHub flags it a mirror, or its own description says so ("Mirror of …", "read-only mirror", "(MIRROR)"; "mirroring" and "a mirror service" do not count). `mirror_of` is the upstream it names (a bare `owner/name` only when held here). A mirror of a held upstream, including the same owner/name on another host or a canonical project's other address, is collapsed into it; a mirror of an upstream The Source does not hold (postgres/postgres) stays, as the only copy |
| `siblings`, `sibling_primary` | one owner, one project split across repositories: `owner/server` + `owner/clients`, `owner/x` + `owner/x-docs` / `-cli` / `-desktop` / `-mobile` / ... A name lookup should land on `sibling_primary`: the repository named after the project, else the most-starred |

The primary of a collapsed group is the canonical project's own row, else the one that is not
a mirror, else the one under its current name, else github.com, else the smallest id. Lanes,
aliases and provenance are unioned. `MANIFEST.counts.identity` counts every kind and
`MANIFEST.identity.facts` names the facts file, its sha256 and when it was observed.
`corpus-validate` checks `identity` against `schema/the-source.corpus.schema.json` and
referentially: a merged id is an alias and no longer a row, a held fork parent is a row, a
sibling group closes on one primary of the same owner, and no address names two rows.

## How the MCP server uses it

The server verifies `data/corpus/` against its MANIFEST before serving it, the way it verifies
the signal layers. If the corpus is missing or fails verification, the canonical tools keep
serving, search covers the canonical layer only and says so, and `source_dataset_info` gives the
reason. If the last refresh is older than `stale_after_days` (10), every answer that uses the
corpus reports `corpus.status: "stale"`.

- `source_search` covers the full corpus, matching host descriptions and topics as well as
  names, and takes a `tier` filter. Every result carries `tier`.
- `source_get_project`, `source_compare_projects` and `source_get_momentum` accept any corpus
  project by id, URL or name. Discovery rows say what they lack.
- `source_emerging_projects` narrows a `query` over the full corpus and labels each row.
- `source_get_alternatives` and `source_recommend` are unchanged. With `include_discovery: true`
  they add a separate `discovery_candidates` list, labelled and never mixed into
  `recommendations`. A test proves, over the whole layer, that the corpus cannot change any
  recommendation answer, and `scripts/recommendation_parity.py` compares every recommendation
  answer with the previous release, byte for byte.

## Licence and privacy

The export carries facts: identities, host-reported metadata, licence identifiers and statuses,
and where each project was found. No list text is copied; a list contributes only the fact that
a URL appears in it, at a pinned commit and line, and only lists with a recognised licence are
read ([LICENCE-REVIEW.md](LICENCE-REVIEW.md)). Descriptions and topics are the host's metadata,
written by each repository's maintainers, shown truncated as identifying metadata. No
private maintainer data is included: the private seed list contributes only the `maintainer_seed` label,
and `corpus-validate` refuses any route that names it.
