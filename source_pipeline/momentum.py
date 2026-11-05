"""Repository momentum and acceleration (KEI-849). Methodology: docs/MOMENTUM.md.

Derived only from the dated daily snapshots KEI-848 keeps in data/history/. For every
corpus repository and every window W in config/momentum.yaml (1, 7, 30, 90 days):

  momentum      stars gained per elapsed day over the W days ending on the newest
                snapshot, and that gain as % of the base per 30 days, banded
                surging / rising / flat / declining.
  acceleration  that rate minus the rate over the W days before it (needs 2W days of
                history), labelled accelerating / steady / decelerating.
  activity      what else moved in the window: pushes, a new release, and on how many
                observed day-pairs the default-branch head changed.

Coverage is honest: a window whose base snapshot is missing, too far from its target day
or too close in time is `insufficient_history` with the reason and the day it becomes
measurable, never an interpolated or invented baseline. A repository absent at the base
is `no_baseline`; one not found now or then is `not_observed`; one whose host repository
id changed is `identity_changed`. Each measured value carries the base and current
observations it came from and a one-line explanation, so it can be checked by hand.

Sanity gate (KEI-961, `sanity` in the config): before a window is published as measured,
its endpoint must agree with the newest live star count the pipeline holds
(data/live/snapshot.json) and every step inside it must be plausible: a sudden jump is
published only once a later observation confirms it. A window that fails is `suppressed`
with the check and the reason, never a figure.

    data/momentum/MANIFEST.json    methodology version and fingerprint, history used,
                                   per-window coverage, sanity gate, counts, file hashes
    data/momentum/projects.jsonl   one record per repository, every window
    data/momentum/live.json        the live star counts the sanity gate compared against
                                   (copied from data/live/snapshot.json at build time)

Deterministic: a pure function of data/history/, data/momentum/live.json and
config/momentum.yaml; no wall clock.
It never touches data/canonical/ or api/v1/, so the canonical dataset version is
unaffected.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

from . import common, daily
from .activity import parse_ts

SCHEMA = "the-source.momentum/1"
STATUSES = ("measured", "insufficient_history", "no_baseline", "not_observed", "identity_changed", "suppressed")
CHECKS = ("live_identity_mismatch", "live_disagreement", "unconfirmed_jump", "transient_spike")


def _dir() -> Path:
    return common.DATA / "momentum"


def load_config() -> dict:
    return common.load_yaml(common.CONFIG / "momentum.yaml")


def parameters_sha256(cfg: dict) -> str:
    params = {k: v for k, v in cfg.items() if k not in ("methodology", "schema")}
    return hashlib.sha256(common.dumps(params).encode()).hexdigest()


def check_version(cfg: dict) -> list[str]:
    """Parameters must equal the ones their version was released with."""
    m = cfg["methodology"]
    released = m.get("released") or {}
    got = parameters_sha256(cfg)
    if m["version"] not in released:
        return [f"methodology {m['version']} is not in methodology.released"]
    if released[m["version"]] != got:
        return [f"parameters changed without a version bump: {m['version']} was released as "
                f"{released[m['version']]}, parameters now hash to {got}"]
    return []


def _d(day: str) -> date:
    return date.fromisoformat(day)


def _days_between(a: str, b: str) -> int:
    return (_d(b) - _d(a)).days


def _elapsed_days(a: str | None, b: str | None) -> float | None:
    ta, tb = parse_ts(a), parse_ts(b)
    return None if ta is None or tb is None else (tb - ta).total_seconds() / 86400


def _r(x: float, n: int = 4) -> float:
    return round(x, n) + 0.0  # + 0.0: never -0.0


# ---------------------------------------------------------------- window resolution

def resolve_base(held: list[str], on: str, window: int, tolerance: int) -> dict:
    """The base snapshot day of the `window` days ending on `on`, or why there is none."""
    target = (_d(on) - timedelta(days=window)).isoformat()
    base = max((d for d in held if d <= target), default=None)
    out = {"target_day": target, "tolerance_days": tolerance}
    if base is None:
        return {**out, "ok": False, "reason": f"no snapshot on or before {target}; oldest held is {held[0]}"}
    gap = _days_between(base, target)
    if gap > tolerance:
        return {**out, "ok": False, "base_day": base,
                "reason": f"nearest snapshot on or before {target} is {base}, {gap} d early (tolerance {tolerance} d)"}
    return {**out, "ok": True, "base_day": base, "exact": gap == 0, "base_gap_days": gap}


def measurable_from(held: list[str], window: int, legs: int = 1) -> str | None:
    """The first day whose `legs` x `window`-day look-back reaches the oldest snapshot held:
    no window can be measured before it, whatever happens to later snapshots."""
    return (_d(held[0]) + timedelta(days=window * legs)).isoformat() if held else None


# ---------------------------------------------------------------- per-project measures

def _band(pct: float, delta: int, cfg: dict) -> str:
    if abs(delta) < cfg["min_abs_change"]:
        return "flat"
    bands = cfg["bands"]
    if pct >= bands["surging"]:
        return "surging"
    if pct >= bands["rising"]:
        return "rising"
    if pct <= bands["declining"]:
        return "declining"
    return "flat"


def _comparable(cur: dict | None, old: dict | None) -> tuple[str | None, str | None]:
    """(status, reason) if the pair cannot be compared, else (None, note-or-None)."""
    if cur is None or not cur.get("found") or cur.get("stars") is None:
        return "not_observed", "not found (or no star count) in the current snapshot"
    if old is None:
        return "no_baseline", "not in the base snapshot (new to the corpus since)"
    if not old.get("found") or old.get("stars") is None:
        return "not_observed", "not found (or no star count) in the base snapshot"
    if old.get("host_repo_id") and cur.get("host_repo_id") and old["host_repo_id"] != cur["host_repo_id"]:
        return "identity_changed", (f"host repository id {old['host_repo_id']} -> {cur['host_repo_id']}: "
                                    "a different repository, not comparable")
    if old.get("canonical_id") != cur.get("canonical_id"):
        return None, f"renamed {old.get('canonical_id')} -> {cur.get('canonical_id')} (same repository id)"
    return None, None


def _rate(cur: dict, old: dict, as_cur: str, as_old: str) -> tuple[float | None, int]:
    span = _elapsed_days(old.get("fetched_at") or as_old, cur.get("fetched_at") or as_cur)
    return span, cur["stars"] - old["stars"]


def _pushed_since(now: str | None, then: str | None) -> bool:
    """Compared as instants: hosts report pushed_at with different UTC offsets."""
    a, b = parse_ts(now), parse_ts(then)
    return bool(a and (b is None or a > b))


def _activity(key: str, days: list[str], snaps: dict, base_day: str, on: str) -> dict:
    cur, old = snaps[on][key], snaps[base_day][key]
    inside = [d for d in days if base_day <= d <= on]
    pairs = changes = 0
    for a, b in zip(inside, inside[1:]):
        if _days_between(a, b) != 1:
            continue
        ra, rb = snaps[a].get(key), snaps[b].get(key)
        ha, hb = ((ra or {}).get("head") or {}).get("oid"), ((rb or {}).get("head") or {}).get("oid")
        if ha and hb:
            pairs += 1
            changes += ha != hb
    tag_now = (cur.get("latest_release") or {}).get("tag")
    tag_then = (old.get("latest_release") or {}).get("tag")
    return {
        "pushed_since_base": _pushed_since(cur.get("pushed_at"), old.get("pushed_at")),
        "new_release": bool(tag_now) and tag_now != tag_then,
        "latest_release_tag": tag_now,
        "head_changed_day_pairs": changes,
        "day_pairs_observed": pairs,
        "day_pairs_in_window": _days_between(base_day, on),
    }


def measure(key: str, window: int, on: str, base: dict, prior: dict | None, snaps: dict, days: list[str],
            index: dict, cfg: dict, live: dict | None = None) -> dict:
    """One project, one window. `base` and `prior` come from resolve_base; `live` is the
    project's live verdict from live_verdict (None: not compared)."""
    if not base["ok"]:
        return {"status": "insufficient_history", "reason": base["reason"]}
    bday = base["base_day"]
    cur, old = snaps[on].get(key), snaps[bday].get(key)
    status, note = _comparable(cur, old)
    if status:
        return {"status": status, "reason": note, "base_day": bday}
    span, delta = _rate(cur, old, index[on]["as_of"], index[bday]["as_of"])
    min_span = cfg["base"]["min_span_ratio"] * window
    if span is None or span < min_span:
        return {"status": "insufficient_history", "base_day": bday,
                "reason": f"only {_r(span or 0, 2)} d elapsed between the {bday} and {on} observations "
                          f"(at least {min_span:g} d needed for a {window}-day window)"}
    mcfg = cfg["momentum"]
    per_day = delta / span
    denom = max(old["stars"], mcfg["relative_floor_stars"])
    pct = per_day * 30 / denom * 100
    band = _band(pct, delta, mcfg)
    f_now, f_then = cur.get("forks"), old.get("forks")
    rec = {
        "status": "measured", "base_day": bday, "exact": base["exact"], "span_days": _r(span),
        "stars_base": old["stars"], "stars_now": cur["stars"], "stars_delta": delta,
        "stars_per_day": _r(per_day), "growth_pct_per_30d": _r(pct, 3), "momentum": band,
        "forks_delta": None if f_now is None or f_then is None else f_now - f_then,
        "activity": _activity(key, days, snaps, bday, on),
    }
    expl = (f"stars {old['stars']} -> {cur['stars']} ({delta:+d}) over {_r(span, 2)} d "
            f"({bday} -> {on}): {per_day:+.2f}/day, {pct:+.3f}% of {denom} per 30 d -> {band}")
    if abs(delta) < mcfg["min_abs_change"]:
        expl += f" (|change| under {mcfg['min_abs_change']} stars)"
    rec["explanation"] = [expl]
    notes = [note] if note else []
    # ---- sanity gate (KEI-961): only a figure that passes it is published
    if live and not live["ok"]:
        return _suppressed(live["check"], live["reason"], bday)
    jump = scan_jumps(key, days, snaps, bday, on, on, cfg)
    if jump["check"]:
        return _suppressed(jump["check"], jump["reason"], bday)
    notes += jump["notes"]
    if notes:
        rec["notes"] = notes
    rec["acceleration"] = _acceleration(key, window, bday, per_day, delta, prior, snaps, index, cfg, days, on)
    if rec["acceleration"]["status"] == "measured":
        rec["explanation"].append(rec["acceleration"]["explanation"])
    return rec


