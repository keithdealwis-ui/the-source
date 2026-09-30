# Contributing

The Source is curated by rules, not by hand. Most changes are to a rule or a reviewed
list in `config/`. Hand-editing the data is never the way in: CI rebuilds everything
from the committed inputs and fails if the committed data differs.

## Suggest a repository

Use the intake surface (`docs/INTAKE.md`). A suggestion is judged by the same pipeline as
everything else. A repository qualifies only if it passes the quality bar and its own
repository states which catalogued SaaS product it replaces.

## Report a wrong recommendation

Open an issue with the SaaS product, the project, and what is wrong. Most fixes land in one of:

| Problem | Where it is fixed |
|---|---|
| The project is not really an alternative to that product | `config/edge_review.yaml` (a reasoned rejection) |
| The product is missing, or is not a hosted SaaS product | `config/saas_catalogue.yaml` |
| A project is listed under the wrong repository, or was renamed | `config/repo_overrides.yaml` |
| A licence reading is wrong | `config/licence_exceptions.yaml` (needs a named human approver) |
| The quality bar itself | `config/policy.yaml` (a policy change forces a full live refresh) |
| A score looks wrong | `config/scoring.yaml` or `config/saas_jobs.yaml` (bump `version`; history is append-only) |
| A product should or should not be served to the extension and site | `config/supported_saas.yaml` |

## Add an upstream list

1. Read its licence file at a specific commit, not the badge. Record the decision,
   rationale and conditions in `config/sources.yaml`, pinned to the commit and to the
   licence file's SHA-256. Add a row to `docs/LICENCE-REVIEW.md`.
2. Add a parser in `source_pipeline/parsers.py` and the id to the `source_id` enum in
   `schema/the-source.schema.json`.
3. Run a cycle (`python -m source_pipeline cycle --trigger local`) and open a pull request.

Lists without a licence that permits reuse are refused. Scraping a website in place of a
list is refused.

## Before you open a pull request

```bash
.venv/bin/python -m source_pipeline build
.venv/bin/python -m source_pipeline canonical-build
.venv/bin/python -m pytest tests/test_canonical.py -q
```

Commit the regenerated `data/` and `api/` with the change that caused them. CI runs the
same steps with the network refused, and fails on any difference.

## Ground rules

- Stars never decide anything.
- Imported facts are never presented as verified; live facts always carry when they were checked.
- Nothing is recommended on anyone's say-so. A human or chatbot assessment is advisory.
- Scores are automatic and are never presented as a personal endorsement.
