"""KEI-849: repository momentum and acceleration from the dated daily history. No network."""
from __future__ import annotations

import copy
import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from source_pipeline import common, cycle, daily, momentum

FIXTURES = Path(__file__).parent / "fixtures" / "momentum"
SPEC = common.load_yaml(FIXTURES / "series.yaml")


def _day(s: str) -> date:
    return date.fromisoformat(s)


def _stars(segments: list[dict], d: date) -> int:
    value, prev_from, rate = None, None, 0
    for seg in segments:
        start = _day(seg["from"])
        if d < start:
            break
        value = seg["base"] if value is None else value + rate * (start - prev_from).days
        prev_from, rate = start, seg["per_day"]
    return value + rate * (d - prev_from).days


def write_fixture_history(spec: dict, as_of_overrides: dict | None = None) -> None:
    """Write the fixture's days through the real history writer (hashes, INDEX.json)."""
    start, end = _day(spec["days"]["start"]), _day(spec["days"]["end"])
    missing = set(spec["days"].get("missing") or [])
    d = start
    while d <= end:
        day = d.isoformat()
        if day not in missing:
            as_of = (as_of_overrides or {}).get(day) or f"{(d - timedelta(days=1)).isoformat()}T23:30:00Z"
            projects = {}
            for key, p in spec["projects"].items():
                if p.get("present_from") and day < p["present_from"]:
                    continue
                t = (d - start).days
                rec = {"found": True, "error": None, "fetched_at": as_of, "canonical_id": key,
                       "canonical_name": key.split("/", 1)[1], "host_repo_id": p["repo_id"], "archived": False,
                       "disabled": False, "licence_spdx": "MIT", "forks": 10 + t // 10, "open_issues": 5,
                       "pushed_at": f"{(d - timedelta(days=1)).isoformat()}T12:00:00Z", "latest_release": None,
                       "head": {"oid": f"oid-{t // p.get('head_every', 10 ** 6)}", "at": None}}
                rec["stars"] = _stars(p["stars"], d) + (t % 2 if p.get("wobble") else 0)
                if p.get("rename") and day >= p["rename"]["day"]:
                    rec["canonical_id"] = p["rename"]["canonical_id"]
                if p.get("new_repo_id") and day >= p["new_repo_id"]["day"]:
                    rec["host_repo_id"] = p["new_repo_id"]["id"]
                for r in p.get("releases") or []:
                    if day >= r["day"]:
                        rec["latest_release"] = {"tag": r["tag"], "published_at": None}
                if p.get("found_until") and day > p["found_until"]:
                    rec = {"found": False, "error": "not_found", "fetched_at": as_of}
                projects[key] = rec
            daily.write_history(day, {"as_of": as_of, "projects": projects}, {}, "fixture", f"run-{day}")
        d += timedelta(days=1)


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "DATA", tmp_path / "data")
    return tmp_path


@pytest.fixture
def fx(root):
    write_fixture_history(SPEC)
    return momentum.compute()


def _w(result: dict, key: str, window: int) -> dict:
    return next(p for p in result["projects"] if p["key"] == key)["windows"][str(window)]


# ---------------------------------------------------------------- fixture: golden and by hand

def test_fixture_output_matches_the_golden_file(fx):
    expected = common.read_json(FIXTURES / "expected.json")
    assert json.loads(common.dumps(fx)) == expected


def test_momentum_is_stars_per_elapsed_day_relative_to_the_base(fx):
    w = _w(fx, "github.com/fx/steady", 7)
    base = 1000 + 2 * 179  # 2026-09-27 is day 179 of the fixture
    assert (w["status"], w["base_day"], w["exact"], w["span_days"]) == ("measured", "2026-09-27", True, 7.0)
    assert (w["stars_base"], w["stars_delta"], w["stars_per_day"]) == (base, 14, 2.0)
    assert w["growth_pct_per_30d"] == round(2 * 30 / base * 100, 3) and w["momentum"] == "rising"
    assert w["acceleration"]["label"] == "steady" and w["acceleration"]["stars_per_day_change"] == 0.0
    assert w["explanation"][0].startswith(f"stars {base} -> {base + 14} (+14) over 7.0 d (2026-09-27 -> 2026-10-04)")
    assert w["activity"] == {"pushed_since_base": True, "new_release": False, "latest_release_tag": None,
                             "head_changed_day_pairs": 4, "day_pairs_observed": 7, "day_pairs_in_window": 7}


