# Operations: the daily intelligence cycle

Daily since KEI-848; the KEI-807 weekly cycle with its guarantees, run every day and split
into a cheap tier for everything and an expensive tier only where something changed.

## Schedule

`.github/workflows/daily-cycle.yml` wakes every hour at minute 7. It asks
`python -m source_pipeline cycle-due` whether a cycle is due under `config/schedule.yaml`:

```yaml
cadence: daily
timezone: Asia/Dubai
time: "03:00"
window_hours: 4
catch_up_hours: 16
max_attempts: 3
```

A cycle is due when all three of these hold: the time is inside the window (or its
catch-up), today's cycle has not yet succeeded, and fewer than `max_attempts` attempts were
made. GitHub's hourly cron is best effort and has gone 4-6 h between wake-ups, so a missed
window still runs on the first wake-up within `catch_up_hours`, marked `late`.
`window_hours + catch_up_hours` must stay under 24 h, so one day's catch-up can never run
into the next day's window. `cadence: weekly` plus `weekday:` restores the KEI-807
behaviour. To change anything, edit the YAML; no code or workflow change is needed.

To run one now, use Actions, then *Daily intelligence cycle*, then *Run workflow*
(trigger `manual`). A manual run does not count against the day's attempts.

## What a cycle does

| Step | Strategy id | What |
|---|---|---|
| 1 | `upstream_lists` | Move each licence-cleared upstream to its newest commit, but only if its licence file is byte-identical to the reviewed one, it still parses, and it yields at least 80% of the previous entries. Otherwise keep the pin and record why. |
| 2 | `intake_ledger` | Harvest the lists at their pins, plus repositories admitted through manual intake. |
| 3 | `light_refresh` | Ask every corpus repository's host the cheap questions: stars, forks, pushed_at, archive state, licence, newest release, default-branch head, repository id and current name. GitHub in GraphQL batches of 50 (~13 calls for ~600 repositories). Transient failures are retried twice; any left fail the cycle. |
| 4 | `discovery_search` | The approved lanes in `config/discovery.yaml` (GitHub topic searches, the awesome-selfhosted seed). Candidates are reconciled against the corpus by key, resolved name and repository id; only genuinely new ones join `data/discovery/candidates.jsonl`. A lane failure is recorded; discovery is additive and its look-back overlaps, so nothing is lost. |
| 5 | `corroboration` | A rotating sample (120/day) checked against ecosyste.ms and deps.dev. Flags only; see `docs/BUILD-VS-INGEST.md`. |
| 6 | `deep_enrichment` | The full KEI-805 observation (commits and PRs with the files they touched, release diffs, licence probes) only for: **new** repositories, **materially changed** ones (archive/disable flip, licence change, rename or transfer, new release, star change of max(500, 10%), found/not-found flip, or pushed since the last deep look while near a maintenance boundary), and **rotation**: any whose deep observation is 7 days old, oldest first, within `deep.max_per_cycle` (150). New and changed always go; rotation beyond the budget is deferred and counted. A known project whose deep lookup fails keeps its last good observation (recorded in `failures`); a new one that fails fails the cycle. Everyone else keeps their deep evidence with today's light fields laid over it (`deep_fetched_at` vs `fetched_at`). |
| 7 | (build) | Rebuild the dataset and run its validation (maintenance classes, lanes, launch selection). |
| 8 | `new_pair_scoring`, `rescoring` | Score unscored recommendable projects and relationships (up to 60), and re-score materially changed projects that already hold a score (up to 40). Overflow is deferred to the next day and counted. |
| 9 | (canonical) | Rebuild `data/canonical/` and `api/v1/`, then run canonical validation, including a byte-for-byte rebuild check. |
| 10 | (intake) | Record on each pending intake outcome whether it is now canonical. |
| 11 | `history` | Write the day's light observations to `data/history/daily/<day>.jsonl.gz` (deterministic gzip) and `data/history/INDEX.json` (hash, rows, run, dataset version). Kept `history.retain_days` (400). |
| 12 | `momentum` | Measure every repository's momentum and acceleration over 1/7/30/90 days from the history, including today's snapshot (KEI-849, [`MOMENTUM.md`](MOMENTUM.md)). Write `data/momentum/` and validate it (released methodology version, schema, byte-identical rebuild). A window without enough history says so per repository. A failure fails the cycle. |
| 13 | `community_radar` | Ask Hacker News (official API) and Forem (public API) which repositories are being discussed, rank them (capped, deterministic), reconcile each with the corpus and pool by name and GitHub repository id, and admit new ones to the discovery pool (KEI-850, [`RADAR.md`](RADAR.md)). A source outage degrades the strategy. A radar that fails its own validation is rolled back to the previous one, and the cycle carries on. |

Typical cost: a steady-state day with nothing changed made ~33 light calls plus discovery
and corroboration, no deep calls, and took about 4 minutes; the first daily run, which
also deep-enriched 133 projects and re-scored 40, took about 7 minutes.

## History: 1, 7, 30 and 90-day deltas

```bash
python -m source_pipeline history-days
python -m source_pipeline history-delta --days 7                    # newest day vs 7 days earlier
python -m source_pipeline history-delta --days 30 --on 2026-11-01 --key github.com/n8n-io/n8n
```

The base is the newest snapshot on or before `on - days`; `exact` says whether it is that
very day. With no snapshot old enough the answer is `insufficient history`, never an
invented baseline. Every snapshot's sha256 is checked against `INDEX.json` on read.

