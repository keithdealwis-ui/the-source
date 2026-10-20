# Controlled corpus expansion (KEI-923, V2.1 WP6)

The discovery corpus (`data/corpus/`, [CORPUS.md](CORPUS.md)) stood at about 9,900 projects, growing by
around ten a day through the daily lanes. WP6 grows it toward **25,000**. It may only do that while
candidate quality and the cost of ingestion stay acceptable. **50,000 is a separate checkpoint** that needs a
review; admission never goes past `milestones.target` on its own.

Nothing here changes a recommendation. Recommendations come only from `data/canonical`. Every member added
here is a `discovery` tier project ("discovered, not scored") with a quick score.

## Run it

```bash
python -m source_pipeline corpus-expand-harvest              # network: every source -> data/expansion/
python -m source_pipeline corpus-expand-harvest --only gitlab --only codeberg   # refresh some sources only
python -m source_pipeline corpus-expand-admit [--max-admit N]  # network: admit under the gate and quotas,
                                                               # then quick-score (S4.4); both or neither
python -m source_pipeline corpus-validate
python -m source_pipeline obs-noise --check                    # S4.3 noise against its baseline
python -m source_pipeline obs-golden --baseline observability/golden-baseline.json --check
python scripts/kei919_golden.py                                # WP2 integrations and constraints
```

## Sources (S4.1)

Every source is declared, with its bound, in `config/expansion.yaml`. `corpus-expand-harvest` records what
each returned in `data/expansion/frontier.jsonl`: one row per address, with every route that reached it. It
also writes `data/expansion/MANIFEST.json`, with each source's status, query count, requests and runtime. Admission
reads only that record, so the same frontier always admits the same rows.

| Source | Family | What it asks |
|---|---|---|
| `github_topics` | github_topics | One GitHub search per product-domain topic (about 330: work management, communication, CRM and support, finance and ERP, commerce and CMS, analytics and BI, no-code and automation, files and media, identity and security, developer platform, AI applications, self-hosting). It asks only for repositories that clear the active floor (`pushed:>=` the policy window, `archived:false`, the star floor). A topic past the API's 1,000-result cap is sliced by star band until every slice fits. |
| `docker_signals` | docker_helm | The same, for repositories that ship a compose file, an image, a Helm chart or an operator. These are the ones a person can self-host. |
| `artifacthub` | docker_helm | Artifact Hub Helm charts, operators and container images, most-starred first. A package's `links` or `home_url` is kept only if it is a repository root. A chart inside a chart monorepo (`…/tree/…`) never stands in for the application it packages. |
| `awesome_lists` | awesome_lists | Curated lists as awesome.ecosyste.ms parses them. Only the addresses are kept (the lists are CC BY-SA; see LICENCE-REVIEW.md, KEI-912). |
| `gitlab`, `codeberg` | gitlab, codeberg | The independent forges, by topic, most-starred first (`source_pipeline/hosts.py`). Anonymous public APIs only. |

All of these are public, documented APIs read without any new credential. GitHub is read with the cycle's
existing read-only token, paced under the search API's 30 requests a minute.

## The bar: the quality floor and the historical tier (S4.2)

Admission (`corpus_admit.admit`) applies the Discover lane's bar unchanged (`config/policy.yaml`).

- **Prefilter.** The repository must be found and public. It must not be archived, disabled, empty, a fork or a mirror. It must have been pushed inside `maintained_days`.
- **Licence.** A recognised SPDX makes the row `recognised`. Anything else is admitted as `retained` with its issue named and is never recommended. Codeberg names no licence through its API, so the licence files at the root are read and matched, as `enrich.codeberg` does.

Expansion adds three things:

- **A star floor per host.** It is applied in the search, so no lookup is spent on a certain rejection: GitHub 30, GitLab 10, Codeberg 10.
- **The historical tier.** A repository refused *only* because it is archived or long inactive, with at least `historical.min_stars` (1,000) stars, is kept apart in `data/historical/`. It is not a member of the active corpus. It does not count toward the milestone, is never served as discovery and is never recommended. Below that star count it is simply refused.
- **Identity.** GitLab and Codeberg members go through the same identity resolution (renames, mirrors, siblings) as GitHub members, because every admitted row is written through `corpus.write_layer`. `corpus-refresh` re-reads them through `hosts.py`.