def test_acceleration_compares_consecutive_windows(fx):
    acc = _w(fx, "github.com/fx/accelerating", 7)
    assert acc["stars_per_day"] == 25.0 and acc["acceleration"]["prior_stars_per_day"] == 5.0
    assert acc["acceleration"]["stars_per_day_change"] == 20.0 and acc["acceleration"]["label"] == "accelerating"
    assert acc["acceleration"]["prior_base_day"] == "2026-09-20" and acc["acceleration"]["threshold_per_day"] == 1.25
    assert acc["activity"]["new_release"] and acc["activity"]["latest_release_tag"] == "v1.1"
    dec = _w(fx, "github.com/fx/decelerating", 7)
    assert dec["acceleration"]["stars_per_day_change"] == -38.0 and dec["acceleration"]["label"] == "decelerating"
    assert len(acc["explanation"]) == 2 and "-> accelerating" in acc["explanation"][1]


def test_bands_cover_declining_and_noise(fx):
    d = _w(fx, "github.com/fx/declining", 7)
    assert d["stars_delta"] == -7 and d["momentum"] == "declining"
    flat = _w(fx, "github.com/fx/flat-small", 7)
    assert abs(flat["stars_delta"]) < 3 and flat["momentum"] == "flat" and "under 3 stars" in flat["explanation"][0]
    one = _w(fx, "github.com/fx/flat-small", 1)["acceleration"]  # +1 then -1: a wobble, not a slowdown
    assert one["stars_per_day_change"] == -2.0 and one["label"] == "steady" and "both windows under 3" in one["explanation"]


def test_base_falls_back_inside_tolerance_and_says_so(fx):
    w = _w(fx, "github.com/fx/steady", 30)
    assert (w["base_day"], w["exact"], w["span_days"], w["stars_delta"]) == ("2026-09-02", False, 32.0, 64)
    cov = fx["manifest"]["coverage"]["30"]
    assert cov["base"] == {"target_day": "2026-09-04", "base_day": "2026-09-02", "exact": False,
                           "base_gap_days": 2, "tolerance_days": 3}
    assert cov["snapshots_in_window"] == 31 and cov["calendar_days_in_window"] == 33  # two days missing


def test_ninety_days_and_its_acceleration_are_measured_with_enough_history(fx):
    w = _w(fx, "github.com/fx/accelerating", 90)
    assert w["base_day"] == "2026-07-06" and w["stars_delta"] == 5 * 83 + 25 * 7
    assert w["acceleration"]["status"] == "measured" and w["acceleration"]["prior_base_day"] == "2026-04-07"
    for win in ("1", "7", "30", "90"):
        assert fx["manifest"]["coverage"][win]["status"] == "measured"
        assert fx["manifest"]["coverage"][win]["acceleration"]["status"] == "measured"


def test_identity_new_and_missing_projects_are_not_compared(fx):
    assert _w(fx, "github.com/fx/new", 7)["status"] == "no_baseline"
    assert _w(fx, "github.com/fx/new", 1)["stars_per_day"] == 3.0
    assert _w(fx, "github.com/fx/new", 1)["acceleration"]["status"] == "measured"
    ren = _w(fx, "github.com/fx/renamed", 7)
    assert ren["status"] == "measured" and "renamed github.com/fx/renamed -> github.com/fx/renamed-now" in ren["notes"][0]
    rep = _w(fx, "github.com/fx/replaced", 7)
    assert rep["status"] == "identity_changed" and "8 -> 88" in rep["reason"]
    assert _w(fx, "github.com/fx/replaced", 1)["status"] == "measured"
    assert _w(fx, "github.com/fx/replaced", 1)["acceleration"]["status"] == "measured"
    for win in (1, 7, 30, 90):
        assert _w(fx, "github.com/fx/gone", win)["status"] == "not_observed"
    counts = fx["manifest"]["coverage"]["7"]["projects"]
    assert counts == {"measured": 6, "insufficient_history": 0, "no_baseline": 1, "not_observed": 1,
                      "identity_changed": 1}


def test_pushes_are_compared_as_instants_not_strings():
    assert not momentum._pushed_since("2026-10-03T23:47:22+02:00", "2026-10-03T22:00:00Z")  # 21:47Z is earlier
    assert momentum._pushed_since("2026-10-03T22:30:00Z", "2026-10-03T23:47:22+02:00")
    assert not momentum._pushed_since(None, "2026-10-03T22:30:00Z")


