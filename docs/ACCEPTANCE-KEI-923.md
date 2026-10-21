# KEI-923: The Source V2.1 WP6, controlled corpus expansion to 25k (acceptance map)

**Executor.** Attended claude-code on the Mac, worktree `the-source-kei-923`, branch `kei-923-corpus-expansion`.
**Base.** origin/main `70b6e2e` (KEI-922/KEI-930 merged), corpus `db61d41f65eb4c27`, 9,901 members.
**Result.** Corpus `99078afc2a6dcb42`, **25,000 members** (15,099 new), quick layer `b587723dc30fb446`. Nothing is
pushed to main, deployed or published. That is behind a Keith gate.

## Acceptance

| # | Criterion | Result |
|---|---|---|
| A1 | 25k corpus milestone; 50k only after a review | **Met.** 25,000 exactly. Admission stops at `milestones.target` (16,979 rows refused as `milestone_reached` in the last run). No path admits past it. |
| A2 | Stable noise (S4.3) | **Met against the baseline. The ticket's 10% top-10 proposal is not met by human judgement.** The audit numbers and the blind human reviews disagree; both are below. |
| A3 | No golden-set regression | **Met.** KEI-919 golden summary is identical to main. obs-golden was also identical to main once Keith added `supabase/storage` (S3-compatible object storage, newly admitted) to `search-object-storage` on 2026-10-21. Before that, unlabelled, it outranked `openstack/swift` and moved the query's recall 1.0 → 0.8. Recall@5 0.6822, precision@5 0.6583, same as main. |
| A4 | Reproducible lineage | **Met.** Every expansion member traces to a row of the recorded frontier (`data/expansion/frontier.jsonl`, sha `acdcfb0f…`) through a declared source, and to an admission run report in `data/expansion/runs/`. Independent check `lineage_ok`. |
| A5 | Acceptable quota and runtime | **Met, with one capacity note.** Per-run caps held in all three runs (independent `quota_ok`). The weekly refresh fits its budget. MCP peak RSS 350 MB of the 512 MB cap at 25k: 50k needs memory work first. |
| S4.1 | GitHub topics, awesome-selfhosted, Docker/Helm, GitLab, Codeberg | **Met except Artifact Hub.** Artifact Hub refused connections for the whole harvest (`status: failed`, 0 returned); its breaker stopped it. Docker/Helm signals came from GitHub instead. |
| S4.2 | Active quality floor plus a historical tier | **Met.** Every expansion member clears the floor (independent `floor_ok`). 1,755 notable archived or inactive projects are in `data/historical/`, none of them members. |
| S4.3 | Intake and top-result noise measured separately | **Measured; see Noise.** |
| S4.4 | Quick score on admission | **Met.** Every new member has quick Health. The 44 members without one are base members whose host no longer finds them, the same 44 as on main. |
| Gates | No new credentials, spend or public publication | **Met.** Public APIs plus the cycle's existing read-only GitHub token. No model calls, no spend. Not pushed to main or deployed. |

## Noise (S4.3)

Two kinds of measurement, and they disagree. The automatic audit (`noise.audit`, `observability/noise-after.json`)
is a regex classifier. Blind human desk reviews (`kei-923-artefacts/desk-review*`) read every row by hand and
show the audit under-counts material and integrations by several times. **Report the human figures.**

| Measure | Main (baseline) | After | Ticket proposal | Method |
|---|---:|---:|---:|---|
| Top-10 discovery for each category query: integration, material or unclear | 37.8% | **27.7%** | < 10% | blind human, 25 categories, main and after mixed (483 rows) |
| … relevant products in that top 10 | 36.5% | **52.1%** | | same |
| Top-10, audit | 34.3% | 4.5% | < 10% | `noise.audit`, 112 categories |
| Top-5 discovery candidates for supported products, audit | 2.4% | 2.7% | < 5% | `noise.audit`, 338 products |
| Integration false positives in recommendations | 13 | 13, the same rows | 0 new | audit; all 13 read by hand as false alarms |
| Intake: material or unclear among admissions | 16.5% (main's corpus) | **12.5%** (final gate, fresh rows) | < 10% (WP6 proposal) | blind human, 400 rows each |
| … integrations | 39.0% | 23.5% | | same |

**Why intake stays above 10%.** Three blind reviews of new admissions read 10.75% (first gate), 11.0% (second) and
12.5% (final gate, rows no earlier review saw). Rules written from one sample barely moved the next: the first
review's rules caught 26 of its 42 material rows, yet the next fresh sample still read 11%. What remains is
semantic (demo apps, deployment recipes, packaging around one product). Regex cannot catch it without refusing
products. A model-based intake classifier would, but that is new spend and needs Keith.

**Why top-10 stays above 10%.** `source_search` now lists a discovery integration or material row after the rest
unless the query asks for that kind (`listed_after_products`; nothing is dropped). That raised relevant results
from 36.5% to 52.1%. But the search fills ten slots, and many categories have fewer than ten relevant products in
the corpus. The remaining slots go to integrations the server's classifier does not recognise (WordPress and
ERPNext apps, ComfyUI nodes) and to off-category products. Below 10% would need the search to stop short
rather than fill, as `source_get_alternatives` already does, or model-based classification.

