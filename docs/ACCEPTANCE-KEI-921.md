# KEI-921 — The Source V2.1 WP4: corpus-wide quick scoring and incremental updates

Status: **implemented and executor-verified**. Method: `docs/QUICK-SCORING.md`. Machine-readable proof: `docs/kei-921/completion.json` and `docs/kei-921/quick-validate.json`.

- **Dependency.** This builds on KEI-919 S1.1 project-level licence eligibility, which is on main (`07f2b0f`, `docs/ACCEPTANCE-KEI-919-S1.1.md`).
- **Data.** Scored against corpus `43c58bea3a8a3226` (9,363 projects). The layer is `quick_version` `62299c79497852dc`.
- **Not done here.** There is no confidence calibration and no scorer redesign; both are V3.

## Criterion by criterion

| Acceptance criterion | Evidence | Result |
|---|---|---|
| ≥95% of the eligible corpus has quick Health | `quick-validate` `eligible_health_coverage_ge_95pct`: **7,990 / 8,027 = 99.54%**. Eligible = licence recognised (7,971), plus eligibility `eligible` (18) or `conditional` (38). The 37 without a score are repositories the host no longer has (`found: false`). | pass |
| Every detectably paired project has quick Fit | `every_detectable_pairing_has_quick_fit`: pairings are re-detected from scratch and compared, and none is missing a quick Fit. There are 803 pairings across 467 projects, all 467 with quick Fit. Detected by: replace_lane 733, description 88, topic 51 (a pairing can carry more than one). | pass |
| Outputs labelled and searchable/comparable | Every row has `label: quick`. The `every_row_labelled_quick...` check passes. MCP: `quick_scores` on discovery rows in `source_get_project` and `source_search`, `source_search min_quick_health`, quick scores side by side in `source_compare_projects`, and labelled `quick_paired_candidates` in `source_get_alternatives`. Tests: `tests/test_quick_score.py::test_discovery_projects_carry_quick_scores`, `test_search_filters_by_quick_health`, `test_compare_puts_quick_scores_side_by_side`. | pass |
| Zero quick-score-only recommendations | Every quick row has `recommendation_eligible: false` and `publish: false`, and the MANIFEST counts 0 eligible. `zero_quick_only_recommendations_canonical`: every recommended canonical relationship has a full Replacement Fit row. `read_api_v1_carries_no_quick_scores` passes. The MCP loader refuses a layer with any row claiming eligibility (`test_validate_refuses_a_row_claiming_recommendation`). For every served SaaS with quick pairings, no quick-paired project appears in `recommendations` (`test_quick_pairings_never_become_recommendations`). | pass |
| Repeat run incremental and cost-bounded | A repeat run on unchanged inputs rescored 0 of 9,319 health rows and 0 of 803 pairings, and `health.jsonl` was byte-identical. When one project's facts changed, exactly 1 row was rescored (`test_repeat_run_is_incremental_and_byte_stable`). API calls are 0 by construction. Runtime is about 8 s for a full run. The rescore and runtime caps defer excess work, keep the previous scores and report it (`test_cap_defers_work_and_keeps_previous_scores`). Runtime, API calls, rescored, deferred and failures are in `MANIFEST.last_run`, `data/quick/runs.jsonl` and the corpus-refresh job summary. | pass |
| No confidence calibration until V3 | `confidence_meaning` on every row says "not calibrated", and the MANIFEST has `calibration: none (V3)`. | pass |

## Scope items

- **S2.1 repository-fact quick Health with confidence and a clear quick label.** `source_pipeline/quick_score.py` `score_health`.
- **S2.2 infer SaaS pairings from topics, descriptions and the Replace lane.** `detect_pairings` reuses the deterministic `intake.find_evidence` rule plus `data/dataset/relationships.jsonl`.
- **S2.3 lightweight quick Fit on detected pairings.** `score_fit`.
- **S2.4 no recommendation or publication from quick scores.** The labels, `quick-validate`, the MCP loader refusal, and separate labelled lists in the read surface.
- **S2.5 incremental rescoring with runtime, API quota and failure visibility.** Fingerprints, caps, deferral, the run record, and the corpus-refresh workflow step. CI runs `quick-validate`.

## Executor test evidence

- `tests/test_quick_score.py`: 18 passed.
- Full suite: 519 passed, 5 skipped, 5 failed before one fix.
  - The 1 failure in this change was `test_reads_only_the_read_layer_and_never_the_network`. `data/quick/` has been added to the published read layers, and the test passes.
  - The remaining 4 failures are environmental: they need gitignored local inputs that a fresh checkout lacks (`raw/` pinned upstream checkouts, `config/discover_seeds.md`). 3 of them fail identically on untouched main `9db492d` on the same machine.

## Residue (NON_BLOCKING)

- **Pairings outside the canonical SaaS set.** 177 of the 406 discovery-tier quick pairings name catalogue SaaS products that are not in the canonical SaaS set, so `source_get_alternatives` cannot resolve those products. The pairings are still served through `source_get_project`, `source_search` and `source_compare_projects`.
- **Live MCP deploy is gated.** The live MCP host serves quick scores only after a Keith-gated deploy of this commit (install.sh now sparse-checks-out `data/quick`). Until then, the layer exists on main and in CI.
- **Uncalibrated scores.** Quick Fit bands lean Strong (685 of 803), because most pairings carry replace-lane evidence. That is expected of an uncalibrated quick score, and it is why quick scores never recommend. Calibration is V3.
