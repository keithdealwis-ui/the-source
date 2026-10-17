# Quick scoring (KEI-921, The Source V2.1 WP4)

The discovery corpus (`data/corpus/`, about 9,400 projects) was searchable but not comparable. Only the canonical layer had Project Health and Replacement Fit. Quick scoring gives every corpus project a cheap programmatic **quick Health**. It infers the SaaS products a project is **detectably paired** with, and it gives each pairing a lightweight **quick Fit**. The output is in `data/quick/`.

**Quick scores are never recommendations.** Every quick row is labelled `quick` and carries `recommendation_eligible: false` and `publish: false`. Only the full evaluation can recommend or publish (`data/scores/`, `docs/METHODOLOGY.md`, the canonical layer and `api/v1/`). `quick-validate` enforces this, and the read surface refuses a layer that breaks it.

**No calibration.** Quick confidence measures how complete the data is. It is not a calibrated probability. Calibration is V3.

```
python -m source_pipeline quick-score [--full]   # incremental by default; no network
python -m source_pipeline quick-validate         # exit 1 on any failed check
```

## Quick Health (`the-source.quick-health/1`)

Quick Health uses only facts the corpus already publishes, from `live.jsonl` and `projects.jsonl`. Weights and buckets are in `config/quick_score.yaml`.

| Component | Weight | From |
|---|---:|---|
| activity | 40 | days since the last push. Bucketed: ≤30 d full marks, then 0.8 / 0.6 / 0.35 / 0.15, and 0 past 2 years |
| adoption | 25 | log-scaled stars, full marks at 10,000 |
| community | 10 | log-scaled forks, full marks at 1,000 |
| releases | 15 | days since the latest release. ≤180 d full marks, then 0.65 / 0.3, then 0. No release scores 0 |
| maintenance | 10 | the Discover lane's evaluation: `active` 1.0, `maintained` 0.7 |

How the score is adjusted:

- **Unknown facts.** A fact that is unknown is not scored as zero. The score is rescaled over the known components, and `confidence` drops by the missing share of the weight.
- **Archived.** An archived repository is capped at 10.
- **Stale facts.** Facts older than 21 days lower confidence by a fifth.

Bands: Strong ≥75, Moderate ≥55, Weak ≥35, Poor. Watch-outs: `archived`, `no_release`, `no_push_in_2_years`, `facts_stale`.

Each row keeps the facts it was scored from and its `evaluated_at`. `quick-validate` recomputes every row from those, so a quick score can be reproduced from the published row alone.

Projects the host no longer has (`found: false`) are not scored.

## Pairings and quick Fit (`the-source.quick-fit/1`)

A project is **detectably paired** with a catalogued SaaS product (`config/saas_catalogue.yaml`) when:

- **description**: its description says "alternative(s)/replacement(s) to/for X" or "X alternative", and X equals a catalogue name or alias. This is the same deterministic rule as `intake.find_evidence`. A bare mention of a product is not a pairing.
- **topic**: one of its topics is `<x>-alternative`.
- **replace_lane**: an upstream curated list pairs it with X (`data/dataset/relationships.jsonl`).

Each pairing gets a quick Fit:

| Component | Weight | From |
|---|---:|---|
| evidence | 40 | how it was detected: replace_lane 1.0, topic 0.85, description 0.8 |
| category | 20 | the share of the SaaS category's words that appear in the project's name, description or topics |
| health | 30 | quick Health / 100 |
| licence | 10 | eligible 1.0, conditional (open core, core only) 0.5, not eligible 0 |

Quick Fit's confidence is the weaker of two things: how strong the evidence is, and how complete quick Health is.

## Incremental runs and cost

Each project and each pairing carries a fingerprint over exactly what can move its score:

- the facts, without the observation timestamp;
- the recency buckets at run time, so crossing a boundary rescores, but only that row;
- the catalogue;
- the config and the method versions.

A repeat run rescores only rows whose fingerprint changed. Unchanged rows stay byte-identical. A config or method change rescores everything, by design.

The cost limits are in `config/quick_score.yaml` `caps`:

- **API calls: 0, always.** Quick scoring reads facts that `corpus-refresh` already fetched, and that refresh reports its own GraphQL cost.
- **Rescore cap per run** (`max_rescore_per_run`) and **runtime cap** (`max_runtime_s`). Work past either cap is **deferred**: the row keeps its previous score, is flagged `deferred: true`, and is counted in the run record.
- **Row failures** are recorded with their error, and the previous score is kept.

Every run appends `{at, mode, runtime_s, api_calls, health_rescored, health_unchanged, pairings_rescored, pairings_unchanged, pairings_removed, deferred, failure_count, failures, caps}` to `data/quick/runs.jsonl` (the last 60 runs) and writes it to `MANIFEST.json` `last_run`. The scheduled corpus refresh (`.github/workflows/corpus-refresh.yml`) runs `quick-score` right after each refresh, commits `data/quick`, and prints the run record in the job summary.

## Serving (MCP)

`data/quick/` is a published read layer. It is verified against its MANIFEST, and it is in the deploy sparse checkout. Quick scores are served for **discovery-tier** projects only; canonical projects keep their full scores.

- `source_get_project` and every discovery row in `source_search` carry `quick_scores` (`label: quick`, `recommendation: false`, quick Health, quick Fit per pairing). `source_get_project` adds components and evidence.
- `source_search` takes `min_quick_health`. It filters discovery rows by quick Health and canonical rows by their full Project Health.
- `source_compare_projects` puts quick scores side by side, and its uncertainty note says they are quick only.
- `source_get_alternatives` with `include_discovery: true` adds `quick_paired_candidates`. This is a separate, labelled list that never mixes into `recommendations`.

## Checks (`quick-validate`)

| Check | |
|---|---|
| schema, file sha256s, quick_version | the layer matches its MANIFEST |
| corpus_version | it was scored against the corpus that is published |
| every row labelled quick and never recommendation-eligible | the S2.4 rule |
| health recomputes from its facts | reproducibility |
| eligible health coverage ≥ 95% | eligible = licence recognised, or eligibility eligible/conditional |
| every detectable pairing has quick Fit | re-detected from scratch and compared |
| zero quick-only recommendations | every recommended canonical relationship has a full Replacement Fit row |
| read API v1 carries no quick scores | quick scores never reach the published pages |
| last run cost visible | api_calls 0, runtime and failures recorded |
