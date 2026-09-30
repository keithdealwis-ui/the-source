# The Source — scoring (KEI-806)

Three separate outputs for every project in the canonical graph (KEI-844). They are never
blended into one number:

| Output | Applies to | Answers |
|---|---|---|
| **Project Health** 0–100 + band | every retained OSS project | "Is this an open-source project I can sensibly depend on?" |
| **Replacement Fit** 0–100 | only a project with a `REPLACES` edge, once per SaaS product | "How credible is this as an alternative to *this specific* SaaS product?" |
| **Evidence Confidence** 0–100 | beside each of the above | "How confident are we that the score is correct?" |

The rubric is Keith's, approved 29 Sep 2026 (the KEI-806 description). It lives, in full
and machine-readable, in `config/scoring.yaml`. This document explains it. If the two ever
disagree, the YAML is what runs.

**Automatic scoring is not an endorsement.** Every record carries
`endorsement: {keith_tested: false, keith_endorsed: false, editorial_recommendation: false}`
and the statement *"Automatically computed from public repository metadata. Not tested,
reviewed or endorsed by Keith de Alwis, and not an editorial recommendation."* Health bands
describe project health, not endorsement.

## Commands

```
.venv/bin/python -m source_pipeline score-harvest            # network: read-only GitHub GraphQL; resumable
.venv/bin/python -m source_pipeline score-harvest --refresh  # re-observe everything under a new as_of (weekly refresh)
.venv/bin/python -m source_pipeline score-build              # no network: extract signals, score, append history, validate
.venv/bin/python -m source_pipeline score-validate           # no network: acceptance checks + calibration set
.venv/bin/python scripts/run_score_tests.py                  # unit tests, stdlib runner, no network
.venv/bin/python scripts/score_refresh_probe.py              # AC11/AC12 probe: refresh a sample in an isolated copy
```

## Pipeline

```
KEI-844 graph view ─┐
KEI-811 deep store ─┤                     ┌─ signals.jsonl        (per project, every raw signal + provenance)
score-harvest ──────┼─► extract (stage 1) ┤
 (github_meta.json, │                     └─ pair_signals.jsonl   (per SaaS pair: the text it is judged on + facts)
  readmes.json)     │                                 │
config/saas_jobs ───┘                                 ▼
                           config/scoring.yaml ─► score (stage 2) ─► project_health.jsonl
                                                                    replacement_fit.jsonl
                                                                    records.jsonl        (presentation, schema-validated)
                                                                    history/scores.jsonl (append-only)
```

Stage 2 reads only the stored signals, so **a rubric change is a rescore, not a re-harvest**:
edit `config/scoring.yaml`, bump `version`, run `score-build` (or `score_build.rescore()`).
The record schema (`schema/the-source.score.schema.json`) does not change with the rubric.

### Evidence gathered for scoring (`score-harvest`)

The KEI-805/811 stores held stars, forks, commit counts, merged PRs, releases and licence,
but not what several rubric dimensions need. `score-harvest` makes one read-only GraphQL
pass per repository (batches of 4) and records, for each: creation/push dates, archived and
disabled state, stars, forks, security-policy status, licence, topics, homepage, the last 10
releases, open/closed issue and PR totals, the 20 most recent issues with their first 3
comments and authors' association, the 20 most recent PRs, the root, `.github` and
`.github/workflows` listings (with file sizes), and commit windows on the default branch
(90 days, 180 days, 365 days with the latest 100 commit authors, and the prior 365 days).
For projects with a `REPLACES` edge it also fetches the README text (first 60,000 chars).
Every observation is also appended to `data/score/history/observations.jsonl`.

## Project Health

Weighted mean of eight dimensions. Each dimension is a weighted mean of its signals, each
signal mapped to 0–100 through a piecewise-linear curve in the YAML.

