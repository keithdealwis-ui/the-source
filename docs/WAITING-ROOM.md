# The waiting room

Shared, authenticated intake for every producer that finds repositories for The Source
(KEI-932). GitWatch in ChatGPT, the Community Radar adapters (YouTube, X, Instagram, Bluesky),
a future "Submit your project" form, and any other approved producer all use the same
boundary. They do not each get their own queue.

A submission is a **request for assessment**. It is never a recommendation, an endorsement, a
trending claim or a direct write to the corpus. Every repository is judged by the existing
intake assessment ([`INTAKE.md`](INTAKE.md)) and promoted by the daily cycle, which applies the
same rules as scheduled discovery.

## How it fits

```
producer ──POST /intake/v1/submissions──▶ waiting room (Bravo)
                                           validate · canonicalise · dedupe · answer at once
                                           │ 21:45 UTC: seal + sign the day's batch
                                           ▼
GitHub "Waiting room" workflow, 22:15 UTC ── fetch batch · verify signature · intake.assess() per repo
                                           │ commits data/intake/outcomes + ledger (intake files only)
                                           ▼
daily cycle, 23:00 UTC (03:00 Dubai) ────── promotes accepted repositories into data/canonical
                                           │
waiting room reconcile, hourly ◀─────────── reads the published outcomes; reason-coded results
```

What is reused, not rebuilt:
- Identity and dedupe: the pipeline's own `repo_key` rules, checked against the corpus
  (`data/corpus`), the canonical dataset, the Replace-lane dataset and the intake ledger.
- Eligibility, licence, activity and quick scoring: `intake.assess` (single-project scoring
  path) and the cycle's corpus admission controls (KEI-919, KEI-921, KEI-923).
- Relationship evidence and canonical matching: the intake's catalogue matching. The cycle's
  reconciliation then sets `canonical.status` on each outcome.
- Momentum and Community Radar signals (KEI-849, KEI-850): these attach to the repository once
  the cycle holds it, as for any other corpus member.

## The contract: `source-submission/1`

```json
{
  "contract": "source-submission/1",
  "repo_url": "https://github.com/owner/repo",
  "idempotency_key": "gitwatch-2026-11-01-001",
  "source": {
    "provider": "chatgpt",
    "evidence_url": "https://chatgpt.com/…",
    "observed_at": "2026-11-01T18:00:00Z"
  },
  "provenance": {"note": "spotted in GitWatch review", "on_behalf_of": "keith"},
  "candidate": {"saas_hint": "notion", "name": "Repo", "description": "One line"}
}
```

| Field | Rules |
|---|---|
| `contract` | must be `source-submission/1` |
| `repo_url` | a repository on github.com, gitlab.com or codeberg.org, max 2,048 characters |
| `idempotency_key` | 6 to 80 characters from `[A-Za-z0-9._-]`, chosen by the producer. The same key with the same payload returns the recorded submission (200). The same key with a different payload is refused (409). |
| `source.provider` | where the producer saw it (`chatgpt`, `youtube`, `x`, `bluesky`, `keith`, …), `[a-z][a-z0-9-]{1,39}` |
| `source.evidence_url` | optional: the post, video or conversation that mentioned it. Kept in the waiting room, never published. |
| `source.observed_at` | ISO 8601 with a timezone, not in the future |
| `provenance.note` | optional, max 500 characters. Private: never in a batch, the audit log or the repository. |
| `candidate.saas_hint` | optional catalogue id of the SaaS product it may replace. Advisory only. |

Unknown fields are refused. **The producer's identity comes from its credential, never from
the payload.**

## Endpoints

Base URL: `https://source-mcp.de-alwis.com`. The OpenAPI 3.1 description is at
`/intake/v1/openapi.json` ([`api/intake/openapi.json`](../api/intake/openapi.json)).

| Method and path | Auth | Answer |
|---|---|---|
| `POST /intake/v1/submissions` | producer | `202` recorded, `200` idempotent replay, `422` invalid (recorded, with reason codes), `401`, `409`, `413`, `415`, `429` |
| `GET /intake/v1/submissions/{submission_id}` | producer | the submission and its repository's standing; another producer's id answers `404` |
| `GET /intake/v1/batches/latest` | public | the latest signed batch |
| `GET /intake/v1/healthz` | public | `{"status": "ok"}` |

A producer reads back only its own submissions. It sees its own observations, plus a count of
how many other producers have seen the same repository.

Every answer to a submission includes `submission_state`, `reasons`, `canonical_id`,
`status_url` and, when the URL resolved, a `repository` block showing its `state`, its
`observations` by producer and provider, and the corpus record if The Source already holds it.
That immediate answer is the "concise analysis" a conversational producer can relay at once.

