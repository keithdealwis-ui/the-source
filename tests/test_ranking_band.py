"""Rubric 1.4.0 (stress test 2026-11-24): within a 5-point Replacement Fit band, adoption (stars, then Project Health)
decides the order; evidence tier and licence tier still come first. Figures are dataset 2702df4c40902975's."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from source_pipeline import canonical, mcp_server


def rec(oss_id, fit, health, stars, cls="listed", disclosure=None):
    r = {"oss_id": oss_id, "stars": stars, "replacement_fit": {"score": fit}, "project_health": {"score": health},
         "evidence": {"relationship": {"evidence_class": cls}}, "evidence_class": cls}
    if disclosure:
        r["licence_disclosure"] = disclosure
    return r


MAILCHIMP = [rec("github.com/knadh/listmonk", 78.5, 90.3, 24439),
             rec("github.com/mautic/mautic", 70.1, 94.9, 11099),
             rec("github.com/pentacent/keila", 82.2, 77.9, 2269),
             rec("github.com/phplist/phplist3", 79.3, 79.2, 881, cls="job_matched")]

NOTION = [rec("github.com/appflowy-io/appflowy", 63.0, 67.3, 79451),
          rec("github.com/bookstackapp/bookstack", 62.2, 85.7, 19150),
          rec("github.com/churichard/notabase", 78.6, 67.5, 915),
          rec("github.com/dexio-wiki/dexio", 80.0, 65.3, 10),
          rec("github.com/docmost/docmost", 83.7, 78.5, 22410),
          rec("github.com/evergreen-it-dev/folio", 92.0, 75.1, 92),
          rec("github.com/wikimedia/mediawiki", 72.7, 82.9, 5233),
          rec("github.com/xwiki/xwiki-platform", 78.5, 87.8, 1336)]


def names(recs):
    return [r["oss_id"].rsplit("/", 1)[-1] for r in recs]


@pytest.mark.parametrize("rank", [canonical.rank, mcp_server._rank], ids=["canonical", "mcp_mirror"])
def test_mailchimp_listmonk_leads_its_band_and_phplist_stays_behind_listed(rank):
    # before: keila (82.2), listmonk, mautic, phplist. Listmonk is 3.7 below Keila with 10x the stars.
    assert names(rank(MAILCHIMP)) == ["listmonk", "keila", "mautic", "phplist3"]


@pytest.mark.parametrize("rank", [canonical.rank, mcp_server._rank], ids=["canonical", "mcp_mirror"])
def test_notion_docmost_leads_its_band_but_a_larger_gap_still_decides(rank):
    # Folio (92.0) is 8.3 above Docmost: outside the band, so it stays first. Docmost (22k stars) then leads Dexio
    # (80.0, 10 stars); XWiki (78.5) and Notabase (78.6) share a band and XWiki's stars place it first.
    assert names(rank(NOTION)) == ["folio", "docmost", "dexio", "xwiki-platform", "notabase", "mediawiki",
                                   "appflowy", "bookstack"]


@pytest.mark.parametrize("rank", [canonical.rank, mcp_server._rank], ids=["canonical", "mcp_mirror"])
def test_evidence_and_licence_tiers_still_come_first(rank):
    recs = [rec("github.com/a/job", 95, 99, 900000, cls="job_matched"),
            rec("github.com/a/sa", 90, 99, 500000, disclosure="source-available: BUSL-1.1"),
            rec("github.com/a/oc", 90, 99, 400000, disclosure="open core: AGPL core"),
            rec("github.com/a/osi", 88, 50, 10)]
    assert names(rank(recs)) == ["osi", "oc", "sa", "job"]


def test_band_is_anchored_on_its_leader_and_the_mirror_agrees_on_the_live_layer():
    recs = [rec("github.com/a/x", 90, 50, 1), rec("github.com/a/y", 85, 50, 2), rec("github.com/a/z", 80.5, 50, 3)]
    # y is within 5 of x; z is 9.5 below x, so it opens the next band even though it is within 5 of y
    assert names(canonical.rank(recs)) == ["y", "x", "z"]
    for group in (MAILCHIMP, NOTION, recs):
        assert names(canonical.rank(group)) == names(mcp_server._rank(group))


def test_score_version_bumped_with_the_ranking_rule():
    cfg = yaml.safe_load((Path(__file__).resolve().parent.parent / "config" / "scoring.yaml").read_text())
    assert cfg["version"] == "1.4.0" and canonical.FIT_BAND == mcp_server.FIT_BAND == 5.0