def _suppressed(check: str, reason: str, base_day: str | None = None, prior_base_day: str | None = None) -> dict:
    out = {"status": "suppressed", "check": check, "reason": reason}
    if base_day:
        out["base_day"] = base_day
    if prior_base_day:
        out["prior_base_day"] = prior_base_day
    return out


# ---------------------------------------------------------------- sanity gate (KEI-961)

def _jump_threshold(prev: int, cfg: dict) -> float:
    j = cfg["sanity"]["jump"]
    return max(j["min_abs_stars"], j["relative"] * max(prev, cfg["momentum"]["relative_floor_stars"]))


def scan_jumps(key: str, days: list[str], snaps: dict, start: str, end: str, on: str, cfg: dict) -> dict:
    """Every step between consecutive observations of `key` from `start` to `end` must be
    plausible, and so must the step into the `start` observation (a base that was itself a
    spike is no base). A step at or over the jump threshold counts only if the observations
    after it, up to `on` (never later history), keep at least confirm_retained of it.
    Returns {check: None | "unconfirmed_jump" | "transient_spike", reason, notes}."""
    def stars(d):
        r = snaps[d].get(key) or {}
        return r.get("stars") if r.get("found") else None

    seen = [(d, stars(d)) for d in days if d <= on and stars(d) is not None]
    first = next((i for i, (d, _) in enumerate(seen) if d >= start), len(seen))
    obs = seen[max(0, first - 1):]
    keep = cfg["sanity"]["jump"]["confirm_retained"]
    notes = []
    for (da, sa), (db, sb) in zip(obs, obs[1:]):
        if db > end:
            break
        step, threshold = sb - sa, _jump_threshold(sa, cfg)
        if abs(step) < threshold:
            continue
        what = f"stars {sa} -> {sb} ({step:+d}) between the {da} and {db} observations (jump threshold {threshold:g})"
        later = [(d, s) for d, s in obs if d > db]
        if not later:
            return {"check": "unconfirmed_jump", "notes": [],
                    "reason": f"{what} is the newest observation; held until a later observation confirms it"}
        retained = min((s - sa) / step for _, s in later)
        if retained < keep:
            low = min(later, key=lambda x: (x[1] - sa) / step)
            return {"check": "transient_spike", "notes": [],
                    "reason": f"{what} reverted: {low[1]} on {low[0]} keeps {retained:.0%} of it "
                              f"(at least {keep:.0%} needed)"}
        notes.append(f"sudden jump: {what}, confirmed by {len(later)} later observation(s) "
                     f"({later[-1][1]} on {later[-1][0]})")
    return {"check": None, "reason": None, "notes": notes}


