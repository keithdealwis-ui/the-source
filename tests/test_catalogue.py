"""KEI-922: the demand-led catalogue — candidates, detection for every entry, ranking, selection, honest states."""
import json

import pytest

from source_pipeline import catalogue, common, detect

API = common.ROOT / "api" / "v1"
HAVE_SURFACE = (API / "catalogue.json").exists()


@pytest.fixture(scope="module")
def cfg():
    return catalogue.load_config()


@pytest.fixture(scope="module")
def cands(cfg):
    return catalogue.candidates(cfg)


@pytest.fixture(scope="module")
def all_rules(cands):
    return catalogue.rules(cands)


# ---------------------------------------------------------------- candidates

def test_about_600_candidates_each_tagged(cands):
    assert 550 <= len(cands) <= 650
    assert len({c["saas_id"] for c in cands}) == len(cands)
    for c in cands:
        assert c["audience"] in ("b2b", "b2c", "both"), c["saas_id"]
        assert c["category"] and c["domain"] and c["match"], c["saas_id"]
    audiences = {c["audience"] for c in cands}
    assert audiences == {"b2b", "b2c", "both"}


def test_every_catalogued_product_is_a_candidate(cands):
    from source_pipeline import normalise

    products, _ = normalise.load_catalogue()
    assert {c["saas_id"] for c in cands} == set(products)


# ---------------------------------------------------------------- detection, every entry

def test_every_rule_detects_its_own_product_among_all_rules(all_rules):
    wrong = [(u, r["saas_id"], detect.match_url(u, all_rules)) for r in all_rules for u in detect.example_urls(r)
             if detect.match_url(u, all_rules) != r["saas_id"]]
    assert wrong == []


def test_every_candidate_is_detectable(cands, all_rules):
    detected = {detect.match_url(u, all_rules) for r in all_rules for u in detect.example_urls(r)}
    assert {c["saas_id"] for c in cands} <= detected


def test_no_rule_is_shared_by_two_products(all_rules):
    seen = {}
    for r in all_rules:
        k = detect.format_rule(r)
        assert seen.setdefault(k, r["saas_id"]) == r["saas_id"], k


def test_detection_examples(cfg, all_rules):
    """HubSpot, Confluence/Jira/JSM, Google, Microsoft, AWS, GitHub, Figma, Salesforce, lookalikes."""
    wrong = [(u, exp, detect.match_url(u, all_rules)) for u, exp in cfg["examples"]
             if detect.match_url(u, all_rules) != exp]
    assert wrong == []
    assert len(cfg["examples"]) >= 60


@pytest.mark.parametrize("url,expected", [
    ("https://app.hubspot.com/contacts/1/objects/0-1", "hubspot"),
    ("https://app.hubspot.com/email/1/manage", "hubspot-marketing-hub"),
    ("https://app.hubspot.com/emailing/1", "hubspot"),
    ("https://app.hubspot.com/website/1/pages", "hubspot-content-hub"),
    ("https://app.hubspot.com/help-desk/1", "hubspot-service-hub"),
    ("https://acme.atlassian.net/wiki/spaces/X", "confluence"),
    ("https://acme.atlassian.net/wikipedia", None),
    ("https://acme.atlassian.net/jira/servicedesk/projects/IT", "jira-service-management"),
    ("https://acme.atlassian.net/jira/software/projects/A", "jira"),
    ("https://www.atlassian.com/software/confluence", "confluence"),
])
def test_hubspot_and_confluence_patterns(all_rules, url, expected):
    assert detect.match_url(url, all_rules) == expected


def test_parse_rule_grammar():
    assert detect.parse_rule("slack.com") == {"host": "slack.com"}
    assert detect.parse_rule("*.slack.com") == {"host_suffix": ".slack.com"}
    assert detect.parse_rule("*-my.sharepoint.com") == {"host_suffix": "-my.sharepoint.com"}
    assert detect.parse_rule("*.atlassian.net/wiki") == {"host_suffix": ".atlassian.net", "path_prefix": "/wiki"}
    for bad in ("https://slack.com", "www.slack.com", "*slack.com", "*.com", "Slack.com", "slack.com/",
                "slack.com?x=1", " slack.com", "slack.com//x", ""):
        with pytest.raises(detect.RuleError):
            detect.parse_rule(bad)