| Dimension | Weight | Signals (curve inputs) |
|---|---|---|
| Maintenance & momentum | 25 | days since last commit; commits in 90 d; commits in 365 d; days since last release; commits this year ÷ prior year |
| Community & contributors | 15 | distinct commit authors in the latest 100 commits of the last year; top author's share (bus factor); share of recent PRs from outside contributors; log10 stars; log10 forks |
| Issue/PR responsiveness | 15 | median hours to first maintainer response (comment by OWNER/MEMBER/COLLABORATOR, or close) on recent non-maintainer issues; share answered; share of recent PRs resolved; median PR resolution hours; open ÷ all issues |
| Maturity & continuity | 15 | age; number of releases; latest stable release ≥ 1.0; active in the prior year |
| Documentation & usability | 10 | README size; docs directory/site config; homepage; examples; CONTRIBUTING |
| Operational readiness | 10 | container/deploy files (Dockerfile, compose, Helm, k8s); CI workflows; releases published; package manifest; upgrade-path evidence (CHANGELOG/UPGRADING or ≥ 3 releases) |
| Security & governance | 5 | security policy (host flag or SECURITY file); dependency automation (Dependabot/Renovate); CODEOWNERS/GOVERNANCE/MAINTAINERS/CODE_OF_CONDUCT; organisation-owned |
| Licence | 5 | usability of the licence: permissive 1.0, copyleft 0.85, network copyleft 0.75, other recognised 0.7, unclear 0.2 |

**Stars are a signal, not the score.** Stars and forks sit inside Community at 15% and 10%
of a 15-point dimension, at most 2.25 and 1.5 points of 100. Star *velocity* needs two
observations at least 28 days apart. Until the first weekly refresh it is reported as
insufficient history, never estimated.

Bands (`project_health.bands`): **85–100 Exceptional · 70–84 Healthy · 55–69 Developing ·
40–54 Caution · < 40 High Risk.**

### Missing evidence is unknown, never zero (rubric rule 1)

A signal with no evidence is `null`. Its weight is dropped from its dimension, and a
dimension with no known signal is dropped from the total. The score is renormalised over
what is known, and the gap is charged to Evidence Confidence (completeness). Examples:

- A repository with issues disabled has no responsiveness evidence. It is scored on the
  other seven dimensions, and its confidence says so.
- A crawler failure (HTTP error, timeout) is never read as a dead project.
- A confirmed zero *is* evidence: "no releases" (the host says 0) scores as 0 releases.

Below 50% of rubric weight known, the record carries `evidence_insufficient: true`.

### Hard disqualifiers (rule 2)

Held outside the number, with the reason and the evidence ref. A disqualified project keeps
its Health score and is never recommendation-eligible while the flag stands:
`archived` · `disabled` · `licence_unclear` (no recognised open-source licence, as decided by
KEI-805/811) · `demonstrably_dead` (no commit in 24 months) · `not_found` ·
`security_concern`. There is no automated vulnerability source yet, so `security_concern` is
raised only from `config/score_overrides.yaml` (project, reason, evidence).

**Watch-outs** are surfaced beside the score but do not disqualify: single-maintainer risk,
slow responses (median ≥ 30 days), no releases, pre-1.0, fork, open-core markers in the README.

### Trajectory (rule 6)

Shown beside Health: **Rising / Stable / Declining / Insufficient history**. It comes from
dated host history (commit windows) and is never a hidden weight. The last 180 days are
compared with the prior 180 (for a project at least 1 year old), and the last 365 days
with the prior 365 (at least 2 years old). The geometric mean of those ratios is Rising
at ≥ 1.3 and Declining at ≤ 0.6. Fewer than 12 commits in 24 months, or a project too young
for a complete prior window, gives *Insufficient history*. A young project has an empty
prior year by construction, and that is not growth. Once stars have two observations
≥ 28 days apart, their velocity is reported alongside.

## Replacement Fit

Only for a `REPLACES` edge in the graph (KEI-805 validated or licence-flagged
relationships). One score per (project, SaaS product). Discover-lane projects with no SaaS
edge get none, and no edge is ever invented.

What each SaaS category is bought to do is written down in `config/saas_jobs.yaml`. The
first core group is the **defining group**: specific phrases for what the product *is* (e.g.
Data warehouse: "data warehouse", "olap", "analytical database", "columnar"). Further groups
are the job's verbs and objects. There is also a `features` checklist. The evidence is the
project's README text (fetched for every replace-lane project), description, topics,
upstream list category and graph capabilities. Matching is case-insensitive, with a word
boundary before every term and after short terms (so "ai" does not match "maintain").
Separators are interchangeable, so "web analytics" matches the topic slug `web-analytics`.
Term matching happens at scoring time (stage 2), so a change to the job definitions is a
rescore, not a re-harvest.