LIVE_FIELDS = ("found", "stars", "host_repo_id", "fetched_at")


def live_view(keys=None) -> dict:
    """The newest live star counts the pipeline holds (data/live/snapshot.json, written by
    enrich.write_snapshot in the cycle's step 6), cut down to `keys`. The build keeps this
    copy in data/momentum/live.json, so the gate's result can be rebuilt and checked even
    after the live snapshot moves on."""
    p = common.DATA / "live" / "snapshot.json"
    if not p.exists():
        return {"as_of": None, "sha256": None, "projects": {}}
    snap = common.read_json(p)
    rows = snap.get("projects") or {}
    want = sorted(rows) if keys is None else sorted(k for k in keys if k in rows)
    return {"as_of": snap.get("as_of"), "sha256": common.sha256_file(p),
            "projects": {k: {f: rows[k].get(f) for f in LIVE_FIELDS} for k in want}}


def live_comparable(view: dict, on_as_of: str, cfg: dict) -> dict:
    """The manifest's sanity.live block: whether the endpoint is compared with the live
    snapshot and why not."""
    if not view.get("as_of"):
        return {"status": "skipped", "reason": "no live snapshot at data/live/snapshot.json"}
    lag = _elapsed_days(on_as_of, view["as_of"])
    max_lag = cfg["sanity"]["live"]["max_lag_hours"]
    out = {"as_of": view["as_of"], "sha256": view["sha256"], "endpoint_as_of": on_as_of,
           "lag_hours": None if lag is None else _r(lag * 24, 2), "max_lag_hours": max_lag}
    if lag is None or abs(lag * 24) > max_lag:
        return {**out, "status": "skipped",
                "reason": f"live snapshot as_of {view['as_of']} is not within {max_lag} h of the endpoint "
                          f"({on_as_of}); not compared"}
    return {**out, "status": "compared"}


