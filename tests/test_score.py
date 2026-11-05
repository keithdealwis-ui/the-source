"""Tests for scoring (KEI-806). Each class names the criterion or rubric rule it guards.

Everything here is synthetic and in memory: no network, no built data. TestBuiltScores
runs only when data/score/ has been built.
"""
import copy

import pytest

from source_pipeline import common, score_build as sb

CFG = sb.load_cfg()
JOBS = sb.load_jobs()
AS_OF = "2026-09-29T12:00:00Z"
BUILT = (sb.SCORE / "records.jsonl").exists()


def repo(**over):
    """A healthy, mature GitHub repository as the GraphQL pass returns it."""
    commits = [{"committedDate": "2026-09-2%dT10:00:00Z" % (i % 9 + 1), "author": {"user": {"login": f"dev{i % 12}"}}}
               for i in range(100)]
    r = {
        "createdAt": "2018-01-01T00:00:00Z", "pushedAt": "2026-09-28T00:00:00Z", "isArchived": False,
        "isDisabled": False, "isFork": False, "description": "Open-source web analytics", "homepageUrl": "https://x.org",
        "stargazerCount": 20000, "forkCount": 1500, "isSecurityPolicyEnabled": True, "hasIssuesEnabled": True,
        "licenseInfo": {"spdxId": "MIT"}, "repositoryTopics": {"nodes": [{"topic": {"name": "analytics"}}]},
        "releases": {"totalCount": 80, "nodes": [{"tagName": "v3.2.0", "publishedAt": "2026-09-01T00:00:00Z",
                                                  "isPrerelease": False, "isDraft": False}]},
        "openIssues": {"totalCount": 100}, "closedIssues": {"totalCount": 2000},
        "recentIssues": {"nodes": [{"createdAt": "2026-09-0%dT00:00:00Z" % (i + 1), "closedAt": None, "authorAssociation": "NONE",
                                    "comments": {"nodes": [{"createdAt": "2026-09-0%dT06:00:00Z" % (i + 1), "authorAssociation": "MEMBER"}]}}
                                   for i in range(8)]},
        "recentPRs": {"nodes": [{"createdAt": "2026-09-0%dT00:00:00Z" % (i + 1), "mergedAt": "2026-09-0%dT12:00:00Z" % (i + 1),
                                 "closedAt": None, "authorAssociation": "CONTRIBUTOR" if i % 2 else "MEMBER"} for i in range(8)]},
        "root": {"entries": [{"name": n, "type": t, "object": {"byteSize": 9000} if n == "README.md" else {}}
                             for n, t in (("README.md", "blob"), ("docs", "tree"), ("Dockerfile", "blob"), ("package.json", "blob"),
                                          ("CHANGELOG.md", "blob"), ("examples", "tree"), ("CONTRIBUTING.md", "blob"))]},
        "dotgithub": {"entries": [{"name": "dependabot.yml", "type": "blob"}, {"name": "CODEOWNERS", "type": "blob"}]},
        "workflows": {"entries": [{"name": "ci.yml"}, {"name": "release.yml"}]},
        "defaultBranchRef": {"target": {"c90": {"totalCount": 120}, "c180": {"totalCount": 240},
                                        "c365": {"totalCount": 480, "nodes": commits}, "cprev": {"totalCount": 450}}},
    }
    r.update(over)
    return r


def view_row(canonical="github.com/acme/app", pid="github:1", replaces=(), spdx="MIT", status="recognised",
             eligible=True, lanes=None):
    return {"project": pid, "canonical": canonical, "name": canonical.split("/", 1)[1], "url": "https://" + canonical,
            "licence": {"spdx": spdx, "status": status}, "owner": [{"to": "org:github:acme", "edge": f"{pid}|OWNED_BY|org:github:acme"}],
            "observations": [], "replaces": [{"to": s, "edge": f"{pid}|REPLACES|{s}"} for s in replaces],
            "capabilities": [], "use_cases": [], "recommendation_eligible": eligible,
            "outcome": "recommendation_eligible" if eligible else "retained",
            "lanes": lanes or {"discover": True, "replace": bool(replaces)}}


