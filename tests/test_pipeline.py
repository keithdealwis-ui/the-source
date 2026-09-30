"""Tests for The Source pipeline. Each class names the KEI-805 criterion it guards."""
import copy
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest

from source_pipeline import build, common, harvest, licence_detect, normalise, parsers
from source_pipeline.activity import Rules, evaluate, iso

AS_OF = datetime(2026, 9, 27, tzinfo=timezone.utc)
POLICY = common.load_policy()
RULES = Rules(POLICY)
SOURCES = common.load_sources()
SCHEMA = common.read_json(common.SCHEMA / "the-source.schema.json")
SHA = "a" * 40


def ago(days):
    return iso(AS_OF - timedelta(days=days))


def live(**obs):
    base = {"commits": [], "merged_prs": [], "releases": [], "tags": []}
    base.update(obs)
    return {"found": True, "archived": False, "disabled": False, "empty": False,
            "licence_spdx": "MIT", "stars": 10, "observations": base}


def commit(days, paths, author="alice"):
    return {"ref": "c" * 40, "at": ago(days), "author": author, "paths": paths}


def release(days, ref="v1.2.0", paths=("src/app.py", "CHANGELOG.md"), **kw):
    return dict({"ref": ref, "at": ago(days), "draft": False, "commit": "d" * 40, "paths": list(paths)}, **kw)


def claim(source, url, saas, name="Proj", locator="README.md#L1"):
    return {"source_id": source, "source_commit": SHA, "locator": locator, "oss_name": name,
            "repo_url": url, "declared_licence": None, "category": None, "saas_names": saas}


def validator(name):
    return jsonschema.Draft202012Validator({"$ref": f"#/$defs/{name}", "$defs": SCHEMA["$defs"]})


class TestParsers:
    def test_solvohq(self, tmp_path):
        (tmp_path / "README.md").write_text(
            "## Contents\n\n- [Chat](#chat) (1)\n\n## Team chat\n\n### [Slack](https://x/slack/)\n\n"
            "_Replaces:_ prose that must not be imported\n\n"
            "- [**Zulip**](https://github.com/zulip/zulip) — some upstream prose _(Apache-2.0)_\n")
        rows = list(parsers.parse_solvohq(tmp_path, "README.md"))
        assert rows == [{"oss_name": "Zulip", "repo_url": "https://github.com/zulip/zulip",
                         "declared_licence": "Apache-2.0", "category": "Team chat",
                         "saas_names": ["Slack"], "locator": "README.md#L11"}]

    def test_no_upstream_prose_is_carried(self, tmp_path):
        (tmp_path / "software").mkdir()
        (tmp_path / "software" / "x.yml").write_text(
            "name: X\ndescription: A long upstream sentence (alternative to Slack and Discord).\n"
            "licenses:\n  - MIT\ntags:\n  - Chat\nsource_code_url: https://github.com/a/x\n")
        (row,) = parsers.parse_awesome_selfhosted(tmp_path, "software")
        assert row["saas_names"] == ["Slack", "Discord"]
        assert "upstream sentence" not in json.dumps(row)

    def test_btwso_heading_names(self, tmp_path):
        (tmp_path / "README.md").write_text(
            "### Web analytics (Google Analytics alternatives):\n|Company|Website|\n|:--|:--|\n"
            "[Matomo](https://github.com/matomo-org/matomo)|[matomo.org](https://matomo.org/)|\n")
        (row,) = parsers.parse_btwso(tmp_path, "README.md")
        assert row["saas_names"] == ["Google Analytics"] and row["oss_name"] == "Matomo"


