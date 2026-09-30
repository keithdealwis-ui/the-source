"""Render docs/ACCEPTANCE.md from the dataset on disk, so its figures cannot drift.

Producer evidence for KEI-805. It maps each acceptance criterion to the artefact and
the check that supports it. It does not decide acceptance.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from source_pipeline import common  # noqa: E402

s = common.read_json(common.DATASET / "summary.json")
v = common.read_json(common.REPORTS / "validation_report.json")
m = common.read_json(common.DATASET / "MANIFEST.json")
sources = common.read_json(common.DATASET / "sources.json")
cands = common.read_json(common.REPORTS / "licence_exception_candidates.json")
cfg = common.load_sources()
s_review = len(common.load_yaml(common.CONFIG / "edge_review.yaml")["rejected"])
by_crit = {}
for c in v["checks"]:
    by_crit.setdefault(c["criterion"], []).append(c)
L, V, R, H = s["launch"], s["verified"], s["reconciled"], s["harvest"]
LANE = s["licence_exception_lane"]
obs = {c["id"]: c["observed"] for c in v["checks"] + v.get("observations", [])}
stars = obs["stars.rejected_despite_stars"]


def checks(n):
    rows = []
    for c in by_crit.get(n, []):
        rows.append(f"| `{c['id']}` | {c['description']} | {'pass' if c['ok'] else 'FAIL'} | {c['failures']} |")
    for o in v.get("observations", []):
        if o["criterion"] == n:
            rows.append(f"| `{o['id']}` | {o['description']} | recorded figure, asserts nothing | n/a |")
    return "\n".join(["| Check | What it establishes | Result | Failures |", "|---|---|---|---|", *rows])


CRITERIA = [
    (1, "Canonical schema exists for SaaS products, OSS projects and SaaS↔OSS relationship edges.",
     "`schema/the-source.schema.json` defines `source`, `saas_product`, `oss_project` and `relationship`. "
     "`docs/SCHEMA.md` describes it. Every record in the dataset is validated against it on every run: "
     f"{len(sources)} sources, {R['saas_products_named']} SaaS products, {R['oss_candidates']} OSS projects, "
     f"{R['relationship_candidates']} relationships.",
     "tests: `TestBuiltDataset::test_records_conform_to_schema`"),
    (2, "Provenance is mandatory at source/relationship level.",
     "The schema requires at least one provenance entry on every relationship and every project, and each entry "
     "must name a registered source, its pinned 40-character commit and a locator (file and line, or file and key). "
     "A record without it fails validation.",
     "tests: `TestProvenance` (3 tests, including mutation of a real edge to confirm the schema refuses it)"),
    (3, "Initial upstream sources are licence-checked with allow/refuse rationale recorded.",
     f"`config/sources.yaml` and `docs/LICENCE-REVIEW.md`. {len(sources)} sources harvested, each with licence as read, "
     "decision, rationale, conditions and the SHA-256 of the licence file the decision was made about. "
     f"{sum(len(x.get('refused_adjacent') or []) for x in sources)} adjacent websites/APIs and "
     f"{len(cfg.get('investigated_not_harvested') or [])} further lists were investigated and refused or found not applicable, "
     "with reasons. All five seeds named on the ticket are covered.",
     "tests: `TestLicenceOfSources` (licence drift stops the harvest; a refused source is not parsed)"),
    (4, "Ingestion/normalisation can run repeatably rather than by hand.",
     "`python -m source_pipeline run` performs harvest, live verification, build and validation in one command. "
     "`run --offline` rebuilds from pinned inputs with no network. Two consecutive offline runs produced the same "
     "`MANIFEST.json`; see `logs/determinism.txt`.",
     "tests: `TestBuiltDataset::test_rebuild_is_byte_identical`, `TestNormalise::test_output_is_independent_of_input_order`"),
    (5, "Duplicate products/repos/relationships are reconciled deterministically.",
     f"{R['oss_candidates_before_identity_merge']} harvested repository keys reconciled to {R['oss_candidates']} projects; "
     f"{R['renamed_or_duplicate_repositories_merged']} were the same repository under an old name or URL and were merged "
     "on the host's own canonical identity. SaaS spellings resolve through the reviewed catalogue. One edge per "
     "(SaaS, OSS) pair, with each upstream statement kept as its own provenance entry. Rules are in `docs/SCHEMA.md`.",
     "tests: `TestNormalise` "),
    (6, "Launch dataset targets 100 high-value SaaS products, 250+ unique OSS projects, and approximately 300–400 validated SaaS↔OSS replacement relationships.",
     "**100 SaaS is read as a floor**, by strategy decision `dr-KEI-805-507e4f70` (Option A, 2026-09-27T05:37:42Z). At exactly "
     "100 SaaS the unchanged quality bar gave 231 distinct projects. The same ranking was continued, one product at a time, "
     f"until 250 was reached: {L['saas_added_beyond_floor']} SaaS products were added, and {L['saas_named_by_a_single_source']} of the "
     "launch SaaS products are named by a single source. "
     f"**{L['saas_products']} SaaS products, {L['oss_projects']} OSS repositories ({L['oss_distinct_products']} distinct products), "
     f"{L['relationships']} validated relationships.** "
     + ("All three targets are met. " if all(L["targets_met"].values()) else
        f"**The OSS target is NOT met: {L['oss_distinct_products']} against 250+.** The SaaS and relationship targets are met. "
        "This is reported as it is; no rule was loosened to reach the number. See \"Criterion 6 position\" below. ")
     + f"{L['relationships_corroborated_by_2plus_sources']} of the relationships are stated by two or more independent sources. "
     f"Every validated relationship was read in a desk review of plausibility, which rejected {s_review} upstream claims "
     "with reasons (`config/edge_review.yaml`). \"High value\" is decided by how many independent sources name the SaaS "
     "product, then by validated alternatives; never by stars. The licence-exception lane is not counted in any of these "
     f"figures. The full recommendable corpus behind the launch set is {V['saas_with_validated_alternatives']} SaaS products, "
     f"{V['oss_recommendable']} recommendable projects and {V['relationships_validated']} validated relationships.",
     "tests: `TestBuiltDataset::test_launch_holds_only_recommendable_projects`; the size targets are asserted by the `launch.targets` validation check, not by the test suite"),
    (7, "Publicly recommended OSS projects must have meaningful activity within the previous 12 months. Repositories with no meaningful activity for >12 months are excluded by default.",
     f"Of {R['oss_candidates']} projects verified live, {V['maintenance_status'].get('stale', 0)} were classed stale and excluded. "
     "Every recommended project carries its dated evidence in `derived.maintenance.evidence`.",
     "tests: `TestMeaningfulActivity::test_class_boundaries`"),
    (8, "\"Meaningful activity\" must not be satisfied by trivial README/docs-only churn alone.",
     "Qualifying evidence is a commit or merged pull request made by a person and touching at least one path that is "
     "neither documentation nor housekeeping, or a release or tag whose name is a version and which ships more than "
     "documentation, judged from the diff against the previous version. Not counted: documentation, images, "
     "translations, CI definitions, dependency manifests, lockfiles, automation accounts, floating tags such as "
     "`nightly`. Patterns are declared in `config/policy.yaml`. What was looked at and rejected is recorded under "
     "`derived.maintenance.disregarded` with the reason. Closed issues are recorded as supporting only. The validation "
     "check re-derives every piece of evidence from the raw host observations rather than trusting the build's summary.",
     "tests: `TestMeaningfulActivity`"),
    (9, "Archived repositories are excluded automatically.",
     f"{obs['archived.excluded']} archived repositories were found among the candidates; none is recommended and each "
     "carries the `archived` exclusion reason. Archive state is read from the host, not from any list.",
     "tests: `TestEligibility::test_archived_is_excluded_even_if_recently_active`"),
    (10, "Repositories without a recognised/usable open-source licence are excluded from normal recommendations unless explicitly reviewed and approved as an exception.",
     f"**Excluded from recommendations: all of them.** {LANE['oss_projects']} projects whose only shortcoming is the licence are "
     f"retained in the `licence_exception` lane, with {LANE['relationships_flagged']} relationships, under Keith's direction "
     "(Linear KEI-805 comment 258b38dd). Every one is `recommendation_eligible: false`, none is in the launch set or the "
     "export, and each carries an issue record: category, exact reason, what was detected, upstream claims, evidence, "
     "review status. By category: " + ", ".join(f"{k} {n}" for k, n in LANE["by_issue_category"].items()) + ". "
     f"A further {LANE['oss_with_licence_issue_also_rejected_for_other_reasons']} projects have a licence issue and another "
     "failing (archived, stale) and are in the `rejected` lane, also with the issue recorded. "
     f"**Named exceptions approved for recommendation: {LANE['named_exceptions_approved']}.** The pipeline refuses an "
     "exception whose approver is an executor or is missing. Where the host's detector reports \"Other\", the licence "
     "file text is matched against pinned reference texts and admitted only if it is a plain standard licence with no "
     "added terms, no carve-outs and no enterprise directory.",
     "tests: `TestLicenceLane`, `TestLicenceTextDetection`, `TestEligibility::test_unrecognised_licence_is_excluded`, `test_licence_exception_needs_a_human_approver`"),
    (11, "Maintenance classification is explicit: Active, Maintained, Stale.",
     "`derived.maintenance.status` on every verified project: "
     + ", ".join(f"{k} {n}" for k, n in sorted(V["maintenance_status"].items())) + ". "
     "\"unverified\" are repositories that could not be found or whose host cannot be queried; they are excluded.",
     "tests: `TestMeaningfulActivity::test_class_boundaries` (90/91 and 365/366 day boundaries)"),
    (12, "Stars are a discovery/adoption signal only and are never sufficient on their own to qualify a project.",
     f"Stars are stored under `live` and are not an input to eligibility, edge validation or launch ordering. "
     f"{stars['rejected_with_10k_plus_stars']} projects with 10,000 or more stars were rejected, for example "
     + "; ".join(stars["examples"][:3]) + ".",
     "tests: `TestEligibility::test_stars_never_qualify_a_project`, `test_stars_never_disqualify_a_project`, `test_stars_are_not_an_input_to_any_decision`"),
    (13, "The ingestion strategy should harvest broadly and reject aggressively.",
     f"{H['entries_read']} upstream entries and {H['relationship_claims_read']} relationship claims read from {len(sources)} sources. "
     f"{R['oss_candidates']} candidate projects; {V['oss_recommendable']} recommendable, {V['oss_rejected']} rejected. "
     f"{R['relationship_candidates']} candidate relationships; {V['relationships_validated']} validated, "
     f"{V['relationships_rejected']} rejected. Every rejection is recorded with its reason in `rejections.jsonl` "
     f"({s['rejections']['total']} records).",
     "—"),
    (14, "Imported data is clearly distinguished from independently refreshed/live fields.",
     "Every value on a project sits under `imported` (tagged with its source, never refreshed), `live` (dated by "
     "`fetched_at`, read from the host) or `derived` (computed, dated by `evaluated_as_of`). There are no top-level "
     "licence, star or activity fields. An upstream's licence claim and the verified licence are both kept, separately.",
     "tests: `TestBuiltDataset::test_imported_and_live_fields_are_separate`, `test_upstream_licence_claims_never_decide_eligibility`"),
    (15, "No copied dataset is published in a way that violates upstream licence/terms.",
     "**Nothing has been published.** The dataset is marked `internal_not_published`. No repository was pushed and no "
     "external service was written to. The export guard builds a publishable view locally with the notices each licence "
     f"requires and withholds the {L['relationships_share_alike_only']} launch relationships that rest only on share-alike "
     "material. Open questions for Keith before any publication are in `docs/LICENCE-REVIEW.md`.",
     "tests: `TestBuiltDataset::test_export_withholds_share_alike_and_publishes_nothing`"),
]

import collections  # noqa: E402

_edges = [e for e in common.read_jsonl(common.DATASET / "relationships.jsonl") if e["validation"]["status"] == "validated"]
_oss = {p["oss_id"]: p for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl")}
_saas = {x["saas_id"]: x for x in common.read_jsonl(common.DATASET / "saas_products.jsonl")}
_per = collections.defaultdict(list)
for _e in _edges:
    _per[_e["saas_id"]].append(_e)
_ranked = sorted(_per, key=lambda k: (-_saas[k]["derived"]["source_count"], -len(_per[k]), _saas[k]["name"].lower()))
_rows, _seen, _n = [], set(), 0
for _i, _k in enumerate(_ranked, 1):
    _es = _per[_k][: L["cap_per_saas"]]
    _n += len(_es)
    _seen |= {_oss[e["oss_id"]]["product_group"] for e in _es}
    if _i in (100, 110, 120, 125, 130, len(_ranked)):
        _rows.append(f"| {_i} | {len(_seen)} | {_n} | {_saas[_k]['derived']['source_count']} |")
POSITION = [] if all(L["targets_met"].values()) else [
    "## Criterion 6 position",
    "",
    f"At exactly 100 SaaS products the quality bar yields {L['oss_distinct_products']} distinct OSS projects, short of 250.",
    "The shortfall is a property of the data at this bar, not a fault in the pipeline. Approximate figures if the same",
    "ranking simply continued past 100 (before removing a second repository of the same product):",
    "",
    "| SaaS products | Distinct OSS projects | Relationships | Fewest sources naming any included SaaS |",
    "|---|---|---|---|",
    *_rows,
    "",
    "Ways to close it, none of which the producer may choose alone:",
    "",
    "| Option | Effect | Cost |",
    "|---|---|---|",
    "| Read \"100 SaaS\" as a floor | about 125 SaaS reaches 250+ projects inside 400 relationships | the added SaaS products are each named by a single source |",
    f"| Approve named licence exceptions | {LANE['oss_projects']} flagged projects are available, many of them the best-known alternatives | recommending software that is not open source under its own label |",
    "| Accept the launch set as it is | nothing changes | the 250+ target is recorded as missed |",
    "| Harvest further sources in a follow-on | more corroboration and breadth | time; nine sources are already in, and seven more were checked and refused or not applicable |",
    "",
    "What was deliberately not done: ranking SaaS products by number of alternatives instead of by independent sources.",
    "It raises the count by replacing better-attested products with weaker ones.",
    "",
]

out = [
    "# KEI-805 — acceptance criteria mapped to evidence",
    "",
    "Producer evidence, prepared by the executor that did the work (`claude-heavy`). It is not",
    "independent verification.",
    "",
    f"- Dataset as of: `{s['as_of']}`",
    f"- Validation: {sum(1 for c in v['checks'] if c['ok'])} of {len(v['checks'])} checks pass",
    f"- Publication status: `{s['publication_status']}`",
    f"- Dataset files and SHA-256: `dataset/MANIFEST.json` ({len(m['sha256'])} files)",
    "",
    "Paths below are relative to the evidence pack root.",
    "",
]
for n, text, evidence, tests in CRITERIA:
    out += [f"## {n}. {text}", "", evidence, "", f"Artefacts and {tests}", "", checks(n), ""]

out += [
    *POSITION,
    "## Findings recorded by the producer",
    "",
    "All are residue that the producer judges NON_BLOCKING. They are listed so the verifier can disagree.",
    "",
    "| Finding |",
    "|---|",
    f"| {len(cands)} well-known projects are open-core or source-available and are excluded under criterion 10 pending any exception Keith chooses to approve. |",
    f"| \"Validated\" includes a desk review of plausibility but not a test of fitness. {L['relationships'] - L['relationships_corroborated_by_2plus_sources']} of {L['relationships']} launch relationships rest on a single upstream's claim. Testing and scoring are out of scope. |",
    f"| Reaching 250 projects took {L['saas_added_beyond_floor']} SaaS products beyond the first 100, under the floor reading strategy approved. Those are less well attested than the first 100; {L['saas_named_by_a_single_source']} launch SaaS products are named by one source only. The launch set sits exactly at the 250 minimum, so a single project going stale would take it below. |",
    "| The five seeds alone did not reach the launch targets at this quality bar. Four further licence-checked sources and a reviewed table of repository overrides were added. |",
    f"| {L['relationships_share_alike_only']} launch relationships rest only on CC BY-SA 3.0 material and are withheld from a public export by default. Where a relationship is also stated by a permissive source, the export cites the permissive source; the project name in that row may still have been first read from the share-alike list. |",
    "| altstack-data states two different licences for itself. Both permit reuse and both sets of terms are met. |",
    "| GitHub mirrors of projects whose home is elsewhere (for example MediaWiki, WordPress, Moodle) are verified as the mirror. The mirror flag is recorded under `live` and is not used to exclude. |",
    "| Live verification inspects the 15 newest commits and the 10 most recently UPDATED merged pull requests; GitHub cannot order pull requests by merge date. A project whose recent history is all automation is judged on releases alone, which can understate it. |",
    "| GitLab and Codeberg projects (a handful) are verified on commits, releases and tags only; merged requests are not read there. |",
    "| Identity-drift rejection is conservative: it also excludes a few projects that were genuinely renamed (for example redkubes/otomi-core to linode/apl-core). They are listed in `rejections.jsonl`. |",
    "| Open-core projects whose licence the host reports as a single recognised licence (for example one with an `ee/` directory but an AGPL root licence detected by GitHub) are admitted on the host's word; the enterprise-directory check runs only where the host reports no recognised licence. |",
    "| The work is not under version control. Strategy approved a dedicated private repository; creating it and committing is left to a sanctioned executor. |",
    "",
    "## Producer-side adversarial review",
    "",
    "Before this pack was frozen the producer commissioned an adversarial review of its own work by a separate agent.",
    "It is producer evidence, not independent verification. It found defects that were then fixed, including projects",
    "admitted on documentation-only tags and CI churn, a licence matcher that accepted added restriction clauses, a",
    "repository repurposed since listing, and validation checks that could not fail. The fixes and their regression",
    "tests are in this pack; the account is in the outcome ledger under the SELF-CORRECTION entries.",
    "",
    "## What was not done",
    "",
    "- Nothing was published, pushed, or sent to any external service.",
    "- No licence exception was approved.",
    "- No project was tested for functional fitness.",
    "- No repository was created and no commit was made.",
    "",
]
path = common.ROOT / "docs" / "ACCEPTANCE.md"
path.write_text("\n".join(out), encoding="utf-8")
print(path, len(out), "lines")