def extract(r=None, row=None, error=None):
    row = row or view_row()
    meta = {row["canonical"]: ({"observed_at": AS_OF, "repo": r or repo()} if not error else {"observed_at": AS_OF, "error": error})}
    return sb.extract_project(row, meta, None, True, CFG)


def edge(pid="github:1", saas="saas:google-analytics", conf=0.8, status="validated", sources=2):
    return {"id": f"{pid}|REPLACES|{saas}", "to": saas, "confidence": conf, "evidence": [],
            "attrs": {"kei805_status": status, "corroborated": sources > 1, "source_count": sources,
                      "recommendation_eligible": status == "validated"}}


GA = {"name": "Google Analytics", "category": "Web analytics"}
README = ("# Plausible\nSimple, privacy-friendly web analytics, an alternative to Google Analytics. Self-host with docker compose.\n"
          "## Quick start\nInstall ... Tracks page views, visitors, goals, UTM, real-time dashboard, GDPR, no cookies.\n"
          "Import from Google Analytics. REST API, webhooks, plugins. Teams and roles, SSO via OIDC. Commercial support.")


def pair(readme=README, p=None, conf=0.8, status="validated", sources=2, saas=GA, saas_id="saas:google-analytics"):
    p = p or extract()
    rr = {"text": readme, "observed_at": AS_OF} if readme else None
    return sb.extract_pair(edge(conf=conf, status=status, sources=sources, saas=saas_id), p, None, rr, saas,
                           JOBS[saas["category"]], ["Analytics"], [], CFG)


class TestHealthRubric:  # AC1
    def test_weights_are_the_approved_rubric(self):
        dims = CFG["project_health"]["dimensions"]
        assert {k: v["weight"] for k, v in dims.items()} == {
            "maintenance_momentum": 25, "community_contributors": 15, "issue_pr_responsiveness": 15,
            "maturity_continuity": 15, "documentation_usability": 10, "operational_readiness": 10,
            "security_governance": 5, "licence": 5}
        for d in dims.values():
            assert sum(s["weight"] for s in d["signals"].values()) == 100

    def test_stars_are_not_a_large_standalone_weight(self):
        comm = CFG["project_health"]["dimensions"]["community_contributors"]
        effective = comm["weight"] * (comm["signals"]["stars_log10"]["weight"] / 100)
        assert effective <= 3  # at most 3 points of 100

    def test_healthy_mature_repo_scores_healthy(self):
        h = sb.score_health(extract(), CFG)
        assert h["score"] >= 80 and h["band"] in ("Exceptional", "Healthy")
        assert h["known_weight_share"] > 0.95

    def test_deterministic(self):
        assert sb.score_health(extract(), CFG) == sb.score_health(extract(), CFG)


class TestMissingIsUnknown:  # R1
    def test_missing_signal_is_null_not_zero(self):
        r = repo()
        del r["recentIssues"]
        p = extract(r)
        assert "median_first_response_hours" not in p["signals"]
        h = sb.score_health(p, CFG)
        sig = h["dimensions"]["issue_pr_responsiveness"]["signals"]["median_first_response_hours"]
        assert sig["value"] is None and sig["points"] is None

    def test_missing_dimension_does_not_punish_the_project(self):
        full = sb.score_health(extract(), CFG)
        r = repo(recentIssues={"nodes": []}, recentPRs={"nodes": []}, openIssues={"totalCount": 0}, closedIssues={"totalCount": 0})
        partial = sb.score_health(extract(r), CFG)
        assert partial["dimensions"]["issue_pr_responsiveness"]["score"] is None
        assert partial["score"] >= full["score"] - 5  # renormalised over known dimensions, not zeroed
        assert partial["known_weight_share"] < full["known_weight_share"]

    def test_gap_is_charged_to_confidence(self):
        full = extract()
        r = repo(recentIssues={"nodes": []}, recentPRs={"nodes": []})
        part = extract(r)
        cf = sb.health_confidence(full, sb.score_health(full, CFG), AS_OF, CFG)
        cp = sb.health_confidence(part, sb.score_health(part, CFG), AS_OF, CFG)
        assert cp["score"] < cf["score"]

    def test_crawler_failure_is_not_a_dead_project(self):
        p = extract(error="graphql_http_502")
        h = sb.score_health(p, CFG)
        assert not any(d["id"] == "demonstrably_dead" for d in sb.disqualifiers(p, CFG, {}))
        assert h["evidence_insufficient"]


