# Provenance

Every fact in The Source is in one of three classes, and every record says which.

| Class | Where it comes from | Refreshed | Marked by |
|---|---|---|---|
| **imported** | An upstream list, at a pinned commit, under a recorded licence decision | When research moves the pin | `imported.*`, with a `source_id` on each value |
| **live** | The project's own repository host, asked directly by The Source | Every weekly cycle | `live.*`, with `checked_at` / `fetched_at` |
| **derived** | Computed by The Source from live facts and `config/` | Every weekly cycle | `derived.*`, scores with `score_version` and `evaluated_at` |

## Tracing a recommendation

Take one row of `api/v1/saas/notion.json`:

1. `sources` names the upstream lists that state the relationship. `data/canonical/relationships.jsonl`
   has the full `provenance` for the edge: `source_id`, `source_commit` and `locator`,
   which is the file and line or record in the upstream at that commit.
2. `config/sources.yaml` holds the licence decision for each source, pinned to the SHA-256
   of the licence file the decision was made about. `data/state/source_pins.json` records
   any newer commit the weekly research moved to, and proves the licence file was
   unchanged. `data/canonical/NOTICE.md` carries the attributions each licence requires.
3. `live_checked_at` says when the host was asked. The full observation, including which
   commits, pull requests and releases counted as meaningful activity and which were
   disregarded and why, is in `data/dataset/oss_projects.jsonl` under `derived.maintenance`.
4. `replacement_fit` and `project_health` carry `score_version` and `evaluated_at`.
   `data/scores/history.jsonl` is the append-only history. `scoring_path` says whether a
   score came from the full scoring lane or the single-project path.
5. `runs/<run_id>.json` records the cycle that produced this version, and the
   `cycle/<run_id>` tag is the commit.

## Manual intake

Repositories admitted through intake have `source_id: intake`. The locator points at the
admitting request (`data/intake/admitted.jsonl#request_id=...`). The outcome record
`data/intake/outcomes/<request_id>.json` keeps the matched evidence. The intake source's
"licence file" is `docs/INTAKE-POLICY.md`, pinned by hash like any upstream licence.

## What is never imported

Descriptions, taglines, comparisons, pros and cons, pricing, setup estimates, referral
links and logos. The Source keeps facts only: names, repository URLs, declared licence
identifiers, short category labels and relationship claims. See
[`docs/LICENCE-REVIEW.md`](docs/LICENCE-REVIEW.md).
