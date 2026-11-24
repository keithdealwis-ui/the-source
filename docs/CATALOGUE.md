# Product coverage and catalogue selection

See which paid products The Source covers and where the evidence is still limited. The [published catalogue](../api/v1/catalogue.json) records each product's category, audience and coverage state:

- **ok:** at least three recommendations that passed the full evaluation.
- **partial:** one or two recommendations.
- **none_yet:** no qualifying recommendations yet.

A catalogue entry is not a promise of alternatives. Check the current counts in [the canonical manifest](../data/canonical/MANIFEST.json), or [search the website](https://the-source.dev/#explore).

For maintainers: `catalogue-rank` selects the configured catalogue, `catalogue-demand --kwi REPORT.json` imports demand evidence, `catalogue-validate` checks it, and `canonical-build` produces the read surface. The following sections describe selection and detection.

## Candidates (602)

Every product in `config/saas_catalogue.yaml` is a candidate. Current candidate and selection counts are recorded in the catalogue outputs. The additions were reviewed against the catalogue's existing admission rule: each is a commercially operated, hosted product sold or metered on its own. That rule leaves out open-source projects, desktop software and products that have shut down. Where an upstream list already named a product the catalogue lacked (Clover, Square POS, Noun Project, Agora, M-Files, DocuWare, Prisma Cloud, Oracle Fusion ERP Cloud, ...), it was added under the spelling the list used. That evidence now validates.

`config/catalogue.yaml` gives each candidate:

- **audience**: `b2b`, `b2c` or `both`. Current counts are recorded in the catalogue outputs.
- **detection rules**: the hosts and paths where a browser is on the product. See "Detection" below.

Name, domain, category and aliases stay in `config/saas_catalogue.yaml`, so there is one source of truth.

## Ranking and selection

Each candidate is scored from evidence. Every signal is recorded on its row in `data/catalogue/candidates.jsonl`.

**Supply**, 0 to 100. Weights are in `config/catalogue.yaml`:

| Signal | Weight | Cap | Meaning |
|---|---:|---:|---|
| recommended | 35 | 5 | fully evaluated, recommended relationships (canonical layer) |
| validated_relationships | 15 | 10 | recommendable relationships in `data/dataset/` |
| upstream_sources | 15 | 7 | independent upstream lists that name the product |
| corpus_pairings | 15 | 10 | corpus projects whose own description or topics call them its alternative |
| category_supply | 20 | 300, log | maintained corpus projects whose identity text has the category's defining phrases (`config/saas_jobs.yaml`) |

**Demand** is monthly alternative-intent search volume from aiq-kwi runs ("notion alternative", "open source notion", "self-hosted slack"). `catalogue-demand --kwi` imports a run's `report.json` into `data/catalogue/demand/`. Those files hold Google Ads Keyword Planner volumes and are not committed (`.gitignore`). `candidates.jsonl` publishes a demand band instead (`high` ≥1,000 a month, `medium` ≥100, `low` ≥1, `none`), and leaves out the rank score, which would give the volume back. The selection is reproducible only where the demand files are present. A keyword only counts if it carries an alternative-intent modifier and names exactly one candidate; the longest name wins. A candidate the run could not have found (it was neither a seed nor matched) has no demand figure. It gets `null`, never zero. Where demand is measured, rank = 0.5 × demand + 0.5 × supply.

Demand measurements are dated inputs, not live search volumes. Consult the candidate records for coverage and import a new run before treating them as current.

**Aggregate MCP telemetry** counts MCP calls per tool and outcome. It records no product argument, by design, so it carries no per-product demand. It is reported as `no_product_dimension` and is not used.

**Selection** keeps these products whatever their rank, then fills to 500 by rank:

- the products already served (`config/supported_saas.yaml`, 20);
- explicitly prioritised products (Slack, Salesforce, Zapier, 1Password);
- every product with a fully evaluated recommendation, so evidence is never dropped.

Ties go to more upstream sources, then `saas_id`. `catalogue-validate` re-runs the ranking on the committed inputs and fails unless it selects exactly the committed products.

Candidates outside the published selection remain available for future ranking as evidence and demand change.

## Status: ok, partial, none_yet

canonical-build sets a selected product's status on every run, from full evaluations alone:

- **ok**: at least 3 recommendations that passed the full evaluation. That means an export-guarded relationship whose Replacement Fit is publishable and a credible alternative, scored by the full lane or the single-project path (`docs/SCORING.md`).
- **partial**: 1 or 2 such recommendations.
- **none_yet**: none.

Quick scores, corpus pairings and demand never change a status. `canonical-validate` recomputes every status from `data/canonical/relationships.jsonl` and fails on any difference.

Every `partial` and `none_yet` entry lists its reasons, as codes and as a sentence:

| Code | Meaning |
|---|---|
| `fewer_than_minimum` | some recommendations passed, fewer than 3 |
| `awaiting_full_evaluation` | relationships in the published layer that have not been fully evaluated yet |
| `evaluated_not_recommended` | fully evaluated relationships that did not qualify (not publishable, not credible, core gate) |
| `outside_launch_selection` | validated relationships outside the published launch corpus. Current selection and catalogue-serving budgets in `config/policy.yaml` govern inclusion; a validated relationship is not automatically served |
| `share_alike_only` | validated relationships that rest only on a share-alike source, withheld from the public layer |
| `licence_review` | candidate relationships waiting on a licence decision (the licence-exception lane) |
| `no_job_definition` | the category has no job definition in `config/saas_jobs.yaml`, so its alternatives cannot be fully evaluated |
| `corpus_candidates_only` | corpus projects describe themselves as alternatives, but none has been fully evaluated |
| `no_relationship_evidence` | no open-source alternative has been found with relationship evidence |

## Support records

Every catalogue product with at least one fully evaluated recommendation (`ok` or `partial`) gets a support record at `api/v1/saas/<saas_id>.json`, plus the 20 products configured in `config/supported_saas.yaml`. A support record carries:

- the ranked recommendations with live facts and scores;
- the catalogue state;
- for every recommendation, an `evidence` block: the relationship's upstream provenance (source, commit, locator) and the full evaluation's scoring path, core gate, evidence families and dimension scores.

`compare_path` is a legacy comparison-route field, populated according to catalogue status and configured product rules. A populated field does not prove a page exists. The current public browsing route is [The Source website](https://the-source.dev/#explore); consumers must verify any product-specific destination before linking to it.

## Detection

`source_pipeline/detect.py` is the reference implementation. `extension/src/match.js` implements the same semantics, and `extension/test/match.test.mjs` checks the two agree URL by URL over every catalogue rule.

- `host` is the exact hostname, matched after lowercasing and stripping one leading `www.`.
- `host_suffix` is for per-customer subdomains. It must start with `.` or `-` (for example `-my.sharepoint.com` for OneDrive for Business), so a lookalike such as `evilatlassian.net` never matches.
- `path_prefix` matches whole path segments: `/wiki` matches `/wiki/spaces/X` but never `/wikipedia`.
- When several rules match, the most specific wins. A path beats no path, a longer path beats a shorter one, an exact host beats a suffix, and a longer suffix beats a shorter one.

That is how one host carries several products:

| URL | Product |
|---|---|
| `app.hubspot.com/contacts/…` | HubSpot (CRM) |
| `app.hubspot.com/email/…`, `/social`, `/ads`, `/campaigns` | HubSpot Marketing Hub |
| `app.hubspot.com/website/…`, `/pages`, `/blog`, `/content` | HubSpot Content Hub |
| `app.hubspot.com/help-desk/…`, `/knowledge`, `/feedback` | HubSpot Service Hub |
| `acme.atlassian.net/wiki/…` | Confluence |
| `acme.atlassian.net/jira/…`, `/browse/…` | Jira |
| `acme.atlassian.net/jira/servicedesk/…`, `/servicedesk/…` | Jira Service Management |
| `docs.google.com/document`, `/spreadsheets`, `/presentation`, `/forms` | Google Docs, Sheets, Slides, Forms |
| `*.console.aws.amazon.com/sqs`, `/dynamodbv2`, `/rds` | Amazon SQS, DynamoDB, RDS |

**Every catalogue entry has detection tests.** `catalogue-validate` checks two things over all 602 candidates. Every one of the 1,197 rules must resolve its own URLs (the bare rule and a deeper page) to its own product among all candidates' rules. The 82 examples in `config/catalogue.yaml` must also hold, including negative cases (`/wikipedia`, `/s3-glacier`, `/emailing`, lookalike hosts).

`api/v1/domains.json` carries the rules of the served products. `api/v1/catalogue.json` carries every entry's rules. Chrome's declarativeContent cannot express "most specific wins". On a page where a served path rule and a served plain rule both hold, the toolbar condition fires for both, and the popup's matcher decides which product it shows.

## Files

| File | What it is |
|---|---|
| `config/catalogue.yaml` | candidates: audience and detection rules; target, weights, examples |
| `config/saas_catalogue.yaml` | product names, domains, categories and aliases (602) |
| `data/catalogue/candidates.jsonl` | every candidate with its signals, scores, rank and whether it was selected |
| `data/catalogue/selection.json` | the selected 500, what they were ranked from, demand and telemetry status, counts |
| `data/catalogue/demand/*.json` | imported demand (`the-source.catalogue-demand/1`), one file per aiq-kwi run; held privately, not committed |
| `api/v1/catalogue.json` | the published catalogue: every entry's status, reasons, evidence counts, support record and rules |