class TestDisqualifiers:  # R2
    def test_archived_is_flagged_outside_the_number(self):
        p = extract(repo(isArchived=True))
        dq = sb.disqualifiers(p, CFG, {})
        assert [d["id"] for d in dq] == ["archived"]
        assert sb.score_health(p, CFG)["score"] is not None

    def test_dead_project(self):
        r = repo()
        r["defaultBranchRef"]["target"].update({"c90": {"totalCount": 0}, "c180": {"totalCount": 0},
                                                "c365": {"totalCount": 0, "nodes": []}, "cprev": {"totalCount": 0}})
        r["pushedAt"] = "2023-01-01T00:00:00Z"
        p = extract(r)
        assert "demonstrably_dead" in [d["id"] for d in sb.disqualifiers(p, CFG, {})]
        assert sb.score_health(p, CFG)["dimensions"]["maintenance_momentum"]["score"] <= 25

    def test_unclear_licence(self):
        p = extract(row=view_row(spdx=None, status="issue", eligible=False))
        assert "licence_unclear" in [d["id"] for d in sb.disqualifiers(p, CFG, {})]

    def test_security_override(self):
        p = extract()
        dq = sb.disqualifiers(p, CFG, {"security_concern": [{"project": "github:1", "reason": "CVE unpatched", "evidence": "x"}]})
        assert dq[0]["id"] == "security_concern"


class TestTrajectory:  # R6
    def t(self, c180, c365, cprev, created="2018-01-01T00:00:00Z"):
        r = repo(createdAt=created)
        tg = r["defaultBranchRef"]["target"]
        tg["c180"]["totalCount"], tg["c365"]["totalCount"], tg["cprev"]["totalCount"] = c180, c365, cprev
        return sb.trajectory(extract(r), [], CFG)["label"]

    def test_labels(self):
        assert self.t(200, 300, 100) == "Rising"
        assert self.t(100, 200, 200) == "Stable"
        assert self.t(10, 60, 300) == "Declining"
        assert self.t(2, 4, 3) == "Insufficient history"

    def test_young_project_is_not_rising_by_construction(self):
        assert self.t(300, 400, 0, created="2026-03-01T00:00:00Z") == "Insufficient history"

    def test_trajectory_is_not_a_weight(self):
        assert "trajectory" not in CFG["project_health"]["dimensions"]


class TestReplacementFit:  # AC2, core gate
    def test_weights_are_the_approved_rubric(self):
        assert {k: v["weight"] for k, v in CFG["replacement_fit"]["dimensions"].items()} == {
            "core_use_case_coverage": 25, "feature_equivalence": 15, "ease_of_adoption": 15, "deployment_ownership": 10,
            "migration_friction": 10, "integrations_extensibility": 10, "production_commercial_readiness": 10,
            "economic_advantage": 5}

    def test_canonical_alternative_is_credible(self):
        f = sb.score_pair(pair(), {"score": 85, "band": "Exceptional"}, [], AS_OF, CFG)
        assert f["core_gate"] == "passed" and f["score"] >= 60 and f["credible_alternative"] and f["publish"]

    def test_core_gate_caps_strong_secondaries(self):
        off_job = ("A drag and drop kanban board with plugins, webhooks, REST API, SSO, teams, docker compose, "
                   "import and export, quick start, demo, commercial support, self-host, GDPR.") * 3
        p = extract(repo(description="A kanban board", repositoryTopics={"nodes": [{"topic": {"name": "kanban"}}]}))
        pr = sb.extract_pair(edge(), p, None, {"text": off_job, "observed_at": AS_OF}, GA, JOBS["Web analytics"], [], [], CFG)
        f = sb.score_pair(pr, {"score": 85, "band": "Exceptional"}, [], AS_OF, CFG)
        assert f["core_gate"] == "failed"
        assert f["score"] <= CFG["replacement_fit"]["core_gate"]["gate_cap"] < f["uncapped_score"]
        assert not f["credible_alternative"]

    def test_no_readme_means_unproven_not_failed(self):
        p = extract(repo(description="A thing", repositoryTopics={"nodes": []}))
        pr = sb.extract_pair(edge(), p, None, None, GA, JOBS["Web analytics"], [], [], CFG)
        f = sb.score_pair(pr, {"score": 80, "band": "Healthy"}, [], AS_OF, CFG)
        assert f["core_gate"] == "unproven" and not f["credible_alternative"]


