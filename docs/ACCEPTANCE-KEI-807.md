# KEI-807 acceptance evidence

The Source 03: publish the canonical repository and build the live metadata refresh and
read surface. Repository: `github.com/keithdealwis-ui/the-source` (public since gate
`gate-KEI-807-bfaf2354`). Every row points at first-hand evidence in the repository or its
GitHub Actions history. `scripts/verify_kei807.py` re-checks each criterion independently
of the pipeline code.

| # | Criterion | Evidence | Status |
|---|---|---|---|
| 1 | Public repo under Keith's GitHub | `keithdealwis-ui/the-source` is **public**, approved by Keith in gate `gate-KEI-807-bfaf2354` (2026-09-30T05:23:38Z). Commit `c57a205` adds the licence package: MIT code (`LICENSE`), CC BY 4.0 own data and scoped CC BY-SA 3.0 (`DATA-LICENCE.md`). `config/publication.yaml` records `status: public` with the approval id, which `validate.py` requires. The personal seed corpus (`config/discover_seeds.md`) was removed from the tree and purged from history before the repository went public (history rewritten and force-pushed; now gitignored). Residual: GitHub can keep pre-rewrite objects reachable by their old SHA until GitHub Support purges them. | Met |
| 2 | Canonical schema validates in CI | `schema/the-source.canonical.schema.json`. `.github/workflows/ci.yml` rebuilds offline, runs `canonical-validate` (27 checks: schema for every record kind, manifest hashes, integrity, read surface, byte-identical rebuild), then the tests, then fails on any diff between committed and rebuilt data. Green on every code commit. | Met |
| 3 | Initial 20 SaaS products and validated OSS mappings | `config/supported_saas.yaml` holds 20 products chosen by a recorded mechanical rule (`scripts/rank_supported_saas.py`). `api/v1/saas/*.json` gives each 3–5 recommendations, every one a `validated` KEI-805 relationship with a publishable, credible Replacement Fit. | Met |
| 4 | Live fields: GitHub link, stars, maintenance, licence, release/activity | Every recommendation carries `github_url`, `stars`, `maintenance_status`, `last_meaningful_activity_at`, `licence_spdx`, `latest_release` and `live_checked_at`. The canonical project records also carry forks, archive state and `pushed_at`. | Met |
| 5 | Refresh updates metadata without editing extension or site code | Cycle commits by `the-source-cycle` touch only `data/`, `api/` and `runs/`. Example: commit `a741093` refreshed 594 repositories (Notion's docmost went from 21,794 to 21,829 stars, with a new `live_checked_at`) and changed no code, config or workflow file. | Met |
| 6 | Deterministic records for supported SaaS domains | `api/v1/index.json`, `domains.json` and `saas/<id>.json` share one content-derived `dataset_version`. Ranking is fixed. There are no wall-clock timestamps. CI and `verify.sh` rebuild and compare byte for byte. `domains.json` gives local host/path rules; the domain-matching examples are covered by tests. | Met |
| 7 | Upstream/API failures degrade safely, no corruption | The cycle works in a scratch copy and promotes only after all validations pass. A lookup failure is retried for the failing repositories only, and after 2 retries fails the cycle (a failed lookup is never data). An upstream licence change keeps the old pin. A failure drill on real infrastructure (run `36666445759`) left `dataset_version` `4a669ecae22a2c92` unchanged. | Met |
| 8 | Every output records when live metadata was last checked | `live.checked_at` on every project, `live_checked_at` on every recommendation, `last_checked` on every read-surface record, and `live_checked_at` in `index.json` and `MANIFEST.json`. Enforced by the `last_checked_recorded` check. | Met |
| 9 | Weekly scheduler, quiet window (Sun 03:00 Asia/Dubai), configurable without code changes | `config/schedule.yaml` (default Sunday 03:00 Asia/Dubai, 4 h window) is read by the hourly `weekly-cycle.yml` wake-up. GitHub's hourly cron proved best effort: wake-ups were 4–6 h apart and missed every window, so no scheduled cycle ran until `catch_up_hours` was added (commit `b2dff30`). If the window is missed, the first wake-up within 24 h runs the week's cycle, marked `late`. Proof: config-only commit `2cdef8e` opened a Thursday 06:00 window. GitHub's scheduler next woke at 08:44Z, after the window, and ran the cycle unattended as a catch-up: Actions run `36838208240` (`event: schedule`, `late: true`), run record `runs/2026-10-01-schedule-a1.json` (`trigger: schedule`, `status: succeeded`, dataset `4a669ecae22a2c92` → `f565b237b781d828`). The schedule was then restored to Sunday 03:00, also by a config-only commit. | Met |
| 10 | Each cycle does research and update | Run records list the strategies: `upstream_lists` (pins advanced only when the licence file is byte-identical; awesome-selfhosted and altstack advanced), `intake_ledger`, `live_refresh` (stale and abandoned detection via `transitions`) and `new_pair_scoring`, then build, validation and canonical rebuild. | Met |
| 11 | Run records: times, strategies, counts, success, commit/version | `runs/<run_id>.json` holds `started_at`, `ended_at`, `strategies[]`, `counts` (discovered/retained/rejected/refreshed/…), `status`, `canonical.dataset_version_before/after` and `commit_tag` (`cycle/<run_id>` git tag = the resulting canonical commit). | Met |
| 12 | Failed cycle keeps last known-good, surfaces durably, safely retryable | Drill run `36666445759`: failed record committed (`runs/` only), issue #1 "Weekly cycle failing" opened, data unchanged. Recovery: the scheduled run `36838208240` succeeded and closed issue #1 automatically ("Recovered: run `2026-10-01-schedule-a1` succeeded"). Attempts are counted per weekly cycle, and outputs are deterministic and de-duplicated. | Met |
| 13 | Narrow authenticated intake surface | `.github/workflows/intake.yml` (GitHub-authenticated `workflow_dispatch`), plus the CLI adapter `scripts/source_intake.py`. Clients need only Actions read/write and Contents read; the job is the only writer and it only adds files. | Met |
| 14 | Submit, status, outcome; suitable for ChatGPT / employees | `submit`, `status` and `result` in the CLI, with the raw HTTP calls documented in `docs/INTAKE.md`. The client-chosen `request_id` is the idempotency key (a duplicate submission returned the recorded outcome unchanged). | Met |
| 15 | Dedupe, same pipeline and gates; Replacement Fit only with evidenced relationship | `intake.py`: dedupe against corpus and ledger (with redirect), then live verification, licence, activity, archive and identity rules (the `build.py` functions), then Project Health and Evidence Confidence via the scoring rubric. Evidence matching requires a whole-item "alternative to X" or topic match. The accepted `graphic-walker` (Tableau) was promoted by the scheduled cycle and scored into the shared history. | Met |
| 16 | Conversational assessment is advisory only | `docs/INTAKE-POLICY.md` (pinned by hash like an upstream licence). Every outcome records `advisory_notice`. Nothing becomes canonical except through the cycle. | Met |
| 17 | Durable outcome with evidence | `data/intake/outcomes/<request_id>.json`, covering accepted, rejected, already_known and needs_more_evidence, with reasons, live facts, matched excerpts and scores. `canonical.status` is updated on promotion. | Met |
| 18 | One corpus, schema, scoring history and promotion path | Intake is a registered source (`source_id: intake`) harvested by the same cycle into the same dataset and canonical schema. Scores go to the one `data/scores/history.jsonl`. Promotion is the normal cycle. | Met |

## Findings recorded while building

- Defects found and fixed during the build: evidence matching (GitHub Gist ≠ GitHub);
  a concurrency-group design that could cancel a queued intake; the intake workflow
  failing on an empty ledger directory; intake writing the cycle-owned commit cache; a
  local stale export leaking into the committed validation report.
- NON_BLOCKING: the full KEI-806 scoring lane and KEI-844 graph are not re-run weekly.
  Their evidence (~0.5 GB) stays outside the repository. New relationships are scored by
  the single-project path using the same rubric; existing scores carry `evaluated_at`
  and are superseded by the next full-lane run (`scores-extract`).
- Defect found at closeout: the acceptance map claimed criterion 9 before any scheduled cycle
  had run, because GitHub's cron skipped every window. It was fixed with `catch_up_hours` and proved by run `36838208240`.
- NON_BLOCKING: the repository is public, so the hourly wake-up costs no Actions minutes.
- NON_BLOCKING: a catch-up run can start outside the quiet window, but it is marked `late` in the run's due decision.
- NON_BLOCKING: launch selection caps (KEI-805) mean a newly admitted relationship can
  displace another at the margin. This is deterministic and recorded as
  `canonical_relationships_added` and `canonical_relationships_removed`.