class TestNormalise:
    """Criterion 5: duplicates reconcile deterministically."""

    @pytest.mark.parametrize("url", [
        "https://github.com/Zulip/Zulip", "http://github.com/zulip/zulip.git",
        "https://www.github.com/zulip/zulip/", "https://github.com/zulip/zulip/tree/main/docs",
        "github.com/zulip/zulip#readme",
    ])
    def test_repo_url_variants_collapse(self, url):
        assert normalise.repo_key(url) == ("github.com/zulip/zulip", None)

    @pytest.mark.parametrize("url", ["https://github.com/zulip", "https://zulip.com", "", "https://github.com/orgs/zulip"])
    def test_non_repository_urls_are_rejected(self, url):
        key, why = normalise.repo_key(url)
        assert key is None and why

    def test_saas_spellings_collapse(self):
        _, index = normalise.load_catalogue()
        assert index[normalise.saas_key("AirTable")] == index[normalise.saas_key("Airtable")] == "airtable"
        assert index[normalise.saas_key("YNAB (You Need A Budget)")] == "ynab"
        assert index[normalise.saas_key("ChatGPT Plus")] == "chatgpt"

    def test_same_relationship_from_two_sources_is_one_edge_with_two_provenances(self):
        rec = normalise.reconcile([
            claim("solvohq", "https://github.com/Zulip/zulip.git", ["Slack"], "Zulip"),
            claim("runacapital", "https://github.com/zulip/zulip/", ["slack"], "zulip", "submissions/z.yaml"),
        ], SOURCES)
        assert list(rec["oss"]) == ["github.com/zulip/zulip"]
        (edge,) = rec["edges"]
        assert edge["source_count"] == 2 and len(edge["provenance"]) == 2
        assert [p["source_id"] for p in edge["provenance"]] == ["solvohq", "runacapital"]

    def test_output_is_independent_of_input_order(self):
        claims = common.read_jsonl(common.STAGING / "claims.jsonl")
        a = normalise.reconcile(claims, SOURCES)
        shuffled = claims[:]
        random.Random(805).shuffle(shuffled)
        b = normalise.reconcile(shuffled, SOURCES)
        for part in ("oss", "edges", "saas", "uncatalogued"):
            assert common.dumps(a[part]) == common.dumps(b[part])

    def test_renamed_repository_collapses_to_one_project(self):
        rec = normalise.reconcile([
            claim("solvohq", "https://github.com/calendso/calendso", ["Calendly"], "Cal.com"),
            claim("runacapital", "https://github.com/calcom/cal.com", ["Calendly"], "Cal.com", "s/c.yaml"),
        ], SOURCES)
        assert len(rec["oss"]) == 2
        merged = normalise.merge_renamed(rec, {"github.com/calendso/calendso": "github.com/calcom/cal.com"})
        assert list(merged["oss"]) == ["github.com/calcom/cal.com"]
        (edge,) = merged["edges"]
        assert edge["source_count"] == 2
        assert merged["oss"]["github.com/calcom/cal.com"]["harvested_as"] == [
            "github.com/calcom/cal.com", "github.com/calendso/calendso"]

    def test_unknown_saas_is_rejected_not_invented(self):
        rec = normalise.reconcile([claim("solvohq", "https://github.com/a/b", ["Totally Unknown Product"])], SOURCES)
        assert rec["edges"] == [] and rec["rejections"][0]["reason"] == "saas_not_in_catalogue"

    def test_display_name_prefers_repository_name(self):
        names = [{"value": "Grafana stack (Mimir + Loki + Tempo)", "source_id": "solvohq"},
                 {"value": "Grafana", "source_id": "runacapital"}]
        assert normalise.choose_name("github.com/grafana/grafana", names) == "Grafana"


class TestProvenance:
    """Criterion 2: provenance is mandatory."""

    def test_schema_refuses_an_edge_without_provenance(self):
        edge = next(e for e in common.read_jsonl(common.DATASET / "relationships.jsonl"))
        assert not list(validator("relationship").iter_errors(edge))
        for mutate in (lambda e: e.pop("provenance"), lambda e: e.update(provenance=[]),
                       lambda e: e["provenance"][0].pop("locator"),
                       lambda e: e["provenance"][0].pop("source_commit")):
            broken = copy.deepcopy(edge)
            mutate(broken)
            assert list(validator("relationship").iter_errors(broken))

    def test_schema_refuses_a_project_without_provenance(self):
        project = common.read_jsonl(common.DATASET / "oss_projects.jsonl")[0]
        broken = copy.deepcopy(project)
        broken["provenance"] = []
        assert list(validator("oss_project").iter_errors(broken))

    def test_every_built_edge_traces_to_a_pinned_commit(self):
        pins = {s["source_id"]: s["pinned_commit"] for s in SOURCES["sources"]}
        for e in common.read_jsonl(common.DATASET / "relationships.jsonl"):
            assert e["provenance"]
            for p in e["provenance"]:
                assert p["source_commit"] == pins[p["source_id"]] and p["locator"]


class TestLicenceOfSources:
    """Criterion 3: sources are licence-checked; drift fails closed."""

    def test_every_source_has_a_decision_and_rationale(self):
        for s in SOURCES["sources"]:
            assert s["decision"] in ("allow", "allow_with_conditions", "refuse")
            assert len(s["rationale"]) > 40 and len(s["licence_file_sha256"]) == 64

    def test_changed_licence_file_stops_the_harvest(self, tmp_path):
        (tmp_path / "LICENSE").write_text("a different licence")
        source = dict(SOURCES["sources"][0])
        with pytest.raises(harvest.LicenceDrift):
            harvest.verify_licence(source, tmp_path)

    def test_refused_source_is_not_parsed(self, monkeypatch, tmp_path):
        cfg = copy.deepcopy(SOURCES)
        for s in cfg["sources"]:
            s["decision"] = "refuse"
        monkeypatch.setattr(common, "load_sources", lambda: cfg)
        monkeypatch.setattr(common, "STAGING", tmp_path)
        stats = harvest.run(offline=True)
        assert all(v["harvested"] is False for v in stats.values())
        assert common.read_jsonl(tmp_path / "claims.jsonl") == []