class TestIndependence:  # AC4, R3
    def test_fit_takes_no_health_input(self):
        pr = pair()
        a = sb.score_pair(pr, {"score": 5, "band": "High Risk"}, [], AS_OF, CFG)
        b = sb.score_pair(pr, {"score": 99, "band": "Exceptional"}, [], AS_OF, CFG)
        assert a["score"] == b["score"]

    def test_healthy_project_can_fit_poorly(self):
        p = extract()
        crm = {"name": "Salesforce", "category": "CRM"}
        pr = sb.extract_pair(edge(saas="saas:salesforce"), p, None, {"text": README, "observed_at": AS_OF}, crm,
                             JOBS["CRM"], [], [], CFG)
        f = sb.score_pair(pr, sb.score_health(p, CFG), [], AS_OF, CFG)
        assert sb.score_health(p, CFG)["score"] >= 70 and f["score"] < 50

    def test_weak_project_can_fit_well(self):
        r = repo(stargazerCount=3, forkCount=0, createdAt="2025-12-01T00:00:00Z", releases={"totalCount": 0, "nodes": []},
                 recentIssues={"nodes": []}, recentPRs={"nodes": []}, isSecurityPolicyEnabled=False, homepageUrl="",
                 openIssues={"totalCount": 40}, closedIssues={"totalCount": 10},
                 root={"entries": [{"name": "README.md", "type": "blob", "object": {"byteSize": 900}}]},
                 dotgithub=None, workflows=None)
        tg = r["defaultBranchRef"]["target"]
        tg.update({"c90": {"totalCount": 0}, "c180": {"totalCount": 1},
                   "c365": {"totalCount": 3, "nodes": [{"committedDate": "2026-05-01T00:00:00Z", "author": {"user": {"login": "solo"}}}] * 3},
                   "cprev": {"totalCount": 0}})
        p = extract(r)
        h = sb.score_health(p, CFG)
        f = sb.score_pair(pair(p=p), h, [], AS_OF, CFG)
        assert h["score"] < 55 and f["score"] >= 60


class TestConfidence:  # AC5
    def test_staleness_moves_confidence_not_score(self):
        p = extract()
        h = sb.score_health(p, CFG)
        now = sb.health_confidence(p, h, AS_OF, CFG)["score"]
        later = sb.health_confidence(p, h, "2027-06-01T00:00:00Z", CFG)["score"]
        assert later < now and sb.score_health(p, CFG)["score"] == h["score"]

    def test_weak_relationship_lowers_fit_confidence_not_fit(self):
        strong = sb.score_pair(pair(conf=0.9, sources=3), {"score": 80}, [], AS_OF, CFG)
        weak = sb.score_pair(pair(conf=0.6, sources=1), {"score": 80}, [], AS_OF, CFG)
        assert weak["confidence"]["score"] < strong["confidence"]["score"] and weak["score"] == strong["score"]


