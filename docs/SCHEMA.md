# Canonical schema

Machine-readable definition: `schema/the-source.schema.json` (JSON Schema 2020-12),
version `1.0.0`. Every record in `data/dataset/` is validated against it on each run.

There are four record types.

## Source — `sources.json`

One per upstream list.

| Field | Meaning |
|---|---|
| `source_id` | stable identifier used in every provenance entry |
| `repo`, `url` | the upstream repository |
| `pinned_commit` | the exact commit harvested |
| `licence_file`, `licence_file_sha256` | the licence file read, and its hash |
| `licence_spdx` | the licence as read from that file |
| `decision` | `allow`, `allow_with_conditions` or `refuse` |
| `rationale` | why |
| `conditions` | what any public export must do |
| `attribution` | the attribution text to reproduce |
| `share_alike` | whether the licence is share-alike |
| `refused_adjacent` | related material (a website, an API) that was refused |
| `harvest` | what the last run read from it |

## SaaS product — `saas_products.jsonl`

| Field | Origin | Meaning |
|---|---|---|
| `saas_id` | reviewed | slug of the canonical name |
| `name`, `domain`, `category`, `aliases` | reviewed | from `config/saas_catalogue.yaml` |
| `record_origin` | — | always `reviewed_catalogue` |
| `imported.named_by_sources` | imported | which sources name this product |
| `imported.spellings_seen` | imported | how they spelled it |
| `derived.source_count` | derived | number of independent sources |
| `derived.validated_alternatives` | derived | validated relationships |
| `derived.launch`, `derived.launch_rank` | derived | launch selection |

## OSS project — `oss_projects.jsonl`

| Field | Meaning |
|---|---|
| `oss_id` | `host/owner/repo`, lowercased, as the host names it today |
| `name` | display name |
| `name_origin` | the name is an imported value; this names the source it came from |
| `product_group` | repositories of one product share a group |
| `repo_url` | canonical URL |
| `harvested_as` | every key the project was harvested under before identity merge |
| `imported` | see below |
| `live` | see below |
| `derived` | see below |
| `provenance` | where the project was read |

### The three origins

Every value on a project record sits under exactly one of three blocks. There are no
top-level licence, star or activity fields.

**`imported`** — copied from an upstream list at harvest. Never refreshed. Never used
to decide eligibility. Every value names the source it came from:

```json
"declared_licences": [{"value": "AGPL-3.0", "source_id": "solvohq"}]
```

Fields: `names`, `repo_urls`, `declared_licences`, `categories`.

**`live`** — read by The Source from the repository host's API. Dated by `fetched_at`.
Replaced on every live run.

Fields: `provider`, `fetched_at`, `found`, `error`, `canonical_name`, `host_repo_id`,
`archived`, `disabled`, `empty`, `is_fork`, `is_mirror`, `licence_spdx`,
`licence_name`, `stars`, `forks`, `pushed_at`, `created_at`, `default_branch`,
`observed` (counts of what was inspected).

**`derived`** — computed from `live` and `config/policy.yaml`. Dated by
`evaluated_as_of`.

| Field | Meaning |
|---|---|
| `maintenance.status` | `active`, `maintained` or `stale` |
| `maintenance.last_meaningful_activity_at` | date of the newest qualifying evidence |
| `maintenance.evidence` | the newest qualifying item of each kind. A commit or pull request carries its reference, author and a sample of the code paths touched. A release or tag carries its version, the tagged commit, a sample of what it shipped and how that was established |
| `maintenance.disregarded` | what was looked at and did not count, and why |
| `maintenance.supporting` | signals recorded but not sufficient (closed issues) |
| `licence.spdx` | the licence relied on |
| `licence.spdx_reported_by_host` | what the host's own detector said |
| `licence.detection` | how the licence was established |
| `licence.exception` | the approved exception, if one applies |
| `licence.status` | `recognised`, `exception_approved` or `issue` |
| `licence.issue` | present whenever the licence is not recognised; see below |
| `recommendation_eligible` | whether the project may be recommended |
| `lane` | `recommendable`, `licence_exception` or `rejected` |
| `eligibility.recommended` | the verdict `recommendation_eligible` restates |
| `eligibility.exclusion_reasons` | every reason it may not |
| `launch` | whether it is in the launch corpus |