# ---------------------------------------------------------------- honest coverage

def test_short_history_is_insufficient_never_invented(root):
    spec = copy.deepcopy(SPEC)
    spec["days"] = {"start": "2026-09-25", "end": "2026-10-04", "missing": []}
    write_fixture_history(spec)
    r = momentum.compute()
    cov = r["manifest"]["coverage"]
    assert cov["1"]["status"] == "measured" and cov["7"]["status"] == "measured"
    assert cov["7"]["acceleration"]["status"] == "insufficient_history"
    assert cov["7"]["acceleration"]["measurable_from"] == "2026-10-09"
    for win, first in (("30", "2026-10-25"), ("90", "2026-12-24")):
        assert cov[win]["status"] == "insufficient_history" and cov[win]["measurable_from"] == first
        assert "oldest held is 2026-09-25" in cov[win]["base"]["reason"]
        for p in r["projects"]:
            w = p["windows"][win]
            assert w["status"] == "insufficient_history" and w["reason"] and "stars_delta" not in w


def test_a_gap_beyond_tolerance_is_insufficient(root):
    spec = copy.deepcopy(SPEC)
    spec["days"]["missing"] = ["2026-09-26", "2026-09-27"]
    write_fixture_history(spec)
    r = momentum.compute()
    w = _w(r, "github.com/fx/steady", 7)
    assert w["status"] == "insufficient_history"
    assert w["reason"] == "nearest snapshot on or before 2026-09-27 is 2026-09-25, 2 d early (tolerance 1 d)"


def test_runs_hours_apart_are_not_a_day(root):
    write_fixture_history(SPEC, as_of_overrides={"2026-10-03": "2026-10-03T20:00:00Z"})
    r = momentum.compute()
    w = _w(r, "github.com/fx/steady", 1)
    assert w["status"] == "insufficient_history" and "only 0.15 d elapsed" in w["reason"]
    w7 = _w(r, "github.com/fx/steady", 7)
    assert w7["status"] == "measured"


def test_measuring_an_earlier_day_ignores_later_history(fx):
    early = momentum.compute("2026-10-01")
    w = _w(early, "github.com/fx/accelerating", 7)
    assert early["manifest"]["on"] == "2026-10-01" and w["base_day"] == "2026-09-24"
    assert w["stars_delta"] == 5 * 3 + 25 * 4


# ---------------------------------------------------------------- versioning, determinism, storage

def test_parameters_cannot_change_without_a_new_version():
    cfg = momentum.load_config()
    assert momentum.check_version(cfg) == []
    changed = copy.deepcopy(cfg)
    changed["momentum"]["bands"]["rising"] = 2.0
    assert "without a version bump" in momentum.check_version(changed)[0]
    with pytest.raises(ValueError):
        momentum.compute(cfg=changed)
    bumped = copy.deepcopy(changed)
    bumped["methodology"]["version"] = "1.1.0"
    bumped["methodology"]["released"]["1.1.0"] = momentum.parameters_sha256(bumped)
    assert momentum.check_version(bumped) == []


def test_written_output_is_deterministic_and_validates(fx, root, monkeypatch):
    momentum.write(fx)
    first = {f: (root / "data/momentum" / f).read_bytes() for f in ("MANIFEST.json", "projects.jsonl")}
    momentum.build()
    assert {f: (root / "data/momentum" / f).read_bytes() for f in first} == first
    assert momentum.validate()["ok"]
    (root / "data/momentum/projects.jsonl").write_text(first["projects.jsonl"].decode().replace('"rising"', '"surging"', 1))
    bad = {c["id"] for c in momentum.validate()["checks"] if not c["ok"]}
    assert {"files_hash", "rebuild_is_byte_identical"} <= bad


def test_tampered_history_is_refused(fx, root):
    p = root / "data/history/daily/2026-10-04.jsonl.gz"
    raw = bytearray(p.read_bytes())
    raw[20] ^= 0xFF
    p.write_bytes(bytes(raw))
    with pytest.raises(ValueError, match="sha256 mismatch"):
        momentum.compute()


# ---------------------------------------------------------------- retrieval

