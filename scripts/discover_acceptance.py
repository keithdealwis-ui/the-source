"""Write docs/DISCOVER-ACCEPTANCE.md: KEI-811 criterion by criterion, from the built corpus.

Every figure is read from data/discover/corpus/, never typed by hand.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
C = ROOT / "data" / "discover" / "corpus"
summary = json.loads((C / "summary.json").read_text())
report = json.loads((C / "reports" / "validation_report.json").read_text())
coverage = json.loads((C / "seed_coverage.json").read_text())
sources = json.loads((C / "sources.json").read_text())
n = summary["counts"]
checks = {c["id"]: c for c in report["checks"]}


def ck(*ids):
    return ", ".join(f"`{i}` {'PASS' if checks[i]['ok'] else 'FAIL'}" for i in ids)


def fmt(x):
    return f"{x:,}" if isinstance(x, int) else str(x)


lists = sources["curated_lists"]
allowed = sorted(k for k, v in lists.items() if v["decision"] == "allow")
refused = sorted(k for k, v in lists.items() if v["decision"] != "allow")
reasons = "\n".join(f"| `{k}` | {fmt(v)} | {summary['reason_meanings'].get(k, 'KEI-805 exclusion reason, carried over')} |"
                    for k, v in n["rejection_reasons"].items())
strategies = "\n".join(f"| `{k}` | {fmt(v)} |" for k, v in n["candidates_by_strategy"].items())
seed_rows = "\n".join(
    f"| {c['line']} | {c['name']} | `{c.get('slug') or '-'}` | {c['outcome']} | "
    f"{', '.join(c.get('reasons') or []) or (c.get('maintenance') or '') + (' / ' + c['quality_lane'] if c.get('quality_lane') else '')} |"
    for c in coverage)

doc = f"""# KEI-811 — acceptance map

Corpus `as_of` {summary['as_of']} (verification clock); frontier `as_of` {summary['frontier_as_of']}.
Validation: **{sum(c['ok'] for c in report['checks'])} of {len(report['checks'])} checks pass**
(`data/discover/corpus/reports/validation_report.json`). Status: internal, not published.

## Headline counts

| | |
|---|---|
| raw candidate addresses discovered | {fmt(n['raw_candidate_addresses'])} |
| addresses evaluated (cap {fmt(n['evaluated_addresses'])}) | {fmt(n['evaluated_addresses'])} |
| **raw unique repositories** (resolved by host id, plus unresolved addresses) | **{fmt(n['raw_unique_repositories'])}** |
| resolved unique repositories | {fmt(n['resolved_unique_repositories'])} |
| **retained** | **{fmt(n['retained'])}** |
| of which **recommendation-eligible** (full KEI-805 bar) | **{fmt(n['recommendation_eligible'])}** |
| of which licence-exception lane (retained, not eligible) | {fmt(n['retained_licence_exception_lane'])} |
| **rejected** repositories (records) | **{fmt(n['rejected_repositories'])}** ({fmt(n['rejected_records'])}) |
| lanes: replace only / discover only / both | {fmt(n['lanes']['replace_only'])} / {fmt(n['lanes']['discover_only'])} / {fmt(n['lanes']['both'])} |
| maintenance of retained: active / maintained | {fmt(n['maintenance']['active'])} / {fmt(n['maintenance']['maintained'])} |
| relationship candidates | {fmt(n['relationship_candidates'])} |

### Candidates by strategy (an address reached by several routes counts once per strategy)

| strategy | candidates |
|---|---|
{strategies}

### Rejection reasons (a record may carry more than one)

| reason | records | meaning |
|---|---|---|
{reasons}

## Criteria

**AC1 — Replace lane intact and identifiable.** The KEI-805 dataset is read, never
written: its six files hash exactly as pinned before KEI-811 began
(`data/discover/kei805_baseline.sha256`). Every KEI-805 project that KEI-805 retained is
in the corpus with `lanes.replace = true`, `replace.kei805_oss_id`, its KEI-805 verdict
carried as `quality.basis = "kei805"`, and its SaaS edges as `saas_replacement_candidate`
relationship candidates. Checks: {ck('kei805_dataset_unchanged', 'replace_lane_complete', 'replace_lane_verdicts_carried_not_recomputed')}.

**AC2 — Discover lane without a SaaS edge.** {fmt(n['lanes']['discover_only'])} retained
projects are in the Discover lane only, none with a SaaS edge; `replace` is null on
them. Check: {ck('discover_lane_accepts_without_saas_edge')}.