def live_verdict(row: dict | None, live_row: dict | None, cfg: dict) -> dict:
    """Does today's history observation agree with the live record for the same key?"""
    if not row or not row.get("found") or row.get("stars") is None:
        return {"ok": True}  # nothing will be measured anyway
    if not live_row or not live_row.get("found") or live_row.get("stars") is None:
        return {"ok": False, "check": "live_disagreement",
                "reason": "the endpoint has stars but the live snapshot has no star count for this key"}
    hid, lid = row.get("host_repo_id"), live_row.get("host_repo_id")
    if hid and lid and hid != lid:
        return {"ok": False, "check": "live_identity_mismatch",
                "reason": f"history observation is host repository id {hid}, the live record is {lid}: "
                          "not the same repository"}
    lc = cfg["sanity"]["live"]
    allowed = max(lc["stars_absolute"], lc["stars_relative"] * live_row["stars"])
    diff = row["stars"] - live_row["stars"]
    if abs(diff) > allowed:
        return {"ok": False, "check": "live_disagreement",
                "reason": f"endpoint stars {row['stars']} disagree with live stars {live_row['stars']} "
                          f"(fetched {live_row.get('fetched_at')}) by {diff:+d}, more than {allowed:g}"}
    return {"ok": True}


def _acceleration(key, window, mid, rate_now, delta_now, prior, snaps, index, cfg, days, on) -> dict:
    if prior is None or not prior["ok"]:
        return {"status": "insufficient_history",
                "reason": (prior or {}).get("reason") or f"needs {2 * window} d of history"}
    pday = prior["base_day"]
    cur, old = snaps[mid].get(key), snaps[pday].get(key)
    status, note = _comparable(cur, old)
    if status:
        return {"status": status, "prior_base_day": pday, "reason": f"prior window {pday} -> {mid}: {note}"}
    span, delta = _rate(cur, old, index[mid]["as_of"], index[pday]["as_of"])
    min_span = cfg["base"]["min_span_ratio"] * window
    if span is None or span < min_span:
        return {"status": "insufficient_history", "prior_base_day": pday,
                "reason": f"prior window {pday} -> {mid}: only {_r(span or 0, 2)} d elapsed "
                          f"(at least {min_span:g} d needed)"}
    jump = scan_jumps(key, days, snaps, pday, mid, on, cfg)
    if jump["check"]:
        return _suppressed(jump["check"], f"prior window {pday} -> {mid}: {jump['reason']}", prior_base_day=pday)
    acfg = cfg["acceleration"]
    floor = cfg["momentum"]["min_abs_change"]
    rate_prior = delta / span
    change = rate_now - rate_prior
    threshold = max(acfg["min_rate_change_per_day"], acfg["relative"] * abs(rate_prior))
    noise = max(abs(delta_now), abs(delta)) < floor  # both windows flat: a wobble, not a trend
    label = ("steady" if noise else "accelerating" if change >= threshold
             else "decelerating" if change <= -threshold else "steady")
    expl = (f"{rate_now:+.2f}/day ({mid} -> now) vs {rate_prior:+.2f}/day ({pday} -> {mid}): "
            f"change {change:+.2f}/day, threshold +/-{threshold:.2f} -> {label}")
    if noise:
        expl += f" (both windows under {floor} stars)"
    return {"status": "measured", "prior_base_day": pday, "prior_span_days": _r(span),
            "prior_stars_delta": delta, "prior_stars_per_day": _r(rate_prior),
            "stars_per_day_change": _r(change), "threshold_per_day": _r(threshold), "label": label,
            "explanation": expl}