class TestThresholds:  # AC10, AC7
    def test_band_edges(self):
        assert sb._band(85, CFG) == "Exceptional" and sb._band(84.9, CFG) == "Healthy"
        assert sb._band(70, CFG) == "Healthy" and sb._band(69.9, CFG) == "Developing"
        assert sb._band(55, CFG) == "Developing" and sb._band(54.9, CFG) == "Caution"
        assert sb._band(40, CFG) == "Caution" and sb._band(39.9, CFG) == "High Risk"

    def test_credible_alternative_threshold(self):
        cfg = copy.deepcopy(CFG)
        f = sb.score_pair(pair(), {"score": 80}, [], AS_OF, cfg)
        cfg["thresholds"]["credible_alternative"]["replacement_fit_min"] = f["score"] + 0.1
        assert f["credible_alternative"]
        assert not sb.score_pair(pair(), {"score": 80}, [], AS_OF, cfg)["credible_alternative"]

    def test_low_confidence_mapping_refused(self):
        cfg = copy.deepcopy(CFG)
        cfg["thresholds"]["publish_mapping"]["confidence_min"] = 99
        f = sb.score_pair(pair(), {"score": 80}, [], AS_OF, cfg)
        assert not f["publish"] and any("confidence" in x for x in f["refusal_reasons"])

    def test_licence_exception_relationship_refused(self):
        f = sb.score_pair(pair(status="flagged_licence_exception"), {"score": 80}, [], AS_OF, CFG)
        assert not f["publish"]

    def test_gated_mapping_is_not_published(self):
        off_job = "A drag and drop kanban board with plugins and webhooks. " * 5
        p = extract(repo(description="A kanban board", repositoryTopics={"nodes": []}))
        pr = sb.extract_pair(edge(), p, None, {"text": off_job, "observed_at": AS_OF}, GA, JOBS["Web analytics"], [], [], CFG)
        f = sb.score_pair(pr, {"score": 80}, [], AS_OF, CFG)
        assert f["core_gate"] == "failed" and not f["publish"]

    def test_disqualified_project_is_never_credible(self):
        f = sb.score_pair(pair(), {"score": 80}, [{"id": "archived"}], AS_OF, CFG)
        assert not f["credible_alternative"] and not f["publish"]


class TestTermMatching:
    def test_short_terms_need_word_boundaries(self):
        assert not sb.has_term("we maintain things", "ai")
        assert sb.has_term("an ai assistant", "ai")
        assert sb.has_term("real-time collaborative editor", "collaborat")


@pytest.mark.skipif(not BUILT, reason="data/score not built")
class TestBuiltScores:
    def test_every_record_is_unendorsed(self):
        for r in common.read_jsonl(sb.SCORE / "records.jsonl"):
            e = r["endorsement"]
            assert not (e["keith_tested"] or e["keith_endorsed"] or e["editorial_recommendation"])

    def test_no_fit_without_a_replaces_edge(self):
        for r in common.read_jsonl(sb.SCORE / "records.jsonl"):
            if not r["lanes"]["replace"]:
                assert not r["replacement_fit"]


# Built by concatenation so this source file never itself carries a key header.
BEGIN_RSA = "-----BEGIN " + "RSA PRIVATE KEY-----"
END_RSA = "-----END " + "RSA PRIVATE KEY-----"
BEGIN_PK = "-----BEGIN " + "PRIVATE KEY-----"


class TestRedaction:  # evidence hygiene: secret-shaped README content is never stored
    def test_terminated_pem_block_removed_whole(self):
        from source_pipeline.score_harvest import redact
        t, n = redact("a\n" + BEGIN_RSA + "\nMIIEpAIB\nAAAA\n" + END_RSA + "\nrest of readme")
        assert "MIIE" not in t and t.endswith("rest of readme") and n == {"private_key": 1}

    def test_unterminated_example_does_not_swallow_the_readme(self):
        from source_pipeline.score_harvest import redact
        t, _ = redact('"key": "' + BEGIN_PK + '\\nMIIEvwIB..." }\n\n## Self-hosting with docker compose')
        assert "PRIVATE KEY" not in t and "Self-hosting with docker compose" in t

    def test_tokens(self):
        from source_pipeline.score_harvest import redact
        t, n = redact("use ghp_" + "a" * 36 + " and AKIA" + "B" * 16)
        assert "ghp_" not in t and "AKIA" not in t and n == {"github_token": 1, "aws_access_key": 1}


# ---------------------------------------------------------------- KEI-961: evidence caps on Replacement Fit

