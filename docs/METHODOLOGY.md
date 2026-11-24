# How The Source evaluates software alternatives

The Source helps you judge whether a project deserves a place on your shortlist. A recommendation combines evidence about a specific product–project pairing with checks on maintenance, licensing and project identity. This guide explains those checks and their limits.

[Explore alternatives](https://the-source.dev/#explore) · [Ask through MCP](MCP.md) · [Understand the scores](SCORING.md)

## Discovery, assessment and recommendation

The [discovery catalogue](CORPUS.md) is the broad starting point: public projects with identifying facts, evidence dates and explicit warnings. Discovery entries may have [quick scores](QUICK-SCORING.md). These are lightweight indicators, not full assessments or recommendations.

The canonical layer contains projects and product–project relationships that have gone through deeper evaluation. A project can be assessed without any of its relationships qualifying as recommended. A recommendation applies to a particular replacement task; it is not a universal endorsement of the project.

The MCP server labels results `discovery`, `scored` or `recommended`. Preserve those labels and the distinction between quick scores and full assessments when displaying results.

## Where candidate alternatives come from

Candidates can be identified by a licence-cleared upstream list, a project's own description of what it replaces, or a documented match between the job a product does and the project's capabilities.

| Evidence | Meaning |
|---|---|
| Listed | An upstream source names the product–project pairing. |
| Self-described | The project's own repository describes it as an alternative. |
| Job-matched | Recorded capability evidence suggests the project does the category's job. This is not an independent listing or corroboration. |

Upstream claims do not establish current maintenance or licensing. Those facts are checked against the project's own repository host. See [provenance](../PROVENANCE.md) and [job matching](JOB-MATCH.md).

## The quality bar

The executable policy is in `config/policy.yaml`. Eligibility checks include:

- The repository exists and is not archived, disabled, empty or repurposed.
- Its licensing is recognised, or a reviewed exception permits the specific scope with a disclosure.
- It has meaningful activity within the policy's maintained window, currently 365 days.
- The claimed replacement relationship has sufficient evidence and survives the evaluation rules.

Meaningful activity can be a human code change or merged pull request, a qualifying release, or a version tag supported by relevant changes. Documentation-only changes, housekeeping, automation and floating tags do not by themselves establish maintenance. The observation bounds inspect recent commits and merged pull requests, so evidence outside those bounds may remain unknown.

`active` means meaningful activity within 90 days; `maintained` means within 365 days. The dates and basis travel with the assessment. A historical observation is not a guarantee of current status.

## Licensing and editions

Open-source, open-core and source-available are different licensing situations. Retained discovery records may also have missing, restricted or unclear licences; retaining a record does not establish reuse rights.

Reviewed exceptions and edition checks can qualify a specific open-core or source-available scope. Recommendations must preserve the associated licence disclosure. A capability available only in an excluded edition must not be presented as part of the eligible core. [Edition checks](LICENCE-EDITION-CHECK.md) describe the evidence and limits; [source licensing](LICENCE-REVIEW.md) describes reuse of upstream information.

## Scores answer separate questions

**Project Health** describes maintenance and other project signals. **Replacement Fit** assesses a particular product–project pairing. **Evidence Confidence** describes the evidence behind an assessment. They should not be combined into a single verdict.

Fit considers the core job, feature evidence, adoption, deployment, migration signals, integrations and other documented factors. Missing evidence remains unknown. Caps apply where the core job is unproven, features are explicitly out of scope or supporting evidence is limited. Scores and caps retain their explanations.

Popularity alone does not qualify a recommendation. Stars are used in some health and quick-score indicators and as a read-surface tie-breaker; repository momentum measures attention separately. Neither a large star count nor a surge proves suitability.

## Recommendation rules

A recommended relationship must be exportable, fully scored, publishable and a credible alternative. The enabled recommendation gate also applies evidence-specific minimum Replacement Fit scores:

| Evidence behind the pairing | Minimum fit |
|---|---:|
| Corroborated by independent listing sources | 60 |
| A single listing source or self-described replacement | 65 |
| Job-matched evidence only | 75 |

These are policy thresholds, not probabilities of success. The live rules are `config/policy.yaml` → `recommendation_gate` and `config/scoring.yaml`. The [canonical manifest](../data/canonical/MANIFEST.json) reports how many evaluated relationships pass and why others fail.

`validated` means a relationship passed its evidence and quality checks. It does not mean someone tested the software, nor does it automatically make the relationship a recommendation. [Rejection records](REJECTIONS.md) explain excluded cases.

## Ranking

Recommendations for a product are ordered in three steps (rubric 1.4.0):

1. Evidence tier: listed and self-described relationships before job-matched ones, whatever their scores.
2. Licence tier: OSI-licensed projects, then disclosed open-core exceptions, then disclosed source-available ones.
3. Replacement Fit, in bands. Inside a tier, a band opens at the highest remaining Fit and holds every recommendation within 5 points of it. Within a band, adoption and maturity decide: more GitHub stars first, then higher Project Health, then higher Fit. The next band opens at the highest Fit left.

A Fit difference of a few points is within what the rubric can resolve, so a widely adopted, healthy project is not placed below a barely used one on that difference alone. A difference larger than the band still decides: a project more than 5 points below the band's leader never moves above it. For example, for Mailchimp, Listmonk (Fit 78.5, about 24k stars) is placed ahead of Keila (Fit 82.2, about 2k stars); for Notion, Docmost (83.7, about 22k stars) leads its band, but Folio (92.0) stays first because the gap is 8.3 points.

The band changes order only. Scores, thresholds and the recommendation gate are unchanged.

## Promotion from discovery

Each daily cycle, a discovery-tier pairing with quick Fit of at least 75 and at least 5,000 stars that the canonical layer does not hold is sent towards full scoring (`config/schedule.yaml` → `scoring.promotion`). If the project's own description or topics state the pairing, it joins the intake ledger and is validated by the build like any other intake. If the pairing is already in the dataset, it is scored first. If it is held back, the cycle record reports the build's reason, for example an open review of contradictory licence evidence. Promotion never skips validation, scoring or the recommendation gate.

## What gets published

The canonical build produces the [read API](READ-API.md), project records and relationships. The current policy combines ranked selection with enabled catalogue serving and preservation of previously served valid relationships. Budgets and per-product limits are declared in `config/policy.yaml`; the original launch target is not the full current coverage.

The catalogue distinguishes `ok` (at least three qualifying recommendations), `partial` (one or two) and `none_yet`. Clients must show the available evidence rather than fill a short list with weaker candidates. [Product coverage](CATALOGUE.md) explains these states.

## Freshness and validation

The scheduled daily cycle refreshes observations and rebuilds the recommendation layer. The larger discovery catalogue has a separate refresh schedule. Fields can have different observation dates; the dataset's publication time is not the age of every fact.

Builds check schemas, hashes, counts, provenance, eligibility and reproducibility. The MCP service verifies published layers before serving them and reports stale or unavailable layers. When an update cannot be verified, the last valid snapshot can remain available with its status disclosed. See [operations](OPERATIONS.md) and [MCP verification](MCP-REFERENCE.md#guarantees).

## What the evidence cannot promise

Assessments are machine-derived from public evidence, not hands-on product reviews, security audits or guarantees of feature parity. Migration effort, operating costs, implementation quality and suitability for a particular team may need additional investigation.

Keep unknown values, `not_evaluated` requirements, caps, licensing disclosures and dates visible. Inspect the project's own documentation and test the workflow before depending on it.

## Improve an assessment

[Report a correction](../CONTRIBUTING.md) with the affected product/project, the claim and a supporting source. Maintainers update the relevant evidence or rule and regenerate the data; they do not fix a recurring problem by editing a generated result alone.
