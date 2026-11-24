"""recommend matches a need to products by category and SaaS lexical match, with a floor on match strength; product-
driven discovery candidates must agree with the product's category, not just share its word."""
from __future__ import annotations

from pathlib import Path

import pytest

from source_pipeline import mcp_server as M

ROOT = Path(__file__).resolve().parent.parent
HAVE = all((ROOT / p).is_file() for p in ("data/canonical/MANIFEST.json", "api/v1/index.json",
                                          "data/momentum/MANIFEST.json", "data/radar/MANIFEST.json"))
pytestmark = pytest.mark.skipif(not HAVE, reason="needs the committed read and signal layers")
NOW = M.datetime(2026, 11, 24, 12, tzinfo=M.timezone.utc)


@pytest.fixture(scope="module")
def server():
    return M.Server(ROOT, NOW)


def _rec(server, **args):
    return M.tool_recommend(server.layer, args, server)


def test_password_manager_need_matches_password_management_only(server):
    r = _rec(server, need="self-hosted password manager for a small team")
    layer = server.layer
    assert r["status"] == "ok"
    assert r["matched_products"]
    assert {layer.saas[m["saas_id"]]["category"] for m in r["matched_products"]} == {"Password management"}
    names = {m["name"] for m in r["matched_products"]}
    assert not names & {"Microsoft Teams", "Adobe Experience Manager"}
    assert r["recommendations"]
    assert {layer.saas[x["for_saas"]]["category"] for x in r["recommendations"]} == {"Password management"}


def test_newsletter_need_finds_email_marketing_alternatives(server):
    r = _rec(server, need="something to send a weekly newsletter to my subscribers")
    layer = server.layer
    assert r["status"] == "ok"
    cats = {layer.saas[m["saas_id"]]["category"] for m in r["matched_products"]}
    assert cats <= {"Email marketing", "Publishing"} and "Email marketing" in cats
    assert {m["saas_id"] for m in r["matched_products"]} >= {"mailchimp", "substack", "kit", "beehiiv"}
    names = {x["name"].lower() for x in _rec(server, need="something to send a weekly newsletter to my subscribers",
                                             limit=20)["recommendations"]}
    assert {"listmonk", "keila"} <= names


def test_low_strength_match_is_never_ok(server):
    r = _rec(server, need="self-hosted manager for a small team")
    assert r["recommendations"], "candidates are still listed for inspection"
    assert r["status"] == "weak_match"
    assert r["match_strength"]["floor"] == M.MIN_PRODUCT_MATCH
    assert all(m.get("weak") for m in r["matched_products"])
    assert "never padded" in r.get("shortfall", "") or len(r["recommendations"]) == 5


def test_no_answer_reports_ok_below_the_floor(server):
    for need in ("self-hosted password manager for a small team",
                 "something to send a weekly newsletter to my subscribers", "team chat", "video conferencing",
                 "self-hosted manager for a small team", "open source alternative to airtable", "the and of"):
        r = _rec(server, need=need)
        if r["status"] == "ok":
            assert (any(m["strength"] >= M.MIN_PRODUCT_MATCH for m in r["matched_products"])
                    or any((x["retrieval"] or {}).get("score", 0) >= M.MIN_PRODUCT_MATCH
                           for x in r["recommendations"])), need


def test_named_product_still_wins_over_category(server):
    r = _rec(server, need="open source alternative to airtable")
    assert [m["matched_by"] for m in r["matched_products"]] == ["named_in_need"]
    assert r["status"] == "ok"


def test_calendly_discovery_excludes_job_schedulers(server):
    r = M.tool_get_alternatives(server.layer, {"saas": "Calendly", "include_discovery": True, "limit": 10}, server)
    dc = r["discovery_candidates"]
    names = {c["name"] for c in dc["candidates"]}
    assert not names & {"Saturn", "nn_dataflow", "PyJobShop", "gocron"}
    assert dc["excluded"].get("off_category", 0) > 0
    for c in dc["candidates"]:
        ret = c["retrieval"]
        assert (ret.get("category_topics") or (ret.get("quick_fit") or 0) >= M.MIN_DISCOVERY_QUICK_FIT
                or ret.get("basis") or M.claims_alternative(c["description"], c["topics"], "Calendly")), c["name"]
    if len(dc["candidates"]) < 10:
        assert "not padded" in dc["shortfall"]