An upstream's claim about a licence and the licence we verified are both kept, in
different blocks, so a disagreement stays visible.

### Licence issue record — `derived.licence.issue`

| Field | Meaning |
|---|---|
| `category` | `source_available`, `open_core`, `restricted`, `non_osi`, `conflicting`, `missing`, `unrecognised` or `undetermined` |
| `category_meaning` | the definition of that category |
| `reason` | the exact reason this project was given it |
| `detected` | what the host reported and what the licence files contain: host SPDX and name, licence files, closest standard licences, enterprise directories, restriction markers |
| `upstream_claims` | the licence each upstream list declared, with its source |
| `upstream_claims_disagree_with_repository` | an upstream declared an open licence the repository does not bear out |
| `evidence` | per licence file: closest reference licence, coverage, left-over text, markers |
| `review_status` | `flagged_retained_not_reviewed_individually` or `exception_approved` |
| `retained_under` | the decision under which the record is kept |
| `named_exception` | the approved exception, if any |
| `reuse_rights_established` | always `false` |

The schema enforces the lane: a record in `licence_exception` or `rejected` cannot be
`recommendation_eligible` or in the launch set, and a `licence_exception` record
without a complete issue record does not validate.

## Relationship — `relationships.jsonl`

| Field | Meaning |
|---|---|
| `edge_id` | `saas_id--oss_id` |
| `saas_id`, `oss_id` | the endpoints |
| `relation` | `open_source_alternative_to` |
| `provenance` | one entry per upstream statement of this relationship; at least one is required |
| `source_ids`, `source_count` | the independent sources |
| `corroborated` | two or more independent sources |
| `share_alike_only` | every source is share-alike |
| `lane`, `recommendation_eligible` | as for projects |
| `validation.status` | `validated`, `flagged_licence_exception` (nothing is wrong except the project's licence) or `rejected` |
| `validation.checks` | each check and its result |
| `validation.plausibility_review` | who read the relationship, when, and the outcome |
| `validation.reasons` | why it was rejected |
| `launch`, `launch_position` | launch selection |

### Provenance entry

```json
{
  "source_id": "solvohq",
  "source_commit": "5008048500e9a7856862ea87e2be30ac0048e1c8",
  "locator": "README.md#L57",
  "claimed_saas_name": "Discord",
  "claimed_oss_name": "Mattermost"
}
```

`source_id`, `source_commit` and `locator` are required by the schema. A relationship
with no provenance does not validate.

Where an upstream linked to an organisation or a website and the repository was
supplied by `config/repo_overrides.yaml`, the entry also carries
`resolved_by: reviewed_override` and the `upstream_url` as written.

## Identity and reconciliation

| Thing | Identity | How duplicates merge |
|---|---|---|
| OSS project | the host's canonical `owner/repo` and its repository id | URL variants normalise to one key; renamed or transferred repositories collapse to the name the host reports; imported values and provenance are unioned |
| SaaS product | normalised name resolved through the catalogue | spellings and plan tiers map to one product; an alias claimed by two products is a configuration error and stops the run |
| Relationship | the pair (`saas_id`, `oss_id`) | one edge per pair; each upstream statement is a separate provenance entry |

Ordering is fixed everywhere (by source priority, then locator, then id), so the same
inputs produce the same bytes regardless of input order.

## Other files

| File | Content |
|---|---|
| `rejections.jsonl` | every rejected claim or project, with stage and reason |
| `summary.json` | counts, launch figures, targets |
| `MANIFEST.json` | SHA-256 of every dataset file |
| `reports/validation_report.json` | each check, tied to its acceptance criterion |
| `reports/licence_exception_candidates.json` | projects excluded only for their licence |
| `reports/uncatalogued_saas_names.json` | names upstreams use that are not in the catalogue |