## States and reason codes

A submission is `recorded` or `invalid`. A recorded duplicate carries the reason
`duplicate_of_existing_entity`: its observation is kept, but no second entity is created.

A repository (entity) moves through these states:

| State | Meaning |
|---|---|
| `received` | waiting for the next daily batch |
| `already_known` | already assessed by The Source (canonical layer, Replace-lane dataset or intake ledger). It is answered at once and never re-ingested. A project held only in the discovery tier is *not* known: it stays `received`, has its discovery record attached, and is assessed like any other. |
| `queued` | in a sealed batch, waiting for its assessment |
| `accepted` | passed the intake assessment, and its relationship is evidenced. Waiting for the cycle to promote it. |
| `canonical` | promoted into the canonical dataset |
| `not_promoted` | accepted by the intake, but the cycle's corpus controls held it back. The cycle's reasons are given. |
| `held` | qualifies, but no SaaS relationship is evidenced (`needs_more_evidence`) |
| `rejected` | fails a rule. The intake's reasons are kept, for example `licence_not_recognised`. |
| `failed_retry` | the assessment failed. It is retried in the next batch under a fresh request id. If no outcome is published within 36 hours (the consumer did not run), the repository goes back to `received` under the same request id, and that costs no attempt. |
| `dead_letter` | three assessments failed. It stays visible, and no further automatic attempt is made. |

Validation reason codes: `contract_unsupported`, `unknown_field`, `idempotency_key_invalid`,
`repo_url_invalid`, `repo_url_missing`, `repo_url_not_a_repository`, `host_not_supported`,
`source_invalid`, `source_provider_invalid`, `source_evidence_url_invalid`,
`source_observed_at_invalid`, `source_observed_at_in_future`, `provenance_invalid`,
`provenance_note_invalid`, `provenance_on_behalf_of_invalid`, `candidate_invalid`,
`candidate_saas_hint_invalid`, `candidate_name_invalid`, `candidate_description_invalid`.

## Security model

- **No unauthenticated writes.** Each producer has its own credential (`swr_…`), stored only as
  a SHA-256 hash. It is scoped to `submit` and `read_own`, and it can be revoked or rotated
  individually (`producer-revoke`, then `producer-add`).
- **Limits.** Each producer has an hourly quota (30 by default) and a daily quota (200).
  nginx enforces a per-address rate. Each address may fail authentication 10 times a minute
  before further failures get `429`. A valid credential is never throttled by other clients' failures. Bodies are limited to 8 KB.
- **Audit.** The audit log (`audit.jsonl`) is append-only and records events, never tokens,
  payloads or notes.
- **GitHub holds no secret.** The batch the GitHub workflow consumes is public and signed with
  an OpenSSH Ed25519 key. The workflow verifies it against
  [`config/waiting_room_allowed_signers`](../config/waiting_room_allowed_signers) and refuses a
  tampered or unsigned batch. A batch more than 30 hours old is skipped. The private key is
  root-owned on the host and passed to the seal unit only (`LoadCredential`). The HTTP server
  never sees it.
- **Least disclosure.** A batch carries only canonical repository URLs, producer names,
  observation counts and the SaaS hint. These are the same facts the existing intake already
  publishes in its outcome records.
- **Failure isolation.** One repository's failure never stops the rest of its batch. A batch
  that cannot be fetched or verified changes nothing. The cycle never depends on the waiting
  room.

## Producer integration guide

1. **Get a credential.** On the host, the operator runs this as the service user. The CLI refuses
   to run as root, because files root creates would lock the service out:
   ```bash
   cd /opt/the-source-intake/app && runuser -u the-source-intake -- python3 -s -m source_pipeline.waiting_room \
     producer-add <name> --state /var/lib/the-source-intake/state --token-out /var/lib/the-source-intake/<name>.token
   ```
   Move the file somewhere safe and delete it from the host.
   The credential is shown once. Store it in the producer's own secret store. Never put it
   in a repository, a ticket or a chat transcript.
2. **Submit.**
   ```bash
   curl -sS https://source-mcp.de-alwis.com/intake/v1/submissions \
     -H "Authorization: Bearer $SWR_TOKEN" -H "Content-Type: application/json" \
     -d '{"contract":"source-submission/1","repo_url":"https://github.com/owner/repo",
          "idempotency_key":"myproducer-0001",
          "source":{"provider":"youtube","evidence_url":"https://youtu.be/…","observed_at":"2026-11-01T18:00:00Z"}}'
   ```
   Choose idempotency keys so that a retry after a timeout reuses the same key.
