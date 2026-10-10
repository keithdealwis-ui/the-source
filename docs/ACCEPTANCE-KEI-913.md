# KEI-913 acceptance map: emerging GitHub projects from X and Instagram buzz

Mapped to the six acceptance criteria in the ticket's Detail block. Methodology:
[`RADAR.md`](RADAR.md) (version `1.1.0`). Feasibility spike: [`RADAR-SOCIAL.md`](RADAR-SOCIAL.md).

- **Code:** `source_pipeline/radar.py` (collectors `_bluesky`, `_x`, `_instagram`; `author_hash`;
  `merge_mentions` observations; `points_per_day`; ranking signals; `_candidates`),
  `source_pipeline/enrich.py` (`Http.call(token=)`), `source_pipeline/cli.py` (`radar --candidates`),
  `config/radar.yaml`, `schema/the-source.radar.schema.json`, `.github/workflows/daily-cycle.yml`
  (inert secret wiring).
- **Tests:** `tests/test_radar_social.py` (14) and `tests/test_radar.py` (24, adapted), run with the
  network refused; fixtures in `tests/fixtures/radar/` follow each operator's documented response
  shape with synthetic content.
- **Data:** `data/radar/` rebuilt under 1.1.0 from the held mentions (`radar-validate` 11/11,
  byte-identical rebuild); `data/radar/candidates.jsonl` is new (30 rows).
- **Live:** the Bluesky collector was run against the real endpoint on 2026-10-10 15:21Z from the
  attended session: 98 posts listed, 69 mentions kept, 66 distinct authors, 6 HTTP calls, no
  content stored. Nothing was written to the repository by that run.

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | A bounded feasibility spike documents lawful access method, reliability, source coverage, rate limits and zero new spend | Met | [`RADAR-SOCIAL.md`](RADAR-SOCIAL.md): per source, the documented interface, credential, cost, rate limits, discovery/resolution capability and verdict, each with the operator's page read on 2026-10-10. Finding: X reads are pay-per-use ($0.005 a post, card required, no free tier since 2026-02); Instagram needs a professional account and Meta App Review, and its display-only oEmbed route forbids this use; no lawful key-less path exists on either. Bluesky's documented public search is lawful, key-less and free. **Zero new spend holds: nothing here spends, grants or publishes.** |
| 2 | Viable source adapters preserve provenance and canonical GitHub URL resolution from supported source material | Met | Three adapters under one contract. Each mention keeps the item id, its own permalink, published and observed times, public points and comments, the query/hashtag that surfaced it and the repository addresses it links; link text is normalised to `host/owner/name` (case, `.git`, deep paths, fragments) and github.com addresses are resolved to the repository id through `GET /repos/{owner}/{name}` so renames reconcile (`test_bluesky_reads_links_and_opaque_authors_never_text_or_handles`, `test_x_and_instagram_collect_under_their_credentials_and_store_no_content`). Bluesky is live; X and Instagram are complete, exercised against their documented shapes, and `unconfigured` until Keith grants a credential (`test_unconfigured_sources_leave_the_strategy_ok_and_say_so_in_retrieval`). |
| 3 | New and existing repositories reconcile deterministically against the 9,364-project corpus, with spam/repost suppression | Met | Reconciliation is KEI-850's: by key, harvested name, current name and host repository id against `data/dataset`, `data/live`, `data/canonical` and the pool; `radar-validate` proves the rebuild byte-identical. A known project is tracked as an observation (status `canonical`/`corpus`), never re-admitted (`test_cycle_step_with_social_sources_reconciles_corroborates_and_admits`). Suppression: X's query excludes reposts and replies at the source; a later mention by the same author on the same source counts 0.25 (`test_three_independent_authors_outrank_one_author_posting_three_times`); per-source like floors; at most 3 repositories per item; one mention per (source, item). |
| 4 | Daily, auditable 20–30 candidate ranking scores source-specific attention velocity, independent authors and cross-source corroboration; Community Momentum remains separate from quality, health and repository momentum | Met | `data/radar/candidates.jsonl`: the strongest `max_candidates` (30) repositories new to The Source, each with rank, radar rank, sources, `signals` (`authors`, `points_per_day`, `corroborating_sources`) and an explanation that restates the arithmetic; rebuilt byte for byte by `radar-validate` (`candidates_are_new_to_the_source_ranked_and_capped`). Velocity: `points_per_day` from kept observations (`test_a_rising_mention_outscores_a_static_one_with_the_same_points`). Authors: truncated hash of the opaque account id, counted per source. Corroboration: +25% per extra source, capped at three (`test_corroboration_bonus_grows_per_source_and_is_capped`). Separation: the radar feeds nothing into Replacement Fit, Project Health or Repository Momentum; the MCP server keeps `community_momentum` a separate measure (`source_get_momentum`, `source_emerging_projects`). Live list today: 30 rows, HN-sourced (the committed mentions predate the new sources). |
| 5 | Real test fixtures demonstrate new and existing repositories, historical signals and two-source corroboration | Met, with a residue | `tests/fixtures/radar/{bluesky_searchPosts,x_search_recent,instagram_hashtag_search,instagram_hashtag_top_media}.json`, shaped exactly as the operators document (post ids, `public_metrics`, `entities.urls`, facets and external embeds, `caption`/`permalink`/`timestamp`). They carry a new repository (`fresh/social-thing`), an existing canonical one (`old-org/tool`), a same-author repeat, an under-floor post, a stale post, a non-repository link, and a second-day re-observation that produces velocity; the end-to-end test proves four-source corroboration and pool admission. **Residue (NON_BLOCKING):** the fixtures' content is synthetic by design (no real person's post is stored in the repository); the live adapter proof is the Bluesky run above. X and Instagram cannot be run live without the credentials Keith has not granted. |
| 6 | Genuinely new/high-signal candidates enter the existing Source enrichment/daily-refresh pipeline; results are exposed through existing retrieval/MCP only where supported | Met | Admission is unchanged and now fed by every source: a resolved, existing, unarchived, non-fork repository scoring at least 4.0 joins `data/discovery/candidates.jsonl` with `provider: community_radar` and `query:` the sources that mentioned it (`bluesky+hn+instagram+x` in the test); the Discover lane takes it from there. The daily cycle's step 13 runs the new collectors with the same guard (a failing radar is rolled back). Retrieval: `python -m source_pipeline radar --candidates [--source S] [--limit N]`; `radar.retrieve(candidates=True)`; `source_status` per source including `unconfigured`. MCP: the existing `source_get_momentum` and `source_emerging_projects` tools serve the new sources through `radar.jsonl` without a schema change; no new tool was added, as the ticket asked. |

