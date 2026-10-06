# The Source

The Source maps commercial SaaS products to open-source projects that can credibly
replace them. Every recommendation passes three tests: it is verified against the
project's own repository host, maintained within the last 12 months, and under a
recognised open-source licence. Each one is scored for fit and health, and each carries
its provenance and the time it was last checked.

This repository is the single source of truth. The Source Chrome extension, the
comparison pages on de-alwis.com and every future Source workflow read from it.
Nothing downstream keeps its own copy.

> **Licence:** code MIT (`LICENSE`); data CC BY 4.0, with the parts adapted from a share-alike source under CC BY-SA 3.0 (`DATA-LICENCE.md`).

## What is here

| Path | What it is |
|---|---|
| `api/v1/` | **The read surface.** Static JSON per supported SaaS product, for clients. See [`docs/READ-API.md`](docs/READ-API.md). |
| `data/canonical/` | **The canonical dataset.** SaaS products, OSS projects, relationships and scores, plus `MANIFEST.json` and `NOTICE.md`. |
| `data/dataset/` | The full evaluated corpus behind it, every lane included, with validation reports. |
| `data/staging/`, `data/live/` | Harvested claims and the dated live snapshot the dataset is rebuilt from. |
| `data/scores/` | Project Health and Replacement Fit summaries, plus append-only score history. |
| `data/history/` | One dated, hashed snapshot per day of every repository's light metadata, for 1/7/30/90-day deltas (`history-delta`). |
| `data/momentum/` | Momentum and acceleration per repository over 1/7/30/90 days, derived from `data/history/`, with honest per-window coverage (KEI-849). |
| `data/radar/` | Community Radar: repositories being discussed on Hacker News and Forem, ranked and reconciled against the corpus (KEI-850). Facts and addresses only. |
| `data/discovery/` | Candidate repositories found by the daily discovery lanes, reconciled against the corpus, awaiting relationship evidence. |
| `data/intake/` | Manual intake: outcome records and the ledger of admitted repositories. |
| `data/state/` | Operational state, such as upstream pins advanced by the daily research. |
| `runs/` | One record per cycle (daily since KEI-848): what ran, what was found, what changed. |
| `config/` | Sources and licence decisions, quality policy, SaaS catalogue, scoring rubric, supported products, schedule, discovery lanes, corroboration. |
| `schema/` | JSON Schema for every record, validated in CI. |
| `extension/` | **The Chrome extension** (KEI-808): a thin client of `api/v1/`. See [`extension/README.md`](extension/README.md). |
| `source_pipeline/` | The pipeline. Python 3.12, standard library plus PyYAML and jsonschema. |
| `docs/` | Methodology, scoring, read API, intake, operations, licence review. |

## Using the data

Clients read `api/v1/`, never the GitHub API:

```text
api/v1/index.json            supported products, dataset version, when live data was last checked
api/v1/domains.json          hostname rules, so a client can detect a product locally
api/v1/saas/<saas_id>.json   3-5 ranked recommendations for one product
```

Each recommendation carries the GitHub link, stars, maintenance status, licence, latest
release, Replacement Fit, Project Health, self-hosting evidence, a short machine-derived
strengths/gaps note, its sources, and `live_checked_at`. The files are regenerated only
by the daily cycle, so they can be cached or served from a CDN. Full contract:
[`docs/READ-API.md`](docs/READ-API.md).

## How it stays current

A scheduled GitHub Actions workflow runs a **daily intelligence cycle** (KEI-848; weekly under
KEI-807), every day at 03:00 Asia/Dubai by default. To change the timing, edit
`config/schedule.yaml`. Each cycle does the following:

1. Moves each licence-cleared upstream list to its newest commit, but only if its licence
   file is byte-identical to the reviewed one, and harvests the lists plus the
   manual-intake ledger.
2. Refreshes cheap metadata for every known repository from its own host (stars, forks,
   pushes, archive state, licence, releases, identity), in batches.
3. Runs the daily discovery lanes and reconciles what they find against the corpus, so a
   renamed or re-found repository never becomes a second entity.
4. Checks a rotating sample against independent services (ecosyste.ms, deps.dev). They can
   flag a disagreement; they never change a value.
5. Re-reads the full evidence only for new, materially changed, or week-old projects, and
   re-scores the ones that changed. Newly stale, archived or missing projects drop out.
6. Rebuilds and validates the dataset, the canonical dataset and the read surface, and
   keeps a dated snapshot for 1/7/30/90-day deltas, from which it measures each
   repository's momentum and acceleration wherever the history is long enough.
7. Ranks the repositories being discussed on Hacker News and Forem (Community Radar),
   reconciles each with what The Source already holds, and adds new ones to the discovery
   pool. This step can never block the steps above.
8. Promotes the result only if every check passes, and commits it with a run record,
   tagged `cycle/<run_id>`.

A failed cycle changes nothing and opens a GitHub issue. It is retried automatically at
the next hourly wake-up inside the window or its catch-up. Details: [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

## Suggesting a repository

Submit a GitHub URL through the authenticated intake workflow, either with
`scripts/source_intake.py submit <url>` or through the equivalent REST calls. The
repository is judged by exactly the same pipeline as scheduled discovery. A preliminary
assessment by a person or a chatbot is advisory only. Every submission gets one recorded
outcome: accepted, rejected, already_known, needs_more_evidence or failed.
Details: [`docs/INTAKE.md`](docs/INTAKE.md).

## Run it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python -m source_pipeline build               # rebuild the dataset from committed inputs; no network
.venv/bin/python -m source_pipeline canonical-build     # data/canonical + api/v1; validates itself
.venv/bin/python -m source_pipeline canonical-validate  # what CI runs
.venv/bin/python -m pytest tests/test_canonical.py -q

GITHUB_TOKEN=... .venv/bin/python -m source_pipeline cycle --trigger local   # a full cycle, with network
```

## Documentation

- [`docs/METHODOLOGY.md`](docs/METHODOLOGY.md): the quality bar, lanes, validation and launch selection
- [`docs/SCORING.md`](docs/SCORING.md): Project Health, Replacement Fit and Evidence Confidence
- [`docs/MOMENTUM.md`](docs/MOMENTUM.md): repository momentum and acceleration (versioned methodology, coverage)
- [`docs/RADAR.md`](docs/RADAR.md): Community Radar (Hacker News and Forem; sources, terms, ranking, reconciliation)
- [`docs/READ-API.md`](docs/READ-API.md): the consumer contract
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md): the daily cycle, failures, retries, run records and history
- [`docs/BUILD-VS-INGEST.md`](docs/BUILD-VS-INGEST.md): which upstream services are used, for what, under which terms, and why the rest are not
- [`docs/INTAKE.md`](docs/INTAKE.md) and [`docs/INTAKE-POLICY.md`](docs/INTAKE-POLICY.md): manual intake
- [`PROVENANCE.md`](PROVENANCE.md): where every fact comes from, and how to trace it
- [`docs/LICENCE-REVIEW.md`](docs/LICENCE-REVIEW.md) and [`NOTICE.md`](NOTICE.md): upstream licences and attribution
- [`CONTRIBUTING.md`](CONTRIBUTING.md): how to propose changes
- [`docs/SCHEMA.md`](docs/SCHEMA.md), [`docs/DISCOVER.md`](docs/DISCOVER.md) and [`docs/GRAPH.md`](docs/GRAPH.md): the research lanes

Scores are computed automatically from public evidence. They are not an endorsement,
and no project here has been tested by hand.