CRM_README = (
    "# warpdrive\nAn open-source, self-hosted <b>Pipedrive alternative</b> for business-development teams.\n"
    "Self-host with docker compose. Quick start below; live demo and screenshots.\n"
    "## Features\nSales pipeline kanban board with deals and custom stages. Contact management for people and "
    "organisations with custom fields. Two-way email over Gmail. Tasks and activity timeline. Pipeline reports. "
    "CSV import. REST API and webhooks, plugins. Teams, roles and permissions, SSO via OIDC. Commercial support.\n"
    "## warpdrive vs Pipedrive\nwarpdrive covers the core BD loop and deliberately leaves the rest out of scope.\n"
    "|  | warpdrive | Pipedrive |\n|---|:---:|:---:|\n"
    "| Kanban pipeline + custom stages | ✅ | ✅ |\n"
    "| Products, Projects, Invoicing, Forecast | ➖ Out of scope | ✅ |\n"
    "| Multi-currency | ➖ Out of scope | ✅ |\n"
    "| Workflow automation | ➖ Email scheduling only | ✅ |\n"
    "| Web forms / chatbot / marketplace | ➖ Out of scope | ✅ |\n"
    "| Native mobile apps | ➖ Web app | ✅ |\n\n"
    "✅ supported · ➖ intentionally out of scope\n")
SALESFORCE = {"name": "Salesforce", "category": "CRM"}
PIPEDRIVE = {"name": "Pipedrive", "category": "CRM"}


def crm_pair(readme=CRM_README, saas=SALESFORCE, saas_id="saas:salesforce", sources=1, cfg=CFG):
    p = extract(repo(description="Self-hosted CRM for BD teams", repositoryTopics={"nodes": [{"topic": {"name": "crm"}}]}))
    rr = {"text": readme, "observed_at": AS_OF} if readme else None
    return sb.extract_pair(edge(saas=saas_id, sources=sources), p, None, rr, saas, JOBS["CRM"], ["CRM"], [], cfg)


class TestOutOfScopeStatements:  # KEI-961 item 2: explicit forms only, deterministic
    def test_table_rows_marker_and_legend(self):
        st = sb.out_of_scope_statements(CRM_README.lower(), CFG)
        assert "products, projects, invoicing, forecast" in st and "multi-currency" in st
        # the legend defines ➖ as out of scope, so its rows count; only the feature cell is read
        assert "workflow automation" in st and "native mobile apps" in st
        assert not any("email scheduling" in s for s in st)
        assert "kanban pipeline + custom stages" not in st

    def test_heading_section_and_clause(self):
        text = ("# x\nDeals and pipelines are supported, but invoicing is out of scope.\n"
                "## Non-goals\n- Forecasting\n- Multi-currency\n## Install\nRun docker compose. Pipelines.\n").lower()
        st = sb.out_of_scope_statements(text, CFG)
        assert st == ["invoicing", "forecasting", "multi-currency"]
        ex = sb.excluded_features(text, ["deal", "pipeline", "invoic", "forecast", "multi-currency"], CFG)
        assert ex == ["forecast", "invoic", "multi-currency"]  # the supported clause never excludes deal/pipeline

    def test_explanations_and_caveats_are_not_exclusions(self):
        text = ("## Not supported/planned\n- Functioning as an SMTP relay without authentication\n- POP3\n"
                "## API\nSupported/unsupported events are listed in the docs. The GPU driver is deliberately left out.\n"
                "A long sentence that mentions voice calls and many other words before it says this is out of scope.\n").lower()
        assert sb.excluded_features(text, ["smtp", "pop3", "event", "gpu", "voice"], CFG) == ["pop3"]
        qualified = ("Synchronization through third-party synchronization disks is not supported.\n"
                     "VA-API, Quick Sync, or OpenCL is not supported for AI inference today.\n").lower()
        assert sb.excluded_features(qualified, ["sync", "api"], CFG) == []

    def test_lists_after_a_marker(self):
        text = "Not supported: forecasting, multi-currency.\nThere are no plans to add SSO or webhooks.\n".lower()
        assert sb.excluded_features(text, ["forecast", "multi-currency", "sso", "webhook", "email"], CFG) == \
            ["forecast", "multi-currency", "sso", "webhook"]

    def test_no_statement_no_exclusion(self):
        assert sb.out_of_scope_statements(README.lower(), CFG) == []
        assert sb.feature_scope_cap([], ["a"], CFG) is None


