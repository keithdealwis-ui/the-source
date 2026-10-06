# Repository momentum and acceleration (KEI-849)

Methodology `the-source.momentum`, version `1.0.0`. Parameters: `config/momentum.yaml`.
Code: `source_pipeline/momentum.py`. Schema: `schema/the-source.momentum.schema.json`.

Momentum shows how fast a repository is gaining (or losing) adoption right now.
Acceleration shows whether that pace is picking up or slowing down. Both are measured
over 1, 7, 30 and 90 days. Both come only from the dated daily snapshots the daily cycle
already keeps (`data/history/`, KEI-848). They add no network calls and do not change
the canonical dataset, its version or the read API.

## What is measured

For a repository and a window of **W** days ending on day **D** (the newest snapshot):

| Quantity | Definition |
|---|---|
| base | The newest snapshot on or before **D − W**. It may be up to `base.tolerance_days[W]` days earlier than that (0 / 1 / 3 / 7 days for 1 / 7 / 30 / 90). It is never interpolated. |
| `span_days` | The elapsed time between the base observation and today's observation (`fetched_at`, falling back to the day's `as_of`), in days. It must be at least `base.min_span_ratio × W` (0.5 W). |
| `stars_delta` | Today's stars minus the stars at the base. |
| `stars_per_day` | `stars_delta / span_days` |
| `growth_pct_per_30d` | `stars_per_day × 30 / max(base stars, relative_floor_stars) × 100`. The floor (100) stops a 5-star repository gaining 3 stars from reading as explosive. |
| `momentum` | `surging` if growth ≥ 10 %/30 d; `rising` if ≥ 1 %; `declining` if ≤ −0.5 %; otherwise `flat`. A window whose \|`stars_delta`\| is under `min_abs_change` (3) is always `flat`. |
| acceleration | `stars_per_day` for the window just ended minus `stars_per_day` for the **W** days before it (base found the same way, ending on this window's base day). It needs **2W** days of history. |
| acceleration label | `accelerating` if the change is ≥ the threshold, `decelerating` if ≤ −threshold, otherwise `steady`. The threshold is `max(0.5 stars/day, 0.25 × abs(prior stars/day))`. If neither window moved by `min_abs_change` stars, the label is `steady`: that is a wobble, not a trend. |
| `activity` | Context, not part of any label. `pushed_since_base`; `new_release` (the latest release tag differs from the base's); `head_changed_day_pairs` out of `day_pairs_observed` (consecutive daily snapshots in the window where the default-branch head moved: a lower bound on the days with commits; GitHub only); `day_pairs_in_window`. |

Stars are the momentum metric because they are the only adoption signal the light
refresh observes for the whole corpus every day. Forks are reported (`forks_delta`) but
not banded. Commit and issue activity are scored in Project Health (`docs/SCORING.md`),
not here.

## Honest coverage

A window is either `measured` or says why it is not, with a `reason`:

| Status | Meaning |
|---|---|
| `measured` | A base inside tolerance, the repository found in both snapshots, the same repository id, and enough elapsed time. |
| `insufficient_history` | There is no snapshot old enough, the nearest one is further from the target day than the tolerance allows, or the two observations are too close in time (for example, two runs a few hours apart do not count as a 1-day window). |
| `no_baseline` | The repository was not in the base snapshot. It joined the corpus after the base day. |
| `not_observed` | Not found, or no star count, today or at the base. |
| `identity_changed` | The host repository id differs between the base and today. The name now points at a different repository, so the two are not compared. A rename under the **same** id is compared, with a `notes` entry. |

Acceleration has the same statuses for its prior window.

`data/momentum/MANIFEST.json` → `coverage[W]` gives, for each window, the base chosen
(`target_day`, `base_day`, `exact`, `base_gap_days`), the snapshots actually held inside
the window against the calendar days it spans, per-status project counts, the band and
label distribution, and `measurable_from`: the first day whose look-back reaches the
oldest snapshot held (`W` days for momentum, `2W` for acceleration). Before that day the
window cannot be measured. After it, the window is measured provided the daily snapshots
keep arriving.

At launch, history starts on 2026-10-02. The 1-day window is measured. Its acceleration
is not: the 2026-10-02 snapshot was taken 0.35 d before the 2026-10-03 one. The 7, 30 and
90-day windows become measurable from 2026-10-09, 2026-11-01 and 2026-12-31, and their
acceleration from 2026-10-16, 2026-12-01 and 2027-03-31.

## Explainability

Every measured window carries the observations it came from (`base_day`, `span_days`,
`stars_base`, `stars_now`, and the prior window's values for acceleration). It also
carries an `explanation` that restates the arithmetic, for example:

```text
stars 1054 -> 1090 (+36) over 0.97 d (2026-10-03 -> 2026-10-04): +36.95/day, +105.160% of 1054 per 30 d -> surging
+25.00/day (2026-09-27 -> now) vs +5.00/day (2026-09-20 -> 2026-09-27): change +20.00/day, threshold +/-1.25 -> accelerating
```

The manifest records the sha256 of every history day it read. Anyone can recompute any
value from `data/history/` alone.

## Versioning

`config/momentum.yaml` → `methodology.released` maps each released version to the sha256
of its parameters (everything outside `methodology`). If a parameter changes without a
new version and a new `released` entry, `momentum-validate`, the tests and the daily
cycle all fail. Every output carries `methodology.version` and `parameters_sha256`. A
change to the method in code must also bump the version, together with the fixture's
golden output (`tests/fixtures/momentum/expected.json`). Without that, the golden test
fails. Version history:

| Version | Date | Change |
|---|---|---|
| `1.0.0` | 2026-10-06 | First release: stars-based momentum and acceleration over 1/7/30/90 days. |

## Where it runs and how to read it

The daily cycle runs it as step 12 (`momentum`), after the day's history snapshot. It
validates the result, including a byte-identical rebuild, and promotes `data/momentum/`
with everything else. A failure fails the cycle and nothing is promoted. The run
record's `momentum` strategy lists each window's status and `measurable_from`, and
`counts.momentum_measured` gives the number of repositories measured on at least one window.

```bash
python -m source_pipeline momentum-build [--on YYYY-MM-DD]      # data/momentum/ from data/history/; no network
python -m source_pipeline momentum-validate                     # what CI runs
python -m source_pipeline momentum --window 7 --sort acceleration --limit 20
python -m source_pipeline momentum --window 30 --key github.com/n8n-io/n8n
python -m source_pipeline momentum --window 7 --momentum surging --acceleration accelerating
```

Retrieval (`momentum.retrieve`) accepts corpus keys or current canonical ids, ignoring
case. It lists unknown keys separately. It always returns the window's coverage, so an
empty ranking reads as `insufficient_history` rather than "nothing is moving".

## Limits

- Stars measure attention, not quality or fitness as a replacement. Momentum is a signal
  to look closer, not a recommendation. It does not feed Replacement Fit or Project Health.
- Rates are per elapsed day between two observations. Day-to-day noise in a 1-day window
  is real noise. Prefer 7 days or more for ranking.
- GitLab and Codeberg repositories have no head commit in the light refresh, so their
  `head_changed_day_pairs` is always 0 of 0 observed.
