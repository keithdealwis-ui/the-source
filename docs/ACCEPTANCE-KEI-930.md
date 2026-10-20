# KEI-930: The Source V2.1 WP5b, evidence-backed support expansion (acceptance map)

**Executor.** Attended claude-code on the Mac, worktree `the-source-kei-922-r2`, uncommitted.
**Base.** The verified KEI-922 pack `KEI-922-wp5-catalogue-demand` (tarball `5fd6b5b4`) on origin/main `88df71a`.

**Decisions.** Every decision is Keith's and is recorded on KEI-930 by AIQ Draper.

| Id | Decision | Record |
|---|---|---|
| D1 | Job-matched candidate relationships: their own evidence class, counting only after the full evaluation | `orch-KEI-930-05ad0e7f` |
| D2 | Read-only use of the existing gh login for the full evaluation | `orch-KEI-930-05ad0e7f` |
| D4 | KEI-922 C2 revised from ≥250 ok to ≥140 ok, still only if genuinely supported | gate comment, `orch-KEI-930-ffb65cef` |
| D5 | Licence exceptions: open core and source-available approved with a mandatory disclosure; the rest refused | comment `e2fe9932` |
| D6 | Reviewed readings for licences the detector misread | comment `65f9fd80` |
| D7 | Open WebUI, OpenFaaS and Teable approved as source-available | comment `65f9fd80` |
| Gate | Launch policy `launch.catalogue_serving`: **gate-KEI-930-08d23cd6**, approved 2026-10-20T06:27:33Z | gate record |
| D8 | C3 is an operating constraint, not a binding criterion. The CI items are before-merge residue | 2026-10-20, after verification `orch-KEI-930-11227bd4` |

## Acceptance

| # | Criterion | Result |
|---|---|---|
| C1 | Every ok entry is genuinely supported: at least 3 full-evaluation recommendations, recomputed by `canonical-validate` | **Met.** The status rule is unchanged and `canonical-validate` passes. Every job-matched pair passed an exhaustive desk review before evaluation (see Method 1). |
| C2 | At least 140 ok (D4) | **Met: 167 ok**, 185 partial, 148 none_yet, out of 500 selected. KEI-922 had 49. |
| C3 | Validators and the CI test list pass (an operating constraint, D8) | **Partly; residue under D8.** 8 of the 9 validators pass. `obs-golden` fails on precision@5 (see Residue 1). pytest: 348 passed. The 7 failures are the 3 known KEI-922 ones, the 2 golden-baseline tests (Residue 1) and the 2 KEI-922 off-cycle observability tests. Extension tests: 32/32. |
| C4 | No commit, push, deploy, spend or new credential; the launch cap is a Keith gate | **Met.** Nothing is committed or published. GitHub calls were read-only through the existing login, about 1,900 in all, and no token was written anywhere. The launch change went through the gate. |

## Why 167, and what it rests on

**The 1,026 recommendations behind those products, by evidence class:**
- 515 listed by an upstream list;
- 510 job-matched;
- 1 self-described.

**By licence:**
- 920 OSI-recognised;
- 80 open-core exceptions;
- 26 source-available exceptions.

59 products would be ok on listed, OSI-licensed recommendations alone. The rest depend on job matches or on the licence decisions.

The four named products: Salesforce ok (8), Zapier ok (5), Slack partial (2), 1Password partial (2).

Reaching 250 genuinely supported would need evidence that does not exist yet. Once reviewed, the corpus has fewer than 3 real counterparts for the remaining products, and GitHub search finds 0 to 5 self-described alternatives per product.

## Method

1. **Job-matched candidates** (`docs/JOB-MATCH.md`, D1).
   - **Candidate selection.** A corpus project whose own description carries a category's defining job phrase becomes a candidate. It must be maintained and licence-eligible, have quick Health of at least 55, not be an integration, and not already be a listed pair.
   - **Ledger.** `data/job_match/ledger.json` holds 2,116 pairs and is reproducible from its inputs (digest-checked).
   - **Review first.** The full evaluation's core gate cannot discriminate these pairs: it checks the very phrase that selected them, and it passed 196 of the first 199. So every pair was read first, in `config/job_match_review.yaml`, which fails closed: an unreviewed pair is rejected. 1,457 were rejected and 660 kept.
   - **Labelled everywhere.** Each pair is labelled `evidence_class: job_matched` in the dataset, the canonical layer, every evidence block and the MCP rows.
   - **Never corroborating, never ranking.** A job match never corroborates a listed pair and never counts as a source naming a product.