class TestFeatureScopeCap:  # KEI-961 item 2
    def test_ruled_out_features_cap_feature_equivalence(self):
        f = sb.score_pair(crm_pair(), {"score": 85}, [], AS_OF, CFG)
        cap = next(c for c in f["caps"] if c["id"] == "feature_out_of_scope")
        assert {"forecast", "multi-currency", "workflow automation", "products"} <= set(cap["excluded_features"])
        assert f["dimensions"]["feature_equivalence"]["score"] == cap["cap"] <= CFG["replacement_fit"]["feature_scope"]["cap_any"]
        assert "out of scope" in cap["reason"] and "Salesforce" in cap["reason"]

    def test_ruled_out_feature_is_not_a_hit(self):
        text = CRM_README.replace("Tasks and activity timeline.", "") + "\n| Tasks | ❌ not supported | ✅ |\n"
        f = sb.score_pair(crm_pair(readme=text), {"score": 85}, [], AS_OF, CFG)
        assert "task" in f["features_out_of_scope"] and "task" not in f["feature_hits"]

    def test_readme_ruling_nothing_out_is_unchanged(self):
        f = sb.score_pair(pair(), {"score": 85}, [], AS_OF, CFG)
        assert f["caps"] == [] and f["features_out_of_scope"] == []

    def test_switch_off(self):
        cfg = copy.deepcopy(CFG)
        cfg["replacement_fit"]["feature_scope"]["enabled"] = False
        f = sb.score_pair(crm_pair(cfg=cfg), {"score": 85}, [], AS_OF, cfg)
        assert not any(c["id"] == "feature_out_of_scope" for c in f["caps"])


class TestCorroborationCap:  # KEI-961 item 1: no Replacement Fit above 80 without corroboration
    def test_single_source_not_named_by_readme_caps_at_80(self):
        f = sb.score_pair(crm_pair(), {"score": 85}, [], AS_OF, CFG)
        assert not f["corroboration"]["corroborated"] and f["corroboration"]["readme_checked"]
        assert f["uncapped_score"] > 80 and f["score"] == CFG["replacement_fit"]["corroboration_cap"]["cap"] == 80
        c = next(c for c in f["caps"] if c["id"] == "uncorroborated_pairing")
        assert "Salesforce" in c["reason"] and "single upstream source" in c["reason"]

    def test_readme_naming_the_product_corroborates(self):
        f = sb.score_pair(crm_pair(saas=PIPEDRIVE, saas_id="saas:pipedrive"), {"score": 85}, [], AS_OF, CFG)
        assert f["corroboration"]["basis"] == ["readme_names_product"]
        assert not any(c["id"] == "uncorroborated_pairing" for c in f["caps"])
        # the README still rules out Pipedrive features, so Feature equivalence is capped against it too
        assert any(c["id"] == "feature_out_of_scope" for c in f["caps"])

    def test_second_source_corroborates(self):
        f = sb.score_pair(crm_pair(sources=2), {"score": 85}, [], AS_OF, CFG)
        assert f["corroboration"]["basis"] == ["second_upstream_source"] and f["score"] > 80

    def test_mention_without_alternative_context_does_not_corroborate(self):
        text = CRM_README.replace("Pipedrive alternative", "CRM") + "\nSyncs contacts with Salesforce through its API.\n"
        assert not sb.names_product_as_alternative(text.lower(), ["Salesforce"], CFG)
        assert sb.names_product_as_alternative("a self-hosted salesforce alternative", ["Salesforce"], CFG)

    def test_cap_is_configurable(self):
        cfg = copy.deepcopy(CFG)
        cfg["replacement_fit"]["corroboration_cap"]["cap"] = 70
        assert sb.score_pair(crm_pair(cfg=cfg), {"score": 85}, [], AS_OF, cfg)["score"] == 70
        cfg["replacement_fit"]["corroboration_cap"]["enabled"] = False
        f = sb.score_pair(crm_pair(cfg=cfg), {"score": 85}, [], AS_OF, cfg)
        assert f["score"] > 80 and not any(c["id"] == "uncorroborated_pairing" for c in f["caps"])

    def test_self_described_corroborates(self):
        corr = sb.corroboration({"evidence_class": "self_described"}, False, CFG)
        assert corr["corroborated"] and corr["basis"] == ["self_described"]

    def test_no_readme_is_uncorroborated(self):
        corr = sb.corroboration({"corroborated": False}, None, CFG)
        score, cap = sb.corroboration_cap(92.0, corr, "Salesforce", CFG)
        assert score == 80 and "no README was available" in cap["reason"]


