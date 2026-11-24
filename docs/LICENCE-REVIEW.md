# Upstream source licensing and reuse decisions

The Source checks the terms of upstream material before importing it. The machine-readable source decisions, conditions and licence-file hashes are in `config/sources.yaml`. Attribution is retained in the published notices.

The source-specific readings below retain their original evidence; they are engineering records, not a new legal opinion or permission to reuse adjacent websites and APIs.

## Method

For each upstream:

1. Read the licence file in the repository at the pinned commit, not the badge and not
   GitHub's label.
2. Compare it with what the README says about licensing. Record any disagreement.
3. Look for terms covering anything adjacent that we might be tempted to use: a
   website, an API.
4. Decide `allow`, `allow_with_conditions` or `refuse`, and write down why.
5. Pin the decision to the commit and to the SHA-256 of the licence file.

## Import policy

Facts only, from every source, whatever its licence permits: project name, repository
URL, the project's self-declared licence identifier, a short category label, and the
claim that a project is an alternative to a SaaS product.

Not imported from any source: descriptions, taglines, comparisons, pros and cons,
pricing, setup estimates, referral links, logos.

## Decisions — harvested

| Source | Licence as read | Decision | Main condition |
|---|---|---|---|
| SolvoHQ/awesome-self-host-saas-alternatives | CC0 1.0 | allow | none; attributed anyway |
| awesome-selfhosted/awesome-selfhosted-data | CC BY-SA 3.0 | allow with conditions | attribution; share-alike |
| awesome-selfhosted/awesome-selfhosted | CC BY-SA 3.0 | covered by the row above | generated from the data repo; not parsed twice |
| RunaCapital/awesome-oss-alternatives | MIT | allow | reproduce the MIT notice |
| open-saas-directory/awesome-saas-directory | MIT | allow | reproduce the MIT notice |
| Emmraan/awesome-saas-alternatives | MIT | allow | reproduce the MIT notice |
| btw-so/open-source-alternatives | MIT | allow | reproduce the MIT notice |
| diegoleme/awesome-open-source-alternatives | MIT | allow | reproduce the MIT notice |
| altstackHQ/altstack-data | Apache-2.0 file, CC BY 4.0 per README | allow with conditions | meet both licences' terms |
| sfermigier/awesome-foss-alternatives | CC BY 4.0 | allow with conditions | attribution; state changes |

## Decisions — refused or not applicable

| Target | Decision | Why |
|---|---|---|
| osalt.dev website and JSON API | refuse | SolvoHQ's CC0 dedication covers the list in the repository; no terms were found for the site or API |
| opensaas.directory website | refuse | no terms or data licence found; `/terms` returned 404 on 2026-09-27 |
| piotrkulpinski/open-source-alternatives | refuse | CC0, but entries link to openalternative.co pages and name no repository or SaaS product; the data is on the website, which has no data licence |
| junaid33/opensource.builders | refuse | no licence |
| tycrek/degoogle | refuse | no licence |
| fffaraz/awesome-selfhosted-aws | refuse | no licence |
| Lissy93/awesome-privacy, pluja/awesome-privacy, geraldohomero/best-foss-alternatives | not applicable | reusable, but they state no SaaS-to-OSS relationships |

## How the conditions are enforced

| Condition | Mechanism |
|---|---|
| Licence text has not changed since review | harvest compares the licence file hash and stops on a mismatch |
| No upstream prose is imported | parsers emit facts only; tested |
| Attribution and notices accompany an export | `export.py` writes `NOTICE.md` for every contributing source |
| Share-alike material is not published under other terms | relationships whose only sources are share-alike are marked `share_alike_only` and withheld from an export by default |
| A refused source contributes nothing | harvest skips it; export refuses if one appears |
| Local export is separate from publication | `export.py` writes locally; the scheduled publication workflow promotes validated outputs |

## Discovery corpus

`data/corpus/` publishes discovery facts under the following boundaries ([corpus guide](CORPUS.md)):

- **Curated lists.** Only lists with a recognised licence (MIT, Apache-2.0, CC0-1.0, CC-BY-4.0,
  BSD-3-Clause, Unlicense; `config/discover.yaml`) are read, and from them only the fact that a
  repository URL appears, at a pinned commit and line. No list text is kept or republished.
  Lists without a recognised licence are refused at harvest.
- **Host metadata.** Names, descriptions (truncated), homepages, topics, languages, licence
  identifiers, stars, forks, dates and release tags, as GitHub reports them for public
  repositories. Facts and short identifying metadata, signed off as such.
- **Licence-issue projects.** Unresolved licence records remain discovery entries with warnings. A separately reviewed exception can qualify a specific scope; eligibility and disclosures must be preserved. Counts and classifications are in the corpus manifest.
- **Private seed list.** Not published. Routes from it appear only as `maintainer_seed`.

## Current publication responsibilities

Preserve the path-specific terms in [DATA-LICENCE.md](../DATA-LICENCE.md) and the required text in [NOTICE.md](../NOTICE.md) and [the canonical notice](../data/canonical/NOTICE.md). An upstream licence-file change requires review before the pin advances. Changes to exception eligibility require the normal policy review and regeneration; documentation edits do not grant new reuse rights.

Source terms can cover compilation or database rights as well as individual content. Retain the recorded conditions, including any overlapping attribution obligations, and review new uses on their own terms.