3. **Relay the immediate answer.** `repository.state` is `already_known` (with the corpus
   record) or `received`.
4. **Read back later.** `GET` the `status_url` the next day for the final reason-coded result.

### GitWatch (ChatGPT)

GitWatch is the `gitwatch` producer. It needs no MCP server and no plugin, just one ChatGPT
**Action**. Actions live in custom GPTs, not in ChatGPT Projects, so if GitWatch is a Project,
create a small "GitWatch" custom GPT for submitting (Explore GPTs → Create → Configure).

1. In that GPT, choose **Configure → Actions → Create new action**, then **Import from URL**, and
   enter
   `https://source-mcp.de-alwis.com/intake/v1/openapi.json`.
2. For authentication, choose **API key → Bearer** and paste the `gitwatch` credential.
3. Add this instruction: *"When Keith says 'add it' about a repository, call submitRepository
   with contract source-submission/1, provider chatgpt, observed_at now, and an
   idempotency_key of gitwatch-<date>-<short id>. Report the repository state and corpus
   record that come back. Never describe a submission as a recommendation. Next day, use
   getSubmission for the result."*

The conversational assessment Keith reviews before saying "add it" is still advisory only, as
in [`INTAKE.md`](INTAKE.md).

### GitWatch as a ChatGPT connector (MCP)

GitWatch can also reach the waiting room as a remote MCP connector instead of a GPT Action:
`https://source-mcp.de-alwis.com/gitwatch/mcp`, with two tools, `submitRepository` and
`getSubmission` (`source_pipeline/gitwatch_mcp.py`, deployed by
`deploy/the-source-gitwatch-mcp/install.sh` on 127.0.0.1:8853).

- **Access** is OAuth 2.1 for Keith alone: authorization code with PKCE (S256), rotating refresh
  tokens (connections re-authorise at least every 90 days), dynamic registration limited to
  ChatGPT's callback addresses, and metadata at
  `/.well-known/oauth-protected-resource/gitwatch/mcp` and
  `/.well-known/oauth-authorization-server/gitwatch` only (never at the host root, so the
  anonymous `/mcp` is unaffected). Keith approves a connection with a passphrase; the server holds
  only its scrypt hash. Tokens are opaque, bound to this resource, and useless anywhere else;
  tokens from other issuers (OpenSEO's included) are unknown here.
- **The producer credential** is the `gitwatch` one, held root-only at
  `/etc/the-source-gitwatch-mcp/gitwatch.token` and given to the service as a systemd credential.
  It never appears in a tool argument, a response or a log.
- **Operations** (as `runuser -u the-source-gitwatch --`, from `/opt/the-source-gitwatch-mcp/app`):
  `python3 -s -m source_pipeline.gitwatch_mcp revoke-all --state /var/lib/the-source-gitwatch-mcp`
  disconnects every connection; `set-passphrase` (stdin) replaces the passphrase; `client-add`
  pre-registers a public client for ChatGPT's "user-defined OAuth client" option; `clients` lists them.

### Adding a new producer

Add the producer, give it a credential, and use the contract. No code change is needed. A
public community form (KEI-933) would be a producer of its own, sitting behind its own
anti-abuse front end. It is never an unauthenticated route to this endpoint.

## Operations

Units are installed by [`deploy/the-source-intake/install.sh`](../deploy/the-source-intake/install.sh):

- `the-source-intake.service`: the HTTP server on `127.0.0.1:8852`, behind nginx at
  `/intake/v1/`
- `the-source-intake-seal.timer`: runs daily at 21:45 UTC
- `the-source-intake-reconcile.timer`: runs hourly at :20. It pulls a sparse clone of
  `data/corpus`, `data/canonical`, `data/dataset` and `data/intake`, then reconciles.

Useful commands (run from `/opt/the-source-intake/app`, as `runuser -u the-source-intake --`):

```bash
python3 -s -m source_pipeline.waiting_room status    --state /var/lib/the-source-intake/state
python3 -s -m source_pipeline.waiting_room status    --state … --submission sub-…
python3 -s -m source_pipeline.waiting_room producers --state …
```

The consumer fails its job when the batch cannot be fetched or is refused, so the failure shows
up in Actions. A batch more than 30 hours old is skipped quietly.

The GitHub side is `.github/workflows/waiting-room.yml`. It runs on a schedule, or on demand
with an optional `batch_url`. Each processed batch leaves a receipt in
`data/intake/waiting_room/<batch_id>.json`.