## Momentum and acceleration

```bash
python -m source_pipeline momentum --window 7 --sort acceleration --limit 20
python -m source_pipeline momentum-validate
```

These commands read `data/momentum/`, which the cycle rebuilds after each history day.
Each window is `measured` or carries its reason (`insufficient_history`, `no_baseline`,
`not_observed`, `identity_changed`), and the manifest gives each window's
`measurable_from` day. Methodology and versioning: [`MOMENTUM.md`](MOMENTUM.md).

## Discovery pool

`python -m source_pipeline discovery-pool` lists candidates with `first_seen`, `last_seen`,
the providers and lanes that found them, aliases (the same repository id seen under
another name) and the GitHub facts search returned. A candidate never becomes a canonical
entity by being found: it still needs a licence-cleared relationship claim (an upstream
list at a reviewed pin, or accepted intake). Once it is in the corpus it leaves the pool.

## Community radar

```bash
python -m source_pipeline radar --limit 20
python -m source_pipeline radar --key github.com/n8n-io/n8n
python -m source_pipeline radar-validate
```

These read `data/radar/`, which step 13 rewrites each day. The run record's
`community_radar` strategy gives each source's status, calls, removed (`gone`) items and
errors, the resolution budget used and deferred, the repositories admitted to the pool,
and the top ten. `counts.radar_ranked` and `counts.radar_pool_added` summarise it.
Repositories the radar admits show `provider: community_radar` in the discovery pool.
Methodology, sources and their terms: [`RADAR.md`](RADAR.md).

## Safety

- **Work happens in a scratch copy.** Nothing in the live checkout changes until every
  step has passed. Promotion replaces the produced directories whole.
- **Failure leaves the last known-good data intact.** The run record is committed with
  `status: failed` and the error. A GitHub issue labelled `cycle-failure` is opened, or
  commented on if already open, and the workflow run is marked failed. The next hourly
  wake-up inside the window (or catch-up) retries. A later success closes the issue.
- **History and the discovery pool are promoted with everything else.** A failed day
  writes no snapshot and changes no pool; the next success writes its own day.
- **Retries are safe.** Every output is a deterministic function of its inputs. Keys are
  content-derived and histories are append-only and de-duplicated, so a retry writes
  nothing twice.
- **One writer, no lost submissions.** Only a due cycle job joins the `canonical-writer`
  concurrency group. The hourly no-op checks run outside it, because GitHub keeps only
  one pending job per group and would otherwise cancel queued work. Intake jobs only
  *add* files that a cycle never writes: an outcome record, and a ledger entry per
  admission. Every job rebases before pushing and retries, so a cycle and concurrent
  intakes never conflict.
- **Lookup failures are not data.** A failed host lookup looks exactly like a project with
  nothing to show, so a cycle with unresolved failures promotes nothing.

## Run records

`runs/<run_id>.json` (schema `run_record`):

- `started_at`, `ended_at`, `status` (`running`, `succeeded` or `failed`), `trigger`, `attempt`, `schedule`
- `strategies[]`: each strategy's id, status and detail (pins advanced or kept, light and
  deep calls, retries, the changed projects with their reasons, discovery lanes, provider
  agreement, projects scored and re-scored, deferrals, the history day and its hash)
- `counts`: `discovered` (in the corpus plus new pool candidates), `added` (new canonical
  projects), `changed`, `unchanged`, `deep_enriched`, `rescored`, `failed`, `deferred`,
  `refreshed_records`, plus the KEI-807 counts (`candidates`, `retained`, `rejected`,
  `stale_or_abandoned_detected`, canonical relationships added or removed, intake promotions)
- `timings_s`: seconds per step; `retries`: retry rounds per step; `failures`: per-repository
  failures the cycle survived and what it did about each
- `transitions`: which projects stopped being recommendable (with reasons) and which became recommendable
- `canonical`: `dataset_version_before` and `dataset_version_after`, `changed`, and
  `commit_tag` (`cycle/<run_id>`, the resulting canonical commit)
- `history_day`: the snapshot this run wrote
- `error`: set when the run failed

The schema requires the KEI-848 counts and `history_day` on every successful daily record.

## API budget

The cycle uses the workflow's own `GITHUB_TOKEN` by default: scoped to this repository,
expires when the job ends, no stored secret. That allows 1,000 REST requests and 1,000
GraphQL points per hour. The light tier costs ~13 points a day; search ~10 calls; deep
enrichment ~3.6 calls per selected repository, with commit-path lookups cached in
`data/live/commit_paths_cache.json` and the HTTP layer waiting out rate-limit resets. For
more headroom, add a repository secret `SOURCE_GITHUB_TOKEN` holding a fine-grained token
with read-only access to public repositories. It is used automatically when present.

## Scoring lanes

The full scoring lane (KEI-806) and the knowledge graph (KEI-844) run where their evidence
lives, not in this repository. After a full-lane run, `python -m source_pipeline
scores-extract` compacts its output into `data/scores/`. The newest evaluation per project
and pair wins; single-project rows are superseded by the next full-lane run.

## Failure drill

To prove the failure path on the real system, run the workflow manually with
`drill_fail_at: light_refresh`, `canonical` or `history` (the last step before promotion). The cycle stops at that step.
Nothing is promoted, the run record says `failed`, and the `cycle-failure` issue is opened.
The next successful run closes the issue. Scheduled runs ignore the drill setting.