class TestMeaningfulActivity:
    """Criteria 7, 8, 11."""

    def test_documentation_only_commits_do_not_count(self):
        m = evaluate(live(commits=[commit(3, ["README.md", "docs/guide.rst", "assets/images/logo.png"])]), RULES, AS_OF)
        assert m["status"] == "stale" and m["evidence"] == []
        assert m["disregarded"][0]["why"] == "documentation_or_housekeeping_only"

    def test_bot_commits_do_not_count(self):
        m = evaluate(live(commits=[commit(3, ["package.json"], "dependabot[bot]"),
                                   commit(4, ["go.mod"], "renovate-bot")]), RULES, AS_OF)
        assert m["status"] == "stale"

    def test_bot_merged_pr_does_not_count(self):
        m = evaluate(live(merged_prs=[commit(3, ["src/a.py"], "github-actions[bot]")]), RULES, AS_OF)
        assert m["status"] == "stale"

    def test_mixed_commit_counts_and_evidence_lists_only_code_paths(self):
        m = evaluate(live(commits=[commit(10, ["README.md", "src/app.py"])]), RULES, AS_OF)
        assert m["status"] == "active"
        assert m["evidence"][0]["code_paths_sample"] == ["src/app.py"]

    @pytest.mark.parametrize("days,status", [(0, "active"), (90, "active"), (91, "maintained"),
                                              (365, "maintained"), (366, "stale"), (900, "stale")])
    def test_class_boundaries(self, days, status):
        m = evaluate(live(releases=[release(days)]), RULES, AS_OF)
        assert m["status"] == status

    def test_draft_release_does_not_count(self):
        m = evaluate(live(releases=[release(5, draft=True)]), RULES, AS_OF)
        assert m["status"] == "stale"

    @pytest.mark.parametrize("paths", [
        [".github/workflows/ci.yml"], ["package-lock.json", "Cargo.lock"], ["composer.json"],
        ["package.json", "pnpm-lock.yaml", "README.md"], ["docker-compose.yml", ".env.example"],
        ["frontend/resources/translations/en-US.json"], ["app/res/values-de/strings.xml"],
    ])
    def test_ci_manifest_lockfile_and_translation_churn_does_not_count(self, paths):
        m = evaluate(live(commits=[commit(3, paths)]), RULES, AS_OF)
        assert m["status"] == "stale", paths

    @pytest.mark.parametrize("path", [
        "requirements.txt", "CMakeLists.txt", "src/history.ts", "app/models/changeset.rb",
        "lib/license_checker.py", "src/components/Notice.tsx", "pkg/lang/parser.go",
        "src/site/handler.go", "internal/wiki/page.go", "core/i18n/loader.go", "Dockerfile",
    ])
    def test_source_files_with_documentation_like_names_are_code(self, path):
        assert RULES.is_code_path(path)

    @pytest.mark.parametrize("login", ["Jenkins", "nextcloud-command", "woocommercebot", "actions-user",
                                       "Dokku Bot", "ArchiveBox Release Bot", "openprojectci",
                                       "cloudquery-ci", "Hosted Weblate", "dependabot[bot]"])
    def test_automation_accounts_are_recognised(self, login):
        assert RULES.is_bot(login)

    @pytest.mark.parametrize("login", ["alice", "abbott", "botond", "marci", "luci", "robotnik", None])
    def test_people_are_not_mistaken_for_automation(self, login):
        assert not RULES.is_bot(login)

    @pytest.mark.parametrize("ref", ["nightly", "latest", "stable", "main-gha.34566", "latest-commit-build"])
    def test_floating_tags_are_not_releases(self, ref):
        m = evaluate(live(tags=[release(2, ref=ref)], releases=[release(2, ref=ref)]), RULES, AS_OF)
        assert m["status"] == "stale"
        assert {d["why"] for d in m["disregarded"]} == {"not_a_version_tag"}

    def test_release_that_ships_only_documentation_does_not_count(self):
        m = evaluate(live(tags=[release(20, ref="6.9.0", paths=["CHANGELOG.md", "VERSION.md"])]), RULES, AS_OF)
        assert m["status"] == "stale"
        assert m["disregarded"][0]["why"] == "tagged_commit_documentation_only"

    def test_release_whose_contents_could_not_be_read_does_not_count(self):
        m = evaluate(live(releases=[release(20, paths=[])]), RULES, AS_OF)
        assert m["status"] == "stale"

    def test_version_bump_release_counts(self):
        m = evaluate(live(releases=[release(20, paths=["package.json", "CHANGELOG.md"])]), RULES, AS_OF)
        assert m["status"] == "active" and m["evidence"][0]["shipped_paths_sample"] == ["package.json"]

    def test_closed_issues_alone_do_not_qualify(self):
        m = evaluate(live(issues_closed_12m=500), RULES, AS_OF)
        assert m["status"] == "stale" and m["supporting"]["issues_closed_12m"] == 500

    def test_future_dated_evidence_is_ignored(self):
        m = evaluate(live(releases=[release(-30)]), RULES, AS_OF)
        assert m["status"] == "stale"


