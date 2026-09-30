# The Source — Discover lane (KEI-811)

**Status: internal. Nothing here is published, recommended or endorsed.**

KEI-805 built the **Replace** lane: open-source projects with a credible SaaS
replacement relationship. KEI-811 adds the **Discover** lane: useful, emerging or
strategically interesting open-source projects, whether or not any SaaS product maps
to them. Both lanes feed one graph-ready corpus for KEI-844 (entity resolution and
knowledge graph) and KEI-806 (scoring). No SaaS mapping is forced.

## Run it

```bash
.venv/bin/python -m source_pipeline discover-harvest     # network: run the discovery strategies
.venv/bin/python -m source_pipeline discover-verify      # network: verify every candidate (hours)
.venv/bin/python -m source_pipeline discover-verify --only-failed   # resume / retry failures only
.venv/bin/python -m source_pipeline discover-build       # offline, deterministic
.venv/bin/python -m source_pipeline discover-validate
```

`discover-build` reads only recorded files, so it runs offline and the same inputs
produce the same bytes. It refuses a live snapshot from an interrupted verify run,
one taken under a different maintenance policy, or one taken against a different
frontier.

**The clock.** A verify run takes hours. The snapshot's `as_of`, which the build judges
every repository by, is when observation *ended*; `started_at` records when it began.
Activity observed during the run therefore always falls inside the clock. Commit and tag
dates set in the future by a committer's machine are ignored.

## Stages

| Stage | Reads | Writes |
|---|---|---|
| `discover-harvest` | `config/discover.yaml`, `config/discover_seeds.md`, GitHub | `data/discover/frontier.json` |
| `discover-verify` | frontier, GitHub | `data/discover/live.json`; appends `data/discover/history/observations.jsonl` |
| `discover-build` | frontier, live, history, KEI-805 `data/dataset/` (read-only) | `data/discover/corpus/` |
| `discover-validate` | corpus | `corpus/reports/validation_report.json`, `corpus/MANIFEST.json` |

## Where candidates come from

Everything is declared, with its bound, in `config/discover.yaml`.

| Depth | Strategy | What it adds |
|---|---|---|
| 0 | `keith_seed` | every GitHub URL in `config/discover_seeds.md` (Keith's corpus, pasted verbatim) |
| 0 | `github_starred` / `github_watching` | stars and watches of configured GitHub accounts, read on every harvest |
| 0 | `replace_lane` | every repository in the KEI-805 dataset |
| 1 | `owner_neighbourhood` | the seed owner's other public non-fork repositories (top 25 by stars) |
| 1 | `readme_links` | repositories a seed's README links to: integrations, dependencies, companions |
| 1 | `topic_search` | the most-starred repositories on the topics the seeds carry (90 topics x 100) |
| 1 | `curated_list` | repository links in licence-cleared curated lists, at a pinned commit |
| 1 | `trending` | pinned GitHub searches for fast-growing and highly starred active repositories |
| 2 | recursion | owner neighbourhood (10) and README links (20) of the 250 depth-1 candidates that at least two independent routes reached |

A curated list is used only if it carries a recognised licence; only repository URLs
are taken and no list text is kept. Lists without a licence are recorded as refused.

`raw_universe_cap` (12,000) bounds how many candidate addresses are evaluated.
Candidates past it, ordered by depth, then number of independent routes, then id, are
recorded as rejected with `discovery_budget_exceeded`, never dropped.

## The quality bar

The Discover lane is judged by **the same `config/policy.yaml` as the Replace lane**,
applied by the same code (`activity.py`, `build.eligibility`, `build.licence_verdict`,
`enrich.github`). Nothing is relaxed for scale. For scale, metadata is read in batches
first and a repository is rejected without deep inspection only where the metadata
alone is a sufficient reason under that policy:

| Reason | Why it is sufficient |
|---|---|
| `archived`, `disabled`, `empty_repository`, `not_public` | excluded by policy however active |
| `stale_no_meaningful_activity_12_months` (from `pushedAt`) | no push since the window opened means no commit, tag or release inside it |
| `fork_alias`, `mirror_alias` | another address for a project that exists elsewhere; the parent is recorded |
| `repository_not_found` | the host has nothing at the address |

Everything else is inspected in full: human-authored code commits and merged PRs,
version releases and tags judged by what they ship, and the licence read from the
host and, where the host cannot identify it, from the licence files themselves.
A failed lookup is recorded as `verification_incomplete` or
`live_verification_failed`; it is never read as inactivity.

Replace-lane projects keep their KEI-805 verdict (`quality.basis = "kei805"`). They are
referenced, not re-verified, and the KEI-805 dataset is never written.

## Outcomes and counts

| Outcome | Meaning |
|---|---|
| `recommendation_eligible` | passed the full KEI-805 bar. Eligible to be considered by KEI-806, nothing more |
| `retained` | passed existence, archive, alias and activity checks; licence not recognised (licence-exception lane) |
| rejected | in `rejections.jsonl` with machine-readable reasons, the routes that found it and the evidence |

Every record carries `endorsed: false` and `publication_status: internal_not_published`.
`summary.json` counts raw addresses, raw unique repositories, retained,
recommendation-eligible and rejected, with rejection reasons and lanes.

## Records (graph-ready)

`schema/the-source.discover.schema.json` defines them. In `data/discover/corpus/`:

| File | Content |
|---|---|
| `projects.jsonl` | one record per retained repository, keyed by a stable id |
| `rejections.jsonl` | every rejected candidate with reasons, stage, routes and evidence |
| `relationship_candidates.jsonl` | `from`, `relation`, `to`, `evidence`: evidence for KEI-844, not resolved edges |
| `nodes.jsonl` | owners, topics, languages, curated lists and SaaS products referenced by edges |
| `seed_coverage.json` | the outcome of every row of Keith's seed corpus |
| `sources.json` | every strategy's query, status, pinned commit and licence decision |
| `summary.json` | counts, input hashes, reason meanings, field origins |

**Stable ids.** `github:<host repository id>`: the host's numeric id survives renames
and transfers, so every address that reaches the same repository lands on the same
record, with all of them listed in `aliases`. Replace-lane projects outside GitHub are
`kei805:<oss_id>`.

**Relationship candidates.** `owned_by`, `has_topic`, `written_in`, `listed_in`,
`readme_links_to`, `discovered_via_topic`, and `saas_replacement_candidate` (from
KEI-805's relationships). A Discover-lane project may have no SaaS edge at all.

**Freshness.** `discover-verify` appends one observation per repository per run to
`history/observations.jsonl`, never rewriting a row. Each record's
`freshness.history` lists every observation up to the build's `as_of`, so a refresh
appends a point to a series rather than replacing the snapshot.

## Adding seeds

Paste rows or bare URLs into `config/discover_seeds.md`, or add a GitHub account to
`seeds.github_accounts`. Then `discover-harvest`, `discover-verify`, `discover-build`.
