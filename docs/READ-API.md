# Read API (v1)

The read surface is a set of static JSON files under `api/v1/`. The weekly cycle
generates them from the canonical dataset. They are meant to be fetched and cached by
clients (the Chrome extension, the de-alwis.com template, internal tools). No client
needs to call GitHub or any upstream to render a recommendation.

## Files

| File | Contents |
|---|---|
| `index.json` | `dataset_version`, `data_as_of`, `live_checked_at` (oldest/newest), and one row per supported SaaS product (`saas_id`, `name`, `slug`, `path`, `compare_path`, `recommendations`, `status`) |
| `domains.json` | Host rules for detecting a supported product locally, plus `match_semantics` |
| `saas/<saas_id>.json` | One product's record: `saas`, `status`, up to 5 `recommendations`, `more_available`, `last_checked`, `scoring`, `provenance` |

Every file carries the same `dataset_version`, a 16-hex content hash of the canonical
dataset. Clients can key caches on it.

## A recommendation

```json
{
  "rank": 1,
  "oss_id": "github.com/docmost/docmost",
  "name": "docmost",
  "repo_url": "https://github.com/docmost/docmost",
  "github_url": "https://github.com/docmost/docmost",
  "stars": 21794,
  "licence_spdx": "AGPL-3.0",
  "maintenance_status": "active",
  "last_meaningful_activity_at": "2026-09-20T20:30:45Z",
  "latest_release": {"tag": "v0.96.0", "published_at": "2026-09-08T14:18:03Z"},
  "replacement_fit": {"score": 83.7, "confidence": 89.5, "score_version": "1.2.0", "evaluated_at": "..."},
  "project_health": {"score": 71.6, "band": "Healthy", "trajectory": "Stable", "confidence": 93.1},
  "self_hosting": {"self_hostable": true, "basis": "self-hosting documented in the project's README"},
  "note": {"strengths": ["core use-case coverage", "migration path"], "gaps": [], "text": "...", "origin": "derived"},
  "sources": ["btwso"],
  "live_checked_at": "2026-09-27T05:08:30Z"
}
```

- `maintenance_status` is `active` (meaningful activity within 90 days) or `maintained`
  (within 12 months). Stale, archived and missing projects are never served.
- `note` is machine-derived from the Replacement Fit dimensions (`origin: "derived"`).
  It is not editorial copy, and a page that shows it should label it as such.
- `self_hosting.self_hostable` is `true` only when there is evidence. `null` means
  "no evidence found", not "not self-hostable".
- `live_checked_at` is when the repository host was last asked. A client should show it,
  and may show a staleness hint if it is more than about 14 days old.

## Ordering and determinism

Recommendations are the relationships that pass the export guard and whose Replacement
Fit is both publishable and a credible alternative. They are ranked by Replacement Fit
(descending), then Project Health, then stars, then `oss_id`. The same inputs always
produce byte-identical files; CI rebuilds them and fails on any difference.

`status` is `ok` when at least 3 recommendations are available. A supported product that
has dropped below 3 (for example because a project went stale) is still served, with
`status: "insufficient_recommendations"`. A client should then show what there is, or
nothing, but never pad.

## Detecting a product (domains.json)

Checked locally; nothing about the page is sent anywhere:

1. Lowercase the page hostname and strip one leading `www.`.
2. A rule matches when its `host` equals the hostname (or the hostname ends with its
   `host_suffix`) and, if it has a `path_prefix`, the URL path starts with it.
3. If several rules match, one with a `path_prefix` wins; otherwise the first in file order.

Examples: `acme.atlassian.net/jira/...` gives `jira`, `acme.atlassian.net/wiki/...` gives
nothing, `aws.amazon.com/s3/...` gives `amazon-s3`, and `shop.myshopify.com` gives `shopify`.

## Serving

While the repository is private, clients need a GitHub token with read access. Once it
is public, the files can be fetched with no credentials from
`https://raw.githubusercontent.com/keithdealwis-ui/the-source/main/api/v1/...`, or through
a CDN such as `https://cdn.jsdelivr.net/gh/keithdealwis-ui/the-source@main/api/v1/...`.
For a fixed version, pin a `cycle/<run_id>` tag in place of `main`.

## Degradation

- If a cycle fails, the previous files stay in place, unchanged. A failure never
  produces a partial read surface.
- If a client cannot fetch, it should fall back to its last cached copy, keyed by
  `dataset_version`, and show that copy's `live_checked_at`.
- A repository whose host lookup failed during a cycle fails the whole cycle after
  bounded retries. It is never published with guessed values.

## Versioning

`api_version` is `v1`. Adding a field is not a breaking change. Removing or renaming one
means a new `api/v2/` alongside v1. The canonical records follow
`schema/the-source.canonical.schema.json`.
