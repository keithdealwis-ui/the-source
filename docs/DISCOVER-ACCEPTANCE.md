# KEI-811 — acceptance map

Corpus `as_of` 2026-09-29T10:34:38Z (verification clock); frontier `as_of` 2026-09-29T07:26:33Z.
Validation: **28 of 28 checks pass**
(`data/discover/corpus/reports/validation_report.json`). Status: internal, not published.

## Headline counts

| | |
|---|---|
| raw candidate addresses discovered | 14,335 |
| addresses evaluated (cap 12,000) | 12,000 |
| **raw unique repositories** (resolved by host id, plus unresolved addresses) | **14,301** |
| resolved unique repositories | 11,937 |
| **retained** | **9,364** |
| of which **recommendation-eligible** (full KEI-805 bar) | **7,971** |
| of which licence-exception lane (retained, not eligible) | 1,393 |
| **rejected** repositories (records) | **2,573** (4,938) |
| lanes: replace only / discover only / both | 302 / 8,878 / 184 |
| maintenance of retained: active / maintained | 7,248 / 2,116 |
| relationship candidates | 138,940 |

### Candidates by strategy (an address reached by several routes counts once per strategy)

| strategy | candidates |
|---|---|
| `curated_list` | 4,767 |
| `keith_seed` | 72 |
| `owner_neighbourhood` | 2,939 |
| `readme_links` | 920 |
| `replace_lane` | 582 |
| `topic_search` | 5,036 |
| `trending` | 1,974 |

### Rejection reasons (a record may carry more than one)

| reason | records | meaning |
|---|---|---|
| `archived` | 339 | archived by its owner |
| `discovery_budget_exceeded` | 2,335 | discovered past raw_universe_cap; not evaluated |
| `empty_repository` | 10 | the repository has no content |
| `fork_alias` | 30 | a fork; recorded as an alias of its parent, not as a project of its own |
| `host_not_independently_verifiable` | 3 | hosted where the pipeline cannot verify it |
| `kei805_rejected` | 104 | rejected in the KEI-805 Replace dataset; its reasons are carried over |
| `no_recognised_open_source_licence` | 300 | licence not recognised; retained in the licence-exception lane only if nothing else is wrong |
| `repository_identity_changed_since_listed` | 5 | renamed into something its listings never described (KEI-805) |
| `repository_not_found` | 41 | the host has no repository at this address |
| `seed_unresolved` | 1 | a seed row that names no repository |
| `stale_no_meaningful_activity_12_months` | 2,410 | no meaningful activity within 12 months (config/policy.yaml) |

## Criteria

**AC1 — Replace lane intact and identifiable.** The KEI-805 dataset is read, never
written: its six files hash exactly as pinned before KEI-811 began
(`data/discover/kei805_baseline.sha256`). Every KEI-805 project that KEI-805 retained is
in the corpus with `lanes.replace = true`, `replace.kei805_oss_id`, its KEI-805 verdict
carried as `quality.basis = "kei805"`, and its SaaS edges as `saas_replacement_candidate`
relationship candidates. Checks: `kei805_dataset_unchanged` PASS, `replace_lane_complete` PASS, `replace_lane_verdicts_carried_not_recomputed` PASS.

**AC2 — Discover lane without a SaaS edge.** 8,878 retained
projects are in the Discover lane only, none with a SaaS edge; `replace` is null on
them. Check: `discover_lane_accepts_without_saas_edge` PASS.

**AC3 — Keith's examples as seeds, no per-project ingestion.** `config/discover_seeds.md`
is the ticket's table pasted verbatim; the harvest parses every GitHub URL in it (and any
pasted later). GitHub accounts in `config/discover.yaml` (`keithdealwis-ui`) are read on
every harvest; that account currently has
0 starred and
0 watched
repositories. All 73 seed rows are accounted for below. Checks:
`every_seed_row_accounted_for` PASS, `seed_file_is_the_input` PASS.

