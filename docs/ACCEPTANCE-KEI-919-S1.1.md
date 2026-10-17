# KEI-919 S1.1 / S1.7: project-level licence classification of flagged corpus rows

Status: **complete**. Machine-readable proof: `docs/kei-919/s1.1-completion.json`.

| | |
|---|---|
| Classifier commit | `d2dd35206c89ce80afdc2d94eea5b6a93d61f470` (module, validator, tests) |
| Classified data commit | `482204ad79ec68c21b77630ec74492707c9e3fdc` (`data/corpus/`, refined classifier) |
| Corpus | `the-source.corpus/1`, `corpus_version` `5b525ee988b39d58` (was `ea0e40ffd99d49aa`), 9,364 projects |
| Method | `the-source.licence-classify/1` (`source_pipeline/licence_classify.py`) |
| Run | 2026-10-17 06:31Z to 07:08Z, GitHub GraphQL, 1,392 repositories read |

## What was classified

All 1,393 rows of `data/corpus/projects.jsonl` with `licence.status == "issue"`. For each
one the classifier read the repository at its default-branch commit: the tree (3 levels;
enterprise-style directories one level further; 2 levels for 31 very large trees), the text of
every root licence file (`LICENSE*`, `LICENCE*`, `COPYING*`, `LICENSING*`, `licenses/*`), every
licence file inside an enterprise-style directory (`ee`, `enterprise`, `*_license`, `pro`, ...),
and `package.json` / `Cargo.toml` / `pyproject.toml` licence fields as supporting evidence only.
8 rows could not be read (7 no longer on GitHub, 1 on GitLab) and are `unclear`.

Each row's `licence` object gains two fields; `status` and `issue` are unchanged (back-compat):

```json
"licence": {
  "spdx": "NOASSERTION", "status": "issue", "issue": {"category": "unrecognised", "reason": "..."},
  "classification": {"class": "open_core", "core_spdx": "AGPL-3.0",
                     "excluded": {"paths": ["bitwarden_license"], "markers": []},
                     "reason": "...", "method": "the-source.licence-classify/1",
                     "provenance": {"source": {"api": "github: GraphQL tree at commit ...", "files": ["LICENSE.txt", "..."]},
                                    "commit": "9129efbd...", "checked_at": "2026-10-17T06:31:30Z"}},
  "eligibility": {"status": "conditional",
                  "condition": "core only; edition check required before recommendation",
                  "follow_up": "edition_check_required_before_recommendation",
                  "self_hostable": true, "self_hosting_evidence": ["dev/docker-compose.yml"],
                  "recommendation_gate": "unchanged: ..."}
}
```

`MANIFEST.json` gains `counts.licence_classification`, `counts.licence_eligibility` and a
`licence_classification` section (method, run time, scope). `corpus-validate` checks every new
field (138,664 checks, 0 failed).

## Counts

Before (Discover-lane issue category) and after (project-level class), 1,393 rows:

| Before | | After | | Eligibility |
|---|---:|---|---:|---|
| missing | 560 | missing | 554 | not eligible |
| source_available | 272 | source_available | 304 | not eligible |
| open_core | 232 | open_core | 39 | **conditional** |
| unrecognised | 202 | unclear | 453 | not eligible |
| restricted | 124 | restricted | 25 | not eligible |
| non_osi | 3 | open | 18 | **eligible** |

Eligibility: 18 eligible, 39 conditional, 1,336 not eligible. Self-hosting evidenced on 454 rows
(`self_hostable: null` on the rest; it never blocks eligibility).

How the categories moved: of the old 232 `open_core` rows, 11 stay open_core, 3 are `open`, 7
`source_available`, 8 `restricted` and 203 `unclear`. That category came from marker words
("enterprise", "directory", ...) and most of those repositories never name an excluded part.
27 of the 202 `unrecognised` rows are open_core (the four named projects among them). 18 rows
are now `open`: at the commit read, `licence_detect` (the strict detector, unchanged) recognises
the root licence and there is no enterprise-style directory. Several of them changed their licence
upstream after the Discover lane's evaluation (fuel-core is now Apache-2.0; sqz is now
Apache-2.0/MIT).

## The four named projects

All four are `open_core`, conditionally eligible, and their licence files bear it out:

| Project | Commit read | Core | Excluded | Evidence |
|---|---|---|---|---|
| twentyhq/twenty | `e7743ba7` | AGPL-3.0 | files marked `/* @license Enterprise */` | `LICENSE`: "This project is mostly licensed under the GNU Affero General Public License v3.0 ... Certain files are licensed under a commercial license. These files are clearly marked with the following comment at the top of the file: /* @license Enterprise */"; full AGPL text follows; "The AGPLv3 applies to the rest of the project". Delimited by an exact file-header marker, not a directory. |
| mattermost/mattermost | `d11343f0` | AGPL-3.0 (Apache-2.0 for named admin/config paths) | `server/enterprise` | `LICENSE.txt` carries the full AGPL and Apache texts and "The software is released under the terms of the GNU Affero General Public License, version 3."; `server/enterprise/LICENSE` and root `LICENSE.enterprise` are the Mattermost Source Available License. |
| RocketChat/Rocket.Chat | `99bef710` | MIT | `apps/meteor/ee`, `ee` | `LICENSE`: "All content that resides under the "apps/meteor/ee/" and "ee/" directories ... is licensed under the license defined in "apps/meteor/ee/LICENSE" ... Content outside of the above-mentioned directories ... is available under the MIT (Expat) license"; MIT text follows. |
| bitwarden/server | `9129efbd` | AGPL-3.0 | `bitwarden_license` | `LICENSE.txt`: "The default license throughout the repository is AGPL v3.0 unless the header specifies another license. Bitwarden Licensed code is found only in the /bitwarden_license directory."; `LICENSE_AGPL.txt` is the full AGPL text; `LICENSE_BITWARDEN.txt` is the excluded licence. |

