# Operations: the weekly research and refresh cycle

## Schedule

`.github/workflows/weekly-cycle.yml` wakes every hour at minute 7. It asks
`python -m source_pipeline cycle-due` whether a cycle is due under `config/schedule.yaml`:

```yaml
timezone: Asia/Dubai
weekday: sunday
time: "03:00"
window_hours: 4
max_attempts: 3
```

A cycle is due when all three of these hold: the time is inside the window, this week's
cycle has not yet succeeded, and fewer than `max_attempts` attempts were made. To change
the day, time, timezone or window, edit the YAML. No code or workflow change is needed.

To run one now, use Actions, then *Weekly research and refresh cycle*, then *Run workflow*
(trigger `manual`). A manual run does not count against the weekly attempts.

## What a cycle does

| Step | Strategy id | What |
|---|---|---|
| 1 | `upstream_lists` | Move each licence-cleared upstream to its newest commit, but only if its licence file is byte-identical to the reviewed one, it still parses, and it yields at least 80% of the previous entries. Otherwise keep the pin and record why. |
| 2 | `intake_ledger` | Harvest the lists at their pins, plus repositories admitted through manual intake. |
| 3 | `live_refresh` | Ask every candidate's host again (stars, licence, archive state, commits, releases, merged PRs). Retry only failed lookups, up to 2 more times. |
| 4 | (build) | Rebuild the dataset and run its validation (maintenance classes, lanes, launch selection). |
| 5 | `new_pair_scoring` | Score Project Health and Replacement Fit for recommendable projects and relationships not yet scored, via the single-project path (up to 60 projects per cycle). |
| 6 | (canonical) | Rebuild `data/canonical/` and `api/v1/`, then run canonical validation, including a byte-for-byte rebuild check. |
| 7 | (intake) | Record on each pending intake outcome whether it is now canonical. |

## Safety

- **Work happens in a scratch copy.** Nothing in the live checkout changes until every
  step has passed. Promotion replaces the produced directories whole.
- **Failure leaves the last known-good data intact.** The run record is committed with
  `status: failed` and the error. A GitHub issue labelled `cycle-failure` is opened, or
  commented on if already open, and the workflow run is marked failed. The next hourly
  wake-up inside the window retries. A later success closes the issue.
- **Retries are safe.** Every output is a deterministic function of its inputs. Keys are
  content-derived, histories are append-only and de-duplicated, and one concurrency group
  (`canonical-writer`) serialises the cycle and intake jobs, so nothing is written twice or
  concurrently.
- **Lookup failures are not data.** A failed host lookup looks exactly like a project with
  nothing to show, so a cycle with unresolved failures promotes nothing.

## Run records

`runs/<run_id>.json` (schema `run_record`):

- `started_at`, `ended_at`, `status` (`running`, `succeeded` or `failed`), `trigger`, `attempt`, `schedule`
- `strategies[]`: each strategy's id, status and detail (pins advanced or kept, retries,
  projects scored, deferrals)
- `counts`: `candidates`, `discovered`, `retained`, `rejected`, `refreshed_records`,
  `stale_or_abandoned_detected`, canonical relationships added or removed, intake promotions
- `transitions`: which projects stopped being recommendable (with reasons) and which became recommendable
- `canonical`: `dataset_version_before` and `dataset_version_after`, `changed`, and
  `commit_tag` (`cycle/<run_id>`, the resulting canonical commit)
- `error`: set when the run failed

## API budget

The cycle uses the workflow's own `GITHUB_TOKEN` by default. It is scoped to this
repository, expires when the job ends, and needs no stored secret. That allows about 1,000
REST requests per hour. A full refresh of ~600 repositories fits in one to two hours,
because commit-path lookups are cached in `data/live/commit_paths_cache.json` and the HTTP
layer waits out rate-limit resets. For more headroom, add a repository secret
`SOURCE_GITHUB_TOKEN` holding a fine-grained token with read-only access to public
repositories. It is used automatically when present.

## Scoring lanes

The full scoring lane (KEI-806) and the knowledge graph (KEI-844) run where their evidence
lives, not in this repository. After a full-lane run, `python -m source_pipeline
scores-extract` compacts its output into `data/scores/`. The newest evaluation per project
and pair wins; single-project rows are superseded by the next full-lane run.
