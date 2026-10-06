"""KEI-882: momentum (KEI-849) and Community Radar (KEI-850) together in the daily cycle. No network."""
from __future__ import annotations

import inspect
import shutil
from datetime import date, timedelta

from source_pipeline import common, cycle, daily, momentum, radar
from test_radar import AS_OF, _world


def test_momentum_is_step_12_and_the_radar_step_13_before_promotion():
    strategies = common.load_yaml(common.CONFIG / "schedule.yaml")["strategies"]
    assert strategies.index("history") < strategies.index("momentum") < strategies.index("community_radar")
    assert {"data/history", "data/momentum", "data/radar", "data/discovery"} <= set(cycle.PROMOTE)
    src = inspect.getsource(cycle.work)
    assert (src.index("daily.write_history(day") < src.index("momentum.cycle_step(day)")
            < src.index("radar.guarded_cycle_step(") < src.index("step(None)\n\n        canon_now"))


def _momentum_day(tmp_data):
    """The real history plus a next day, then the cycle's momentum step, in the scratch tree."""
    shutil.copytree(common.ROOT / "data" / "history", tmp_data / "history")
    last = daily.history_days()[-1]
    today = (date.fromisoformat(last) + timedelta(days=1)).isoformat()
    rows = daily.load_day(last)
    as_of = f"{last}T23:40:00Z"
    projects = {k: {**r, "fetched_at": as_of, "stars": (r["stars"] + 3) if r.get("stars") is not None else None}
                for k, r in rows.items()}
    daily.write_history(today, {"as_of": as_of, "projects": projects}, {}, "next", "run-next")
    got = momentum.cycle_step(today)
    assert got["ok"], got["failed_checks"]
    return {p.name: p.read_bytes() for p in (tmp_data / "momentum").iterdir()}


def test_steps_12_then_13_both_validate_and_the_radar_leaves_momentum_alone(tmp_path, monkeypatch):
    data, http, light = _world(tmp_path, monkeypatch)
    before = _momentum_day(data)
    rad = radar.guarded_cycle_step(AS_OF, "2026-10-06", light, http)
    assert rad["ok"], rad["failed_checks"]
    assert rad["pool_added"] == 1
    assert {p.name: p.read_bytes() for p in (data / "momentum").iterdir()} == before
    assert momentum.validate()["ok"] and radar.validate()["ok"]


def test_a_failing_radar_rolls_back_without_touching_momentum(tmp_path, monkeypatch):
    data, http, light = _world(tmp_path, monkeypatch)
    before = _momentum_day(data)
    pool = (data / "discovery" / "candidates.jsonl").read_bytes()
    monkeypatch.setattr(radar, "cycle_step", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = radar.guarded_cycle_step(AS_OF, "2026-10-06", light, http)
    assert not out["ok"] and out["strategy"]["kept_previous"]
    assert (data / "discovery" / "candidates.jsonl").read_bytes() == pool
    assert {p.name: p.read_bytes() for p in (data / "momentum").iterdir()} == before
    assert momentum.validate()["ok"]