# ---------------------------------------------------------------- build

def compute(on: str | None = None, cfg: dict | None = None, live: dict | None = None) -> dict:
    """{manifest, projects, live} for `on` (default: the newest history day). Reads history,
    and `live` (a live_view; default: the current data/live/snapshot.json)."""
    cfg = cfg or load_config()
    errors = check_version(cfg)
    if errors:
        raise ValueError("; ".join(errors))
    held = daily.history_days()
    if not held:
        raise ValueError("no history held: momentum needs data/history/ (written by the daily cycle)")
    on = on or held[-1]
    if on not in held:
        raise ValueError(f"no snapshot for {on}")
    held = [d for d in held if d <= on]
    index = common.read_json(common.DATA / "history" / "INDEX.json")["days"]
    windows = [int(w) for w in cfg["windows"]]
    tol = {int(k): int(v) for k, v in cfg["base"]["tolerance_days"].items()}

    resolved = {}
    for w in windows:
        base = resolve_base(held, on, w, tol[w])
        prior = resolve_base(held, base["base_day"], w, tol[w]) if base["ok"] else None
        resolved[w] = (base, prior)
    needed = {on} | {r["base_day"] for b, p in resolved.values() for r in (b, p) if r and r.get("ok")}
    lookback = min(needed)
    # one snapshot before the oldest base, so the sanity gate sees the step into every base
    lookback = max((d for d in held if d < lookback), default=lookback)
    days = [d for d in held if d >= lookback]
    snaps = {d: daily.load_day(d) for d in days}
    live = live if live is not None else live_view(snaps[on])
    live_info = live_comparable(live, index[on]["as_of"], cfg)
    live_rows = live["projects"] if live_info["status"] == "compared" else None

    projects = []
    for key in sorted(snaps[on]):
        row = snaps[on][key]
        rec = {"key": key, "canonical_id": row.get("canonical_id"), "canonical_name": row.get("canonical_name"),
               "on": on, "stars": row.get("stars"), "windows": {}}
        verdict = None if live_rows is None else live_verdict(row, live_rows.get(key), cfg)
        for w in windows:
            base, prior = resolved[w]
            rec["windows"][str(w)] = measure(key, w, on, base, prior, snaps, days, index, cfg, verdict)
        projects.append(rec)

    coverage = {}
    suppressed = {str(w): {"momentum": {c: 0 for c in CHECKS}, "acceleration": {c: 0 for c in CHECKS}}
                  for w in windows}
    for w in windows:
        base, prior = resolved[w]
        counts = {s: 0 for s in STATUSES}
        acc = {s: 0 for s in STATUSES}
        bands, labels = {}, {}
        for p in projects:
            m = p["windows"][str(w)]
            counts[m["status"]] += 1
            if m["status"] == "suppressed":
                suppressed[str(w)]["momentum"][m["check"]] += 1
            elif m["status"] == "measured" and m["acceleration"]["status"] == "suppressed":
                suppressed[str(w)]["acceleration"][m["acceleration"]["check"]] += 1
            if m["status"] == "measured":
                bands[m["momentum"]] = bands.get(m["momentum"], 0) + 1
                a = m["acceleration"]
                acc[a["status"]] += 1
                if a["status"] == "measured":
                    labels[a["label"]] = labels.get(a["label"], 0) + 1
        inside = [d for d in held if base["base_day"] <= d <= on] if base["ok"] else []
        coverage[str(w)] = {
            "status": "measured" if counts["measured"] else "insufficient_history",
            "base": {k: base[k] for k in ("target_day", "base_day", "exact", "base_gap_days", "tolerance_days",
                                          "reason") if k in base},
            "snapshots_in_window": len(inside),
            "calendar_days_in_window": (_days_between(base["base_day"], on) + 1) if base["ok"] else None,
            "measurable_from": measurable_from(held, w),
            "projects": counts, "momentum": dict(sorted(bands.items())),
            "acceleration": {
                "status": "measured" if acc["measured"] else "insufficient_history",
                "prior_base": None if prior is None else {k: prior[k] for k in (
                    "target_day", "base_day", "exact", "base_gap_days", "reason") if k in prior},
                "measurable_from": measurable_from(held, w, legs=2),
                "projects": acc, "labels": dict(sorted(labels.items())),
            },
        }
    m = cfg["methodology"]
    manifest = {
        "schema": SCHEMA,
        "methodology": {"id": m["id"], "version": m["version"], "doc": m["doc"],
                        "parameters_sha256": parameters_sha256(cfg)},
        "on": on, "as_of": index[on]["as_of"],
        "history": {"days_held": len(held), "oldest": held[0], "newest": held[-1],
                    "days_read": days, "day_sha256": {d: index[d]["sha256"] for d in days},
                    "dataset_version": index[on].get("dataset_version"), "run_id": index[on].get("run_id")},
        "windows": windows,
        "coverage": coverage,
        "sanity": {"live": live_info, "parameters": cfg["sanity"], "suppressed": suppressed},
        "counts": {"projects": len(projects),
                   "measured_any_window": sum(1 for p in projects
                                              if any(v["status"] == "measured" for v in p["windows"].values()))},
    }
    return {"manifest": manifest, "projects": projects, "live": live}