| Dimension | Weight | Evidence |
|---|---|---|
| Core use-case coverage | 25 | credited share of the category's core groups: 1.0 for a group found in the project's *identity text* (description, topics, upstream category, first 2,000 chars of README), 0.25 for a group found only deeper in the README, where long READMEs mention everything |
| Feature equivalence | 15 | share of the category's feature checklist evidenced |
| Ease of adoption | 15 | quick-start/install section; demo/screenshots; container files; docs; README depth |
| Deployment & ownership | 10 | self-host evidence or container files; cloud option; data-ownership language; licence usability |
| Migration friction | 10 | import/export/migration language; the incumbent named within 120 chars of import/migrate/switch/alternative; API |
| Integrations & extensibility | 10 | API; webhooks; plugins/extensions/integrations; SSO/OIDC/SAML/LDAP |
| Production/commercial readiness | 10 | multi-user/roles/permissions; SSO; support/enterprise options; stable version; releases |
| Economic advantage | 5 | self-hostable 80, hosted-only 40, minus 20 for open-core markers, times licence usability. There is no price data in the catalogue (a known limitation), so this is a TCO proxy, not a price comparison |

**The core gate.** Replacement Fit is capped at 45, however strong the secondary dimensions,
when core coverage is below 50 or when the **defining group is absent from the project's
identity text** (`core_gate: failed`). The same cap applies when core coverage cannot be
established at all (no README and nothing in the identity text: `core_gate: unproven`).

**The gate is proven to discriminate.** `score-validate` runs a negative control: every
scored pair is re-judged against a randomly chosen *wrong* category (seeded). The gate must
pass at most 15% of those mismatched pairs, and the true pass rate must be at least three
times the mismatched rate. The first version of the job definitions failed this badly
(49% of mismatched pairs passed, e.g. a newsletter tool as a data warehouse, because generic
stems like "analytic" and "docs" matched almost any README). v1.1.0 tightened the defining
groups, and the control now runs at about 4% mismatched against about 97% true. The true
pairs that were still gated were reviewed one by one. Where a real alternative describes
itself in words the definition missed, the phrase was added and the revision logged in
`config/saas_jobs.yaml`. Correct gatings were left gated: Plausible is web analytics and not
a product-analytics replacement for Mixpanel, Grafana is not an uptime monitor like Better
Stack, and GIMP is not Canva. A project that cannot be shown to do
the incumbent's main job never reads as a strong replacement for it. The uncapped figure is
kept in `uncapped_score` for inspection.

Replacement Fit takes no Health input (tested by perturbing Health: the Fit is identical).
AC4 is checked symmetrically about the Healthy band edge. There must be Healthy projects
(≥ 70) with a poor Fit (< 50), strong Fits (≥ 70) from projects below Healthy, and projects
whose Fit differs by 20+ points between incumbents. A genuinely weak project (Health < 55)
with a strong Fit is rare in this corpus, because the replace lane is pre-filtered by the
KEI-805 live quality bar. That direction is proven synthetically in
`tests/test_score.py::TestIndependence` and reported as a count, not assumed.

## Evidence Confidence

| Component | Weight | Project Health | Replacement Fit |
|---|---|---|---|
| Evidence completeness | 30 | share of rubric weight with known evidence | share of Fit weight with known evidence |
| Evidence freshness | 25 | age of the observation at evaluation (100 ≤ 7 d → 0 at 365 d) | age of the README observation |
| Source quality | 20 | mean over known signals: host API 90, host listing 80, graph inference 50 | README + host API 75; description/topics only 55 |
| Relationship confidence | 15 | not applicable (renormalised away) | the graph `REPLACES` edge confidence, +10 if corroborated |
| Cross-source agreement | 10 | this pass vs the KEI-811 REST record: stars ±15%, forks ±20%, archived, licence | number of upstream lists claiming the pair (1 → 40, 2 → 70, 3+ → 100), +20 if the README names the incumbent |

Confidence changes nothing about the score it sits beside. A score ≥ 70 with confidence
below 50 is labelled **"Promising, uncertain"** (`presentation_label`), not presented as
authoritative.

## Thresholds (explicit and tested)