class TestRecap:  # KEI-961 item 4: stored rows under the new caps, without a full rescore
    ROW = {"saas_id": "salesforce", "oss_id": "github.com/sneg55/warpdrive", "score": 90.4, "core_gate": "passed",
           "credible_alternative": True, "publish": True, "confidence": 84.5,
           "dimensions": {"core_use_case_coverage": 100.0, "deployment_ownership": 75.0, "ease_of_adoption": 98.5,
                          "economic_advantage": 80.0, "feature_equivalence": 100.0, "integrations_extensibility": 75.0,
                          "migration_friction": 66.7, "production_commercial_readiness": 100.0},
           "evidence_families": [], "refusal_reasons": [], "score_version": "1.2.0",
           "evaluated_at": "2026-10-03T23:46:08Z", "scoring_path": "single_project"}

    def recap(self, saas_id="salesforce", name="Salesforce", rel=None, readme=CRM_README):
        from source_pipeline import score_recap
        row = {**self.ROW, "saas_id": saas_id}
        return score_recap.recap_row(row, rel or {"corroborated": False, "evidence_class": "listed"},
                                     {"text": readme, "observed_at": AS_OF} if readme else None, None,
                                     {"name": name, "aliases": []}, JOBS["CRM"], sb.load_product_scope().get(saas_id), CFG)

    def test_warpdrive_salesforce_row(self):
        r = self.recap()
        assert r["score"] == 80 and r["score_version"] == CFG["version"] and r["recap"]["from_version"] == "1.2.0"
        assert r["dimensions"]["feature_equivalence"] < 100 and r["uncapped_score"] > 80
        assert [c["id"] for c in r["caps"]] == ["feature_out_of_scope", "uncorroborated_pairing"]
        assert r["credible_alternative"]  # 80 is still credible; it is just no longer above 80

    def test_warpdrive_pipedrive_row(self):
        r = self.recap(saas_id="pipedrive", name="Pipedrive")
        assert [c["id"] for c in r["caps"]] == ["feature_out_of_scope"] and 80 < r["score"] < 90.4

    def test_untouched_row_keeps_its_score(self):
        r = self.recap(rel={"corroborated": True}, readme=README)
        assert r["score"] == self.ROW["score"] and r["caps"] == []

    def test_cap_can_end_credibility(self):
        row = {**self.ROW, "score": 62.0}
        from source_pipeline import score_recap
        cfg = copy.deepcopy(CFG)
        cfg["replacement_fit"]["corroboration_cap"]["cap"] = 55
        r = score_recap.recap_row(row, {}, None, None, {"name": "Salesforce"}, JOBS["CRM"], [], cfg)
        assert r["score"] == 55 and not r["credible_alternative"]

    def test_newer_rubric_row_is_not_replaced_by_an_older_one(self):
        from source_pipeline import canonical
        new = {"evaluated_at": "2026-10-03T23:46:08Z", "score_version": "1.3.0"}
        old = {"evaluated_at": "2026-10-03T23:46:08Z", "score_version": "1.2.0"}
        assert not canonical.supersedes(old, new) and canonical.supersedes(new, old)
        assert canonical.supersedes({**old, "evaluated_at": "2026-11-01T00:00:00Z"}, new)