def test_retrieval_by_key_canonical_id_and_ranking(fx):
    momentum.write(fx)
    got = momentum.retrieve(["GITHUB.COM/fx/renamed-now", "github.com/fx/gone", "github.com/x/unknown"], window=7)
    assert [p["key"] for p in got["projects"]] == ["github.com/fx/gone", "github.com/fx/renamed"]
    assert got["unknown_keys"] == ["github.com/x/unknown"] and got["coverage"]["status"] == "measured"
    assert got["projects"][0]["window"]["status"] == "not_observed"
    ranked = momentum.retrieve(window=7, sort="acceleration")
    assert [p["key"] for p in ranked["projects"]][:1] == ["github.com/fx/accelerating"]
    assert ranked["projects"][-1]["key"] == "github.com/fx/decelerating"
    assert momentum.retrieve(window=7, sort="growth", limit=1)["projects"][0]["key"] == "github.com/fx/accelerating"
    assert [p["key"] for p in momentum.retrieve(window=7, momentum="declining")["projects"]] == ["github.com/fx/declining"]
    assert momentum.retrieve(window=7, acceleration="decelerating")["matched"] == 1
    with pytest.raises(ValueError):
        momentum.retrieve(window=14)


def test_retrieval_reports_coverage_when_nothing_is_measured(root):
    spec = copy.deepcopy(SPEC)
    spec["days"] = {"start": "2026-10-02", "end": "2026-10-04", "missing": []}
    write_fixture_history(spec)
    momentum.build()
    got = momentum.retrieve(window=30, sort="growth")
    assert got["matched"] == 0 and got["projects"] == []
    assert got["coverage"]["status"] == "insufficient_history" and got["coverage"]["measurable_from"] == "2026-11-01"


# ---------------------------------------------------------------- the committed data and the daily flow

def test_committed_momentum_validates():
    report = momentum.validate()
    assert report["ok"], [c for c in report["checks"] if not c["ok"]]


def test_committed_momentum_is_honest_about_short_history():
    m = common.read_json(common.DATA / "momentum" / "MANIFEST.json")
    held = daily.history_days()
    assert m["on"] == held[-1] and m["history"]["days_held"] == len(held)
    span = (date.fromisoformat(held[-1]) - date.fromisoformat(held[0])).days
    for win, cov in m["coverage"].items():
        if int(win) > span:
            assert cov["status"] == "insufficient_history" and cov["projects"]["measured"] == 0


def test_daily_cycle_builds_and_promotes_momentum():
    assert "data/momentum" in cycle.PROMOTE
    assert "momentum" in common.load_yaml(common.CONFIG / "schedule.yaml")["strategies"]
    src = Path(cycle.__file__).read_text()
    assert src.index("momentum.cycle_step(day)") > src.index("daily.write_history(day")  # after today's snapshot


def test_cycle_step_on_the_committed_history_plus_a_new_day(tmp_path, monkeypatch):
    """What the next daily cycle does: write tomorrow's snapshot over the real history,
    then run the cycle's momentum step on it."""
    shutil.copytree(common.DATA / "history", tmp_path / "data" / "history")
    monkeypatch.setattr(common, "DATA", tmp_path / "data")
    last = daily.history_days()[-1]
    today = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
    rows = daily.load_day(last)
    as_of = f"{last}T23:40:00Z"
    projects = {k: {**r, "fetched_at": as_of, "stars": (r["stars"] + 7) if r.get("stars") is not None else None}
                for k, r in rows.items()}
    daily.write_history(today, {"as_of": as_of, "projects": projects}, {}, "next", "run-next")
    got = momentum.cycle_step(today)
    assert got["ok"], got["failed_checks"]
    s = got["strategy"]
    assert s["on"] == today and s["methodology_version"] == momentum.load_config()["methodology"]["version"]
    assert s["windows"]["1"]["status"] == "measured" and s["windows"]["1"]["acceleration"] == "measured"
    assert s["windows"]["90"]["status"] == "insufficient_history"
    found = [k for k, r in rows.items() if r.get("found") and r.get("stars") is not None]
    w = momentum.retrieve([found[0]], window=1)["projects"][0]["window"]
    assert w["stars_delta"] == 7 and w["base_day"] == last


def test_methodology_doc_names_the_version_and_every_label():
    cfg = momentum.load_config()
    doc = (common.ROOT / cfg["methodology"]["doc"]).read_text()
    assert f"`{cfg['methodology']['version']}`" in doc
    for word in ("surging", "rising", "flat", "declining", "accelerating", "steady", "decelerating",
                 *momentum.STATUSES):
        assert f"`{word}`" in doc, word