**AC4 — Recursive, bounded, reproducible discovery.** Seven strategies, each bounded
in `config/discover.yaml`; depth-2 recursion from the 250
depth-1 candidates reached by at least two independent routes. Every strategy's query,
status, pinned commit and licence decision is in `sources.json`; everything returned is
in `data/discover/frontier.json`, from which the build recomputes the universe.
Curated lists used: Shubhamsaboo/awesome-llm-apps, VoltAgent/awesome-agent-skills, punkpeye/awesome-mcp-clients, punkpeye/awesome-mcp-servers, steven2358/awesome-generative-ai, wong2/awesome-mcp-servers. Refused (no recognised licence):
ComposioHQ/awesome-claude-skills, e2b-dev/awesome-ai-agents, hesreallyhim/awesome-claude-code. Checks: `recursion_bounded` PASS, `multiple_strategies_used` PASS, `frontier_matches_config` PASS.

**AC5 — ~10,000 candidates without lowering the bar.** 11,937
unique repositories evaluated, resolved by host id, out of 14,335
candidate addresses discovered. The quality bar is `config/policy.yaml` unchanged, applied by
the KEI-805 code. Metadata rejections are only those the policy already makes sufficient
(archived, disabled, empty, no push inside the window, fork/mirror alias, not found).
Everything else is inspected in full. The one scale measure — stop inspecting once the
evidence already in hand decides the result — is proven equivalent by
`tests/test_discover.py::TestEarlyStop`, is off by default, and leaves KEI-805's own
behaviour and tests unchanged. Checks: `raw_universe_order_10k` PASS, `quality_bar_not_lowered` PASS, `quality_policy_is_kei805_policy` PASS, `no_lookup_left_unexplained` PASS.

**AC6 — Provenance for every retained project.** Each record's `provenance` lists every
route that reached it (strategy, source, depth, via, locator) and `entry_reason` says
why in one line. Check: `provenance_for_every_retained` PASS.

**AC7 — Deterministic de-duplication.** Records are keyed by the host's numeric
repository id, so renamed, transferred and differently-cased addresses resolve to one
record with every address in `aliases`. Forks and mirrors are rejected as aliases with
their parent recorded. Checks: `unique_ids` PASS, `each_address_one_record` PASS, `retained_and_rejected_disjoint` PASS, `forks_point_to_parent` PASS.

**AC8 — Machine-readable rejection reasons.** `rejections.jsonl`: reason codes from a
fixed vocabulary (meanings in `summary.json`), the stage, the routes and the evidence.
Nothing evaluated disappears: retained + rejected = raw. Checks:
`rejection_reasons_known` PASS, `every_candidate_accounted_for` PASS.

**AC9 — Graph-ready records.** `schema/the-source.discover.schema.json`; stable ids;
`relationship_candidates.jsonl` (138,940 candidates:
discovered_via_topic 5,505, has_topic 86,518, listed_in 4,106, owned_by 9,364, readme_links_to 497, saas_replacement_candidate 730, written_in 32,220);
`nodes.jsonl` for owners, topics, languages, lists and SaaS products. Canonical entity
and edge resolution is left to KEI-844. Checks: `schema_valid_projects` PASS, `schema_valid_rejections` PASS, `schema_valid_relationships` PASS, `relationship_endpoints_resolve` PASS, `records_have_stable_ids_and_entities` PASS.

**AC10 — Appendable history.** `discover-verify` appends one row per repository per
run to `data/discover/history/observations.jsonl` and never rewrites one; records carry
the series in `freshness.history`. Tested by `TestHistory` and
`test_history_is_appended_not_overwritten`. Check: `freshness_history_present` PASS.

**AC11 — Determinism.** `discover-build` reads only recorded files, uses the snapshot's
clock, and refuses an interrupted, re-policied or mismatched snapshot. The verifier pack
rebuilds twice offline (network refused) and compares bytes with `EXPECTED_DISCOVER.json`.

**AC12 — No publication or endorsement.** Every record carries `endorsed: false` and
`publication_status: internal_not_published`; nothing was published or exported.
Check: `no_endorsement_or_publication` PASS.

## Keith's seed corpus, row by row

