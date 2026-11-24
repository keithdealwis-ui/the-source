# How candidate alternatives are matched

Some projects describe a useful capability without naming the paid software they could replace. Job matching identifies candidates from those descriptions so the full evaluation can assess a specific product–project pairing.

A category match is a candidate, not a recommendation or independent corroboration. Published relationships retain their evidence class so readers can distinguish listed, self-described and job-matched pairings.

For maintainers, `python -m source_pipeline job-match` builds the candidate ledger from existing facts, and `job-match-observe` verifies projects missing from the live snapshot.

## What a job match is

A project is a job-matched candidate for a catalogue product when all of these hold:

- **The job.** Its own description or topics contain the category's defining phrase and one of the category's second-group terms (`config/saas_jobs.yaml`). The defining phrase is the same group the full evaluation's core gate requires.
- **Health.** It is on github.com, maintained or active, licence-eligible, and has a quick Health of at least 55 (`docs/QUICK-SCORING.md`).
- **Not an integration.** It is not an integration with, or companion to, another product. That means not an MCP server, SDK, bot, API client or plugin (the eligibility rules), not a list, template or prompt pack, and not a client, front end, downloader or add-on for a named catalogue product.
- **Not already paired.** No other source already pairs it with the product. A job match is never a second source on a listed pair, so it never corroborates one.

For each product, at most 6 candidates are kept: strongest quick Health first, then `oss_id`. The ledger records the matched phrase and a short excerpt of the project's own text.

## How it is labelled

The ledger is harvested as its own source, `job_match`. Every relationship that rests only on it carries `evidence_class: job_matched`. That label appears in `data/dataset/relationships.jsonl`, in the canonical relationships and in every recommendation's `evidence.relationship` block.

The other classes are:

- `listed`: an upstream list names the pair.
- `self_described`: the project's own repository says it is an alternative, through the intake ledger.

A job match is never presented as an upstream-listed alternative. A job match does not count as a source naming the product, so it does not change catalogue supply or ranking.

## What it does not do

A job match recommends nothing. A job-matched relationship counts toward a product's `ok` status only after it passes the unchanged full evaluation: the core gate, plus a publishable and credible Replacement Fit (`docs/SCORING.md`). The ok rule (at least 3 such recommendations) is unchanged. The launch policy cap (`config/policy.yaml`, `edges_max`) applies to job-matched relationships exactly as to any other.

## Licence

No third-party material is involved. The ledger is The Source's own derivation from facts already in its corpus. It reproduces no upstream text beyond the short excerpt kept as evidence. The excerpt is the project's own repository description, as the corpus already records it.