Caveats to carry into the edition check (the required follow-up, not done here): Twenty also
releases some packages under MIT and adds an AGPL section 7 permission; Mattermost offers the
AGPL source under "the exceptions outlined in this policy" (permissive ones) and ships compiled
builds under MIT; Bitwarden allows a file header to name another licence.

The other 35 open_core rows (activepieces, baserow, chatwoot, databend, formbricks, infisical,
langfuse, posthog, signoz, ...) are listed in the JSON proof. Several exclude wide paths (e.g.
webiny `packages`, affine `packages/backend`); that is a reason the edition check comes before
any recommendation.

## Classes and eligibility rules

| Class | Rule | Eligibility |
|---|---|---|
| `open` | `licence_detect.detect` recognises the root licence files (unchanged, strict) and no enterprise-style directory is found anywhere in the tree read | `eligible` |
| `open_core` | a reference open licence text (>= 90% coverage) at the root, a notice that names it as the default / rest-of-repository licence (or a pure standard root text when the parts carry their own licence files), and every part under other terms clearly delimited: a directory with its own non-open licence file, a directory named under commercial/enterprise terms, or an exact file-header marker. No unconfined source-available terms, no unaccounted enterprise directory, no foreign licence file, no other-terms path | `conditional`: "core only; edition check required before recommendation"; `follow_up: edition_check_required_before_recommendation` |
| `source_available` | Business Source, Elastic, SSPL, FSL, Sustainable Use, Commons Clause, PolyForm, fair-code, non-commercial terms not confined to a delimited part | `not_eligible` |
| `restricted` | a standard open text plus a short notice with prohibitive terms ("may not", "shall not", "prohibited", ...) | `not_eligible` |
| `missing` | no licence file at the root (a manifest `license` field is recorded as `supporting`, never used) | `not_eligible` |
| `unclear` | everything else | `not_eligible` |

Eligibility depends on the class only; `corpus-validate` refuses any row whose eligibility does
not follow from its class (an open_core row marked `eligible` fails the build).

## Fail-closed behaviour

Lands in `unclear`, never in an eligible class:

- the repository, its tree or any root licence file could not be read;
- a licence text that matches no reference licence, or open text with notices that are not a standard text;
- an enterprise-style directory with no stated licence, at the root, or nested when no notice says the rest is open;
- parts licensed separately without a notice naming the open licence as the default;
- a path named under terms that are neither open nor a delimited commercial edition (e.g. third-party code under unnamed terms);
- a `LICENSES/` directory without a root licence file.

Open-content licences (Creative Commons, OFL, zlib, ...) that name a docs or vendored path are
open components, never commercial exclusions. A source-available core with an open SDK is
`source_available`, not open_core (tested).

## What did not change

- The recommendation gate. Nothing here publishes or admits a recommendation: the canonical
  build and `config/licence_exceptions.yaml` (a named human approver per exception) still decide.
  No exception entry was added.
- `licence.status` / `licence.issue`. Both are kept. A served view that admits open_core rows
  must read `licence.eligibility.status == "conditional"` (always together with
  `licence.classification.class == "open_core"`); `eligible` is `open`. Eligible and conditional
  rows still keep `status: "issue"`.
- No edition-wide audit (non-goal). The edition check is a recorded follow-up on each open_core row.
- No confidence calibration (V3 non-goal). Provenance is `source`, `commit`, `checked_at` only.

`corpus-export` carries a row's classification over while its licence evaluation is unchanged;
`corpus-refresh` keeps it untouched. New flagged rows from a later export have no classification
until `corpus-licence-classify` is run again.

## Reproduce

```bash
export GITHUB_TOKEN=$(gh auth token)                     # read-only use
python -m source_pipeline corpus-licence-classify        # 2-3 GraphQL requests per repository; cached under raw/licence_classify/ (gitignored)
python -m source_pipeline corpus-validate                # 0 failed checks
python -m pytest tests/test_licence_classify.py tests/test_corpus.py -q
```

The fetch is cached per repository, so a rerun re-classifies offline from the cache. Delete
`raw/licence_classify/` to read every repository again at its current commit.

Tests: `tests/test_licence_classify.py` (18 cases: open; open_core via a named `ee/`, a nested
enterprise directory with its own licence, a file-header marker, a LICENSING.md path list;
source_available BSL and inverse SDK case; restricted; missing; four fail-closed `unclear` cases;
provenance; validator refusal). Full suite: 437 passed, 5 skipped, 4 failed (the four known
environment-only failures that need pinned upstream checkouts).
