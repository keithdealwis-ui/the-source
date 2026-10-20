# Job-matched candidates (KEI-930)

Most catalogue products have no upstream list or repository that names three open-source alternatives to them. Many of those products are in categories where maintained, licence-eligible projects in the corpus plainly do the same job. The KEI-922 sizing found 363 non-ok products with at least three such projects. Job matching records those projects as **candidates**, so the full evaluation can judge them.

Keith decided this on 2026-10-19 (decision D1, recorded on KEI-930 by AIQ Draper, `orch-KEI-930-05ad0e7f`).

```
python -m source_pipeline job-match            # rewrite data/job_match/ledger.json; no network
python -m source_pipeline job-match-observe    # live-verify ledger projects the snapshot lacks (network)
```

## What a job match is

A project is a job-matched candidate for a catalogue product when all of these hold:

- **The job.** Its own description or topics contain the category's defining phrase and one of the category's second-group terms (`config/saas_jobs.yaml`). The defining phrase is the same group the full evaluation's core gate requires.
- **Health.** It is on github.com, maintained or active, licence-eligible, and has a quick Health of at least 55 (`docs/QUICK-SCORING.md`).
- **Not an integration.** It is not an integration with, or companion to, another product. That means not an MCP server, SDK, bot, API client or plugin (the KEI-919 rule), not a list, template or prompt pack, and not a client, front end, downloader or add-on for a named catalogue product.
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
