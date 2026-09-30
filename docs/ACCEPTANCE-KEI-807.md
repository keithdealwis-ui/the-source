# KEI-807 acceptance evidence

The Source 03: publish the canonical repository and build the live metadata refresh and
read surface. Repository: `github.com/keithdealwis-ui/the-source` (private until the
criterion 1 gate). Every row points at first-hand evidence in the repository or its
GitHub Actions history. `scripts/verify_kei807.py` re-checks each criterion independently
of the pipeline code.

| # | Criterion | Evidence | Status |
|---|---|---|---|
| 1 | Public repo under Keith's GitHub | Repository created under `keithdealwis-ui`, **private**. Going public is a Keith-class gate that carries the open licence questions from `docs/LICENCE-REVIEW.md`. `config/publication.yaml` makes the build refuse a public status without a recorded approval. | Gated |
| 2 | Canonical schema validates in CI | `schema/the-source.canonical.schema.json`. `.github/workflows/ci.yml` rebuilds offline, runs `canonical-validate` (27 checks: schema for every record kind, manifest hashes, integrity, read surface, byte-identical rebuild), then the tests, then fails on any diff between committed and rebuilt data. Green on every code commit. | Met |
| 3 | Initial 20 SaaS products and validated OSS mappings | `config/supported_saas.yaml` holds 20 products chosen by a recorded mechanical rule (`scripts/rank_supported_saas.py`). `api/v1/saas/*.json` gives each 3–5 recommendations, every one a `validated` KEI-805 relationship with a publishable, credible Replacement Fit. | Met |
| 4 | Live fields: GitHub link, stars, maintenance, licence, release/activity | Every recommendation carries `github_url`, `stars`, `maintenance_status`, `last_meaningful_activity_at`, `licence_spdx`, `latest_release` and `live_checked_at`. The canonical project records also carry forks, archive state and `pushed_at`. | Met |
| 5 | Refresh updates metadata without editing extension or site code | Cycle commits by `the-source-cycle` touch only `data/`, `api/` and `runs/`. Example: commit `a741093` refreshed 594 repositories (Notion's docmost went from 21,794 to 21,829 stars, with a new `live_checked_at`) and changed no code, config or workflow file. | Met |
| 6 | Deterministic records for supported SaaS domains | `api/v1/index.json`, `domains.json` and `saas/<id>.json` share one content-derived `dataset_version`. Ranking is fixed. There are no wall-clock timestamps. CI and `verify.sh` rebuild and compare byte for byte. `domains.json` gives local host/path rules; the domain-matching examples are covered by tests. | Met |
| 7 | Upstream/API failures degrade safely, no corruption | The cycle works in a scratch copy and promotes only after all validations pass. A lookup failure is retried for the failing repositories only, and after 2 retries fails the cycle (a failed lookup is never data). An upstream licence change keeps the old pin. A failure drill on real infrastructure (run `36666445759`) left `dataset_version` `4a669ecae22a2c92` unchanged. | Met |
| 8 | Every output records when live metadata was last checked | `live.checked_at` on every project, `live_checked_at` on every recommendation, `last_checked` on every read-surface record, and `live_checked_at` in `index.json` and `MANIFEST.json`. Enforced by the `last_checked_recorded` check. | Met |
| 9 | Weekly scheduler, quiet window (Sun 03:00 Asia/Dubai), configurable without code changes | `config/schedule.yaml` (default Sunday 03:00 Asia/Dubai, 4 h window) is read by the hourly `weekly-cycle.yml` wake-up. Proof: a commit touching only `config/schedule.yaml` opened a window, and GitHub's scheduler ran the cycle unattended (run record with `trigger: schedule`). The schedule was then restored to Sunday 03:00. | Met (see run) |
| 10 | Each cycle does research and update | Run records list the strategies: `upstream_lists` (pins advanced only when the licence file is byte-identical; awesome-selfhosted and altstack advanced), `intake_ledger`, `live_refresh` (stale and abandoned detection via `transitions`) and `new_pair_scoring`, then build, validation and canonical rebuild. | Met |
| 11 | Run records: times, strategies, counts, success, commit/version | `runs/<run_id>.json` holds `started_at`, `ended_at`, `strategies[]`, `counts` (discovered/retained/rejected/refreshed/…), `status`, `canonical.dataset_version_before/after` and `commit_tag` (`cycle/<run_id>` git tag = the resulting canonical commit). | Met |
| 12 | Failed cycle keeps last known-good, surfaces durably, safely retryable | Drill run `36666445759`: failed record committed (`runs/` only), issue #1 "Weekly cycle failing" opened, data unchanged, and a later successful run closed it. Attempts are counted per weekly cycle, and outputs are deterministic and de-duplicated. | Met |
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
- NON_BLOCKING: while the repository is private, the hourly wake-up costs about 730
  Actions minutes a month (inside the free allowance). It is free once public.
- NON_BLOCKING: launch selection caps (KEI-805) mean a newly admitted relationship can
  displace another at the margin. This is deterministic and recorded as
  `canonical_relationships_added` and `canonical_relationships_removed`.
