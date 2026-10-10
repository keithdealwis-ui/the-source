# Community Radar: social sources (KEI-913)

The feasibility spike behind the X, Instagram and Bluesky adapters in `source_pipeline/radar.py`,
and the record of what each one may lawfully ask. Methodology and ranking: [`RADAR.md`](RADAR.md).
Everything below was read from the operators' own documentation on **2026-10-10**; the links are
the evidence. Where a page could not be read, it says so.

## The question

KEI-850's radar reads Hacker News and Forem. KEI-913 asked whether X and Instagram "buzz" can be
read the same way: lawfully, through an interface the operator intends for programmatic use, with
no scraping, no credential bypass, no new spend, and without storing anything a person wrote.

## The answer

| Source | Lawful documented interface | Credential | Cost today | Discovery | Resolution (post → repository) | Verdict |
|---|---|---|---|---|---|---|
| **X API v2** recent search | Yes: `GET /2/tweets/search/recent` ([docs](https://docs.x.com/x-api/posts/recent-search)), app-only bearer, `url:` and `has:links` operators ([operators](https://docs.x.com/x-api/posts/search/integrate/operators)), posts of the last 7 days, 10–100 per page, 450 requests / 15 min per app ([rate limits](https://docs.x.com/x-api/fundamentals/rate-limits)). | A developer account **and a saved payment card**. | **Pay-per-use** since 2026-02-06 ([changelog](https://docs.x.com/changelog)): Posts read **$0.005 each**, capped at 3 M reads a month ([pricing](https://docs.x.com/x-api/getting-started/pricing)). No free tier for a new account; a one-off promotional credit exists ($20 on saving a card, expiring after 3 months, [free credits](https://docs.x.com/x-api/getting-started/free-credits)). One query of 100 posts a day is about **$15 a month**. | Yes | Yes: `entities.urls[].expanded_url` / `unwound_url` | **Built, dormant.** Viable with a credential and a small, recurring spend. Both are Keith's decisions; neither is taken here. |
| X oEmbed `publish.x.com/oembed` | Documented for *embedding* a known post ([docs](https://docs.x.com/x-for-websites/oembed-api)): no key, no stated rate limit. | None | $0 | No (needs the post address) | Partial: links only inside the returned HTML | Not used. It cannot discover anything, and reading it as a data API is not the documented purpose. |
| X embedded timelines / syndication | Only a browser embed is documented ([docs](https://docs.x.com/x-for-websites/timelines/overview)); no server-side endpoint. | — | — | — | — | **Not used.** Undocumented endpoints are not a means the operator made available. |
| **Instagram Platform** hashtag search | Yes: `ig_hashtag_search` then `/{hashtag-id}/top_media` or `recent_media` ([reference](https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-hashtag-search), [guide](https://developers.facebook.com/docs/instagram-api/guides/hashtag-search)). Fields include `caption`, `permalink`, `timestamp`, `like_count`, `comments_count`; `username` is not available. 30 distinct hashtags per account per 7 days; `recent_media` covers only the last 24 hours. | A Meta app with the *Instagram Public Content Access* feature, an Instagram **Business or Creator** account connected to a Facebook Page, a user token for that Page, and **App Review**. | $0 once reviewed | Hashtags only | Yes: links in `caption` | **Built, dormant.** Viable only after a Meta app review for a professional account; coverage is thin (hashtags, 30 per week). Keith's decision. |
| Instagram oEmbed | Documented, but "for any purpose other than providing a front-end view" is prohibited, including deriving metadata "for analytics purposes" ([docs](https://developers.facebook.com/docs/instagram-platform/oembed)); gated by *Meta oEmbed Read* with business verification ([feature](https://developers.facebook.com/docs/features-reference/meta-oembed-read)). | App review | $0 | No | No (display-only terms) | **Not used.** The documented terms exclude this use. |
| Any key-less Instagram read | None documented. Every read path needs an app token. | — | — | — | — | **Does not exist.** The spike confirms it rather than assuming scraping is allowed. |
| **Bluesky** `app.bsky.feed.searchPosts` | Yes: documented public read with a `domain` filter ("Filter to posts with URLs … linking to the given domain"), `since`, `sort`, `limit` ≤ 100 ([lexicon](https://raw.githubusercontent.com/bluesky-social/atproto/main/lexicons/app/bsky/feed/searchPosts.json)). "Most `app.bsky.*` GETs are public and can be called without authentication" ([endpoints.bsky.app](https://endpoints.bsky.app/)); PDS limit 3,000 requests / 5 min per IP ([rate limits](https://bsky.network/docs/rate-limits/)). | **None** | **$0** | Yes: `domain=github.com` | Yes: link facets and the external embed carry full addresses | **Built, live.** On 2026-10-10 one page returned 98 posts of the last 7 days linking github.com, 72 with 5+ likes, from 93 distinct accounts; the collector kept 69 mentions. |

The ticket's premise, "lawful X and Instagram discovery at zero new spend", is therefore **not
available today** on either platform. X sells reads; Instagram gates them behind a business
account and a review, and forbids the display-only route for this purpose. What *is* available
at zero spend, lawfully and with real coverage, is Bluesky, which the ticket's own instruction
("prefer approved public feeds, embeds or permitted methods") points at. So the radar gained
three adapters: Bluesky live, X and Instagram complete but `unconfigured` until Keith decides to
fund one and grant its credential. Nothing here spends, grants or publishes.

Other lawful zero- or low-cost sources the spike found and did not build (each would be its own
small adapter under the same contract): Mastodon public hashtag timelines (no key on instances
that allow public preview; 300 requests / 5 min; hashtag discovery only), Reddit's Data API (free
OAuth app, 100 queries a minute, non-commercial; key-less `.json` is not permitted under the
2026 terms), Lobste.rs RSS (documented; its `robots.txt` asks AI crawlers to stay out), and the
Product Hunt API (free developer token, non-commercial).

## What each adapter asks, and what it keeps

| Adapter | Calls per cycle | Query | Kept per mention | Not kept |
|---|---|---|---|---|
| `bluesky_search` | 1 per query (1 configured), on the documented public host first and on the AppView the Bluesky app itself uses if that host refuses the method (it answered 403 on 2026-10-10) | `q=github domain=github.com sort=top since=<7 days ago> limit=100` | post id (`did/rkey`), permalink by DID, published, observed, likes, replies, query, repositories, `author` = truncated sha256 of the DID | text, handle, display name, avatar, labels, media |
| `x_api_v2` | 1 per query (1 configured): `max_results=100` posts, so at most 100 reads a day | `url:"github.com" -is:retweet -is:reply` with `tweet.fields=id,author_id,created_at,public_metrics,entities` | post id, permalink `x.com/i/web/status/<id>` (no handle), published, observed, likes, replies, query, repositories, `author` = truncated sha256 of `author_id` | text, usernames, names, metrics other than likes and replies |
| `instagram_graph` | 2 per hashtag (3 configured): the hashtag id, then its media page | `/{hashtag}/top_media` with `fields=id,permalink,timestamp,like_count,comments_count,caption,media_type` | media id, permalink, published, observed, likes, comments, hashtag, repositories | caption (read in memory for links, then discarded), media, owner (not exposed) |

An `author` is a 16-hex-character truncation of `sha256("<source>:<opaque account id>")`. It
lets the ranking count independent authors and notice one author repeating, and it names nobody:
the schema refuses any other author form and any text, caption or username field
(`tests/test_radar_social.py::test_the_schema_refuses_a_mention_that_carries_a_username_or_text`).
Hacker News, Forem and Instagram mentions carry no author at all.

Credentials are read from the environment (`X_BEARER_TOKEN`, `INSTAGRAM_ACCESS_TOKEN`,
`INSTAGRAM_USER_ID`), sent only as a bearer header on that source's calls, and never written
anywhere; a source whose variable is unset reports `unconfigured`, makes no call, and does not
degrade the strategy. The daily cycle workflow passes the three variables from repository
secrets that do not exist yet, so turning a source on is a credential decision, not a code change.

## Platform terms that bind the adapters

- **X** ([agreement](https://docs.x.com/developer-terms/agreement), [policy](https://docs.x.com/developer-terms/policy), [guidelines](https://docs.x.com/developer-guidelines)): use the API "as intended and documented"; non-API automation means permanent suspension; stored content must be kept current and removed within 24 hours of deletion or a removal request; only post ids may be redistributed in bulk. The adapter stores ids and public counts, re-reads them daily, drops them after 30 days, and redistributes no text.
- **Meta** ([platform terms](https://developers.facebook.com/terms/)): access only as expressly licensed; a material change in processing needs re-review; delete Platform Data when no longer necessary. The adapter keeps ids, counts and addresses for 30 days and reads captions only in memory.
- **Bluesky**: the AT Protocol is open and every post is public by design; the documented public AppView exists for unauthenticated reads. The adapter identifies accounts by DID, not handle, and stores no text.
- **Y Combinator and DEV**: unchanged from [`RADAR.md`](RADAR.md).

## Decisions left to Keith

1. **Fund X or not.** About $15 a month for one query of 100 posts a day, plus a card on file with X. If yes: create the developer app, store `X_BEARER_TOKEN` as a repository secret, and the next cycle reads X.
2. **Apply for Instagram or not.** A Meta app, a professional Instagram account and an App Review, for hashtag-only coverage. If yes: store `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_USER_ID`.

Neither decision holds this ticket open: the ranking, reconciliation, candidate list and pipeline
admission are live on Bluesky, Hacker News and Forem today, and the X and Instagram adapters are
exercised against their documented response shapes in the test suite.