## Constraints the ticket set

- **No new spend, credential grant, infrastructure change, publication or external action.** None
  taken. The X and Instagram adapters read their credentials from environment variables that the
  daily-cycle workflow maps from repository secrets that do not exist; until Keith creates one, each
  source reports `unconfigured` and makes no call. The decision to fund X (about $15 a month at one
  query a day) or to apply for Instagram access is recorded in `RADAR-SOCIAL.md` and left to Keith.
- **KEI-912 untouched.** No file under `data/corpus/`, `config/corpus.yaml` or
  `source_pipeline/corpus.py` changed.
- **Nothing a person wrote is stored.** The schema refuses text, caption and username fields and
  any `author` that is not a 16-hex hash (`test_the_schema_refuses_a_mention_that_carries_a_username_or_text`);
  `radar-validate`'s `mentions_hold_no_personal_or_authored_content` check covers the committed data.

## Residues

- **X and Instagram dormant** until a Keith decision on credential and spend (NON_BLOCKING; see above).
- **Bluesky's documented public host** (`public.api.bsky.app`) answered 403 to the search method on
  2026-10-10; the collector falls back to the AppView the Bluesky app itself uses, which answered
  without authentication. If Bluesky closes unauthenticated search, the source degrades and the
  radar carries on (`test_bluesky_failing_on_every_host_is_a_source_failure_not_a_crash`).
- **`tests/test_momentum.py::test_cycle_step_on_the_committed_history_plus_a_new_day`** failed on
  untouched `main` (a1b62b6) before this branch: the 2026-10-10 history snapshot was rewritten by a
  manual cycle at 14:28Z and the test placed its synthetic next day at 23:40Z of the same date, under
  the momentum `min_span_ratio`. Daily-cycle pushes do not trigger CI, so `main` showed green.
  Fixed on this branch in a separate, test-only commit that places the synthetic day 24 h after the
  last snapshot. Unrelated to KEI-913; noted so the CI result is explicable.
