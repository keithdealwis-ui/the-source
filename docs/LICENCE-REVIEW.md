# Licence review of upstream sources

Reviewed 2026-09-27 by claude-heavy under KEI-805. The machine-readable record, with
full rationale and conditions, is `config/sources.yaml`.

These are engineering readings of licence texts. They are not legal advice. The
questions that need a human decision before anything is published are listed at the
end.

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

The first five rows are the seeds named on the ticket. The last four were added
because the seeds alone could not reach the launch targets once the quality bar was
applied.

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
| Nothing is published by the pipeline | `export.py` writes to a local directory and records `published: false` |

## Discovery corpus (KEI-912)

`data/corpus/` publishes the Discover lane's corpus as facts ([CORPUS.md](CORPUS.md)). Keith
approved it on 2026-10-10, gate `gate-KEI-912-dba84c8b`, option A:

- **Curated lists.** Only lists with a recognised licence (MIT, Apache-2.0, CC0-1.0, CC-BY-4.0,
  BSD-3-Clause, Unlicense; `config/discover.yaml`) are read, and from them only the fact that a
  repository URL appears, at a pinned commit and line. No list text is kept or republished.
  Lists without a recognised licence are refused at harvest.
- **Host metadata.** Names, descriptions (truncated), homepages, topics, languages, licence
  identifiers, stars, forks, dates and release tags, as GitHub reports them for public
  repositories. Facts and short identifying metadata, signed off as such.
- **Licence-issue projects.** About 1,400 projects whose licence The Source does not recognise
  are published only as `discovery`, with the issue and a warning, and are never recommended or
  offered as discovery candidates. This widens Keith's 2026-09-27 retention decision (KEI-805,
  comment 258b38dd: "schema inclusion and flagged retention only; not recommendation, publication
  or redistribution") to publication of the facts above, under the same no-recommendation rule.
- **Private seed list.** Not published. Routes from it appear only as `maintainer_seed`.

## Open questions for Keith before any publication

These do not affect the internal dataset. Each needs a decision before the data is
shown publicly.

1. **Share-alike.** Some launch relationships rest only on awesome-selfhosted
   (CC BY-SA 3.0). The count is reported, per run, in `summary.json` at
   `launch.relationships_share_alike_only` and is not repeated here, so that this
   document cannot disagree with the data. The export withholds them by default. The alternatives are to
   publish that portion under CC BY-SA, or to corroborate those relationships from a
   second source. Relationships that awesome-selfhosted shares with a permissive
   source are exported on the permissive source alone.

2. **Compilation rights.** We import facts, which copyright does not protect. A
   curated list may still attract protection as a compilation, and in the UK and EU a
   database right can apply to substantial extraction. Our extraction from each list
   is partial and every fact is re-verified, but whether it is "substantial" is a legal
   judgement. Worth a short legal review before launch.

3. **altstack-data licence inconsistency.** Its LICENSE file is Apache-2.0; its README
   says CC BY 4.0. Both permit reuse and we meet both. An issue asking the maintainers
   to reconcile them would settle it; raising one is an external action and has not
   been done.

4. **Attribution placement.** Where and how attribution appears on the website is a
   rendering decision outside this ticket. `NOTICE.md` holds the required text.

5. **Licence exceptions.** Decided in part. Keith directed on 2026-09-27 (Linear
   KEI-805, comment 258b38dd) that projects with licence issues are retained as
   flagged, non-recommendable records. That is done: see the `licence_exception` lane.
   Still open is whether any named project should be recommended despite its licence,
   and how The Source should label it. Candidates, with category and reason, are in
   `data/dataset/reports/licence_exception_candidates.json`.