2. **Job definitions.** `config/saas_jobs.yaml` v1.2.0 adds 62 categories. Generic stems were removed. Categories with no honest open-source counterpart are left undefined.
3. **Full evaluation.** The single-project path ran with read-only gh. It also scored the 45 listed KEI-922 pairs that were waiting for a token.
4. **Licences** (D5–D7).
   - **Re-read first.** Every lane project's licence files were re-read before anything was approved. 48 of the 123 classifier readings were wrong.
   - **Exceptions.** `config/licence_exceptions.yaml` holds 78 entries: 44 open core and 34 source-available. Each is approved by Keith, carries an `approval_ref`, and has a disclosure that states its actual terms.
   - **Readings.** `config/licence_readings.yaml` holds 31 OSI-licensed projects the detector misread. They need no exception and no disclosure. ImageMagick, GoatCounter and highlight were left out.
   - **Disclosure everywhere.** The disclosure is shown on every recommendation, in the canonical licence block and in every MCP project or relationship view. `canonical-validate` fails if one is missing.
5. **Scoring honours an approved exception.** This is a methodology change, made to give D5 effect.
   - Before, any licence that was not plainly recognised was hard-disqualified (`licence_unclear`), approved exceptions included. Even the automated open-core exceptions were never publishable.
   - Now a named-human or edition-check exception is not disqualified. An open-core exception is scored on its core's licence family; a source-available one at licence usability 0.5 (`config/scoring.yaml`).
   - The score version is unchanged. Only projects that were previously disqualified are affected.
6. **Catalogue serving** (gate). Each catalogue-selected product is served with up to 8 of its full-evaluation recommendations, within a budget of its own: 700, of which 685 are used. `launch.edges_max` stays at 400 and still bounds the ranked selection. Catalogue-served edges are marked `via: catalogue_serving` and re-derived on every build, never pinned.
7. **Ranking and retrieval fixes.**
   - **Ranking.** Recommendations are ranked by evidence class first (listed and self-described before job-matched), then by licence (OSI, then open core, then source-available), then by score.
   - **Stopword-only queries.** A query of nothing but stopwords ("the and of") is never strong against anything.
   - **Product matching.** When some products cover every content word of a need, a product sharing only one generic word is not a match. "password manager" was matching Adobe Experience Manager.
   - **Quick scoring** no longer treats job matches as upstream-listed pairings.
   - **Discovery.** A developer library, a client for another service, or an integration is kept out of discovery unless it claims to be the alternative. That is the KEI-919 audit's own definition. The KEI-919 audit's `integrations_in_top5` is back to 1 (Aurora, a listed PagerDuty alternative the audit flags anyway), down from 12.

## Residue (NON_BLOCKING)

1. **`obs-golden` precision@5 is 0.663 against a baseline of 0.778.** Most of the drop is newly admitted genuine alternatives that the golden labels do not contain: KeePassXC for 1Password, GitLab for GitHub, Rocket.Chat for Slack, Outline for Confluence, Baserow for Airtable. The licence misreads used to keep them out. The labels were written before any ranking and still await Keith's label review, so they were not edited here. Before merge: review the labels, then re-baseline. The CI step gates on this check.
2. **`score-validate`** (resolved 2026-10-20 at landing): run against the Mac full-lane store, the v1.2.0 job-definition negative control passes (mismatched-category pass rate 0.02, max 0.15; true pairs 0.972; 690 pairs). The only failing check was a missing local probe file (`evidence/KEI-806-refresh-probe.json`, not in the run copy). Originally: It needs the full-lane evidence store (`data/score/`), which only the daily cycle has. It re-runs the job-definition negative control for v1.2.0.
3. **Public wording.** The Source now recommends source-available projects, with a disclosure. The MCP description ("open-source alternatives") and the landing page need updating before merge.
4. **Raw Google Ads volumes** (KEI-922 f2): resolved 2026-10-20. Keith chose to keep them private; `data/catalogue/demand/` is not committed and `candidates.jsonl` carries a demand band only.
5. **Single-project fits replace full-lane ones** for the licence-exception projects, rescored here. The next full-lane run supersedes them.
6. **`launch.catalogue_serving.edges_max` is 700, and 685 are used.** Growth will need the budget revisited.
7. **Draper's D6/D7 record says "31 source-available"; the actual count is 34** (31 plus the 3 from D7).
