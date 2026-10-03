# KEI-848 acceptance map: daily intelligence cycle

Each criterion maps to direct evidence in this repository and to the independent checks
in `scripts/verify_kei848.py` (no producer code imported; run by the evidence pack's
`verify.sh` together with the offline rebuild, canonical validation, the test suite and
the KEI-807 checks). Producer: claude-code, 2026-10-02/03.

Live runs referred to below:

| Run | Trigger | Result | Evidence |
|---|---|---|---|
| `manual-20261002T155246Z-manual-a1` | manual (Actions 37030007530) | succeeded, dataset `621e24f0` | first live daily cycle; tag `cycle/manual-20261002T155246Z-manual-a1` |
| `manual-20261002T155854Z-manual-a1` | manual drill (Actions 37030701050), `drill_fail_at=history` | failed, nothing promoted | commit `86e5c7e` touches only its run record; issue #2 opened |
| `2026-10-03-schedule-a1` | **schedule**, unattended (Actions 37081808219) | succeeded, dataset `18326d92` | commit `c88ad4b` by `the-source-cycle`, tag `cycle/2026-10-03-schedule-a1`; issue #2 closed |

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | A production-like daily scheduled cycle is demonstrated end to end | **Met** | `config/schedule.yaml` `cadence: daily` (03:00 Asia/Dubai, 4 h window, 16 h catch-up); `.github/workflows/daily-cycle.yml` hourly wake-up asks `cycle-due`. Run `2026-10-03-schedule-a1` ran unattended on schedule, executed all nine strategies, committed and tagged its result and wrote history day 2026-10-03 (287 s of steps, ~5 min job). |
| 2 | The cycle discovers new candidates and reconciles them against the existing corpus | **Met** | `discovery_search` (5 GitHub topic lanes + the awesome.ecosyste.ms awesome-selfhosted seed). Reconciliation by key, resolved name and repository id (`daily.reconcile_candidates`): 40 sightings on 10-02 matched existing corpus entities and were not added; 414 genuinely new candidates entered `data/discovery/candidates.jsonl`; on 10-03, 13 new repositories entered the corpus through upstream lists and 2 new pool candidates appeared. The verifier recomputes that no pool candidate shares a key, resolved name or repository id with the corpus. |
| 3 | Known-project metadata is refreshed without unnecessary full re-enrichment | **Met** | `light_refresh`: 611-624 repositories in 34 calls a day (GraphQL batches of 50). Deep enrichment selected 2 of 611 (10-02) and 25 of 624 (10-03); a steady-state local run selected 0. 594 of 596 found projects in the snapshot carry today's light fields over an earlier deep observation (`deep_fetched_at < fetched_at`). Rotation re-reads every project's deep evidence at least every 7 days, within a budget of 150/day. |
| 4 | Daily snapshots are retained and queryable for historical delta calculations | **Met** | `data/history/daily/2026-10-02.jsonl.gz`, `2026-10-03.jsonl.gz`; `INDEX.json` holds each day's sha256, rows, run and dataset version (deterministic gzip; tampering is refused). `history-delta --days 1` on 10-03: base 10-02 (exact), 596 comparable projects, 317 with a star change. 7/30/90-day queries return `insufficient history` until enough days exist, never an invented baseline (unit test covers 1/7/30/90 with a synthetic history). Retention 400 days. |
| 5 | A materially changed or new project can trigger deeper enrichment and re-scoring | **Met** | 10-02: a project near the 90-day maintenance boundary that had been pushed since was re-read and re-scored. 10-03: 12 material changes (new releases, star jumps) and 13 new projects → 25 deep enrichments, 7 re-scored, every re-score present in the append-only score history at the run's `as_of`. Material triggers: archive/disable, licence, identity (rename/transfer), new release, found flip, star change ≥ max(500, 10%), maintenance boundary. |
| 6 | Duplicate candidates do not create duplicate canonical entities | **Met** | Verifier: canonical ids unique; one canonical id per repository id across the snapshot; no two canonical projects share a repository id; pool ids unique (aliases merged). Live example: `crowddotdev/crowd.dev`, now `linuxfoundation/crowd.dev`, is one entity (harvested under the old name, resolved by the host). Unit test covers case variants, renamed corpus repositories, same-id-different-name and repeated sightings. |
| 7 | Each run records timing, counts, resulting dataset/version reference and failures/retries | **Met** | Every daily record has `timings_s` (per step), `retries`, `failures`, `counts` (`discovered`, `added`, `changed`, `unchanged`, `failed`, `deferred`, `deep_enriched`, `rescored`, …), `canonical.dataset_version_before/after`, `commit_tag`, `history_day`. The schema (`run_record`, with an `if/then`) requires these on every successful daily record; the verifier validates every record and proves the schema rejects one with a count removed. |
| 8 | A failed partial run leaves the last known-good canonical dataset readable | **Met** | Drill at `history`, the last step before promotion (canonical build and validation had already succeeded in scratch): status `failed`, commit `86e5c7e` touches only `runs/…json`, dataset stayed `621e24f0`, no history day written, issue #2 "Daily cycle failing" opened. The next scheduled run succeeded and closed it. Unit test: a worker that corrupts its scratch copy then fails leaves the live copy byte-identical. Light-refresh failures fail the cycle; known-project deep failures keep the last good observation and are listed in `failures`. |
| 9 | Existing KEI-807 provenance, licensing and schema guarantees remain intact | **Met** | `verify_kei807.py` passes in full against the same clone (39/39), including the export guard, licence notices, schema validation, byte-identical rebuild and run-record checks; `canonical-validate` 27/27; CI rebuild-equals-committed is green on the KEI-848 commits. Only two KEI-807 checks were adapted, and only for the renamed workflow and the daily default at the same local time. |
| 10 | A documented build-vs-ingest decision | **Met** | `docs/BUILD-VS-INGEST.md`: 11 upstreams evaluated from live probes (GitHub, GitLab, Codeberg, ecosyste.ms repos/awesome/packages/topics/timeline, deps.dev, OSS Insight, GH Archive, Libraries.io, Software Heritage), with what each is trusted for, licence and attribution, limits and cost, freshness, data-quality risks and fallback; attribution in `PROVENANCE.md`. |
| 11 | Commodity ingestion is not rebuilt where an upstream meets the requirement without a documented reason | **Met** | Nothing rebuilt that an upstream provides: no event warehouse, star-history store or topic index. The one origin query (batched light refresh) is justified against every upstream ("Why the core refresh is not ingested": freshness, cost, exactness, identity). Upstreams wired where they meet the need: two corroborators, one discovery seed. |
| 12 | The canonical Source remains provider-independent | **Met** | `corroborate.py` has no write path; every provider block says `canonical_effect: none`; canonical stars equal the host's where a provider disagreed; a provider over 20% errors or under 80% agreement is `degraded` (visible in the record and job summary) without failing the cycle (unit test simulates a deps.dev outage); an origin outage fails the cycle instead and promotes nothing. Discovery can only add to the pool. |

## Notes

- **Canonical membership moved on 10-03, by design.** Three upstream lists advanced
  (awesome-selfhosted, altstack, fossalt). The KEI-805 launch selection rotated three
  unsupported SaaS products out (Amazon Neptune, Raindrop.io, Teachable) and two in (Common
  Room, Fivetran), so 8 projects left the canonical set and 8 joined. All 8 leavers are
  still `recommendable` with `validated` relationships in the full dataset, and all 20
  supported SaaS products stay `ok`.
- **Fixed before commit:** comparing GraphQL `latestRelease` (prereleases excluded) with
  the deep observation's newest release (prereleases included) invented 99 changes in the
  first local run; corroboration under a stale name reported renamed repositories as
  missing. Both have tests.

## Residue (NON_BLOCKING, recorded on the outcome)

1. Repository growth: `data/live/snapshot.json` (~17 MB) and the commit-path cache (~12 MB)
   are now committed daily rather than weekly.
2. Software Heritage evaluated as an existence fallback, not wired (no current need).
3. A courtesy confirmation to ecosyste.ms about API use, and setting `mailto` for its
   polite pool, is an external action for Keith.