class TestEligibility:
    """Criteria 9, 10, 12."""

    def verdict(self, lv):
        m = evaluate(lv, RULES, AS_OF)
        lic = build.licence_verdict(lv, POLICY, {}, "github.com/a/b")
        return build.eligibility(lv, m, lic)

    def good(self, **kw):
        lv = live(commits=[commit(5, ["src/a.py"])])
        lv.update(kw)
        return lv

    def test_healthy_project_is_recommended(self):
        assert self.verdict(self.good()) == {"recommended": True, "exclusion_reasons": []}

    def test_archived_is_excluded_even_if_recently_active(self):
        assert "archived" in self.verdict(self.good(archived=True))["exclusion_reasons"]

    @pytest.mark.parametrize("spdx", [None, "NOASSERTION", "BUSL-1.1", "SSPL-1.0", "Elastic-2.0", "CC-BY-NC-4.0"])
    def test_unrecognised_licence_is_excluded(self, spdx):
        v = self.verdict(self.good(licence_spdx=spdx))
        assert v == {"recommended": False, "exclusion_reasons": ["no_recognised_open_source_licence"]}

    def test_stars_never_qualify_a_project(self):
        stale = live(commits=[commit(3, ["README.md"])])
        for stars in (0, 250_000):
            stale["stars"] = stars
            assert self.verdict(stale)["recommended"] is False
        unlicensed = self.good(licence_spdx=None, stars=250_000)
        assert self.verdict(unlicensed)["recommended"] is False

    def test_stars_never_disqualify_a_project(self):
        assert self.verdict(self.good(stars=0))["recommended"] is True

    def test_stars_are_not_an_input_to_any_decision(self):
        import inspect
        from source_pipeline import activity
        for fn in (build.eligibility, build.select_launch, activity.evaluate, build.licence_verdict):
            assert "stars" not in inspect.getsource(fn).replace("Stars are not consulted", "")

    def test_repository_renamed_into_something_else_is_excluded(self):
        project = {"harvested_as": ["github.com/casejs/case", "github.com/mnfst/manifest"],
                   "imported": {"names": [{"value": "CASE"}, {"value": "Manifest"}]}}
        assert build.identity_changed("github.com/mnfst/llm-gateway", project)
        lv = self.good()
        m = evaluate(lv, RULES, AS_OF)
        lic = build.licence_verdict(lv, POLICY, {}, "github.com/mnfst/llm-gateway")
        assert build.eligibility(lv, m, lic, True)["exclusion_reasons"] == ["repository_identity_changed_since_listed"]

    def test_ordinary_rename_is_not_treated_as_a_different_project(self):
        project = {"harvested_as": ["github.com/calendso/calendso", "github.com/calcom/cal.com"],
                   "imported": {"names": [{"value": "Cal.com"}]}}
        assert not build.identity_changed("github.com/calcom/cal.com", project)
        same = {"harvested_as": ["github.com/zulip/zulip"], "imported": {"names": [{"value": "Zulip"}]}}
        assert not build.identity_changed("github.com/zulip/zulip", same)

    def test_missing_repository_is_excluded(self):
        v = build.eligibility({"found": False, "error": "not_found"}, None, None)
        assert v == {"recommended": False, "exclusion_reasons": ["repository_not_found"]}

    def test_licence_exception_needs_a_human_approver(self, monkeypatch):
        for approver in ("claude-heavy", "draper", ""):
            monkeypatch.setattr(common, "load_yaml", lambda p, a=approver: {"exceptions": [
                {"oss_id": "github.com/a/b", "approved_by": a, "approval_ref": "KEI-1"}]})
            with pytest.raises(ValueError):
                build.load_exceptions()
        monkeypatch.setattr(common, "load_yaml", lambda p: {"exceptions": [
            {"oss_id": "github.com/a/b", "approved_by": "keith", "approval_ref": "KEI-1 comment"}]})
        exc = build.load_exceptions()
        lv = self.good(licence_spdx="NOASSERTION")
        lic = build.licence_verdict(lv, POLICY, exc, "github.com/a/b")
        assert lic["usable"] and not lic["recognised"] and lic["exception"]["approved_by"] == "keith"


MIT = (common.CONFIG / "licence_texts" / "MIT.txt").read_text().replace("[year]", "2024").replace("[fullname]", "Alice")
GPL3 = (common.CONFIG / "licence_texts" / "GPL-3.0.txt").read_text()


