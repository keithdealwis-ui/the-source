# Rejection reason codes and the review queue (KEI-919 S1.5)

Every record in `data/dataset/rejections.jsonl` carries exactly one **code** from a
closed enumeration, and a **detail** that says it in words. The machine `reason` written
before KEI-919 is kept unchanged beside them, so existing readers keep working.

The enumeration lives in `source_pipeline/reasons.py` (`CODES`) and in the JSON schema
(`schema/the-source.schema.json`, `$defs.rejection_code`). Both must agree: a test
checks they do. The set is closed in two places:

- **Build:** a rejection whose reason has no code stops the build (`reasons.UnknownReason`).
  Nothing is written under an unknown or catch-all code.
- **Validation:** check `schema.rejection` fails on any record whose code is missing, not in
  the enum, or disagrees with its `reason`.

## Codes

| code | stage | raw reason(s) mapped | meaning |
|---|---|---|---|
| `not_a_repository` | normalise | `repo_url_missing`, `repo_url_not_a_repository` | the upstream URL does not name a source repository |
| `saas_not_in_catalogue` | normalise | `saas_not_in_catalogue` | the SaaS product named is not in the reviewed catalogue |
| `not_found` | eligibility | `repository_not_found` | the host reports the repository does not exist |
| `host_unverifiable` | eligibility | `host_not_independently_verifiable` | a host The Source cannot independently verify |
| `verification_failed` | eligibility | `live_verification_failed` | live verification did not complete |
| `archived` | eligibility | `archived` | archived (read-only) on its host |
| `disabled` | eligibility | `disabled` | disabled on its host |
| `empty_repository` | eligibility | `empty_repository` | the repository has no content |
| `identity_unverified` | eligibility | `repository_identity_changed_since_listed` | identity changed since listing; no reviewed rename confirms it |
| `licence_not_open` | eligibility | `no_recognised_open_source_licence` | no recognised open-source licence and no approved exception |
| `unmaintained` | eligibility | `stale_no_meaningful_activity_12_months` | no meaningful activity in 12 months |
| `not_a_replacement` | plausibility_review | any reason in `config/edge_review.yaml` | desk review: a different kind of software, not a replacement |
| `contradictory_evidence` | review queue | (see below) | three or more independent sources contradict each other |

**One code per record.** An eligibility rejection can have several raw reasons
(`archived;no_recognised_open_source_licence;...`). It is coded by the first in this
precedence: `not_found`, `host_unverifiable`, `verification_failed`, `archived`,
`disabled`, `empty_repository`, `identity_unverified`, `licence_not_open`, `unmaintained`.
`detail` lists every reason in words, and `reason` keeps all of them.

`summary.json` reports `rejections.by_code` beside the older `rejections.by_reason`.

## Review queue: contradictory evidence

`data/dataset/review_queue.jsonl` (schema `$defs.review_queue_item`) holds claims that
**three or more independent sources** contradict. The build does not settle these by
rule, either way. It queues them with code `contradictory_evidence`, the claim, every
source's assertion and the relationships affected.

Today the build checks one claim, **a project's licence**. The independent sources are:

- each upstream list that declares a licence (`imported.declared_licences`), counted once per list;
- the repository host's API (`live.licence_spdx`);
- The Source's own reading of the licence files, when it was used (`derived.licence.detection`).

Values are compared after folding case, a trailing "License", and `-only` / `-or-later` / `+`.
Values that assert nothing (`Other`, `NOASSERTION`, empty) are not counted as sources. An item
is queued when at least 3 asserting sources give at least 2 distinct values.

**Fail closed.** While an item is `open`, every relationship of that project fails the
check `validation.checks.evidence_not_contradicted`, so it is not
`recommendation_eligible` and cannot be in the launch set. Its `validation.reasons` names
the queue item. Check `review_queue.fail_closed` asserts this. A relationship that is
otherwise only short of an approved licence exception stays `flagged_licence_exception`.

**Resolving.** A human records the decision in `config/evidence_review.yaml`
(`queue_id`, `resolution`, `reviewed_by`, `reviewed_at`, `approval_ref`). The build
refuses a resolution from an executor identity or one without an `approval_ref`. A
resolved item stays in the queue file with `status: resolved` and stops holding its
relationships. Nothing here changes a score, a confidence value or a gate threshold.

**Not yet covered.** The upstream corroboration providers (ecosyste.ms, deps.dev; see
`source_pipeline/corroborate.py`) also flag disagreements with the host about archive
state, licence and identity. Those flags are kept in run records, not build inputs, so the
pure build does not read them. No committed run record so far has both providers
disagreeing with the host on the same key and field, which is the three-source case.
