"""KEI-919 (V2.1 WP2) correctness: owner/repo and bare-name lookup (S1.2), no padding on weak word matches and
explicit constraint-filtered responses (S1.3), integrations excluded from replacement candidates (S1.4)."""
import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

from source_pipeline import mcp_server as M

ROOT = Path(__file__).resolve().parent.parent
HAVE = all((ROOT / p).is_file() for p in ("data/canonical/MANIFEST.json", "api/v1/index.json",
                                           "data/corpus/MANIFEST.json"))
NOW = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


class TestIntegrationKind(unittest.TestCase):
    def k(self, full_name, description="", topics=(), products=("Slack", "Notion", "Jira"), alt=False):
        return M.integration_kind(full_name.split("/")[-1], full_name, description, list(topics), products, alt)

    def test_mcp_servers(self):
        self.assertEqual(self.k("x/slack-mcp"), "mcp_server")
        self.assertEqual(self.k("x/mcp-notion-server"), "mcp_server")
        self.assertEqual(self.k("x/things", "An MCP server for Jira"), "mcp_server")
        self.assertEqual(self.k("x/things", topics=["model-context-protocol"]), "mcp_server")

    def test_sdks_bots_clients_plugins(self):
        self.assertEqual(self.k("x/notion-sdk-js"), "sdk")
        self.assertEqual(self.k("x/standup-bot"), "bot")
        self.assertEqual(self.k("x/jira-api-client"), "api_client")
        self.assertEqual(self.k("x/agent-slack"), "api_client")
        self.assertEqual(self.k("x/tool", "A Python wrapper for the Jira REST API"), "api_client")
        self.assertEqual(self.k("x/tool", "CLI client for Notion"), "api_client")
        self.assertEqual(self.k("x/tool", topics=["obsidian-plugin"]), "plugin")
        self.assertEqual(self.k("x/tool", topics=["agent-skills"]), "plugin")

    def test_replacements_are_not_integrations(self):
        self.assertIsNone(self.k("zulip/zulip", "Zulip team chat"))
        self.assertIsNone(self.k("mountain-loop/yaak", "The most intuitive desktop API client.",
                                 ["postman-alternative", "http-client"]))
        # an alternative that also ships an MCP server stays a replacement
        self.assertIsNone(self.k("paca-ai/paca", "open-source alternative to Jira", ["mcp", "jira-alternative"],
                                 alt=True))
        # ...but a plugin is an add-on whatever it claims
        self.assertEqual(self.k("x/claude-obsidian", "an open-source Notion alternative", ["claude-plugin"],
                                alt=True), "plugin")

    def test_claims_alternative(self):
        self.assertTrue(M.claims_alternative("The open source Trello alternative.", [], "Trello"))
        self.assertTrue(M.claims_alternative("", ["postman-alternative"], "Postman"))
        self.assertTrue(M.claims_alternative("free, open-source alternative to Jira, Trello & Monday", [], "Monday"))
        self.assertTrue(M.claims_alternative("A self-hosted Heroku", [], "Heroku"))
        self.assertFalse(M.claims_alternative("PHP Library for Google Analytics 4", ["google-analytics"],
                                              "Google Analytics"))
        self.assertFalse(M.claims_alternative("Integrates with PagerDuty, Datadog", ["datadog"], "Datadog"))

    def test_not_a_product(self):
        self.assertTrue(M.not_a_product("awesome-n8n-templates", "x/awesome-n8n-templates", []))
        self.assertTrue(M.not_a_product("crm-starter", "x/crm-starter", []))
        self.assertTrue(M.not_a_product("thing", "x/thing", ["awesome-list"]))
        self.assertFalse(M.not_a_product("espocrm", "espocrm/espocrm", ["crm"]))
        # KEI-923: material said in the description; a learning platform is still a product
        self.assertTrue(M.not_a_product("siem", "x/siem", ["siem", "blue"], "SIEM Tactics, Techiques, and Procedures"))
        self.assertTrue(M.not_a_product("k8s", "x/k8s", [], "Kubernetes cheat sheet"))
        self.assertFalse(M.not_a_product("moodle", "moodle/moodle", [], "The world's open source learning platform, "
                                                                          "for online courses"))

    def test_add_ons_are_not_discovery_candidates(self):
        for d in ("Tailscale browser extension for Chrome", "Tunneled network connector for Pangolin",
                  "Mac DSLR webcam plugin for using DSLRs for video conferencing"):
            self.assertTrue(M._LIBRARY_OR_CLIENT.search(M._norm(d)), d)
        self.assertFalse(M._LIBRARY_OR_CLIENT.search(M._norm("Open source video conferencing with plugins")))