class TestLicenceTextDetection:
    """Criterion 10: the second-opinion detector admits only plain standard licences."""

    def test_plain_mit(self):
        d = licence_detect.detect([{"name": "LICENSE", "type": "file"}], {"LICENSE": MIT})
        assert d["recognised"] and d["spdx"] == "MIT"

    def test_gpl_with_short_preface(self):
        text = "Project X\nCopyright 2020 the X authors\n\n" + GPL3
        d = licence_detect.detect([{"name": "COPYING", "type": "file"}], {"COPYING": text})
        assert d["recognised"] and d["spdx"] == "GPL-3.0"

    def test_open_core_carve_out_is_refused(self):
        text = ("Portions of this software are licensed as follows:\n\n- All content that resides under the "
                "\"packages/backend\" directory is licensed under the license defined in that directory.\n\n" + MIT)
        d = licence_detect.detect([{"name": "LICENSE", "type": "file"}], {"LICENSE": text})
        assert not d["recognised"] and d["per_file"]["LICENSE"]["restriction_markers"]

    def test_enterprise_directory_is_refused(self):
        d = licence_detect.detect([{"name": "LICENSE", "type": "file"}, {"name": "ee", "type": "dir"}], {"LICENSE": MIT})
        assert not d["recognised"] and "enterprise_directory_at_root" in d["refusal_reasons"]

    def test_second_non_standard_licence_file_is_refused(self):
        d = licence_detect.detect(
            [{"name": "LICENSE-MIT", "type": "file"}, {"name": "LICENSE-EE", "type": "file"}],
            {"LICENSE-MIT": MIT, "LICENSE-EE": "Business Source License 1.1. Commercial use requires a subscription."})
        assert not d["recognised"]

    def test_commons_clause_is_refused(self):
        d = licence_detect.detect([{"name": "LICENSE", "type": "file"}],
                                  {"LICENSE": '"Commons Clause" License Condition v1.0\n\n' + MIT})
        assert not d["recognised"]

    def test_ordinary_words_are_not_mistaken_for_an_ee_path(self):
        assert not licence_detect.DIRECTORY_REFERENCE.search("free/libre software, see guarantee/terms")
        assert licence_detect.DIRECTORY_REFERENCE.search('files under "ee/" are proprietary')

    @pytest.mark.parametrize("clause", [
        "The Software shall not be used to offer a hosted or managed service to third parties.",
        "Use of the Software by organisations with more than 10 employees requires the purchase of a licence.",
        "This licence applies only to the files under src/community.",
        "Files in the packages/ee tree are licensed under the Acme EE License.",
        "The server component is licensed under the Acme License.",
        "You must not use the Software in production without a subscription.",
    ])
    def test_standard_licence_with_an_added_restriction_is_refused(self, clause):
        d = licence_detect.detect([{"name": "LICENSE", "type": "file"}], {"LICENSE": MIT + "\n\n" + clause})
        assert not d["recognised"], clause

    @pytest.mark.parametrize("note", [
        "The project logo is licensed under the Creative Commons Attribution 4.0 International Public License.",
        "X is licensed under the terms of the GNU General Public License, version 2 or later.",
    ])
    def test_benign_notice_is_accepted(self, note):
        d = licence_detect.detect([{"name": "LICENSE", "type": "file"}], {"LICENSE": MIT + "\n\n" + note})
        assert d["recognised"], note

    def test_bsd3_is_not_reported_as_bsd2(self):
        text = (common.CONFIG / "licence_texts" / "BSD-3-Clause.txt").read_text()
        text = text.replace("[year]", "2020").replace("[fullname]", "Alice")
        d = licence_detect.detect([{"name": "LICENSE", "type": "file"}], {"LICENSE": text})
        assert d["recognised"] and d["spdx"] == "BSD-3-Clause"

    def test_no_licence_file(self):
        d = licence_detect.detect([], {})
        assert not d["recognised"] and d["refusal_reasons"] == ["no_licence_file_at_root"]


LANE = common.load_yaml(common.CONFIG / "licence_lane.yaml")


