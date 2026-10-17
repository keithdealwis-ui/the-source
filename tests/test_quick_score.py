"""KEI-921 (V2.1 WP4): quick Health, inferred pairings, quick Fit, incremental rescoring, and the rule that
quick scores never recommend. No network."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from source_pipeline import mcp_server as M
from source_pipeline import quick_score as Q
from source_pipeline.normalise import load_catalogue

ROOT = Path(__file__).resolve().parent.parent
HAVE = (ROOT / "data" / "quick" / "MANIFEST.json").is_file() and (ROOT / "data" / "corpus" / "MANIFEST.json").is_file()
needs_quick = pytest.mark.skipif(not HAVE, reason="needs the committed corpus and quick layers")
AT = datetime(2026, 10, 17, tzinfo=timezone.utc)
CFG = Q.load_cfg()


def _facts(**kw):
    f = {"pushed_at": "2026-10-10T00:00:00Z", "stars": 5000, "forks": 300, "latest_release_at": "2026-09-01T00:00:00Z",
         "has_release": True, "archived": False, "found": True, "maintenance": "active",
         "observed_as_of": "2026-10-16T00:00:00Z"}
    f.update(kw)
    return f


# -- quick Health -----------------------------------------------------------------------------------

def test_health_is_labelled_quick_and_never_recommendation_eligible():
    h = Q.score_health("github.com/x/y", _facts(), AT, CFG)
    assert h["label"] == "quick" and h["method"] == Q.HEALTH_METHOD
    assert h["recommendation_eligible"] is False and h["publish"] is False
    assert 0 <= h["score"] <= 100 and h["band"] in {"Strong", "Moderate", "Weak", "Poor"}
    assert "not calibrated" in h["confidence_meaning"]


def test_health_rewards_activity_and_adoption():
    fresh = Q.score_health("a", _facts(), AT, CFG)["score"]
    stale = Q.score_health("a", _facts(pushed_at="2023-01-01T00:00:00Z", latest_release_at=None, has_release=False),
                           AT, CFG)["score"]
    tiny = Q.score_health("a", _facts(stars=2, forks=0), AT, CFG)["score"]
    assert fresh > tiny and fresh > stale


def test_archived_is_capped_and_flagged():
    h = Q.score_health("a", _facts(archived=True), AT, CFG)
    assert h["score"] <= CFG["health"]["archived_cap"] and "archived" in h["watch_outs"]


def test_missing_facts_lower_confidence_not_score_to_zero():
    full = Q.score_health("a", _facts(), AT, CFG)
    gap = Q.score_health("a", _facts(pushed_at=None, maintenance=None), AT, CFG)
    assert gap["confidence"] < full["confidence"] and gap["score"] > 0


def test_stale_facts_lower_confidence():
    h = Q.score_health("a", _facts(observed_as_of="2026-08-01T00:00:00Z"), AT, CFG)
    assert "facts_stale" in h["watch_outs"] and h["confidence"] < 100


def test_health_is_a_pure_function_of_facts_and_time():
    assert Q.score_health("a", _facts(), AT, CFG) == Q.score_health("a", _facts(), AT, CFG)


# -- pairings and quick Fit -------------------------------------------------------------------------

def _catalogue():
    products, _ = load_catalogue()
    return [products[k] for k in sorted(products)]


def _proj(pid="github.com/x/notes", description=None, topics=()):
    return {"id": pid, "name": pid.rsplit("/", 1)[-1], "description": description, "topics": list(topics)}


def test_pairing_from_description_topic_and_replace_lane():
    cat = _catalogue()
    d = Q.detect_pairings(_proj(description="An open-source alternative to Notion and Confluence"), cat, {})
    assert {r["saas_id"] for r in d} == {"notion", "confluence"}
    t = Q.detect_pairings(_proj(topics=["airtable-alternative"]), cat, {})
    assert [r["saas_id"] for r in t] == ["airtable"] and t[0]["evidence"][0]["source"] == "topic"
    r = Q.detect_pairings(_proj(), cat, {"github.com/x/notes": {"trello"}})
    assert [x["saas_id"] for x in r] == ["trello"] and r[0]["evidence"][0]["source"] == "replace_lane"


def test_a_bare_mention_is_not_a_pairing():
    assert Q.detect_pairings(_proj(description="Sync your Notion pages to Markdown"), _catalogue(), {}) == []


def test_fit_scores_each_pairing_and_is_labelled():
    p = _proj(description="Open-source alternative to Notion: notes, wiki and docs")
    pairing = Q.detect_pairings(p, _catalogue(), {})[0]
    h = Q.score_health(p["id"], _facts(), AT, CFG)
    fit = Q.score_fit(p, pairing, h, "eligible", CFG)
    assert 0 < fit["score"] <= 100 and set(fit["components"]) == {"evidence", "category", "health", "licence"}
    assert Q.score_fit(p, pairing, h, "not_eligible", CFG)["score"] < fit["score"]


# -- the committed layer ----------------------------------------------------------------------------

@needs_quick
def test_committed_layer_validates():
    report = Q.validate()
    bad = [c for c in report["checks"] if not c["ok"]]
    assert not bad, bad
    assert report["coverage_pct"] >= 95.0


@needs_quick
def test_manifest_records_cost_and_no_api_calls():
    m = json.loads((ROOT / "data" / "quick" / "MANIFEST.json").read_text())
    lr = m["last_run"]
    assert lr["api_calls"] == 0 and lr["failure_count"] == 0 and lr["runtime_s"] < m["last_run"]["caps"]["max_runtime_s"]
    assert m["counts"]["recommendation_eligible"] == 0
    assert m["counts"]["paired_projects"] == m["counts"]["paired_projects_with_fit"]


# -- incremental rescoring and caps -----------------------------------------------------------------

def _scratch(tmp_path) -> Path:
    root = tmp_path / "root"
    for rel in ("config", "data/corpus", "data/scores", "data/canonical", "api/v1"):
        shutil.copytree(ROOT / rel, root / rel)
    (root / "data" / "dataset").mkdir(parents=True)
    shutil.copy(ROOT / "data" / "dataset" / "relationships.jsonl", root / "data" / "dataset" / "relationships.jsonl")
    return root


def _run(root, *args):
    env = dict(os.environ, THE_SOURCE_ROOT=str(root), THE_SOURCE_NO_NETWORK="1",
               PYTHONPATH=os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")])))
    p = subprocess.run([sys.executable, "-m", "source_pipeline", *args], cwd=ROOT, env=env, capture_output=True,
                       text=True, timeout=300)
    return p


@needs_quick
def test_repeat_run_is_incremental_and_byte_stable(tmp_path):
    root = _scratch(tmp_path)
    assert _run(root, "quick-score", "--full").returncode == 0
    before = (root / "data" / "quick" / "health.jsonl").read_bytes()
    p = _run(root, "quick-score")
    assert p.returncode == 0, p.stderr
    lr = json.loads(p.stdout)["last_run"]
    assert lr["mode"] == "incremental" and lr["health_rescored"] == 0 and lr["pairings_rescored"] == 0
    assert (root / "data" / "quick" / "health.jsonl").read_bytes() == before

    # one project's facts change: exactly that project is rescored
    live = root / "data" / "corpus" / "live.jsonl"
    rows = [json.loads(l) for l in live.read_text().splitlines()]
    target = next(r for r in rows if r["found"] and r["stars"] and r["stars"] > 100)
    target["stars"] = target["stars"] * 50
    live.write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows))
    lr = json.loads(_run(root, "quick-score").stdout)["last_run"]
    assert lr["health_rescored"] == 1 and lr["health_unchanged"] == lr["projects"] - 1 - lr["not_scored"]["host_not_found"]
    h = {json.loads(l)["oss_id"]: json.loads(l) for l in (root / "data" / "quick" / "health.jsonl").read_text().splitlines()}
    assert h[target["id"]]["facts"]["stars"] == target["stars"]


@needs_quick
def test_cap_defers_work_and_keeps_previous_scores(tmp_path):
    root = _scratch(tmp_path)
    assert _run(root, "quick-score", "--full").returncode == 0
    cfg = (root / "config" / "quick_score.yaml").read_text()
    # a config change rescores everything; a cap of 5 must defer the rest, keep their previous scores, and say so
    cfg = cfg.replace("archived_cap: 10", "archived_cap: 11").replace("max_rescore_per_run: 20000", "max_rescore_per_run: 5")
    (root / "config" / "quick_score.yaml").write_text(cfg)
    lr = json.loads(_run(root, "quick-score").stdout)["last_run"]
    assert lr["health_rescored"] + lr["pairings_rescored"] == 5 and lr["deferred"] > 0
    rows = [json.loads(l) for l in (root / "data" / "quick" / "health.jsonl").read_text().splitlines()]
    assert sum(1 for r in rows if r.get("deferred")) > 0 and len(rows) == lr["projects"] - lr["not_scored"]["host_not_found"]


@needs_quick
def test_validate_refuses_a_row_claiming_recommendation(tmp_path):
    root = _scratch(tmp_path)
    assert _run(root, "quick-score", "--full").returncode == 0
    path = root / "data" / "quick" / "pairings.jsonl"
    rows = [json.loads(l) for l in path.read_text().splitlines()]
    rows[0]["recommendation_eligible"] = True
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert _run(root, "quick-validate").returncode == 1
    # and the read surface will not serve a tampered layer
    assert M._load_quick(root)["status"] == "unavailable"


# -- the read surface: searchable, comparable, never recommended -------------------------------------

@pytest.fixture(scope="module")
def srv():
    s = M.Server(ROOT, M.datetime(2026, 10, 17, 12, tzinfo=M.timezone.utc))
    s.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}})
    return s


def _ok(s, name, **args):
    r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})["result"]
    assert r["isError"] is False, r
    return r["structuredContent"]["result"]


@needs_quick
def test_discovery_projects_carry_quick_scores(srv):
    assert srv.layer.quick_state["status"] == "ok", srv.layer.quick_state
    pid = next(p for p in sorted(srv.layer.corpus) if p in srv.layer.quick_health)
    out = _ok(srv, "source_get_project", project=pid)
    q = out["project"]["quick_scores"]
    assert q["label"] == "quick" and q["recommendation"] is False and q["quick_health"]["score"] is not None
    assert out["relationships"] == []


@needs_quick
def test_search_filters_by_quick_health(srv):
    out = _ok(srv, "source_search", query="self-hosted", kind="project", tier="discovery", min_quick_health=70, limit=50)
    assert out["results"] and out["quick_scores"]["status"] == "ok"
    assert all(r["project"]["quick_scores"]["quick_health"]["score"] >= 70 for r in out["results"])


@needs_quick
def test_compare_puts_quick_scores_side_by_side(srv):
    pids = [p for p in sorted(srv.layer.corpus) if p in srv.layer.quick_health][:2]
    out = _ok(srv, "source_compare_projects", projects=pids)
    assert all(r["quick_scores"]["quick_health"]["score"] is not None for r in out["projects"])
    assert all(r["project_health"] is None for r in out["projects"])
    assert any("quick scores only" in u for u in out["uncertainty"])


@needs_quick
def test_quick_pairings_never_become_recommendations(srv):
    served = [(sid, rows) for sid, rows in sorted(srv.layer.quick_by_saas.items()) if sid in srv.layer.saas]
    assert served
    for sid, rows in served:
        out = _ok(srv, "source_get_alternatives", saas=sid, include_discovery=True, limit=20)
        recommended = {r["oss_id"] for r in out["recommendations"]}
        assert not recommended & {r["oss_id"] for r in rows}, sid
        q = out["quick_paired_candidates"]
        assert q["recommendation"] is False and q["label"] == "quick" and q["total"] == len(rows)
