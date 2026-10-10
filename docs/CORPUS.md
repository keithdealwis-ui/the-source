# Discovery corpus (KEI-912)

`data/corpus/` is every project the Discover lane ([DISCOVER.md](DISCOVER.md), KEI-811) found and
kept, published as facts so that the MCP server can serve it as a **discovery tier** beside the
canonical layer. About 9,400 projects; the canonical layer scores a few hundred of them.

Publication was approved by Keith de Alwis, gate `gate-KEI-912-dba84c8b` (2026-10-10): the full
export, the licence-issue projects only as discovery entries with the issue shown, and the
licence and provenance posture below.

## Tiers

The export carries no tier. The MCP server derives it at load time from the canonical layer it
serves beside, so a tier can never disagree with the recommendations being served.

| Tier | Meaning | Recommended? |
|---|---|---|
| `recommended` | in the canonical layer with at least one recommended relationship | yes, exactly as before |
| `scored` | in the canonical layer, with Replacement Fit / Project Health, no recommended relationship | no |
| `discovery` | discovered, not scored: no Replacement Fit or Project Health | never |

Projects whose licence The Source does not recognise (about 1,400: no licence, `NOASSERTION`,
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
| `licence` | `spdx`, `status` (`recognised` or `issue`), `issue` (`category`, `reason`) when there is one |
| `maintenance` | `status` (`active` or `maintained`) and `last_meaningful_activity_at`, as the Discover lane evaluated them at `MANIFEST.membership.as_of` |
| `lanes` | `discover` and/or `replace` |
| `provenance` | every route that found it: `strategy`, `source`, `commit`, `locator`, `depth`, `via` |

Provenance names a curated list with its pinned commit and line (`list:owner/repo`, `commit`,
`line N`), a GitHub topic (`github-topic:x`), a recorded search (`search:id`), an owner's other
repositories (`github-owner:x`), a README link (`readme:owner/repo`), or the Replace lane.
Routes from the maintainer's private seed list are published as `maintainer_seed` with no file,
line or other detail.

`live.jsonl` row: `found`, `full_name`, `stars`, `forks`, `pushed_at`, `archived`,
`latest_release`, `licence_spdx` (as the host reports it now), `observed_as_of`, and
`unresolved` (why the last refresh could not read it, if it could not).

## Building and refreshing

```bash
python -m source_pipeline corpus-export     # data/corpus/ from data/discover/corpus/ (internal); no network
python -m source_pipeline corpus-refresh    # re-read every project's live facts from the host (network)
python -m source_pipeline corpus-validate   # MANIFEST, schema and publication-rule checks
python -m source_pipeline corpus-due        # is a scheduled refresh due
```

**Membership** changes only when the Discover lane is rebuilt and `corpus-export` is run again.
The lane's harvest reads the maintainer's private seed list, so it runs where that list lives;
everything after it is public and reproducible.

**Live facts** are refreshed without the Mac by `.github/workflows/corpus-refresh.yml`: a daily
wake-up that refreshes when `corpus-due` says so (weekly: due after 6 days,
`config/corpus.yaml`). One GraphQL request carries 50 repositories, so a full refresh is about
190 requests plus a REST call for each rename, roughly 8 minutes. That is inside the job token's
hourly budget, and Actions minutes on a public repository are free, so there is no new spend. A
refresh stops and keeps the previous layer if it would exceed `max_graphql_points`, or if it
cannot read more than 2% of the corpus. A repository that has gone (deleted, made private) stays
listed with `found: false` and its last known facts. A renamed one keeps its `id`; the new key
is added to `aliases`, so both resolve. Failures open a `corpus-refresh-failure` issue, as the
daily cycle does.

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
written by each repository's maintainers, shown truncated as identifying metadata. Nothing
private to AIQ is included: the private seed list contributes only the `maintainer_seed` label,
and `corpus-validate` refuses any route that names it.
