# Community Radar (KEI-850, KEI-913)

Methodology `the-source.radar`, version `1.1.0`. Parameters: `config/radar.yaml`.
Code: `source_pipeline/radar.py`. Schema: `schema/the-source.radar.schema.json`.

The radar shows which repositories people are discussing right now on Hacker News, on
Forem (dev.to) and on Bluesky, and on X and Instagram once Keith grants a credential for
either ([`RADAR-SOCIAL.md`](RADAR-SOCIAL.md)). For each one it says whether The Source already holds it (canonical or in
the corpus), already has it as a discovery candidate, or has not seen it before. It is a
signal to look closer, not a recommendation. It never changes the canonical dataset, its
version or `api/v1/`.

## Sources, and why they are lawful to use

| Source | Interface | Terms | What is asked |
|---|---|---|---|
| Hacker News | The official API, `https://hacker-news.firebaseio.com/v0` ([docs](https://github.com/HackerNews/API)): "we're making the public Hacker News data available"; no key; "there is currently no rate limit". | The [Y Combinator terms](https://www.ycombinator.com/legal/) prohibit scraping and data mining of the *site* and access "through any means not intentionally made available". The API is the means YC made available. The radar never fetches an HTML page. | `showstories`, `beststories`, `topstories` (the first 150 ids of each), then each distinct story, at most 300 a day. |
| Forem (dev.to) | The public Forem v1 API, `https://dev.to/api` ([docs](https://developers.forem.com/api/v1)): published-article endpoints need no key. | The [DEV terms](https://dev.to/terms) restrict copying and redisplaying the site's *materials* (articles). The radar uses the documented API and redisplays nothing an author wrote. | The most popular articles of the last 7 days for each of `opensource`, `selfhosted`, `showdev` and `github` (30 per tag), then the body of at most 60, read only to find repository links. |
| Bluesky | The documented public search, `app.bsky.feed.searchPosts` on the public AppView ([endpoints.bsky.app](https://endpoints.bsky.app/): "Most `app.bsky.*` GETs are public and can be called without authentication"), with its `domain` filter; no key. | The AT Protocol is open and posts are public by design; the public AppView exists for exactly this kind of read. If the documented public host refuses the search method (it answered 403 on 2026-10-10), the AppView the Bluesky app itself uses is asked instead; nothing else is tried. | One page of up to 100 posts of the last 7 days that link `github.com`, most-liked first, for the query `github`. |
| X | The X API v2 recent search, `GET /2/tweets/search/recent` ([docs](https://docs.x.com/x-api/posts/recent-search)), under a bearer token X issues. | X's [developer agreement and policy](https://docs.x.com/developer-terms/policy): use the API as documented, keep stored content current, redistribute only ids. Reads are pay-per-use ($0.005 a post), so this source stays `unconfigured` until Keith funds it ([`RADAR-SOCIAL.md`](RADAR-SOCIAL.md)). | One page of up to 100 posts for `url:"github.com" -is:retweet -is:reply`, a day. |
| Instagram | The Instagram Platform hashtag endpoints, `ig_hashtag_search` and `/{hashtag-id}/top_media` ([docs](https://developers.facebook.com/docs/instagram-api/guides/hashtag-search)), under a token Meta issues for a professional account after App Review. | [Meta platform terms](https://developers.facebook.com/terms/): access as licensed, data kept only while needed. 30 hashtags per account per week. Stays `unconfigured` until Keith applies. | Up to 50 top posts for each of `opensource`, `github` and `selfhosted`, captions read in memory for links only. |
| GitHub | `GET /repos/{owner}/{name}`, authenticated with the cycle's `GITHUB_TOKEN`, under the same terms as the rest of the pipeline (`docs/BUILD-VS-INGEST.md`). | As for the light refresh: aggregate repository metadata only. | One call per linked github.com address not already resolved in the last 30 days, at most 60 a day. |

**What is stored.** A mention keeps only these fields: the item id, the item's own
address, when it was published, when it was observed, its public points (HN score, Forem
positive reactions, Bluesky/X/Instagram likes) and comment count, the list, tag or query
that surfaced it, the repository addresses it links, and, since 1.1.0, two optional fields:
`author`, a 16-character truncation of `sha256("<source>:<opaque account id>")` where the
source exposes one (Bluesky's DID, X's `author_id`; never Hacker News, Forem or Instagram),
and `observations`, the earlier (points, comments) readings of the same item. Titles, text,
captions, article bodies, usernames, handles and profile data are never stored. The schema
forbids any other field (`additionalProperties: false`) and any author that is not a hash,
and `radar-validate` checks the field set separately. Article bodies and captions are read in
memory to find links and then discarded. A Forem article's address contains its author's
handle, and a Bluesky address its account's DID. Those addresses are kept because they are
how a mention is attributed and traced; X addresses use the handle-free `x.com/i/web/status`
form.

**Politeness.** Each source is asked once a day, inside the daily cycle. At most about 340
requests go to the HN API, 100 to Forem (live on 2026-10-06: 303 and 37), 2 to Bluesky, 1 to
X and 6 to Instagram, with fixed caps, the pipeline's own User-Agent and the shared
retry/back-off client. A source whose credential is not set is `unconfigured`: it makes no
call, and the strategy is not degraded by it.

## How a rank is made

1. **Extract.** Each mention's links are read from the HN story URL and text, from the
   Forem article body, from a Bluesky post's link facets and external embed, from an X
   post's `entities.urls` (expanded and unwound), or from an Instagram caption. Each is normalised to `host/owner/name` (lower case, `.git` and
   deep paths dropped, HTML entities decoded) for github.com, gitlab.com and codeberg.org.
   GitHub site pages (`/sponsors`, `/topics`, `/user-attachments`, ...) are not owners. An
   item counts at most 3 repositories, in order of first appearance.
2. **Dedupe.** There is one mention per (source, item id), and the newest observation wins,
   so points grow day by day without duplicating the item. There is one entry per
   repository, however many addresses its mentions used: case, an old name and a
   transferred address all reconcile to one entity (step 4). An item that links two names
   of the same repository counts once.
3. **Score.** Only mentions published in the last `window_days` (7) count. For each one:

   ```text
   velocity     = points gained between the first and the latest observation, per day (interval floored at 1 day; 0 with one observation)
   engagement   = 1.0 x ln(1 + points) + 0.5 x ln(1 + comments) + 0.5 x ln(1 + velocity)
   contribution = source_weight (hn 1.0, forem 0.8, bluesky 0.7, x 0.6, instagram 0.5) x engagement x 0.5^(age_days / 3) / repositories_in_item
                  x 0.25 if the same author already has a stronger mention of this repository on this source
   score        = sum of the 5 strongest contributions x (1 + 0.25 x (corroborating_sources - 1)), corroborating_sources capped at 3
   ```

   The logarithms stop one viral story from swamping everything else, and they put HN
   points, Forem reactions and likes on a comparable scale. The 3-day half-life favours what
   is being discussed now; the velocity term favours what is being discussed *more* than
   yesterday, which is what the kept `observations` are for. Sharing an item's weight between
   the repositories it links stops a list post from outranking a launch. The same-author cut
   is the repost and self-promotion guard: three independent accounts beat one account
   posting three times, and X's query already excludes reposts at the source. Each extra
   source that corroborates adds a quarter, up to three sources, so attention on two
   communities outranks the same attention on one. Entries are sorted by score, with ties
   broken by key. At most `max_ranked` (50) are kept, and the manifest counts the rest
   (`truncated`). Each entry carries `signals`: distinct `authors` (null when no mention
   carries a hash), the fastest `points_per_day` among its counted mentions, and
   `corroborating_sources`.
4. **Reconcile.** Each address is matched to the corpus by key, harvested name, the host's
   current name (light refresh) and host repository id. github.com addresses that do not
   match by name are resolved against GitHub, highest-ranked first, within the daily
   budget, and cached in `data/radar/resolved.jsonl`. A renamed or transferred repository
   therefore reconciles by id and never becomes a second entity. Every entry gets one
   status:

   | Status | Meaning |
   |---|---|
   | `canonical` | In the canonical dataset (`oss_id` given). |
   | `corpus` | In the evaluated corpus but not canonical (e.g. not recommendable). |
   | `candidate` | In the discovery pool (`pool_key` given), found earlier or admitted now. |
   | `not_found` | GitHub answered 404/410/451 for the address. |
   | `unresolved` | Not resolved yet (resolution budget), a gitlab.com/codeberg.org address that matched nothing, or resolved but not admitted to the pool. The `status_reason` says which. |

   A resolved repository that is new to both the corpus and the pool joins the pool
   (`provider: community_radar`, `query:` the sources that mentioned it, joined with `+`). It must exist, must
   not be a fork or archived, and must score at least `pool_min_score` (4.0). At most 20 a
   day join, highest-ranked first. Joining the pool is as far as the radar goes. Entering
   the corpus still needs a licence-cleared relationship claim, as for every discovery
   lane.

Every entry carries its mentions (address, points, comments, age, contribution, whether
it counted, its own `points_per_day` when it has been observed more than once, and
`repeat_author` when it was cut) and an `explanation` that restates the sum, for example
`4 mention(s) (bluesky 3, hn 1; 3 author(s), 1 repeat(s) x0.25; +45.00 points/day); strongest 4 sum 7.1234 x1.25 (2 sources) = 8.9043`.

5. **List the candidates.** `data/radar/candidates.jsonl` is the day's answer to "what new
   should we look at": the strongest `max_candidates` (30) repositories whose status is
   `candidate` or `unresolved`, taken from every repository mentioned (not only the 50
   ranked), with their own rank, their rank on `radar.jsonl` where they have one, their
   sources, signals and explanation. Repositories already canonical or in the corpus are
   observations, never candidates; `not_found` addresses are left out.

## Determinism and versioning

Collection is the only step that needs the network. Ranking, reconciliation and the
candidate list are a pure function of `data/radar/{collection.json,mentions.jsonl,resolved.jsonl}`, the corpus
files (`data/dataset/oss_projects.jsonl`, `data/live/snapshot.json`,
`data/canonical/oss_projects.jsonl`, `data/discovery/candidates.jsonl`) and
`config/radar.yaml`, evaluated at the collection's `as_of`. The manifest records the
sha256 of each input. `radar-validate` rebuilds `radar.jsonl`, `candidates.jsonl` and `MANIFEST.json` and
requires them byte for byte.

`extract`, `ranking` and `reconcile` define what a rank means. They are fingerprinted, and
`methodology.released` maps each version to its fingerprint. Changing one without
releasing a new version fails `radar-validate`, the tests and the cycle. `sources`,
`resolve` and `retain_days` are collection budgets. They change how much is asked, not
what a rank means.

| Version | Date | Change |
|---|---|---|
| `1.0.0` | 2026-10-06 | First release: HN and Forem mentions, log engagement, 3-day half-life, 7-day window. |
| `1.1.0` | 2026-10-10 | KEI-913: Bluesky, X and Instagram sources with their weights; velocity from kept observations; same-author cut; corroboration bonus per source, capped at three; the candidate list (`max_candidates` 30). Existing HN/Forem mentions carry no author and one observation, so their contributions are unchanged. |

## Where it runs and how to read it

The daily cycle runs it as step 13 (`community_radar`), after the canonical build and the
history snapshot, so it reconciles against the corpus that cycle is about to promote.
`data/radar/` is promoted with everything else.

- A source outage (non-2xx after retries) marks that source `failed` or `degraded` in the
  run record. The other source carries on, and mentions already held still rank. Items
  that were listed and then return 404/410 are counted as `gone`, not as failures.
- If the radar fails its own validation, or crashes, the cycle puts back the previous
  `data/radar/` and discovery pool and records the strategy as `failed` (with
  `kept_previous` and a `failures` entry), then carries on. A community signal never
  blocks or corrupts the canonical refresh.

```bash
python -m source_pipeline radar --limit 20                             # the ranking
python -m source_pipeline radar --status canonical                      # what The Source already recommends
python -m source_pipeline radar --source forem --status unresolved
python -m source_pipeline radar --candidates --limit 30                 # the day's repositories new to The Source
python -m source_pipeline radar --source bluesky --candidates
python -m source_pipeline radar --key github.com/n8n-io/n8n             # any name: radar key, oss_id, pool key, old address
python -m source_pipeline radar-build                                   # rebuild from what is held; no network
python -m source_pipeline radar-validate                                # what CI runs
python -m source_pipeline radar-collect                                 # the cycle's step, on demand (network)
```

Retrieval always returns each source's status from the last collection, so an empty answer
on a day a source was down is not read as "nobody is talking about it". Before the first
cycle that runs the radar, `data/radar/` does not exist and `radar-validate` says so.
Once a successful cycle has produced it, its absence is a failure.

## Limits

- Points, reactions and likes measure attention on a few communities, not quality or
  fitness as a replacement. The radar does not feed Replacement Fit, Project Health or
  Repository Momentum, and Community Momentum stays a separate measure in the MCP server.
- HN coverage is the front-page lists at collection time. A story that rose and fell
  between two daily runs can be missed. Forem coverage is four tags; Bluesky coverage is the
  100 most-liked posts of the week linking github.com for one query; Instagram, when
  configured, is three hashtags.
- X and Instagram are `unconfigured` until Keith funds X or applies for Instagram access
  ([`RADAR-SOCIAL.md`](RADAR-SOCIAL.md)). Until then their adapters are proven only against
  their documented response shapes in the tests.
- Velocity needs two observations of the same item, so a mention is never "rising" on the
  day it is first seen, and an item that leaves a source's page stops being re-observed.
- gitlab.com and codeberg.org addresses are matched by address only. They are not
  resolved, so a renamed project on those hosts shows as `unresolved`.
- With the default budget of 60 resolutions a day, a busy day leaves some lower-ranked
  addresses `unresolved` until the next day. The cache means nothing is asked twice.