| Decision | Rule (`config/scoring.yaml` → `thresholds`) |
|---|---|
| Health status | the band |
| **Credible alternative** | Fit ≥ 60 **and** core coverage ≥ 50 **and** Fit confidence ≥ 50 **and** no disqualifier |
| **Mapping publishable** | Fit confidence ≥ 50 **and** relationship status `validated` **and** no disqualifier **and** core gate passed. A refused mapping stays out of publication while the project stays in the corpus with its Health (AC7) |
| **Recommendation-eligible** | Health ≥ 55 **and** Health confidence ≥ 50 **and** no disqualifier **and** the graph already recommendation-eligible (licence/lane, KEI-805/811) |

`score-validate` recomputes every one of these decisions from the stored numbers and fails
on any mismatch. `tests/test_score.py` exercises each edge.

## Outputs (`data/score/`)

| File | What |
|---|---|
| `evidence/github_meta.json`, `evidence/readmes.json` | raw observations from `score-harvest`, keyed by canonical repo, with `observed_at` |
| `signals.jsonl`, `pair_signals.jsonl`, `signals_meta.json` | stage-1 signals: value, source, provenance ref, observation time |
| `project_health.jsonl` | Health: score, band, every dimension and signal with its points and weight, trajectory, confidence and components, disqualifiers, watch-outs, `score_version` |
| `replacement_fit.jsonl` | per pair: score, uncapped score, core gate, dimensions, core groups with hits, confidence, credible, publish + refusal reasons, evidence refs |
| `records.jsonl` | the presentation record (schema `the-source.score.schema.json`): Health N/100 + band, trajectory, Fit per SaaS, confidence, "why" reasons with evidence, disqualifiers/watch-outs, last evaluated date, evidence pointers |
| `history/observations.jsonl` | append-only: every score-relevant observation from every harvest |
| `history/scores.jsonl` | append-only: one row per (project, score_version, evidence as_of), never overwritten |
| `reports/validation_report.json`, `reports/calibration.json`, `summary.json`, `MANIFEST.json` | checks, calibration results, counts, hashes |

### Provenance refs

Every known signal, reason and flag cites where it came from:

- `score:evidence/github_meta.json#<canonical>@<as_of>:<field path>`: this ticket's GraphQL observation
- `score:evidence/readmes.json#<canonical>@<as_of>`: README text read for Replacement Fit
- `graph:edges.jsonl#<edge id>`: a KEI-844 graph edge (REPLACES, OWNED_BY), which carries its own upstream provenance
- `graph:views/scoring_inputs.jsonl#<project>:<field>`: the KEI-844 scoring view
- `kei811:live.json#deep/<owner/name>`: the KEI-811 REST observation

`score-validate` resolves every one of them to an existing record.

## Refresh and history

Weekly: `score-harvest --refresh` then `score-build`. The refresh appends to both history
files and never rewrites them. Trajectory and star velocity sharpen as observations
accumulate. `scripts/score_refresh_probe.py` demonstrates the cycle end to end on a sample,
in an isolated copy, and records the result in `evidence/KEI-806-refresh-probe.json`.

## Known limitations

- **Confidence is compressed on the first run.** Every observation is under a day old, so freshness is 100 everywhere and Health confidence sits at 61–97. Low-completeness projects still reach about 61 under the approved component weights. They carry `evidence_insufficient: true` separately. Spread widens as evidence ages between refreshes.

- **Price.** No SaaS price data exists in the catalogue. Economic advantage is a self-host/licence proxy.
- **Vulnerabilities.** No automated vulnerability feed. `security_concern` is override-only.
- **Response sampling.** The first 3 comments of the 20 most recent issues. A maintainer who answers after three community comments is missed, so responsiveness is slightly pessimistic for very busy trackers.
- **Contributor spread.** Measured on the latest 100 commits of the last year, not the full history.
- **Star velocity.** Insufficient history until the first refresh ≥ 28 days after 2026-09-29.
- **Job definitions.** `config/saas_jobs.yaml` is a desk-authored description of what each category is bought to do. It shapes Replacement Fit, is versioned (v1.1.0), and is policed by the negative control. A real alternative that describes itself in words no definition anticipates can still be gated, and it shows as `core_gate: failed` with the uncapped score kept for review.
- **External docs sites are not crawled.** A short README (< 6,000 chars) that defers to a docs site leaves absent terms *unknown*, not zero. Crawling the docs would firm up those Fit dimensions (follow-on).
- **Non-GitHub hosts** (2 GitLab, 2 Codeberg): scored from the KEI-811/805 observations only, with lower completeness.
