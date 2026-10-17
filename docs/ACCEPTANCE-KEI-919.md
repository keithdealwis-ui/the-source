# KEI-919 — The Source V2.1 WP2: correctness and eligibility gate

Status: **implemented and verified by the executor; awaiting independent verification and the gated
landing.** Branch `kei-919-wp2-correctness`, built on WP1 (KEI-918, `a282fb31`).

Outcome: remove obvious lookup/recommendation/discovery errors quickly and unblock flagged projects
without relaxing publication controls.

| | |
|---|---|
| Canonical dataset | `533f675629469d6e` (run `manual-20261017T071711Z-manual-a1`, succeeded, committed with its run record and intake report) |
| Corpus | `a041a3db9d4c296e`, 9,363 projects (`projects.jsonl` sha256 `e7c5c7af…07b7f5`) |
| Full recommendation gate | unchanged: thresholds, scorer, launch selection and export guard untouched |
| CI-equivalent run | build, canonical/momentum/radar/corpus-validate all pass; rebuild == committed; CI pytest set 268 passed; extension 29 passed |

## Acceptance criteria

| Criterion (ticket) | Result | Evidence |
|---|---|---|
| Golden-set precision@5, recall@5 and constraints reported; baseline-calibrated targets | **Reported, targets met.** WP2 set (`config/golden_kei919.yaml`, 36 need queries + 8 constraint + 5 nonsense + 12 lookups + 20 supported products): named-need P@5 1.000 / R@5 0.960 (baseline 1.000 / 0.960); unnamed-need P@5 0.781 / R@5 0.775 (baseline 0.756 / 0.750); constraint violations 0. WP1 S0.4 set (48 queries) `obs-golden --check` passes: P@5 0.7801 (baseline 0.7783), R@5 0.7122 (baseline 0.7141) | `docs/kei-919/golden-baseline.json`, `docs/kei-919/golden-wp2.json`, `scripts/kei919_golden.py`, `python -m source_pipeline obs-golden --baseline observability/golden-baseline.json --check` |
| Twenty / Mattermost / Rocket.Chat / Bitwarden eligible where justified | **All four conditionally eligible (open core), justified by their licence files.** Core AGPL-3.0 / AGPL-3.0 / MIT / AGPL-3.0; excluded `/* @license Enterprise */`, `server/enterprise`, `apps/meteor/ee` + `ee`, `bitwarden_license`. Served in discovery with the condition; never a recommendation without an edition check | `docs/ACCEPTANCE-KEI-919-S1.1.md`, `tests/test_kei919_correctness.py::test_named_open_core_projects_are_conditionally_eligible` |
| No integrations among top five discovery results for supported products | **0 of 20 products** (baseline 37 integrations in the top fives), audited by an independent name/description check | golden `integrations_in_top5` |
| Existing full recommendation gate unchanged | **Unchanged.** S1.5 adds a fail-closed hold only (9 contradictory-evidence items); no published api/v1 page changed its recommendations | `git diff 23c313f -- api/v1` (versions and timestamps only) |

## What changed, by sub-scope

**S1.2 lookup.** `owner/repo` with no host, bare names, URLs and `.git` forms resolve across every host
the layer holds, in get_project, momentum, compare and relationship. A bare organisation resolves to its
one repository, its eponymous one, or its sibling primary (`bitwarden` → `bitwarden/server`); otherwise
it lists them. Lookups 5/12 → 12/12.

**S1.3 no padding; explicit constraint filtering.** A hit must match at least half of the query's content
words with one in a field that says what the project is (or be an exact name or graph match);
stopword-only queries match nothing. Product-driven discovery requires an explicit alternative claim
("open-source Trello alternative", topic `postman-alternative`) or every category word. Responses carry
`shortfall`, `weak_matches_excluded` and `constraint_filtered` (before/after counts). Padding rows on
nonsense queries 18 → 0.

**S1.4 integrations excluded.** MCP servers, SDKs, bots, API clients, plugins/skills, and non-products
(awesome lists, templates, boilerplates) are excluded from replacement candidates (`excluded` reasons
`integration_not_replacement`, `not_a_product`). A project that claims to be an alternative is judged on
its name and description, not on an `mcp` tag (Paca, EdgeEver, HitKeep stay); one that "integrates with"
the product is not its replacement.

**S1.5 rejection codes and review queue.** Every rejection carries one code from a closed enum
(`docs/REJECTIONS.md`; unknown codes fail the build and the schema): saas_not_in_catalogue 197,
licence_not_open 136, unmaintained 51, not_a_replacement 39, not_a_repository 21, archived 18,
not_found 13, identity_unverified 5, host_unverifiable 3. Licence claims contradicted across 3+ sources go
to `data/dataset/review_queue.jsonl` (9 open: forgejo, cal.diy, chatwoot, vaultwarden, espocrm, joplin,
nocodb, rudder-server, tooljet; 27 relationships held) until a human records a resolution in
`config/evidence_review.yaml`. No confidence calibration or scorer change.

**S1.6 radar stars.** Radar-only repositories carry a dated star reading from one batched GitHub request
per run (`data/radar/stars.jsonl`); never estimated. Ranked with stars 0/50 → 49/50 (the one left is a
GitHub advisory URL, not a repository). `source_emerging_projects` uses the reading when momentum has none.

**S1.1 licence classification / S1.7 provenance.** All 1,393 flagged rows classified at project level
(open 18 eligible; open_core 38 conditional; source_available 304, missing 554, restricted 25, unclear 453
not eligible; fail closed on unclear rights). Each conclusion carries source, commit and checked_at.
Edition checks are recorded as a required follow-up for open-core pairings heading to recommendation, not
performed (non-goal). Self-hosting evidence recorded, never blocking. Full proof:
`docs/ACCEPTANCE-KEI-919-S1.1.md`, `docs/kei-919/s1.1-completion.json`.

**S1.8 identity.** 61 renames (both names resolve), 23 mirrors (excluded from discovery), 21 sibling
groups (46 repositories), 0 forks; `github.com/baserow/baserow` collapsed into canonical
`gitlab.com/baserow/baserow`.

Also fixed: `source_get_relationship` crashed on a pairing with no Replacement Fit; the WP1 intake-report
test pinned a fixed run id and would have failed after the next cycle.

## Residue (NON_BLOCKING)

- **Decision for Keith:** the 9 review-queue holds (some look like stale list entries, e.g. forgejo listed
  MIT, files GPL-3.0). Two WP1 golden queries lose recall while held (alt-github, alt-calendly).
- Mattermost and Rocket.Chat are eligible but not retrieved for Slack (their descriptions never name
  Slack or team chat); pairing inference is WP4 S2.2 (KEI-921).
- Unnamed-need product matching still mis-seeds a few queries ("knowledge base wiki for teams" → Zoom/
  Slack seeds); unchanged from baseline, scorer territory (V3 non-goal).
- Sibling detection is a conservative name rule; trees are read 3 levels deep for licence classification.
- `tests/test_pipeline.py::test_every_built_edge_traces_to_a_pinned_commit` reads the intake source's
  placeholder pin from YAML instead of `common.load_sources`; fails on `main` too (not in CI).
- `github.com/advisories/...` should be a reserved radar owner (methodology version bump).

## Reproduce

```
python -m source_pipeline build && python -m source_pipeline canonical-validate
python -m source_pipeline corpus-validate && python -m source_pipeline radar-validate
python scripts/kei919_golden.py
python -m source_pipeline obs-golden --baseline observability/golden-baseline.json --check
python -m pytest tests/test_kei919_correctness.py tests/test_licence_classify.py tests/test_identity.py tests/test_rejection_codes.py -q
```