def write(result: dict, out: Path | None = None) -> dict:
    out = out or _dir()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    common.write_jsonl(out / "projects.jsonl", result["projects"])
    common.write_json(out / "live.json", result["live"])
    manifest = dict(result["manifest"])
    manifest["files"] = {f: common.sha256_file(out / f) for f in ("projects.jsonl", "live.json")}
    common.write_json(out / "MANIFEST.json", manifest)
    return manifest


def build(on: str | None = None) -> dict:
    return write(compute(on))


def validate() -> dict:
    """Committed momentum equals a rebuild from committed history, conforms to its schema,
    and was produced under the released methodology version."""
    from jsonschema import Draft202012Validator

    checks = []

    def check(cid, ok, detail=None):
        checks.append({"id": cid, "ok": bool(ok), "detail": detail})

    cfg = load_config()
    errs = check_version(cfg)
    check("parameters_match_released_version", not errs, errs or cfg["methodology"]["version"])
    d = _dir()
    if not (d / "MANIFEST.json").exists():
        check("momentum_present", False, str(d))
        return {"ok": False, "checks": checks}
    manifest = common.read_json(d / "MANIFEST.json")
    rows = common.read_jsonl(d / "projects.jsonl")
    check("manifest_version_is_config_version",
          manifest["methodology"]["version"] == cfg["methodology"]["version"]
          and manifest["methodology"]["parameters_sha256"] == parameters_sha256(cfg),
          manifest["methodology"])
    check("files_hash", all((d / f).exists() and common.sha256_file(d / f) == h for f, h in manifest["files"].items())
          and "live.json" in manifest["files"])
    schema = common.read_json(common.SCHEMA / "the-source.momentum.schema.json")
    bad = []
    for name, obj in [("manifest", manifest)] + [(r["key"], r) for r in rows]:
        sub = {**schema, "$ref": "#/$defs/" + ("manifest" if name == "manifest" else "project")}
        bad += [f"{name}: {e.message}" for e in Draft202012Validator(sub).iter_errors(obj)][:3]
    check("schema_valid", not bad, bad[:10])
    honest = [r["key"] for r in rows for w, v in r["windows"].items()
              if v["status"] != "measured" and not v.get("reason")]
    check("unmeasured_windows_say_why", not honest, honest[:10])
    # KEI-961: no measured endpoint may disagree with the live counts it was checked against,
    # and that copy must be what the live snapshot said (while it is still the same snapshot)
    kept = common.read_json(d / "live.json") if (d / "live.json").exists() else None
    lv = (manifest.get("sanity") or {}).get("live") or {}
    if kept is not None and lv.get("status") == "compared":
        stale = []
        for r in rows:
            if any(v["status"] == "measured" for v in r["windows"].values()):
                v = live_verdict({"found": True, "stars": r["stars"]}, kept["projects"].get(r["key"]), cfg)
                if not v["ok"]:
                    stale.append(f"{r['key']}: {v['reason']}")
        check("measured_endpoints_agree_with_live", not stale, stale[:10])
    if kept is not None and kept.get("as_of"):
        now = live_view(kept["projects"])
        if now["as_of"] == kept["as_of"]:
            diff = sorted(k for k, v in kept["projects"].items() if now["projects"].get(k) != v)
            check("live_copy_matches_live_snapshot", not diff, diff[:10])
        else:
            check("live_copy_matches_live_snapshot", True,
                  f"live snapshot has moved on ({kept['as_of']} -> {now['as_of']}); momentum keeps its own copy")
    if not errs:
        with tempfile.TemporaryDirectory() as tmp:
            again = write(compute(manifest["on"], cfg, live=kept), Path(tmp) / "momentum")
            same = all((Path(tmp) / "momentum" / f).read_bytes() == (d / f).read_bytes()
                       for f in ("MANIFEST.json", "projects.jsonl", "live.json"))
        check("rebuild_is_byte_identical", same, {"on": manifest["on"], "files": again["files"]})
        check("built_on_newest_history_day", manifest["on"] == daily.history_days()[-1],
              {"on": manifest["on"], "newest": daily.history_days()[-1]})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def summary(manifest: dict) -> dict:
    return {w: {"status": c["status"], "measured": c["projects"]["measured"],
                "acceleration": c["acceleration"]["status"], "measurable_from": c["measurable_from"],
                "acceleration_measurable_from": c["acceleration"]["measurable_from"]}
            for w, c in manifest["coverage"].items()}


