# Methodology

How The Source decides what it recommends. The pipeline was built in stages: the data
foundation (KEI-805), discovery (KEI-811), the knowledge graph (KEI-844), scoring (KEI-806),
and the canonical repository with its cycle (weekly under KEI-807, daily since KEI-848) and intake. This document
covers the rules every recommendation must pass. For the canonical dataset and read
surface see `docs/READ-API.md`; for the daily cycle see `docs/OPERATIONS.md`; for manual
intake see `docs/INTAKE.md`.

Nothing an upstream list says about a project's licence, archive state or activity is
trusted. Each repository is asked directly through its host's API, and stars never decide
anything.

## What the pipeline does


| Stage | Module | Reads | Writes |
|---|---|---|---|
| Harvest | `harvest.py`, `parsers.py` | `config/sources.yaml`, upstream repos at pinned commits | `data/staging/claims.jsonl` |
| Normalise and reconcile | `normalise.py` | claims, `config/saas_catalogue.yaml`, `config/repo_overrides.yaml` | in memory |
| Verify live | `enrich.py` | GitHub, GitLab and Codeberg APIs | `data/live/snapshot.json` |
| Classify and select | `build.py`, `activity.py`, `licence_detect.py` | snapshot, `config/policy.yaml`, `config/licence_exceptions.yaml` | `data/dataset/` |
| Validate | `validate.py` | dataset, schema | `data/dataset/reports/validation_report.json` |
| Export guard | `export.py` | dataset | a local directory; publishes nothing |

Harvest fails closed: each upstream is pinned to a commit **and** to the SHA-256 of the
licence file its reuse decision was made about. If the licence file changes, that
source is not parsed until the decision is reviewed again.

## The quality bar

Declared in `config/policy.yaml`. A project is recommended only if all of these hold,
each established by asking the repository host directly:

- it exists, and is not archived, disabled or empty;
- it carries a recognised open-source licence (OSI-approved SPDX identifier), or has a
  licence exception approved by a named human;
- it shows **meaningful activity** within 12 months, which is one of:
  - a commit or merged pull request, made by a person, that touches at least one
    path that is neither documentation nor housekeeping;
  - a published release or a tag whose name is a version and which ships more than
    documentation, judged from what changed since the previous version;
- the repository is still the project it was listed as. One that has since been
  renamed into something its listings never described is excluded.

What does not count:

| Not counted | Examples |
|---|---|
| Documentation | README, changelog, docs directories, images, translations |
| Housekeeping | CI definitions, dependency manifests, lockfiles, editor and lint configuration |
| Automation | commits and pull requests by dependency, CI, release and translation bots |
| Floating tags | `nightly`, `latest`, `stable` |
| Closed issues | recorded as a supporting signal only |

Live verification inspects the 15 newest commits and 10 most recently updated merged
pull requests. A project whose newest activity is all automation and whose code
changes lie deeper than that is judged on its releases alone.

| Class | Meaning |
|---|---|
| `active` | meaningful activity within 90 days |
| `maintained` | meaningful activity within 12 months |
| `stale` | none within 12 months; not recommended |

**Stars decide nothing.** They are stored as a live field and are not an input to
eligibility, to edge validation, or to any ordering in the launch selection.

## The three lanes

Every project and every relationship is in exactly one lane.

| Lane | Meaning | Recommendation-eligible | In launch set or export |
|---|---|---|---|
| `recommendable` | passed every rule | yes | may be |
| `licence_exception` | the ONLY shortcoming is the licence: open-core, source-available, restricted, missing, conflicting or unrecognised | no | never |
| `rejected` | archived, stale, missing, repurposed, or implausible | no | never |

Keith directed (Linear KEI-805, comment 258b38dd) that licence-issue projects are kept
as flagged records rather than discarded. Each carries `derived.licence.issue`: the
category, the exact reason, what was detected, what upstream lists claimed, the
evidence and the review status. Keeping a record establishes no right to reuse it, and
every one is marked `reuse_rights_established: false`.

One project is moved from this lane into recommendations only by a named entry in
`config/licence_exceptions.yaml` with a human approver. None has been made.

The lane is counted apart from the recommendable corpus and from the launch set, in
`summary.json` under `licence_exception_lane` and `lanes`.

## What "validated" means, and what it does not

A relationship is `validated` when all of these hold:

- it has provenance from at least one licence-cleared source;
- it names a SaaS product in the reviewed catalogue;
- it points at a project that passed live verification and the quality bar;
- it was left standing by a desk review of plausibility. Every relationship that passed
  the first three was read, and those where the project is plainly a different kind
  of software were rejected with a reason (`config/edge_review.yaml`).

It does **not** mean anyone has tested how well the project replaces the product.
Fitness is scored separately (Replacement Fit, KEI-806); see `docs/SCORING.md`. Relationships stated by two or more
independent sources are marked `corroborated`; the rest rest on a single upstream's
word.

## Launch selection

1. Rank SaaS products by the number of independent sources naming them, then by number
   of validated alternatives, then by name.
2. Take the first 100. **100 is a floor** (strategy decision `dr-KEI-805-507e4f70`): the
   same ranking then continues, one product at a time, only until the launch set holds
   250 distinct OSS projects, and never past 400 relationships.
3. For each SaaS product, order alternatives by number of independent sources, then by
   most recent meaningful activity, then by id. A product with several repositories is
   listed once.
4. Cap alternatives per SaaS at the largest number that keeps the total within 400.

Nothing in the ranking is skipped or promoted to reach a number; a validation check
confirms the launch set is exactly the first N of the ranking. The SaaS products added
beyond the first 100 are less well attested, and `summary.json` reports how many of the
launch products are named by a single source.

## Layout

```
config/     sources + licence decisions, policy, SaaS catalogue, overrides, exceptions
schema/     the-source.schema.json (JSON Schema 2020-12)
source_pipeline/   the pipeline
tests/      test suite
raw/        upstream checkouts at pinned commits (third-party material; do not publish)
data/staging/   raw claims
data/live/      dated snapshot of live observations
data/dataset/   the canonical dataset, MANIFEST.json, reports/
docs/       SCHEMA.md, LICENCE-REVIEW.md, ACCEPTANCE.md
NOTICE.md   third-party attributions
```

## The Discover lane (KEI-811)

Beyond the replacement mappings above (the **Replace** lane), a second pipeline builds a
broad **Discover** corpus: useful open-source projects found from Keith's seed corpus by
bounded, recorded strategies, judged by this same quality bar, with no SaaS mapping
required. It reads `data/dataset/` and never writes it. See `docs/DISCOVER.md`;
criterion-by-criterion evidence in `docs/DISCOVER-ACCEPTANCE.md`.

```bash
.venv/bin/python -m source_pipeline discover-harvest   # network
.venv/bin/python -m source_pipeline discover-verify    # network, hours; resumable with --only-failed
.venv/bin/python -m source_pipeline discover-build     # offline, deterministic
```

## Scoring (KEI-806)

Three separate outputs per project, never blended: **Project Health** (every retained
project), **Replacement Fit** (only where a `REPLACES` edge exists, per SaaS product) and
**Evidence Confidence** beside each. Keith's approved rubric lives in `config/scoring.yaml`
and the category job definitions in `config/saas_jobs.yaml`. Automatic scoring is never
presented as Keith-tested or endorsed. Method in `docs/SCORING.md`; criterion-by-criterion
evidence in `docs/SCORING-ACCEPTANCE.md`.

```bash
.venv/bin/python -m source_pipeline score-harvest            # network, read-only GraphQL; resumable
.venv/bin/python -m source_pipeline score-harvest --refresh  # weekly: re-observe under a new as_of (history appends)
.venv/bin/python -m source_pipeline score-build              # offline: signals -> scores -> history -> validate
```

## Changing things

| To | Edit | Then |
|---|---|---|
| add an upstream list | `config/sources.yaml` (pin commit, hash the licence file, record the decision), add a parser, add the id to the schema enum | `run` |
| move an upstream to a newer commit | `pinned_commit`; re-hash and re-read the licence file | `run` |
| admit a SaaS product | `config/saas_catalogue.yaml` | `run --offline` |
| change the quality bar | `config/policy.yaml` | `run` (a live run is required) |
| reject an implausible relationship | `config/edge_review.yaml` | `run --offline` |
| admit a project despite its licence | `config/licence_exceptions.yaml`, with a human approver and a reference | `run --offline` |
| refresh live facts | nothing | `run` |
| change the scoring rubric | `config/scoring.yaml` (bump `version`) | `score-build` (rescores stored signals; no re-harvest) |
| refresh scoring evidence | nothing | `score-harvest --refresh`, then `score-build` |