| line | name | repository | outcome | detail |
|---|---|---|---|---|
| 13 | Prime Agent | `primeintellect-ai/prime-agent` | recommendation_eligible | active / recommendable |
| 14 | Omnigent | `omnigent-ai/omnigent` | recommendation_eligible | active / recommendable |
| 15 | LibreChat | `danny-avila/librechat` | recommendation_eligible | active / recommendable |
| 16 | Cap | `capsoftware/cap` | retained | active / licence_exception |
| 17 | Harper | `automattic/harper` | recommendation_eligible | active / recommendable |
| 18 | OpenSEO | `every-app/open-seo` | recommendation_eligible | active / recommendable |
| 19 | SEOMonster | `avansaber/seo-monster` | recommendation_eligible | active / recommendable |
| 20 | DispatchSEO | `neozi12/dispatchseo` | recommendation_eligible | active / recommendable |
| 21 | Ian Nuttall SEO | `iannuttall/seo` | recommendation_eligible | active / recommendable |
| 22 | SEO Agent | `citeworthyio/seo-agent` | recommendation_eligible | active / recommendable |
| 23 | SE Ranking SEO Skills | `seranking/seo-skills` | recommendation_eligible | maintained / recommendable |
| 24 | Google SEO Skill | `mikezupper/google-seo-skill` | rejected | no_recognised_open_source_licence, stale_no_meaningful_activity_12_months |
| 25 | Claude SEO GEO | `thibaultbm/claude-seo-geo` | recommendation_eligible | active / recommendable |
| 26 | GEO SEO AEO Skill | `staksoft/geo-seo-aeo-skill` | recommendation_eligible | maintained / recommendable |
| 27 | Backlink Skills | `flaqai/backlink_skills` | recommendation_eligible | active / recommendable |
| 28 | Claude SEO | `agricidaniel/claude-seo` | recommendation_eligible | active / recommendable |
| 29 | Web Quality Skills | `addyosmani/web-quality-skills` | recommendation_eligible | active / recommendable |
| 30 | Google Analytics MCP | `googleanalytics/google-analytics-mcp` | recommendation_eligible | active / recommendable |
| 31 | Chrome DevTools MCP | `chromedevtools/chrome-devtools-mcp` | recommendation_eligible | active / recommendable |
| 32 | Marketing Skills | `coreyhaines31/marketingskills` | recommendation_eligible | active / recommendable |
| 33 | LinkedIn Skills | `sergebulaev/linkedin-skills` | recommendation_eligible | active / recommendable |
| 34 | X Skills | `sergebulaev/x-skills` | recommendation_eligible | active / recommendable |
| 35 | Social Media Skills | `social-media-skills/skills` | recommendation_eligible | active / recommendable |
| 36 | Instagram Agent Skill | `jakeschincariol/instagram-agent-skill` | recommendation_eligible | active / recommendable |
| 37 | Instagram MCP | `thenavidm/instagram-mcp` | recommendation_eligible | active / recommendable |
| 38 | Instagram MCP — Meta API | `adelaidasofia/instagram-mcp` | recommendation_eligible | active / recommendable |
| 39 | Postiz | `gitroomhq/postiz-app` | recommendation_eligible | active / recommendable |
| 40 | Meta Scheduler | `arillera/meta-scheduler` | recommendation_eligible | maintained / recommendable |
| 41 | Instagram Scheduler | `gayathriv-builds/instagram-scheduler` | retained | maintained / licence_exception |
| 42 | AutoSocial | `katzca/autosocial` | recommendation_eligible | maintained / recommendable |
| 43 | Hypit | `hypit-ai/hypit` | retained | active / licence_exception |
| 44 | video-use | `browser-use/video-use` | recommendation_eligible | active / recommendable |
| 45 | X Algorithm | `xai-org/x-algorithm` | recommendation_eligible | active / recommendable |
| 46 | X Algorithm Skill | `attainmentlabs/x-open-source-algorithm-skill` | rejected | stale_no_meaningful_activity_12_months |
| 47 | X Algorithm Playbook | `tang-vu/x-algorithm-playbook` | recommendation_eligible | active / recommendable |
| 48 | Everything Claude Code (ECC) | `affaan-m/ecc` | recommendation_eligible | active / recommendable |
| 49 | Codex Plugin CC | `openai/codex-plugin-cc` | recommendation_eligible | active / recommendable |
| 50 | Orca | `stablyai/orca` | recommendation_eligible | active / recommendable |
| 51 | Zoetrope | `furkankly/zoetrope` | recommendation_eligible | active / recommendable |
| 52 | OpenHuman | `tinyhumansai/openhuman` | recommendation_eligible | active / recommendable |
| 53 | Shannon | `keygraphhq/shannon` | recommendation_eligible | active / recommendable |
| 54 | EvoMap / Evolver | `evomap/evolver` | recommendation_eligible | active / recommendable |
| 55 | Lossless Claw | `martian-engineering/lossless-claw` | recommendation_eligible | active / recommendable |
| 56 | OpenClaw | `openclaw/openclaw` | recommendation_eligible | active / recommendable |
| 57 | Paperclip | `syntax-syndicate/paperclip-agent-orchestration` | rejected | fork_alias |
| 58 | Agent Reach | `panniantong/agent-reach` | recommendation_eligible | active / recommendable |
| 59 | Scrapling | `d4vinci/scrapling` | recommendation_eligible | active / recommendable |
| 60 | agenticSeek | `fosowl/agenticseek` | recommendation_eligible | active / recommendable |
| 61 | QMD | `tobi/qmd` | recommendation_eligible | active / recommendable |
| 62 | Treg | `superdesigndev/treg` | retained | active / licence_exception |
| 63 | Atlas | `pacifico/atlas` | rejected | repository_not_found |
| 64 | Cloudflare Security Audit Skill | `cloudflare/security-audit-skill` | recommendation_eligible | active / recommendable |
| 65 | Agentic Inbox | `cloudflare/agentic-inbox` | recommendation_eligible | maintained / recommendable |
| 66 | OpenBB | `openbb-finance/openbb` | retained | active / licence_exception |
| 67 | TradingAgents | `tauricresearch/tradingagents` | recommendation_eligible | active / recommendable |
| 68 | Fincept Terminal | `fincept-corporation/finceptterminal` | retained | active / licence_exception |
| 69 | MoneyPrinterTurbo | `harry0703/moneyprinterturbo` | recommendation_eligible | active / recommendable |
| 70 | HyperFrames | `heygen-com/hyperframes` | recommendation_eligible | active / recommendable |
| 71 | VoxCPM | `openbmb/voxcpm` | recommendation_eligible | active / recommendable |
| 72 | openGym | `duartesantos8/opengym` | recommendation_eligible | active / recommendable |
| 73 | Documenso | `documenso/documenso` | recommendation_eligible | active / recommendable |
| 74 | Coolify | `coollabsio/coolify` | recommendation_eligible | active / recommendable |
| 75 | F.R.I.D.A.Y | `saravanaspar/f.r.i.d.a.y` | recommendation_eligible | active / recommendable |
| 76 | Career Ops | `career-ops-hq/career-ops` | recommendation_eligible | active / recommendable |
| 77 | Skills for Real Engineers | `mattpocock/skills` | recommendation_eligible | active / recommendable |
| 78 | PWN Pentest Agent Suite | `h-mmer/pentest-agents` | retained | maintained / licence_exception |
| 79 | AI Engineering Hub | `patchy631/ai-engineering-hub` | recommendation_eligible | active / recommendable |
| 80 | SwarmLLM | `nehanth/swarmllm` | recommendation_eligible | active / recommendable |
| 81 | SOLVENT | `ianalloway/solvent-agent` | recommendation_eligible | active / recommendable |
| 82 | consulting-deck | `zairuilab/consulting-deck` | retained | active / licence_exception |
| 83 | ppt-master | `hugohe3/ppt-master` | recommendation_eligible | active / recommendable |
| 84 | Facebook Ads Library MCP | `ramsesaguirre777/facebook-ads-library-mcp` | recommendation_eligible | active / recommendable |
| 85 | Meta advertising skills | `-` | rejected | seed_unresolved |