## Method

1. **Harvest** (`corpus-expand-harvest`, 2026-10-20). 40,293 addresses (GitHub 39,885, GitLab 166, Codeberg 242),
   1,869 historical candidates. 1,455 requests, 1,321 of them GitHub search, 98 min. Per-source checkpoints and
   `--resume` were added after the first harvest stalled 80 min on list/chart back-off.
2. **Admission** (`corpus-expand-admit`, three runs, 2026-10-20T23:44Z to 2026-10-21T00:50Z): 12,005 + 3,103 + 1.
   Run 1 admitted 9,600 from GitHub topics (the 60% family cap of a 16,000 budget), 1,772 Docker/Helm, 384 awesome
   lists, 156 Codeberg and 93 GitLab. Intake noise refused 3,312 rows (curated list 844, template or example
   1,061, learning 495, personal site 329, prompt/skill pack 300, personal config 210, research 40, docs 33).
3. **The gate was rebuilt twice.** The first 25k (corpus `5f0c0eb5…`) was reset after the first blind review
   (10.75%) and the KEI-919 suite showed add-ons as top-5 alternatives. The second (`f7d57a43…`) was reset after
   the second review (11.0%), to admit the final corpus under exactly the committed gate. Each new rule was
   checked against the whole corpus for products it would refuse. Rules that did (venue tags, "reference
   implementation", dataset and survey topics) were dropped.
4. **Catalogue.** `api/v1/catalogue.json` is rebuilt (`canonical.build`): only `corpus_pairings` counts, reason
   text and the corpus version change, and all 500 statuses are unchanged. `data/canonical` and `data/scores` are
   byte-identical to the base.
5. **Independent check** (`kei-923-artefacts/check_independent.py`). Reads raw files and git history only, imports
   no producer code: milestone, manifest, no member lost, lineage, floor, quotas, historical, quick coverage,
   recommendations unchanged, noise bounds. All pass.

## Cost and capacity

- **MCP server** (`mcp-serve --check`): peak RSS 191 MB at 9.9k, **350 MB at 25k**, against the 512 MB cap. Load
  2.3 s. The growth is linear, so 50k needs slimmer loading or a higher cap (follow-on).
- **Weekly `corpus-refresh`**: about 505 GraphQL points at 25k (190 at 9.9k), inside `max_graphql_points` 1,500.
  About 35 min, inside the workflow's 60 min.
- **Source diversity**: the 60% cap bounds each run's budget, not what accumulates across runs. GitHub topics
  holds 85% of expansion admissions (12,869 of 15,103 charged rows).

## Residue and decisions for Keith

1. **Merge and deploy** to main and the live MCP: public publication, Keith's gate.
2. **Noise below the 10% proposals**: accept the human-measured improvement (intake 16.5% → 12.5%, top-10 junk
   37.8% → 27.7%), or open a follow-on: model-based intake classification (spend) and/or a search that stops
   short instead of filling ten.
3. **Golden label**: Keith added `supabase/storage` to `search-object-storage` (2026-10-21).
4. **Artifact Hub** gap: retry in a later harvest.
5. **50k**: a separate checkpoint, after MCP memory work.
6. `data/dataset/reports/validation_report.json` turns `launch.ranking_unchanged` false whenever
   `canonical.build` runs, on main `70b6e2e` as well. It predates this ticket, and the committed copy is kept.
