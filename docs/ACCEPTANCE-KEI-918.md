# KEI-918 acceptance map: The Source V2.1 WP1 observability

This map is against the ticket's own objective. No separate V2.1 specification was available
in the workspace, so S0.1–S0.6 are taken as the six deliverables the objective names, in its
order. The details are in [OBSERVABILITY.md](OBSERVABILITY.md).

- **Base:** `origin/main` at `23c313f`, which is cycle `2026-10-17-schedule-a1`, dataset `02568a5dd656e3a8`.
- **Branch:** `kei-918-wp1-observability`.

| # | Objective | Status | Evidence |
|---|---|---|---|
| S0.1 | Intake reports | Met | Every cycle writes `runs/reports/<run_id>.intake.json`: an eight-stage funnel from upstream lists to canonical, with stop reasons, manual intake, discovery and the change from the previous run. Live report for `2026-10-17-schedule-a1` committed. `test_intake_report_follows_the_funnel_of_the_live_run` ties each stage to the MANIFEST and summary. |
| S0.2 | Failure/fingerprint alerts | Met | `obs-alerts` and `obs-fingerprints`; the cycle records `failed_step`. Five rules, thresholds in `config/observability.yaml`. The workflow keeps one issue per fingerprint (`scripts/surface_alerts.py`). On the real history, the repeat GitLab-403 failures and the repeat canonical drills each share a fingerprint. Tests cover new, recurring and recovered faults, staleness, shrink, and issue de-duplication. |
| S0.3 | `dataset_info` `last_run` | Met | `source_dataset_info.last_run`: the newest run, last success, `serving_last_success`, and staleness after 48 h. A bare layer degrades to `unavailable`. The hosted sparse clone gains `runs`. Audit-hook test updated to allow `/runs/`. |
| S0.4 | 40–50 golden queries: recall@5, precision@5, constraint violations | Met | 48 queries (`config/golden_queries.yaml`). Baseline on `02568a5dd656e3a8`: recall@5 **0.714**, precision@5 **0.778**, **0 violations**, 2/2 must-fail. Violations are recomputed from the published files, independently of the server. Gate in CI; non-blocking cycle step 14. **Residue:** the labels await Keith's review. |
| S0.5 | Privacy-preserving hosted MCP telemetry and weekly reporting | Met in code; deployment pending | Telemetry is off unless a transport has a sink: stdio counts nothing, HTTP only with `--telemetry`. Schema `/2` adds empty-answer counts and nothing identifying. `telemetry_report.py` reports whole weeks only, suppresses counts under 5, withholds rates computed from them, and gives latency as buckets. systemd timer `the-source-mcp-report.*`. **Not deployed:** installing on the MCP host is Keith's action. |
| S0.6 | Reproducible dataset lineage, including 118→102 catalogue reconciliation | Met | `obs-lineage history`, `verify` and `reconcile`, from git alone. `verify` passes on all 24 versions. 118 → 102 is reconciled product by product: 17 left, all `below_launch_cutoff` (the launch extension past the 100-product floor shrank from 19 to 3); 1 joined (SAP Business One); Pingdom left and returned. `select_launch` reproduces the committed ranks on all 11 versions in the span. |
| — | Local stdio usable without mandatory telemetry | Met | `test_stdio_counts_nothing_and_needs_no_telemetry`. |
| — | Protect client privacy | Met | No new data about callers anywhere. Weekly reports are coarser than the counters. `last_run` exposes only public run records. |
| — | No credentials, spend, publication or authority expansion | Met | Nothing deployed, pushed to `main` or published. No secrets touched. |

**Verification.** All run locally with the network refused:

- `pytest`: the CI set plus `test_runs_latest.py`, 196 passed
- `build`, then `canonical-validate`: 27/27 checks
- momentum, radar and corpus validation
- `obs-golden --check`
- `obs-lineage verify`
- `git diff -- data api`: clean

**Left for Keith and Draper:**

- review the golden labels
- deploy to the MCP host (`install.sh`, which adds `runs` to the sparse clone and the weekly
  report timer)
- merge, after which the next scheduled cycle exercises step 14, the intake-report writer and
  fingerprinted issues for real
- the launch-floor policy question raised by the reconciliation