class TestMatchStrength(unittest.TestCase):
    def hit(self, matched=None, semantic=(), graph=0.0, exact=False):
        return {"matched": matched or {}, "semantic_terms": list(semantic), "exact": exact,
                "components": {"lexical": 0.5, "semantic": 0.1, "graph": graph}}

    def test_one_word_of_three_is_weak(self):
        terms = M._query_terms("zzqx frobnicate widget")
        self.assertFalse(M.match_strength(self.hit({"name": ["widget"]}), terms)[0])

    def test_description_only_is_weak(self):
        terms = M._query_terms("blue")
        self.assertFalse(M.match_strength(self.hit({"description": ["blue"]}), terms)[0])

    def test_stopwords_only_match_nothing(self):
        self.assertEqual(M._query_terms("the and of"), [])
        self.assertFalse(M.match_strength(self.hit({"name": ["the"]}), M._query_terms("the and of"))[0])

    def test_strong(self):
        terms = M._query_terms("password manager")
        self.assertTrue(M.match_strength(self.hit({"topics": ["password", "manager"]}), terms)[0])
        self.assertTrue(M.match_strength(self.hit(exact=True), terms)[0])
        self.assertTrue(M.match_strength(self.hit(graph=0.4), terms)[0])

    def test_product_driven(self):
        terms = ["project", "management"]
        weak = self.hit({"topics": ["management"]})
        self.assertFalse(M.match_strength(weak, terms, anchors=False)[0])
        self.assertTrue(M.match_strength(weak, terms, anchors=True)[0])  # claims to be an alternative
        self.assertTrue(M.match_strength(self.hit({"topics": ["project", "management"]}), terms, anchors=False)[0])


