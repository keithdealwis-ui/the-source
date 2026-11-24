# Repository submissions for maintainers and integrations

Found a project or a fact that needs attention? [Suggest it through support](https://the-source.dev/support/) or follow the [contribution guide](../CONTRIBUTING.md). You do not need access to an authenticated producer API to make a suggestion.

The reference below is for maintainers and approved integrations. Intake assesses submitted repositories through the same rules as scheduled discovery. A submission requests assessment; it does not approve a project or directly publish a recommendation.

Routine producers use the shared [authenticated submission service](WAITING-ROOM.md). The reviewed [intake policy](INTAKE-POLICY.md) is pinned by hash and must be updated through its policy-review process.

## Submission flow

1. A contributor supplies a public repository URL and supporting context.
2. An authorised maintainer or producer submits it to intake.
3. The pipeline assesses the repository and records the outcome and reasons.
4. The submitting integration reports that outcome. Accepted records still require normal cycle validation and promotion before publication.

## The surface

The intake surface is the `Intake` GitHub Actions workflow, authenticated by GitHub. A
client needs a token with **Actions: read and write** and **Contents: read** on this
repository. It never gets contents write access; the job is the only writer.

| Operation | HTTP |
|---|---|
| submit | `POST /repos/keithdealwis-ui/the-source/actions/workflows/intake.yml/dispatches` with `{"ref": "main", "inputs": {"repo_url": "...", "request_id": "...", "saas_hint": "...", "submitted_by": "...", "note": "..."}}` |
| status | `GET /repos/keithdealwis-ui/the-source/actions/workflows/intake.yml/runs?event=workflow_dispatch`, then find the run whose `display_title` is `intake <request_id>` |
| outcome | `GET /repos/keithdealwis-ui/the-source/contents/data/intake/outcomes/<request_id>.json` |

`request_id` is chosen by the client (6 to 80 characters from `[A-Za-z0-9._-]`) and is
the idempotency key. Submitting the same id again returns the recorded outcome unchanged.

The CLI adapter wraps all three:

```bash
python scripts/source_intake.py submit https://github.com/owner/repo --hint notion --by keith --wait
python scripts/source_intake.py status <request_id>
python scripts/source_intake.py result <request_id>
```

Or run the assessment locally, without GitHub Actions:

```bash
python -m source_pipeline intake-assess --url https://github.com/owner/repo --request-id my-request-01
```

## What is checked

The checks run in this order, and the first failure decides the outcome.

1. **URL**: must be a repository on a supported host (GitHub, GitLab, Codeberg).
2. **Deduplication**: the repository, or the repository its URL redirects to, is checked
   against the corpus and the intake ledger. If found, the outcome is `already_known`,
   with its current lane and relationships.
3. **Live verification**: the host is asked directly. Failed lookups give `failed`,
   which is retryable under a new request id.
4. **Quality bar**: not archived, disabled or empty; a recognised open-source licence;
   meaningful activity within 12 months (`config/policy.yaml`). A failure gives
   `rejected`, with reasons.
5. **Scores**: Project Health and Evidence Confidence through the scoring rubric
   (single-project path). These are reported in the outcome as preliminary. The cycle that
   promotes the repository scores it again through the same path and writes the shared
   score history, so there is one history and one writer.
6. **Relationship evidence**: the project's own description, topics and README are
   searched for "alternative to X", "X alternative" or a topic `x-alternative`, where
   `X` is a product in the reviewed SaaS catalogue. The whole item must match, so
   "alternative to GitHub Gist" does not count as GitHub. None found gives
   `needs_more_evidence`. Found gives a preliminary Replacement Fit per relationship, and
   the repository is admitted to `data/intake/admitted/<request_id>.json` with outcome `accepted`.

## The outcome record

`data/intake/outcomes/<request_id>.json` (schema `intake_outcome`) holds:

- `outcome`: `accepted`, `rejected`, `already_known`, `needs_more_evidence` or `failed`
- `reasons`: why, in the pipeline's own terms
- `evidence`: live facts, maintenance, licence, and matched relationship excerpts
- `scores`: preliminary Project Health and Replacement Fit
- `canonical.status`: `pending_promotion`, `canonical` (with `promoted_in_run`),
  `not_promoted` (with the pipeline's reasons), `not_admitted`, or the lane of a
  rejected project

That is enough for the submitting tool to report what happened, and why.
