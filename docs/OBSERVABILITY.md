# Observability (KEI-918, The Source V2.1 WP1)

This file covers how to see what the daily cycle took in, what went wrong, how good the
retrieval answers are, how the hosted MCP is used, and where every published dataset came from.
It has six parts, S0.1 to S0.6.

| | What | Command | Where it lives |
|---|---|---|---|
| S0.1 | Intake report per run | `obs-intake-report [--run ID] [--write]` | `runs/reports/<run_id>.intake.json`, written by every cycle |
| S0.2 | Failure fingerprints and alerts | `obs-alerts [--run ID]`, `obs-fingerprints` | one GitHub issue per fingerprint, via the daily-cycle workflow |
| S0.3 | `last_run` in `source_dataset_info` | MCP tool `source_dataset_info` | read from `runs/*.json` |
| S0.4 | Golden queries: recall@5, precision@5, constraint violations | `obs-golden` | `config/golden_queries.yaml`, `observability/golden-baseline.json`, CI and cycle step 14 |
| S0.5 | Privacy-preserving MCP telemetry and weekly reports | `obs-telemetry-report` | `/var/lib/the-source-mcp/` on the MCP host only |
| S0.6 | Reproducible lineage; catalogue reconciliation | `obs-lineage history\|verify\|reconcile` | computed from git, nothing stored |

All commands are `python -m source_pipeline <command>`. None of them uses the network, and
only the cycle's report writer changes any file.

## S0.1 Intake reports

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

## S0.2 Failure fingerprints and alerts

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

## S0.3 `last_run` in `source_dataset_info`

`source_dataset_info` now carries `last_run`:

- the newest run record, using the KEI-887 rule (latest end; a running record counts by its
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

## S0.4 Golden queries

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

**Baseline** (dataset `02568a5dd656e3a8`, `observability/golden-baseline.json`):

| | Queries | recall@5 | precision@5 |
|---|---|---|---|
| All | 48 (46 measured, 2 must-fail) | **0.714** | **0.778** |
| `source_get_alternatives` | 28 | 0.738 | 0.836 |
| `source_recommend` | 15 | 0.664 | 0.719 |
| `source_search` | 3 | 0.722 | 0.533 |

Constraint violations: **0**. Must-fail queries passed: 2/2.

The weakest answers point to work for the ranking and the catalogue (each to be its own ticket):

- **Notion, three ways (0.2 / 0.2).** By name, by URL and as "a workspace wiki like Notion",
  the top rows are Folio Wiki, Dexio and Notabase rather than AppFlowy, SiYuan or Logseq.
- **"Self-hosted password manager for a team" (precision 0.25).** It returns Jitsi Meet and
  Zulip before Passbolt.
- **Recommendation coverage.** Vaultwarden is not a recommended 1Password alternative.
  Confluence and Slack each have a single recommendation.

`obs-golden --baseline observability/golden-baseline.json --check` fails on any of these:

- a constraint violation
- a must-fail query that does not fail safely
- mean recall@5 or precision@5 falling more than 0.05 below the baseline (`config/observability.yaml`)

CI runs it on every push. The daily cycle runs the same evaluation as step 14,
`golden_queries`, on the layer it has just built. A problem there degrades that strategy and
raises a fingerprinted alert, but it never blocks the canonical data. To accept a new
baseline deliberately, run `obs-golden --write-baseline observability/golden-baseline.json`
and commit the file.

**Label review.** The relevance labels were written by linux-claude from domain knowledge,
against the list of projects only (not their ranks, fit or health). They name projects The
Source may not hold yet, which then count as misses. Keith reviewed them on 2026-10-20 (KEI-930)
and approved them unchanged. The baseline was re-recorded that day, on the KEI-930 tree (precision@5
0.663, recall@5 0.687). The fall from 0.778 is mostly genuine alternatives the labels do not name,
admitted by KEI-930's licence decisions (KeePassXC, GitLab, Rocket.Chat, Outline, ...). A precision@5
regression is measured from the new baseline.

On 2026-10-21 Keith added `supabase/storage` (S3-compatible object storage, admitted by KEI-923) to
`search-object-storage`. Unlabelled, it outranked `openstack/swift` and moved that query's recall@5 from
1.0 to 0.8. The baseline was not re-recorded.

## S0.5 Privacy-preserving MCP telemetry and weekly reports

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

**Restarts keep the counters (KEI-927).** Every flush replaces the file with the in-memory
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

The reports stay on the host. Publishing them anywhere is a separate decision for Keith.

## S0.6 Reproducible lineage and catalogue reconciliation

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
  is the initial KEI-807 build, `8b122029564c`.
- `obs-lineage reconcile --from A --to B`: compares the canonical SaaS products at A and B,
  attributing every product that left or joined to the cycle that changed it. To do that, it
  re-runs the launch selection (`build.select_launch`) on that cycle's own committed dataset
  and policy. The recomputation must reproduce the committed launch ranks exactly; the cause
  is then read from it.

`A` and `B` may be git revisions or dataset versions.

### The 118 → 102 catalogue

`obs-lineage reconcile --from a6e5bef0eaf47a24 --to 02568a5dd656e3a8`: dataset
`a6e5bef0eaf47a24` (cycle `2026-10-10-schedule-a1`, the version KEI-851 served) held 118
canonical SaaS products. Dataset `02568a5dd656e3a8` (cycle `2026-10-17-schedule-a1`) holds 102.
Of the 11 versions in between, every one reproduces, and the count balances:
118 − 17 + 1 = 102.

| Cycle | Products | Change |
|---|---|---|
| `manual-20261010T142835Z` | 118 → 117 | −Otter.ai |
| `2026-10-14-schedule` | 117 → 107 | −Discourse hosting, Ecwid, Grammarly, LastPass, Loggly, MongoDB Atlas, Papertrail, Power BI, Render, Webex |
| `2026-10-15-schedule` | 107 → 100 | −Circle, Fivetran, Gmail, Pingdom, Splunk, Twitch, Wix |
| `2026-10-16-schedule` | 100 → 102 | +Pingdom (back), +SAP Business One |

**Cause.** All 17 departures share one cause, `below_launch_cutoff`. None of them lost its
validated relationships. They were ranked past the launch cut-off:

- The launch rule (decision dr-KEI-805-507e4f70) takes the first 100 products as a floor.
  It extends down the same ranking only as far as the 250-distinct-OSS minimum requires, and
  never past 400 edges.
- As relationships were validated and the computed per-product cap moved from 10 to 12 to 11,
  the minimum was reached sooner.
- The extension beyond the floor therefore shrank from 19 products to 18, then 8, then 1,
  then 3.

The products that left are the tail of that extension. This is the rule working as written,
not data loss. Whether the launch floor should keep products once published is a strategy
question for Keith and Draper.
