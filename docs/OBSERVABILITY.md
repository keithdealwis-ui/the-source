# Monitoring data freshness, quality and service health

Inspect what a cycle processed, diagnose failures, measure retrieval quality and trace the origin of a published dataset. This maintainer guide also describes the hosted MCP service's aggregate telemetry and retention.

For users, `source_dataset_info` reports freshness and the served snapshot. [Support](https://the-source.dev/support/) explains what to include when reporting a problem. The sections below preserve the operational commands and record formats.

## Intake reports

Each cycle writes `runs/reports/<run_id>.intake.json` (schema `the-source.intake-report/1`)
next to its run record, and the workflow commits both. The report follows the run's material
through eight stages. For each stage it gives `in`, `out`, `stopped` and, where the pipeline
records reasons, `stopped_by_reason` (most frequent first):

| Stage | In → out |
|---|---|
| `upstream_lists` | licence-cleared lists → lists that yielded entries at their pins (pins advanced or kept) |
| `claims` | every list's relationship claim → distinct relationship candidates (merging plus refused claims) |
| `live_verification` | repositories → repositories the host answered for |
| `eligibility` | repositories → recommendable (licence, 12-month activity, not archived) |
| `relationship_validation` | relationship candidates → validated |
| `launch_selection` | validated → launch set (100-product floor, 250-OSS minimum, 400-edge cap) |
| `export_guard` | launch set → exported (share-alike-only claims withheld) |
| `canonical` | exported → canonical relationships, with the MANIFEST counts |

The report also covers manual intake (submissions by outcome and canonical status, ledger
admissions, promotions this run), discovery (search, pool, radar admissions) and the run's
counts. `change_from_previous` lists every stage figure that moved since the previous promoted
run's report. A failed run promotes nothing. Its report says so (`promoted: false`) and shows
the last good funnel.

The report is a deterministic function of the committed files and the run record, so
`obs-intake-report --run <id>` on the commit that run produced always gives the same report.
Writing the report can never fail a cycle. If the writer itself fails, the error goes to
stderr and the run's outcome stands.

Verified live: `runs/reports/2026-10-17-schedule-a1.intake.json`, for dataset
`02568a5dd656e3a8`. Funnel:

- 10 lists
- 1,570 claims, giving 874 relationship candidates
- 637 repositories checked, 621 answered
- 618 repositories, 392 eligible
- 874 relationships, 509 validated
- 358 relationships in the launch set
- 333 exported, the same 333 canonical (25 withheld as share-alike-only)

## Failure fingerprints and alerts

Each failure signal in a run record gets a fingerprint. A signal is one of:

- a failed run (`error`, at `failed_step`)
- a per-repository failure the cycle survived (`failures[]`)
- a strategy reporting anything but `ok`

The fingerprint is the first 12 hex digits of
`sha256(step + message with numbers, hashes, timestamps, URLs, repository keys and quoted lists masked)`.
The same fault on another day therefore has the same fingerprint, and a different fault gets a
different one. The cycle now records `failed_step`, the step that was running when it stopped.
For older records the step is inferred from the error text.

`obs-alerts` judges one run against every run before it, using the rules in
`config/observability.yaml`:

| Rule | Severity | When |
|---|---|---|
| `cycle_failed` | critical | the run failed. `status: new` the first time this fingerprint is seen, `recurring` after that, with `first_seen` and `consecutive_runs` |
| `no_recent_success` | critical | no successful run for 36 h |
| `new_failure_fingerprint` | warning | a survived failure or degraded strategy never seen before |
| `recurring_fault` | warning | the same fingerprint in 3 or more consecutive runs |
| `canonical_shrink` | warning | a successful run shrinks canonical SaaS products, projects or relationships by at least 5 items and at least 5% |

The daily-cycle workflow runs `obs-alerts` after every cycle. `scripts/surface_alerts.py` then
keeps **one GitHub issue per fingerprint**:

- The title ends `[fp:<fingerprint>]`.
- Critical alerts get the label `cycle-failure`; warnings get `cycle-alert`.
- A repeat of the same fault comments on the open issue.
- A new fault opens its own issue.
- An issue whose fault is neither raised by the run nor present in it is closed as recovered.
- The single pre-fingerprint "Daily cycle failing" issue is closed by the first successful run.

The previous behaviour of a failed workflow run and a durable issue is kept.

`obs-fingerprints` lists every fingerprint in `runs/`. The history today has five:

- the GitLab 403 build failure of 2026-10-10 (twice), together with the deep-enrichment
  degradation and the survived failures behind it
- the `canonical` failure drill (twice)
- the `history` failure drill

Run records carry `counts.canonical_saas_products` from this change on, so `canonical_shrink` can watch
the SaaS catalogue from the next cycle. Older records do not have that count. Replayed over the
2026-10-14 cycle (117 → 107 products), the rule therefore raises nothing: projects did not move,
and relationships fell only 348 → 337 (3%). For past drops, use `obs-lineage reconcile` (S0.6).

## `last_run` in `source_dataset_info`

`source_dataset_info` now carries `last_run`:

- the newest run record, using the latest-record rule (latest end; a running record counts by its
  start), with:
  - its status, times, error (truncated to 300 characters) and `failed_step`
  - the dataset versions before and after the run
  - the number of failures it survived and the strategies that degraded
  - the main counts
- `age_hours`, and `status`: `ok`, `stale` (no run record for 48 h) or `unavailable`
- `last_success`
- `serving_last_success`: whether the snapshot being served is the output of the last
  successful run

A failed run promotes nothing, so after a failure `run.status` is `failed` while
`serving_last_success` stays true. A client therefore sees both that the cycle is failing and
that what it is reading is still the last good snapshot.

The records are the public `runs/*.json` committed to `main`. The hosted server's sparse
clone gains `runs` (`deploy/the-source-mcp/install.sh`). A read layer without `runs/` still
serves, with `last_run.status: unavailable`.

## Golden queries

`config/golden_queries.yaml` holds 48 real tool calls:

| Kind | Count | Tool |
|---|---|---|
| "What replaces X" | 28 | `source_get_alternatives` (one by URL) |
| Needs in words | 8 | `source_recommend` |
| Hard-constraint calls | 7 | `source_recommend` |
| Hybrid searches | 3 | `source_search` |
| Calls that must fail safely | 2 | unknown product; impossible constraint |

`obs-golden` runs them in process against the MCP engine, with the clock pinned to
`data_as_of`. It reports these measures:

- **recall@5**: relevant projects in the top 5 / min(5, relevant projects that could be returned).
  For a constrained call, a relevant project whose published facts fail the constraint
  cannot be returned and is not counted as a miss.
- **precision@5**: relevant projects in the top 5 / rows in the top 5.
- **constraint violations**: every row returned is rechecked against the published files, not
  against the server's own filter:
  - a recommendation must be a recommended relationship for the product asked about
  - a recommendation must never be a discovery-tier project
  - the first rows for a supported product must be its published `api/v1` page
  - each row must meet every hard constraint: licence, family, maintenance, stars, days since
    activity, health, fit, evidenced self-hosting, 7-day momentum

The target is zero violations.

The checked-in reference results are in `observability/golden-baseline.json`. They describe that baseline dataset, not a live assessment of today’s recommendations. Inspect the latest run for current results.

`obs-golden --baseline observability/golden-baseline.json --check` fails on any of these:

- a constraint violation
- a must-fail query that does not fail safely
- mean recall@5 or precision@5 falling more than 0.05 below the baseline (`config/observability.yaml`)

CI runs it on every push. The daily cycle runs the same evaluation as step 14,
`golden_queries`, on the layer it has just built. A problem there degrades that strategy and
raises a fingerprinted alert, but it never blocks the canonical data. To accept a new
baseline deliberately, run `obs-golden --write-baseline observability/golden-baseline.json`
and commit the file.

**Label review.** Relevance labels are a separately reviewed reference set, not inferred from the ranks being tested. They can name projects not yet held by The Source, which count as misses. Review label changes deliberately and record a new baseline only when appropriate; current values live in `observability/golden-baseline.json`.

## Privacy-preserving MCP telemetry and weekly reports

**Off by default.** The engine counts nothing unless a transport gives the counters somewhere
to go:

- **stdio (`mcp-serve`)** has no telemetry option and counts nothing. Local use needs no
  telemetry and writes no files.
- **Streamable HTTP** counts only with `--telemetry FILE`. This is how the hosted server runs.

**What is counted** (schema `the-source.mcp-telemetry/2`), per UTC day, per tool, per outcome:

- a count
- a latency histogram
- for the list tools, how many `ok` answers were empty (the zero-result rate, the one quality
  signal kept)

No arguments, queries, results, client names, sessions or addresses are kept. Retention is
90 days.

**Restarts keep the counters .** Every flush replaces the file with the in-memory
counters, so the server resumes an existing `/1` or `/2` file at start before it serves. Before
this fix a deploy or reboot erased the retained days, and the weeks it touched read as zero use.
A file that cannot be resumed (unparseable, unknown schema, malformed cells) is moved aside to
`<file>.unreadable-<UTC>` and counting starts from zero; it is never overwritten. If it cannot
even be moved aside, that run counts nothing. Days resumed from a `/1` file have no `empty`
counts, so the report's zero-result rate is understated for those days only.

**Weekly report** (`source_pipeline/telemetry_report.py`, standard library only). A systemd
timer, `the-source-mcp-report.timer`, runs every Monday at 01:30 UTC. It writes the last
complete ISO week to `/var/lib/the-source-mcp/reports/<week>.json` and `.md`. The report makes
the counters coarser still:

- **Whole weeks only, no daily figures.** At low volume a single call on a given day could be
  linked to the one person who made it.
- **Small-count suppression.** Every count from 1 to 4 is published as `<5`, and any rate
  computed from a suppressed count is withheld.
- **Latency as buckets.** Latency appears only as the histogram bucket holding the median and
  the 95th percentile.

The reports stay on the host. Publishing them requires a separate review of the data and privacy implications.

## Reproducible lineage and catalogue reconciliation

Every cycle commits its canonical MANIFEST, the dataset it was built from and its run record
together. The lineage of the published layer can therefore be recomputed from git alone.

- `obs-lineage history`: one record per dataset version, containing:
  - the version it replaced
  - the run and commit that produced it
  - its counts and file hashes
  - its inputs: upstream pins, and the git tree hashes of `source_pipeline/` and `config/` at that commit
- `obs-lineage verify`: for each version, checks that:
  - the committed files hash to the MANIFEST
  - `dataset_version` is the hash of those hashes
  - the counts equal the rows
  - the run record names the same `dataset_version_after`, and the previous version as
    `dataset_version_before`

  CI runs it. Today it checks 24 versions, all of which pass. The one not produced by a cycle
  is the initial published build, `8b122029564c`.
- `obs-lineage reconcile --from A --to B`: compares the canonical SaaS products at A and B,
  attributing every product that left or joined to the cycle that changed it. To do that, it
  re-runs the launch selection (`build.select_launch`) on that cycle's own committed dataset
  and policy. The recomputation must reproduce the committed launch ranks exactly; the cause
  is then read from it.

`A` and `B` may be git revisions or dataset versions.
