# Review digests (KEI-783)

Weekly digests of what the daily cycles changed in the committed data, for a human to review.
Each one is `digest-YYYY-Www.md` with a JSON twin, written by
`python -m source_pipeline review-digest` and proposed as a pull request on the branch
`review/YYYY-Www` by `.github/workflows/review-digest.yml`. Merging a digest's pull request
keeps it here on record; the dataset itself is already live, refreshed by the daily cycle.

Everything in a digest is machine-derived. Keith's own verdicts live in `data/verdicts/`
(docs/SCORING.md, Machine scores and Keith's verdicts). See docs/OPERATIONS.md, Human review digest.