def test_segments_and_specificity():
    rules = [{"saas_id": "a", "host": "x.com"}, {"saas_id": "b", "host": "x.com", "path_prefix": "/p"},
             {"saas_id": "c", "host": "x.com", "path_prefix": "/p/q"}, {"saas_id": "d", "host_suffix": ".x.com"},
             {"saas_id": "e", "host_suffix": ".deep.x.com"}, {"saas_id": "f", "host": "y.com", "path_prefix": "/d/"}]
    m = lambda u: detect.match_url(u, rules)
    assert m("https://x.com/") == "a"
    assert m("https://www.x.com/p") == "b"
    assert m("https://x.com/p/") == "b"
    assert m("https://x.com/pq") == "a"
    assert m("https://x.com/p/q/r") == "c"
    assert m("https://a.x.com/") == "d"
    assert m("https://a.deep.x.com/") == "e"
    assert m("https://y.com/d/x") == "f"
    assert m("https://y.com/d") is None
    assert m("ftp://x.com/") is None
    assert m("x.com/p") == "b"                      # a bare host is read as https
    assert m("https://x.com.evil.example/") is None


# ---------------------------------------------------------------- honest states

def test_status_rule():
    ev = {"awaiting": 0, "not_recommended": 0, "outside_launch": 0, "share_alike_only": 0, "licence_review": 0,
          "corpus_pairings": 0, "job_defined": True}
    assert catalogue.status(3, 3, ev)["status"] == "ok"
    assert catalogue.status(3, 3, ev)["reasons"] == ["full_evaluation"]
    p = catalogue.status(2, 3, {**ev, "awaiting": 4})
    assert p["status"] == "partial" and p["reasons"] == ["fewer_than_minimum", "awaiting_full_evaluation"]
    assert "2 of the 3" in p["reason"]
    n = catalogue.status(0, 3, ev)
    assert n["status"] == "none_yet" and n["reasons"] == ["no_relationship_evidence"]
    # corpus pairings, demand or anything short of full evaluation never makes a product ok
    many = {**ev, "corpus_pairings": 50, "awaiting": 20}
    assert catalogue.status(0, 3, many)["status"] == "none_yet"
    assert catalogue.status(0, 3, {**ev, "corpus_pairings": 5})["reasons"] == ["corpus_candidates_only"]
    j = catalogue.status(0, 3, {**ev, "job_defined": False, "outside_launch": 2}, edges_max=400)
    assert j["reasons"] == ["outside_launch_selection", "no_job_definition"]
    assert "edges_max 400" in j["reason"]


@pytest.mark.skipif(not HAVE_SURFACE, reason="needs the committed read surface")
def test_published_statuses_come_from_full_evaluation_only():
    cat = json.loads((API / "catalogue.json").read_text())
    rels = common.read_jsonl(common.DATA / "canonical" / "relationships.jsonl")
    full = {}
    for r in rels:
        if r["recommended"] and (r["replacement_fit"] or {}).get("scoring_path") in catalogue.FULL_PATHS:
            full[r["saas_id"]] = full.get(r["saas_id"], 0) + 1
    assert len(cat["entries"]) == 500
    for e in cat["entries"]:
        n = full.get(e["saas_id"], 0)
        assert e["recommended"] == n
        assert e["status"] == ("ok" if n >= 3 else "partial" if n else "none_yet"), e["saas_id"]
        assert e["reason"] and e["reasons"]
        if e["status"] != "none_yet":
            assert e["path"] == f"saas/{e['saas_id']}.json"
    counts = cat["counts"]
    assert counts["ok"] == sum(e["status"] == "ok" for e in cat["entries"])
    assert counts["ok"] + counts["partial"] + counts["none_yet"] == 500
    assert cat["demand"]["status"] in ("available", "unavailable")


@pytest.mark.skipif(not HAVE_SURFACE, reason="needs the committed read surface")
def test_named_products_evaluated_and_honest():
    cat = {e["saas_id"]: e for e in json.loads((API / "catalogue.json").read_text())["entries"]}
    for sid in ("slack", "salesforce", "zapier", "1password"):
        e = cat[sid]
        doc = json.loads((API / "saas" / f"{sid}.json").read_text())
        assert doc["catalogue"]["status"] == e["status"]
        assert len(doc["recommendations"]) == min(e["recommended"], 5)
        for rec in doc["recommendations"]:
            assert rec["evidence"]["replacement_fit"]["scoring_path"] in catalogue.FULL_PATHS
            assert rec["evidence"]["relationship"]["provenance"]


@pytest.mark.skipif(not HAVE_SURFACE, reason="needs the committed read surface")
def test_support_records_for_every_ok_and_partial_entry():
    cat = json.loads((API / "catalogue.json").read_text())
    index = json.loads((API / "index.json").read_text())
    served = {r["saas_id"] for r in index["supported_saas"]}
    for e in cat["entries"]:
        if e["status"] != "none_yet":
            assert e["saas_id"] in served and e["path"] == f"saas/{e['saas_id']}.json"
        if e["compare_path"] and e["status"] != "ok":
            assert e["saas_id"] in {p["saas_id"] for p in common.load_yaml(common.CONFIG / "supported_saas.yaml")["products"]}
    assert index["catalogue"]["ok"] == cat["counts"]["ok"]
    assert cat["counts"]["support_records"] == len(served)