@unittest.skipUnless(HAVE, "the published layer and corpus are needed")
class TestLiveLayer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = M.Server(ROOT, NOW)

    def call(self, tool, args):
        return M.TOOL_BY_NAME[tool][4](self.s.layer, args, self.s)

    def test_owner_repo_and_bare_name_lookup(self):
        for value in ("docmost/docmost", "Docmost", "https://github.com/docmost/docmost",
                      "github.com/docmost/docmost.git"):
            self.assertEqual(self.call("source_get_project", {"project": value})["project"]["oss_id"],
                             "github.com/docmost/docmost", value)
        bw = self.call("source_get_project", {"project": "bitwarden/server"})["project"]
        self.assertEqual(bw.get("id") or bw.get("oss_id"), "github.com/bitwarden/server")  # corpus or canonical view
        rel = self.call("source_get_relationship", {"saas": "Notion", "project": "docmost/docmost"})
        self.assertEqual(rel["relationship"]["edge_id"], "notion--github.com/docmost/docmost")

    def test_relationship_without_replacement_fit_does_not_crash(self):
        unscored = next(((r["saas_id"], r["oss_id"]) for rs in self.s.layer.rel_by_saas.values() for r in rs
                         if not r["replacement_fit"]), None)
        if unscored is None:
            self.skipTest("every relationship in this layer is scored")
        r = self.call("source_get_relationship", {"saas": unscored[0], "project": unscored[1]})
        self.assertIn("not yet scored", r["verdict"])

    def test_bare_organisation_resolves_to_its_primary_repository(self):
        r = self.call("source_get_project", {"project": "bitwarden"})
        self.assertEqual(r["project"].get("id") or r["project"].get("oss_id"),
                         "github.com/bitwarden/server")  # siblings: server + clients (S1.8)

    def test_named_open_core_projects_carry_their_edition_check(self):
        # Before KEI-930 these stayed conditional in discovery. Keith's D5 approves open-core projects as licence
        # exceptions; a project then served from the canonical layer must say so, with its disclosure, wherever it
        # is shown. One still only in the corpus keeps the KEI-919 conditional view and its edition check.
        for value, core in (("twentyhq/twenty", "AGPL-3.0"), ("mattermost/mattermost", "AGPL-3.0"),
                            ("RocketChat/Rocket.Chat", "MIT"), ("bitwarden/server", "AGPL-3.0")):
            p = self.call("source_get_project", {"project": value})["project"]
            self.assertEqual(p["licence_label"], "open_core", value)
            if p.get("oss_id"):  # canonical: an approved exception, disclosed
                self.assertEqual(p["licence_status"], "exception_approved", value)
                self.assertIn(f"open core: recommendation covers the {core} core", p["licence_disclosure"], value)
                continue
            self.assertEqual(p["licence_eligibility"], "conditional", value)
            cl = p["licence"]["classification"]
            self.assertEqual((cl["class"], cl["core_spdx"]), ("open_core", core), value)
            self.assertIn(f"open core: recommendation covers the {core} core", p["open_core"]["disclosure"])
            ec = p["licence"]["edition_check"]
            self.assertTrue(ec["provenance"]["checked_at"] and ec["provenance"]["commit"] == cl["provenance"]["commit"])
            if p["licence_eligibility"] == "eligible":
                self.assertEqual(ec["result"], "pass")
                self.assertIn("open core", p["warning"])
            else:
                self.assertEqual(p["licence"]["eligibility"]["reason_code"], ec["reason_code"], value)
                self.assertIn("edition check", p["warning"])
            prov = cl.get("provenance") or cl
            self.assertTrue(prov.get("checked_at") and prov.get("commit"), value)  # S1.7

    def test_conditional_rows_reach_discovery_but_never_recommendations(self):
        r = self.call("source_get_alternatives", {"saas": "salesforce", "include_discovery": True, "limit": 10})
        # KEI-930 D5: a conditional (open-core) project is recommended only under an approved licence exception,
        # and then always with its disclosure; otherwise it stays a discovery candidate.
        disc = {c["id"]: c for c in r["discovery_candidates"]["candidates"]}
        for x in r["recommendations"]:
            lic = (self.s.layer.projects[x["oss_id"]]["licence"])
            if lic["status"] != "recognised":
                self.assertEqual(lic["status"], "exception_approved", x["oss_id"])
                self.assertTrue(x.get("licence_disclosure"), x["oss_id"])
        if "github.com/twentyhq/twenty" in disc:
            self.assertEqual(disc["github.com/twentyhq/twenty"]["licence_label"], "open_core")
        for c in self.s.layer.corpus.values():
            self.assertIn(M.corpus_licence_eligibility(c), ("eligible", "conditional", "not_eligible"))
            if c["licence"]["status"] != "recognised" and not c["licence"].get("eligibility"):
                self.assertEqual(M.corpus_licence_eligibility(c), "not_eligible")  # fail closed

    def test_nonsense_is_not_padded(self):
        for need in ("zzqx frobnicate widget", "blue", "the and of"):
            r = self.call("source_recommend", {"need": need, "include_discovery": True})
            self.assertEqual(r["recommendations"], [], need)
            self.assertEqual(r["discovery_candidates"]["candidates"], [], need)
            self.assertIn("never padded", r["shortfall"])

    def test_constraint_filtered_response_is_explicit(self):
        r = self.call("source_recommend", {"saas": "jira", "licence_family": "permissive", "limit": 5})
        self.assertIn("constraint_filtered", r)
        self.assertEqual(r["constraint_filtered"]["before_constraints"],
                         r["constraint_filtered"]["after_constraints"] + sum(r["excluded_by_constraint"].values()))

    def test_no_integrations_in_supported_product_discovery(self):
        for p in json.loads((ROOT / "api" / "v1" / "index.json").read_text())["supported_saas"]:
            top = self.call("source_get_alternatives", {"saas": p["saas_id"], "include_discovery": True})
            for c in top["discovery_candidates"]["candidates"][:5]:
                self.assertIsNone(M.integration_kind(c["name"], c["full_name"], c["description"], c["topics"],
                                                     [p["name"]], M.claims_alternative(c["description"],
                                                                                       c["topics"], p["name"])),
                                  (p["saas_id"], c["id"]))
                self.assertNotIn("-mcp", c["full_name"].lower())

    def test_golden_set_targets(self):
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "kei919_golden.py")], capture_output=True,
                             text=True, check=True, cwd=ROOT)
        s = json.loads(out.stdout)
        self.assertEqual(s["constraint_violations"], 0)
        self.assertEqual(s["padding_rows"], 0)
        # At most 1: github.com/arvo-ai/aurora, an upstream-listed PagerDuty alternative whose description says it
        # "integrates with" the tools it watches. The audit flags it; it is a real replacement (KEI-922 base: 1).
        self.assertLessEqual(s["integrations_in_top5"], 1)
        self.assertEqual(s["lookups_ok"].split("/")[0], s["lookups_ok"].split("/")[1])
        # Re-calibrated by Keith on 2026-10-20 (KEI-930, D10) to the scores on the KEI-930 tree, which serves
        # 352 products: no regression against those (was 1.0 / 0.96 / 0.75 / 0.75 on the pre-WP2 run,
        # docs/kei-919/golden-baseline.json).
        self.assertGreaterEqual(s["need_named"]["precision_at_5"], 0.96)
        self.assertGreaterEqual(s["need_named"]["recall_at_5"], 0.96)
        self.assertGreaterEqual(s["need_unnamed"]["precision_at_5"], 0.725)
        self.assertGreaterEqual(s["need_unnamed"]["recall_at_5"], 0.70)


if __name__ == "__main__":
    unittest.main()