## Quotas

From `config/expansion.yaml` `quotas`:

- `max_lookups_per_run` (40,000): host lookups in one run. Candidates are looked up most-starred first.
- `max_admit_per_run` (16,000): admissions in one run.
- `max_source_share` (0.60): no single source family may take more than 60% of a run's admission budget.
- `max_per_query` (1,200): one topic, list or forge topic contributes at most this many in a run.

A route is charged when its row is admitted. A row reached by several routes is charged to the least-used route
that is still under its cap. A refusal is counted (`deferred_source_quota`, `deferred_admission_quota`,
`milestone_reached`), never silent.

## Noise (S4.3)

Intake noise and top-result noise are measured separately (`source_pipeline/noise.py`, `obs-noise`).

**The intake gate.** `noise.intake_reasons` refuses rows that are not software a person would run, judged from
their own name, description and topics:

- `curated_list`
- `learning_material`: courses, tutorials, exercises, interview preparation
- `template_or_example`: boilerplates, starters, examples, demos
- `personal_configuration`: dotfiles, "my homelab", personal infrastructure
- `prompt_pack`

The gate applies to every admission, daily ones included, while `noise.gate` is on. Each refusal is counted as
`intake_noise:<code>`. Integrations (MCP servers, SDKs, clients) are *not* refused at intake. The corpus
holds them deliberately and labels them. They are noise only when they surface as answers to a product
question, which the top-result measures count.

**The measures.** `noise.audit` is a second classifier, written independently of the gate and of the server's
own filters, so that neither grades itself.

| Measure | What is counted | Proposal |
|---|---|---|
| `top10_discovery_irrelevant` | The top ten `discovery` results of `source_search` for every catalogue category (112 queries), flagged as material, integration or undescribed | < 10% |
| `top5_supported_irrelevant` | The top five discovery candidates of `source_get_alternatives` for every supported product (338) | < 5% |
| `intake_irrelevant` | Expansion admissions flagged as material or undescribed (before any expansion: the whole corpus) | < 10% |
| `integration_false_positives_in_recommendations` | Published recommendations the audit flags as integrations that were not already flagged in the baseline | 0 new |

**Calibration.** The baseline was recorded on corpus `db61d41f65eb4c27` (9,901 members) before any expansion:
`observability/noise-baseline.json`. A measure fails `--check` only when it is worse than
max(threshold, baseline + `tolerance` 0.02). That is "stable noise": an expansion may leave a measure where it
was, or improve it, but not worsen it. The baseline top-ten share is 34.3%. That is far above the 10% proposal,
because the pre-expansion corpus is dominated by MCP servers and agent tooling, which answer category queries
badly. So the bound for that measure is the baseline, and the expansion is expected to *lower* it.

The 13 recommendation rows the audit flags in the baseline were each read by hand. All 13 are the audit's own
false alarms, not integrations:

- Passbolt's API repository
- Appsmith and Activepieces, both "integrates with"
- Aster Mail, "email client for"
- Tunnelmole's client
- Kokoro-FastAPI, "wrapper for"
- Firecrawl, "the library for"

The check therefore fails only on a recommendation flagged that the baseline did not hold.

## Quick scoring on intake (S4.4)

`corpus-expand-admit` runs `quick_score.run` straight after admission and validates the quick layer, exactly as
the daily cycle does (cycle.py, KEI-921). If quick scoring cannot follow, the previous corpus *and* quick layer
are put back. No member is left without a quick score.

## Cost

GitHub search is paced at one request per 2.1 s. Metadata lookups go in GraphQL batches of 50. The weekly
`corpus-refresh` cost scales with membership: about 500 GraphQL requests at 25k, inside `max_graphql_points`
(1,500). Measured figures for the 25k run are in [ACCEPTANCE-KEI-923.md](ACCEPTANCE-KEI-923.md).