# ---------------------------------------------------------------- selection

def test_selection_reproduces_and_keeps_must_include():
    rep = catalogue.validate()
    failed = [c for c in rep["checks"] if not c["ok"]]
    assert failed == []


def test_selection_records_demand_honestly():
    sel = catalogue.load_selection()
    rows = common.read_jsonl(common.DATA / "catalogue" / "candidates.jsonl")
    assert sel["telemetry"]["status"] == "no_product_dimension"
    if sel["demand"]["status"] == "unavailable":
        assert all(r["demand"]["status"] == "unavailable" and r["demand"]["band"] is None for r in rows)
    assert [r["rank"] for r in rows] == list(range(1, len(rows) + 1))


# ---------------------------------------------------------------- demand (aiq-kwi import), synthetic input

def test_keyword_matching(cands, cfg):
    names = catalogue._name_index(cands)
    mods = cfg["alternative_intent"]
    assert catalogue.match_keyword("notion alternative", names, mods) == "notion"
    assert catalogue.match_keyword("open source alternatives to google docs", names, mods) == "google-docs"
    assert catalogue.match_keyword("self-hosted slack", names, mods) == "slack"
    assert catalogue.match_keyword("notion pricing", names, mods) is None          # no alternative intent
    assert catalogue.match_keyword("best free alternative", names, mods) is None   # names no product


def test_kwi_import_and_ranking_use_demand(tmp_path, cfg):
    # Synthetic numbers, made up for the test; nothing here is a real search volume.
    report = {"run": {"seeds": ["notion alternative", "trello alternative", "clover alternative"], "location": "GB"},
              "keywords": [{"keyword": "notion alternative", "volume": 1000},
                           {"keyword": "open source notion", "volume": 500},
                           {"keyword": "trello alternative open source", "volume": 10},
                           {"keyword": "notion pricing", "volume": 99999}]}
    src = tmp_path / "report.json"
    src.write_text(json.dumps(report))
    out = catalogue.import_kwi(src, cfg, out_dir=tmp_path / "demand")
    assert out["with_demand"] == 2 and out["keywords_unmatched"] == 1
    d = catalogue.load_demand(tmp_path / "demand")
    assert d["status"] == "available" and d["volume"] == {"notion": 1500, "trello": 10}
    assert "clover" in d["covered"]                        # seeded, nothing found: measured as zero
    rows = {r["saas_id"]: r for r in catalogue.rank(cfg, demand_dir=tmp_path / "demand")["rows"]}
    assert rows["notion"]["demand"] == {"status": "measured", "monthly_volume": 1500,
                                        "score": rows["notion"]["demand"]["score"]}
    assert rows["clover"]["demand"]["monthly_volume"] == 0
    assert rows["slack"]["demand"] == {"status": "not_covered", "monthly_volume": None, "score": None}
    assert rows["notion"]["score"] != rows["notion"]["supply_score"]


def test_selection_is_a_snapshot_when_the_cycle_moves_inputs(monkeypatch):
    """After a daily cycle the ranking inputs move; the selection stands and validation says so instead of failing."""
    sel = catalogue.load_selection()
    moved = {**sel, "ranked_from": {**sel["ranked_from"], "corpus_version": "0000000000000000"}}
    monkeypatch.setattr(catalogue, "load_selection", lambda root=None: moved)
    rep = catalogue.validate()
    assert rep["ok"] and rep["selection_inputs_moved"]
    detail = next(c["detail"] for c in rep["checks"] if c["id"] == "selection_reproduces")
    assert "corpus_version" in detail and "catalogue-rank" in detail


def test_public_rows_carry_a_band_not_a_volume():
    # KEI-922 f2: raw Keyword Planner volumes, and the rank score that would give them back, are never committed.
    r = {"saas_id": "x", "score": 70.1, "supply_score": 50.0,
         "demand": {"status": "measured", "monthly_volume": 1500, "score": 80.0}}
    assert catalogue.public_row(r) == {"saas_id": "x", "supply_score": 50.0,
                                       "demand": {"status": "measured", "band": "high"}}
    assert [catalogue.demand_band(v) for v in (None, 0, 1, 99, 100, 999, 1000)] == \
        [None, "none", "low", "low", "medium", "medium", "high"]
    rows = common.read_jsonl(common.DATA / "catalogue" / "candidates.jsonl")
    assert all("score" not in r and set(r["demand"]) == {"status", "band"} for r in rows)
