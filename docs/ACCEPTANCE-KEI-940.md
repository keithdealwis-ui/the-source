# KEI-940: full evaluation of discovery-tier pairings from the 25k corpus (acceptance map)

**Executor.** Attended claude-code on the Mac, worktree `the-source-discovery-eval`, branch `discovery-eval`.
**Base.** origin/main `9c50bbc` (KEI-923 merged and live: corpus 25,010, 168 catalogue products ok).
**Assigned.** By Keith directly, 2026-10-21: "The quicker that we get them evaluated from the discovery tier, the better."
**Result.** 652 newly paired projects fully evaluated. Catalogue **ok 168 → 251**, none_yet 162 → 100, and no product
moves down. Recommended relationships 1,021 → 1,527, and none that main serves is lost. Nothing is merged, deployed
or published; that is behind a Keith gate.

## Acceptance

| # | Criterion | Result |
|---|---|---|
| 1 | Every new ledger pair desk-reviewed; every kept pair evaluated or a reason recorded | **Met.** The rebuilt ledger holds 4,167 pairs, 2,440 of them new. All 2,440 were read (687 kept, 1,753 rejected with reasons) in `config/job_match_review.yaml`, which fails closed. 652 kept pairs have a full evaluation; the rest carry the build's recorded reason (licence, not live-verified, or no job definition for the category). |
| 2 | Existing gates only; no threshold or methodology change | **Met.** Unchanged `score_one` (single_project path), core gate, licence and publishable/credible Replacement Fit. No scoring config changed. |
| 3 | No pairing main serves is lost | **Met.** 0 of main's 1,021 recommendations lost (checked pair by pair). Two fixes were needed, below. |
| 4 | CI validators, obs-golden, obs-noise | **Met, with one KEI-919 threshold miss for Keith.** All CI validators pass. obs-golden is better than main once 23 new relevant answers are labelled (labels for Keith). obs-noise passes: 12 newly flagged rows from 4 projects were read by hand as products. **KEI-919 `need_unnamed` precision@5 is 0.694, below Keith's D10 floor of 0.725** (main 0.7375). |
| 5 | Independent verification before merge | Pack and Draper verification follow this commit. |

## What was done

1. **Ledger** (`job-match`, no network). Rebuilt on the 25k corpus: 2,116 → 4,167 pairs. **Fix:** a pair the full
   evaluation has already scored keeps its place under the 10-per-product cap. Without it, stronger new candidates
   displaced 268 scored pairs, 102 of them live recommendations.
2. **Observe** (`job-match-observe`, existing read-only gh token). 599 new projects, 2,271 calls, 0 failures.
3. **Desk review.** Eight independent blind reviewers, 305 pairs each, using the KEI-930 standard (what the project
   *is*, not how good it is; fail-closed). Two batches cross-contaminated through a shared scratch file, so both were
   discarded and re-reviewed in isolated directories. After that, no reason text is shared across batches. Kept 687 of
   2,440 (28.2%), against KEI-930's 31%. Raw labels: `kei-923-artefacts/jm-review/`.
4. **Evaluate** (`job-match-score --budget 2000`, the cycle's own new-pair step without its 60-a-day cap). 652 pairs
   across 577 projects. Two projects had no job definition for their category.
5. **Serve.** `launch.catalogue_serving.edges_max` 700 → **1,250** (Keith, in chat; confirmed on the merge gate). The
   evaluation needs 1,203. At 700, the products served first took the budget and 118 products lost the
   relationships they had. **Fix:** with `preserve_served`, a relationship catalogue serving served in the previous
   build keeps its place, and a newly evaluated, higher-scoring candidate takes only the room left under
   `max_per_saas` (8). Without this, newcomers displaced 17 served recommendations, among them FreshRSS for Feedly,
   Dub for Rebrandly and Medusa for Ecwid.
6. **Manual cycle** (`cycle --trigger manual`), so the run records the observability tests read match the data
   (the KEI-922 pattern). The cycle admitted 3 daily corpus members (25,013).

## Golden queries (for Keith)

29 entries are new in golden top-5 lists. Read one by one:

- **23 are right answers the labels do not name.** Examples: AliasVault and gopass for 1Password, Hanko for Auth0,
  phpList and BillionMail for Mailchimp, Aspia and CrossDesk for TeamViewer, libredesk, opendesk and bytedesk for
  helpdesk, five password managers, and VersityGW for S3. Labelled in `config/golden_queries.yaml`, obs-golden reads
  recall@5 **0.709** and precision@5 **0.693**, against main's 0.682 and 0.658.
- **6 are wrong.** They come from a weakness in `source_recommend`'s need-to-product matching that is already on
  main, not from wrong recommendations. "Self-hosted password manager for a team" matches Microsoft Teams on
  "team", and on main it already returns Jitsi and Zulip. "API client for testing REST APIs" now also matches Apigee,
  whose new, correct API-gateway recommendations outrank Postman's. The same effect costs KEI-919 `need_unnamed`
  precision: "knowledge base wiki for teams" and "privacy friendly website analytics". Follow-on: need matching.

## Decisions for Keith (merge gate)

1. Merge and deploy to main and the live MCP.
2. `launch.catalogue_serving.edges_max` 1,250, and catalogue serving keeping what it served.
3. The 23 golden labels.
4. Accept KEI-919 `need_unnamed` precision@5 0.694 (floor 0.725), or hold for the need-matching follow-on.

## Residue

- `score-validate` needs the full-lane store (`data/score/`, `data/graph/`), which only the daily cycle has. It fails
  the same way on main, and it is not in CI (KEI-930 residue).
- Need-to-product matching in `source_recommend` (above): follow-on.
- The KEI-929 date-dependent momentum test is flaky.