class TestLicenceLane:
    """Criterion 10 and Keith's direction: licence-issue projects are retained, flagged, never recommended."""

    def issue(self, texts=None, root=None, spdx="NOASSERTION", claims=(), probe=True):
        lv = live(commits=[commit(5, ["src/a.py"])])
        lv["licence_spdx"] = spdx
        lv["licence_name"] = "Other"
        lv["fetched_at"] = "2026-09-27T00:00:00Z"
        if probe:
            texts = texts if texts is not None else {}
            lv["licence_probe"] = {"root": root if root is not None else
                                   [{"name": n, "type": "file"} for n in texts], "texts": texts}
        lic = build.licence_verdict(lv, POLICY, {}, "github.com/a/b")
        imported = {"declared_licences": [{"value": c, "source_id": "solvohq"} for c in claims]}
        return lv, lic, build.licence_issue(lv, lic, imported, POLICY, LANE)

    @pytest.mark.parametrize("texts,root,spdx,claims,probe,category", [
        ({}, [], None, [], True, "missing"),
        ({"LICENSE": "Business Source License 1.1\nLicensor: X"}, None, "NOASSERTION", [], True, "source_available"),
        ({"LICENSE": MIT}, [{"name": "LICENSE", "type": "file"}, {"name": "ee", "type": "dir"}], "NOASSERTION", [], True, "open_core"),
        ({"LICENSE": "Portions of this software are licensed as follows: content under the ee directory is commercial.\n\n" + MIT},
         None, "NOASSERTION", ["MIT"], True, "open_core"),
        ({"LICENSE": MIT + "\n\nYou must not remove the footer."}, None, "NOASSERTION", [], True, "restricted"),
        ({"LICENSE": "some licence"}, None, "CC-BY-NC-4.0", [], True, "non_osi"),
        ({"LICENSE": "A short pointer to a licence held elsewhere."}, None, "NOASSERTION", ["AGPL-3.0"], True, "conflicting"),
        ({"LICENSE": "A short pointer to a licence held elsewhere."}, None, "NOASSERTION", [], True, "unrecognised"),
        (None, None, None, [], False, "undetermined"),
    ])
    def test_issue_categories(self, texts, root, spdx, claims, probe, category):
        lv, lic, issue = self.issue(texts, root, spdx, claims, probe)
        assert issue["category"] == category, issue["reason"]
        assert issue["reason"] and issue["reuse_rights_established"] is False
        assert issue["review_status"] == "flagged_retained_not_reviewed_individually"
        assert issue["retained_under"]["decided_by"] == "keith"
        verdict = build.eligibility(lv, evaluate(lv, RULES, AS_OF), lic)
        assert verdict["recommended"] is False and build.lane_of(verdict) == "licence_exception"

    def test_recognised_licence_has_no_issue(self):
        lv = live(commits=[commit(5, ["src/a.py"])])
        lic = build.licence_verdict(lv, POLICY, {}, "github.com/a/b")
        assert build.licence_issue(lv, lic, {"declared_licences": []}, POLICY, LANE) is None

    def test_licence_issue_plus_another_failure_is_rejected_not_retained_as_candidate(self):
        lv = live(commits=[commit(3, ["README.md"])])
        lv["licence_spdx"] = None
        lv["licence_probe"] = {"root": [], "texts": {}}
        lic = build.licence_verdict(lv, POLICY, {}, "github.com/a/b")
        verdict = build.eligibility(lv, evaluate(lv, RULES, AS_OF), lic)
        assert build.lane_of(verdict) == "rejected"

    def test_upstream_claim_is_kept_beside_what_was_detected(self):
        _, _, issue = self.issue({"LICENSE": "Business Source License 1.1"}, claims=["MIT"])
        assert issue["upstream_claims"] == [{"value": "MIT", "source_id": "solvohq"}]
        assert issue["upstream_claims_disagree_with_repository"] is True
        assert issue["detected"]["host_spdx"] == "NOASSERTION"

    def test_dataset_lane_records_are_flagged_and_never_recommended(self):
        oss = common.read_jsonl(common.DATASET / "oss_projects.jsonl")
        lane = [p for p in oss if p["derived"]["lane"] == "licence_exception"]
        assert len(lane) > 50
        for p in lane:
            d = p["derived"]
            assert d["recommendation_eligible"] is False and d["launch"] is False
            assert d["licence"]["status"] == "issue" and d["licence"]["issue"]["reason"]
            assert not list(validator("oss_project").iter_errors(p))
        ids = {p["oss_id"] for p in lane}
        flagged = [e for e in common.read_jsonl(common.DATASET / "relationships.jsonl") if e["oss_id"] in ids]
        assert flagged
        for e in flagged:
            assert e["recommendation_eligible"] is False and e["launch"] is False
            assert e["validation"]["status"] in ("flagged_licence_exception", "rejected")

    def test_schema_refuses_a_lane_record_marked_recommendable(self):
        p = next(p for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl")
                 if p["derived"]["lane"] == "licence_exception")
        for mutate in (lambda d: d.update(recommendation_eligible=True), lambda d: d.update(launch=True),
                       lambda d: d["licence"].update(issue=None),
                       lambda d: d["licence"]["issue"].update(reuse_rights_established=True),
                       lambda d: d["licence"]["issue"].pop("reason")):
            broken = copy.deepcopy(p)
            mutate(broken["derived"])
            assert list(validator("oss_project").iter_errors(broken))

    def test_export_refuses_a_lane_record(self, monkeypatch, tmp_path):
        from source_pipeline import export
        real = common.read_jsonl

        def tampered(path):
            rows = real(path)
            if Path(path).name == "relationships.jsonl" and "dataset" in str(path):
                victim = next(r for r in rows if r["lane"] == "licence_exception")
                victim["launch"] = True
            return rows

        monkeypatch.setattr(common, "read_jsonl", tampered)
        with pytest.raises(export.ExportRefused):
            export.run(str(tmp_path / "pub"))

    def test_lane_is_counted_apart_from_launch(self):
        s = common.read_json(common.DATASET / "summary.json")
        lane = s["licence_exception_lane"]
        assert lane["oss_projects"] == s["lanes"]["oss_projects"]["licence_exception"]
        assert s["verified"]["oss_recommendable"] == s["lanes"]["oss_projects"]["recommendable"]
        assert "licence_exception" not in json.dumps(s["launch"])
        launch = {p["oss_id"] for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl") if p["derived"]["launch"]}
        lane_ids = {p["oss_id"] for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl")
                    if p["derived"]["lane"] == "licence_exception"}
        assert not launch & lane_ids


class TestNoNetwork:
    """Criterion 4: an offline rebuild provably touches no network."""

    def test_host_lookup_is_refused_when_network_is_forbidden(self, monkeypatch):
        from source_pipeline import enrich
        monkeypatch.setenv("THE_SOURCE_NO_NETWORK", "1")
        with pytest.raises(common.NetworkRefused):
            enrich.Http().call("https://api.github.com/rate_limit")

    def test_upstream_fetch_is_refused_when_network_is_forbidden(self, monkeypatch, tmp_path):
        monkeypatch.setenv("THE_SOURCE_NO_NETWORK", "1")
        monkeypatch.setattr(common, "RAW", tmp_path)
        with pytest.raises(common.NetworkRefused):
            harvest.fetch(dict(SOURCES["sources"][0]))

    def test_offline_harvest_and_build_run_with_network_forbidden(self, monkeypatch):
        from source_pipeline import validate
        monkeypatch.setenv("THE_SOURCE_NO_NETWORK", "1")
        harvest.run(offline=True)
        build.run()
        assert validate.run()["ok"]


class TestFiguresAgree:
    """One number, one meaning: share-alike counts are labelled by population."""

    def test_share_alike_counts_are_labelled_and_consistent(self):
        edges = common.read_jsonl(common.DATASET / "relationships.jsonl")
        s = common.read_json(common.DATASET / "summary.json")
        v = common.read_json(common.REPORTS / "validation_report.json")
        obs = next(c["observed"] for c in v["checks"] if c["id"] == "publication.guard")
        in_launch = sum(1 for e in edges if e["share_alike_only"] and e["launch"])
        assert obs["share_alike_only_relationships_in_launch"] == in_launch
        assert s["launch"]["relationships_share_alike_only"] == in_launch
        assert obs["withheld_from_default_export"] == in_launch
        assert obs["share_alike_only_relationships_all_lanes"] == sum(1 for e in edges if e["share_alike_only"])
        assert "share_alike_only_edges" not in obs
        export = common.DATA / "export-public" / "EXPORT.json"
        if export.exists() and common.read_json(export).get("dataset_as_of") == s["as_of"]:
            assert common.read_json(export)["relationships_withheld_share_alike_only"] == in_launch

    def test_documents_do_not_hard_code_the_share_alike_count(self):
        text = (common.ROOT / "docs" / "LICENCE-REVIEW.md").read_text()
        import re
        assert not re.search(r"\b\d+ launch relationships", text)


class TestBuiltDataset:
    """Criteria 1, 4, 6, 14, 15 against the dataset on disk."""

    def test_rebuild_is_byte_identical(self):
        from source_pipeline import validate

        def snapshot():
            return {str(p.relative_to(common.DATASET)): common.sha256_file(p)
                    for p in sorted(common.DATASET.rglob("*")) if p.is_file()}

        build.run()
        validate.run()
        first = snapshot()
        harvest.run(offline=True)
        build.run()
        validate.run()
        second = snapshot()
        assert second == first, sorted(k for k in first if first[k] != second.get(k))
        assert len(first) >= 8

    def test_launch_is_the_unbroken_top_of_the_ranking(self):
        saas = common.read_jsonl(common.DATASET / "saas_products.jsonl")
        ranked = sorted((x for x in saas if x["derived"]["validated_alternatives"] > 0),
                        key=lambda x: (-x["derived"]["source_count"], -x["derived"]["validated_alternatives"], x["name"].lower()))
        launch = sorted((x for x in saas if x["derived"]["launch"]), key=lambda x: x["derived"]["launch_rank"])
        assert [x["saas_id"] for x in launch] == [x["saas_id"] for x in ranked[: len(launch)]]

    def test_fixed_target_stops_at_exactly_one_hundred(self, monkeypatch):
        fixed = copy.deepcopy(POLICY)
        fixed["launch"]["saas_target_is"] = "fixed"
        edges = common.read_jsonl(common.DATASET / "relationships.jsonl")
        projects = {p["oss_id"]: p for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl")}
        saas = {x["saas_id"]: dict(x, source_ids=x["imported"]["named_by_sources"])
                for x in common.read_jsonl(common.DATASET / "saas_products.jsonl")}
        sel = build.select_launch(saas, edges, projects, fixed)
        assert len(sel["saas_ranked"]) == 100 and sel["saas_added_beyond_floor"] == 0
        floor = build.select_launch(saas, edges, projects, POLICY)
        assert floor["saas_ranked"][:100] == sel["saas_ranked"]
        assert len(floor["edge_picks"]) <= POLICY["launch"]["edges_max"]

    def test_changed_policy_refuses_an_offline_rebuild(self, monkeypatch):
        changed = copy.deepcopy(POLICY)
        changed["maintenance"]["active_days"] = 30
        monkeypatch.setattr(common, "load_policy", lambda: changed)
        with pytest.raises(RuntimeError, match="policy has changed"):
            build.run()

    def test_failed_lookups_make_the_snapshot_untrusted(self, monkeypatch):
        real = common.read_json

        def tampered(path):
            doc = real(path)
            if Path(path).name == "snapshot.json":
                doc["lookup_failures"] = ["403 https://api.github.com/repos/a/b/commits/x"]
            return doc

        monkeypatch.setattr(common, "read_json", tampered)
        with pytest.raises(RuntimeError, match="lookups failed"):
            build.run()

    def test_plausibility_review_is_applied(self):
        review = common.load_yaml(common.CONFIG / "edge_review.yaml")
        edges = {e["edge_id"]: e for e in common.read_jsonl(common.DATASET / "relationships.jsonl")}
        seen = 0
        for r in review["rejected"]:
            e = edges.get(f"{r['saas']}--{r['oss'].lower()}")
            if e:
                seen += 1
                assert e["validation"]["status"] == "rejected" and not e["launch"]
                assert e["validation"]["checks"]["passed_plausibility_review"] is False
        assert seen >= 20

    def test_no_imported_value_is_prose(self):
        for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl"):
            for field in p["imported"].values():
                for v in field:
                    assert len(v["value"]) <= 80, (p["oss_id"], v)
            for v in p["imported"]["declared_licences"]:
                assert "\u2014" not in v["value"] and "(" not in v["value"]

    def test_records_conform_to_schema(self):
        for name, file in (("saas_product", "saas_products.jsonl"), ("oss_project", "oss_projects.jsonl"),
                           ("relationship", "relationships.jsonl")):
            v = validator(name)
            for row in common.read_jsonl(common.DATASET / file):
                assert not list(v.iter_errors(row)), row

    def test_imported_and_live_fields_are_separate(self):
        for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl"):
            assert set(p) == {"schema_version", "oss_id", "name", "name_origin", "product_group", "host", "repo_url",
                              "harvested_as", "imported", "live", "derived", "provenance"}
            assert p["live"]["fetched_at"]
            assert all("source_id" in v for field in p["imported"].values() for v in field)
            assert not set(p["imported"]) & set(p["live"])

    def test_upstream_licence_claims_never_decide_eligibility(self):
        for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl"):
            claimed = {v["value"] for v in p["imported"]["declared_licences"]}
            lic = p["derived"]["licence"]
            if lic and not lic["usable"] and claimed & {"MIT", "AGPL-3.0", "Apache-2.0"}:
                assert not p["derived"]["eligibility"]["recommended"]

    def test_launch_holds_only_recommendable_projects(self):
        # Whether the launch set reaches the ticket's size targets is a fact about the
        # data, reported by the `launch.targets` validation check. It is not asserted
        # here, so that this suite tests the code and cannot be made green by the data.
        s = common.read_json(common.DATASET / "summary.json")["launch"]
        assert s["saas_products"] >= 100 and s["relationships"] <= 400
        oss = {p["oss_id"]: p for p in common.read_jsonl(common.DATASET / "oss_projects.jsonl")}
        for e in common.read_jsonl(common.DATASET / "relationships.jsonl"):
            if e["launch"]:
                p = oss[e["oss_id"]]
                assert p["derived"]["eligibility"]["recommended"]
                assert not p["live"]["archived"]
                assert p["derived"]["maintenance"]["status"] in ("active", "maintained")
                assert p["derived"]["licence"]["usable"]

    def test_export_withholds_share_alike_and_publishes_nothing(self, tmp_path):
        from source_pipeline import export
        out = export.run(str(tmp_path / "pub"))
        assert out["published"] is False
        assert "awesome-selfhosted" not in out["sources_attributed"]
        rows = common.read_jsonl(tmp_path / "pub" / "relationships.jsonl")
        assert len(rows) == out["relationships_exported"]
        assert out["relationships_withheld_share_alike_only"] > 0
        notice = (tmp_path / "pub" / "NOTICE.md").read_text()
        for s in SOURCES["sources"]:
            if s["source_id"] in out["sources_attributed"]:
                assert s["url"] in notice
        blob = json.dumps(rows)
        assert "description" not in blob and "tagline" not in blob