def cycle_step(day: str) -> dict:
    """The daily cycle's step 12, run right after `day`'s history snapshot is written."""
    manifest = build(day)
    report = validate()
    return {"ok": report["ok"], "failed_checks": [c["id"] for c in report["checks"] if not c["ok"]],
            "measured": manifest["counts"]["measured_any_window"],
            "strategy": {"id": "momentum", "status": "ok" if report["ok"] else "failed", "on": manifest["on"],
                         "methodology_version": manifest["methodology"]["version"],
                         "windows": summary(manifest)}}


# ---------------------------------------------------------------- retrieval

SORTS = {
    "growth": lambda w: w["growth_pct_per_30d"],
    "stars_per_day": lambda w: w["stars_per_day"],
    "acceleration": lambda w: (w["acceleration"].get("stars_per_day_change")
                               if w["acceleration"]["status"] == "measured" else None),
}


def retrieve(keys: list[str] | None = None, window: int = 7, sort: str | None = None, momentum: str | None = None,
             acceleration: str | None = None, limit: int | None = None) -> dict:
    """Read the committed momentum (data/momentum/). With `keys`, those repositories (by
    corpus key or current canonical id, case-insensitive), measured or not. Otherwise a
    ranking of measured repositories. Always returns the window's coverage, so an empty
    answer reads as `insufficient_history`, never as "nothing is moving"."""
    d = _dir()
    manifest = common.read_json(d / "MANIFEST.json")
    if str(window) not in manifest["coverage"]:
        raise ValueError(f"window {window} is not one of {manifest['windows']}")
    rows = common.read_jsonl(d / "projects.jsonl")
    out = {"methodology": manifest["methodology"], "on": manifest["on"], "as_of": manifest["as_of"],
           "window_days": window, "coverage": manifest["coverage"][str(window)]}
    if keys:
        want = {k.lower() for k in keys}
        hits = [r for r in rows if r["key"].lower() in want or (r.get("canonical_id") or "").lower() in want]
        found = {r["key"].lower() for r in hits} | {(r.get("canonical_id") or "").lower() for r in hits}
        out["projects"] = [_view(r, window) for r in hits]
        out["unknown_keys"] = sorted(want - found)
        return out
    sel = [r for r in rows if r["windows"][str(window)]["status"] == "measured"]
    if momentum:
        sel = [r for r in sel if r["windows"][str(window)]["momentum"] == momentum]
    if acceleration:
        sel = [r for r in sel if r["windows"][str(window)]["acceleration"].get("label") == acceleration]
    if sort:
        f = SORTS[sort]
        sel = [r for r in sel if f(r["windows"][str(window)]) is not None]
        sel.sort(key=lambda r: (-f(r["windows"][str(window)]), r["key"]))
    out["matched"] = len(sel)
    out["projects"] = [_view(r, window) for r in (sel[:limit] if limit else sel)]
    return out


def _view(r: dict, window: int) -> dict:
    return {k: r[k] for k in ("key", "canonical_id", "canonical_name", "stars")} | {"window": r["windows"][str(window)]}