**AC3 — Keith's examples as seeds, no per-project ingestion.** `config/discover_seeds.md`
is the ticket's table pasted verbatim; the harvest parses every GitHub URL in it (and any
pasted later). GitHub accounts in `config/discover.yaml` (`keithdealwis-ui`) are read on
every harvest; that account currently has
{sources['github_accounts'].get('keithdealwis-ui', {}).get('starred', 0)} starred and
{sources['github_accounts'].get('keithdealwis-ui', {}).get('watching', 0)} watched
repositories. All {len(coverage)} seed rows are accounted for below. Checks:
{ck('every_seed_row_accounted_for', 'seed_file_is_the_input')}.

**AC4 — Recursive, bounded, reproducible discovery.** Seven strategies, each bounded
in `config/discover.yaml`; depth-2 recursion from the {len(sources['recursion']['frontier'])}
depth-1 candidates reached by at least two independent routes. Every strategy's query,
status, pinned commit and licence decision is in `sources.json`; everything returned is
in `data/discover/frontier.json`, from which the build recomputes the universe.
Curated lists used: {', '.join(allowed)}. Refused (no recognised licence):
{', '.join(refused) or 'none'}. Checks: {ck('recursion_bounded', 'multiple_strategies_used', 'frontier_matches_config')}.

**AC5 — ~10,000 candidates without lowering the bar.** {fmt(n['resolved_unique_repositories'])}
unique repositories evaluated, resolved by host id, out of {fmt(n['raw_candidate_addresses'])}
candidate addresses discovered. The quality bar is `config/policy.yaml` unchanged, applied by
the KEI-805 code. Metadata rejections are only those the policy already makes sufficient
(archived, disabled, empty, no push inside the window, fork/mirror alias, not found).
Everything else is inspected in full. The one scale measure — stop inspecting once the
evidence already in hand decides the result — is proven equivalent by
`tests/test_discover.py::TestEarlyStop`, is off by default, and leaves KEI-805's own
behaviour and tests unchanged. Checks: {ck('raw_universe_order_10k', 'quality_bar_not_lowered', 'quality_policy_is_kei805_policy', 'no_lookup_left_unexplained')}.

**AC6 — Provenance for every retained project.** Each record's `provenance` lists every
route that reached it (strategy, source, depth, via, locator) and `entry_reason` says
why in one line. Check: {ck('provenance_for_every_retained')}.

**AC7 — Deterministic de-duplication.** Records are keyed by the host's numeric
repository id, so renamed, transferred and differently-cased addresses resolve to one
record with every address in `aliases`. Forks and mirrors are rejected as aliases with
their parent recorded. Checks: {ck('unique_ids', 'each_address_one_record', 'retained_and_rejected_disjoint', 'forks_point_to_parent')}.

**AC8 — Machine-readable rejection reasons.** `rejections.jsonl`: reason codes from a
fixed vocabulary (meanings in `summary.json`), the stage, the routes and the evidence.
Nothing evaluated disappears: retained + rejected = raw. Checks:
{ck('rejection_reasons_known', 'every_candidate_accounted_for')}.

**AC9 — Graph-ready records.** `schema/the-source.discover.schema.json`; stable ids;
`relationship_candidates.jsonl` ({fmt(n['relationship_candidates'])} candidates:
{', '.join(f"{k} {fmt(v)}" for k, v in n['relationship_candidates_by_relation'].items())});
`nodes.jsonl` for owners, topics, languages, lists and SaaS products. Canonical entity
and edge resolution is left to KEI-844. Checks: {ck('schema_valid_projects', 'schema_valid_rejections', 'schema_valid_relationships', 'relationship_endpoints_resolve', 'records_have_stable_ids_and_entities')}.

**AC10 — Appendable history.** `discover-verify` appends one row per repository per
run to `data/discover/history/observations.jsonl` and never rewrites one; records carry
the series in `freshness.history`. Tested by `TestHistory` and
`test_history_is_appended_not_overwritten`. Check: {ck('freshness_history_present')}.

**AC11 — Determinism.** `discover-build` reads only recorded files, uses the snapshot's
clock, and refuses an interrupted, re-policied or mismatched snapshot. The verifier pack
rebuilds twice offline (network refused) and compares bytes with `EXPECTED_DISCOVER.json`.

**AC12 — No publication or endorsement.** Every record carries `endorsed: false` and
`publication_status: internal_not_published`; nothing was published or exported.
Check: {ck('no_endorsement_or_publication')}.

## Keith's seed corpus, row by row

| line | name | repository | outcome | detail |
|---|---|---|---|---|
{seed_rows}
"""
(ROOT / "docs" / "DISCOVER-ACCEPTANCE.md").write_text(doc)
print("wrote docs/DISCOVER-ACCEPTANCE.md;", "all checks pass" if report["ok"] else "SOME CHECKS FAIL")
sys.exit(0 if report["ok"] else 1)
