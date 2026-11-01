"""Weekly human review digest: what changed in the committed data, proposed as a PR (KEI-783 AC5).

    python -m source_pipeline review-digest [--since-days 7] [--to REV] [--out DIR]

The daily cycle refreshes the data and commits it to main by itself (docs/OPERATIONS.md). This
command reads what those cycles committed and lists, for a human, only the changes worth a look:

* recommendations that appeared, and those that went away (and why: withdrawn relationship, no
  longer publishable, project stale, archived or off-licence);
* relationships and projects scored for the first time;
* canonical projects that dropped out, with the dataset's own reason;
* large Replacement Fit moves on recommendations that stayed;
* the largest momentum movers (data/momentum/, 7-day window);
* intake acceptances and the window's corpus admissions (data/intake/outcomes/, runs/);
* verdicts Keith recorded (data/verdicts/).

It writes reviews/digest-YYYY-Www.md and its JSON twin. The weekly workflow
(.github/workflows/review-digest.yml) commits them to the branch review/YYYY-Www and opens a pull
request for review; nothing here changes the dataset, and nothing is merged automatically.

Deterministic: the window is measured from git history alone. The end is REV (default HEAD) and its
commit time; the start is the last commit that changed the canonical MANIFEST at least
--since-days before that. The ISO week is the end commit's. No wall clock, no network.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import common, lineage

MANIFEST = lineage.MANIFEST
REVIEWS = common.ROOT / "reviews"
DIGEST_SCHEMA = "the-source.review-digest/1"
FIT_MOVE_MIN = 10.0      # a Replacement Fit change at least this large on a recommendation that stayed
MOVER_MIN_STARS = 100    # momentum movers: ignore projects below this many stars
LIMITS = {"newly_recommended": 25, "no_longer_recommended": 25, "newly_scored": 15, "dropped_projects": 25,
          "fit_moves": 15, "momentum_surging": 10, "momentum_declining": 5, "corpus_admissions": 10}


# ---------------------------------------------------------------- git reads

def _ls(rev: str, prefix: str) -> list[str]:
    out = lineage._git("ls-tree", "--name-only", rev, prefix.rstrip("/") + "/")
    return sorted(line for line in out.splitlines() if line)


def _jsonl(rev: str, path: str) -> list:
    b = lineage._blob(rev, path, missing_ok=True)
    return [] if b is None else [json.loads(x) for x in b.decode().splitlines() if x.strip()]


def _commit_time(rev: str) -> datetime:
    return datetime.fromisoformat(lineage._git("log", "-1", "--format=%cI", rev).strip()).astimezone(timezone.utc)


def window(since_days: int, to: str | None = None) -> dict:
    head = lineage.resolve(to or "HEAD")
    end = _commit_time(head)
    cutoff = end - timedelta(days=since_days)
    base = lineage._git("log", "-1", "--format=%H", f"--before={cutoff.isoformat()}", head, "--", MANIFEST).strip()
    note = None
    if not base:
        commits = lineage.manifest_commits(head)
        if not commits:
            raise lineage.LineageError(f"no commit up to {head[:12]} changed {MANIFEST}")
        base, note = commits[0], "the history starts inside the window: compared with the first canonical version"
    iso = end.isocalendar()
    return {"base": base, "head": head, "base_at": _commit_time(base).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "head_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "since_days": since_days,
            "week": f"{iso[0]}-W{iso[1]:02d}", "note": note}


# ---------------------------------------------------------------- sections

def _recommended(rels: list) -> dict:
    return {(r["saas_id"], r["oss_id"]): r for r in rels if r["recommended"]}


def _fit(r: dict | None):
    return ((r or {}).get("replacement_fit") or {}).get("score")


def _renamed_to(oss_id: str, head_projects: dict, dataset: dict, base_projects: dict | None = None) -> str | None:
    """A canonical project that left while one of the same name joined: most often a rename or a move."""
    name = ((base_projects or {}).get(oss_id) or dataset.get(oss_id) or {}).get("name")
    if not name:
        return None
    same = sorted(o for o, p in head_projects.items() if o != oss_id and o not in (base_projects or {}) and (p.get("name") or "").lower() == name.lower()
                  and o.rsplit("/", 1)[-1] == oss_id.rsplit("/", 1)[-1])
    return same[0] if same else None


def _drop_reason(oss_id: str, head_projects: dict, dataset: dict, base_projects: dict | None = None) -> str:
    """Why a project is no longer canonical, in the dataset's own words."""
    if oss_id in head_projects:
        return "still canonical"
    moved = _renamed_to(oss_id, head_projects, dataset, base_projects)
    if moved:
        return f"probably renamed or moved: {moved} (same name) joined in the window"
    p = dataset.get(oss_id)
    if p is None:
        return "no longer in the dataset (removed upstream or merged into another identity)"
    d = p.get("derived") or {}
    live = p.get("live") or {}
    parts = []
    if live.get("archived"):
        parts.append("archived")
    m = (d.get("maintenance") or {}).get("status")
    if m and m not in ("active", "maintained"):
        parts.append(f"maintenance {m}")
    lic = (d.get("licence") or {}).get("status")
    if lic and lic not in ("recognised", "exception_approved"):
        parts.append(f"licence {lic}")
    parts += [f"excluded: {x}" for x in (d.get("eligibility") or {}).get("exclusion_reasons") or []]
    if not parts and d.get("lane"):
        parts.append(f"lane {d['lane']}")
    return "; ".join(parts) or "no exported relationship left"


def recommendation_changes(base_rels: list, head_rels: list, head_projects: dict, dataset: dict,
                           base_projects: dict | None = None) -> dict:
    before, after = _recommended(base_rels), _recommended(head_rels)
    head_by = {(r["saas_id"], r["oss_id"]): r for r in head_rels}
    new = [{"saas_id": s, "oss_id": o, "replacement_fit": _fit(after[(s, o)]),
            "confidence": (after[(s, o)]["replacement_fit"] or {}).get("confidence"),
            "keith_verdict": (after[(s, o)].get("keith_verdict") or {}).get("verdict")}
           for s, o in sorted(set(after) - set(before))]
    gone = []
    for s, o in sorted(set(before) - set(after)):
        now = head_by.get((s, o))
        if now is None:
            why = "relationship withdrawn from the canonical dataset"
            if o not in head_projects:
                why += f" ({_drop_reason(o, head_projects, dataset, base_projects)})"
        elif now["replacement_fit"] is None:
            why = "no longer scored"
        else:
            f = now["replacement_fit"]
            why = ", ".join(x for x, ok in (("not publishable", f.get("publish")),
                                            ("not a credible alternative", f.get("credible_alternative"))) if not ok) \
                or "no longer recommended"
        gone.append({"saas_id": s, "oss_id": o, "replacement_fit_before": _fit(before[(s, o)]),
                     "replacement_fit_now": _fit(now), "reason": why})
    base_by = {(r["saas_id"], r["oss_id"]): r for r in base_rels}
    scored = [{"saas_id": s, "oss_id": o, "replacement_fit": _fit(r), "recommended": r["recommended"]}
              for (s, o), r in sorted(head_by.items())
              if r["replacement_fit"] is not None and (base_by.get((s, o)) or {}).get("replacement_fit") is None
              and not r["recommended"]]
    moves = []
    for k in sorted(set(before) & set(after)):
        a, b = _fit(before[k]), _fit(after[k])
        if a is not None and b is not None and abs(b - a) >= FIT_MOVE_MIN:
            moves.append({"saas_id": k[0], "oss_id": k[1], "replacement_fit_before": a, "replacement_fit_now": b,
                          "change": round(b - a, 1)})
    moves.sort(key=lambda m: (-abs(m["change"]), m["saas_id"], m["oss_id"]))
    return {"newly_recommended": new, "no_longer_recommended": gone, "newly_scored": scored, "fit_moves": moves}


def project_changes(base_projects: dict, head_projects: dict, dataset: dict) -> dict:
    dropped = [{"oss_id": o, "name": base_projects[o]["name"],
                "reason": _drop_reason(o, head_projects, dataset, base_projects)}
               for o in sorted(set(base_projects) - set(head_projects))]
    health = [{"oss_id": o, "project_health": head_projects[o]["project_health"]["score"]}
              for o in sorted(set(head_projects) & set(base_projects)) if head_projects[o].get("project_health")
              and not base_projects[o].get("project_health")]  # joined projects are counted as joined
    joined = sorted(set(head_projects) - set(base_projects))
    return {"dropped_projects": dropped, "newly_health_scored": health, "joined_projects": joined}


def momentum_movers(rows: list, canonical_ids: set) -> dict:
    def pick(band):
        out = []
        for r in rows:
            w = (r.get("windows") or {}).get("7") or {}
            if w.get("status") == "measured" and w.get("momentum") == band and (r.get("stars") or 0) >= MOVER_MIN_STARS:
                out.append({"key": r["key"], "stars": r["stars"], "stars_delta_7d": w["stars_delta"],
                            "growth_pct_per_30d": w["growth_pct_per_30d"],
                            "acceleration": (w.get("acceleration") or {}).get("label"),
                            "canonical": (r.get("canonical_id") or r["key"]) in canonical_ids})
        return out

    surging = sorted(pick("surging"), key=lambda m: (-m["stars_delta_7d"], m["key"]))
    declining = sorted(pick("declining"), key=lambda m: (m["stars_delta_7d"], m["key"]))
    on = rows[0].get("on") if rows else None
    return {"on": on, "surging": surging, "declining": declining}


def intake_changes(w: dict) -> dict:
    prefix = "data/intake/outcomes"
    new_files = sorted(set(_ls(w["head"], prefix)) - set(_ls(w["base"], prefix)))
    accepted = []
    for path in new_files:
        o = lineage._json(w["head"], path, missing_ok=True) or {}
        if o.get("outcome") == "accepted":
            accepted.append({"request_id": o.get("request_id"), "oss_id": o.get("oss_id"),
                             "saas_hint": o.get("saas_hint"), "assessed_at": o.get("assessed_at")})
    runs = sorted(set(_ls(w["head"], "runs")) - set(_ls(w["base"], "runs")))
    added, failed, top = 0, [], {}
    for path in runs:
        if not path.endswith(".json"):
            continue
        r = lineage._json(w["head"], path, missing_ok=True) or {}
        if r.get("status") not in (None, "succeeded"):
            failed.append(Path(path).stem)
        corpus = r.get("corpus") or {}
        added += corpus.get("corpus_added") or 0
        for t in corpus.get("top_new_by_stars") or []:
            if t.get("licence_status") == "recognised" and t["id"] not in top:
                top[t["id"]] = {"oss_id": t["id"], "stars": t.get("stars"), "run": Path(path).stem}
    admissions = sorted(top.values(), key=lambda t: (-(t["stars"] or 0), t["oss_id"]))
    return {"intake_accepted": accepted, "cycles": len([p for p in runs if p.endswith(".json")]),
            "cycles_not_succeeded": failed, "corpus_added": added, "corpus_admissions": admissions}


def verdict_changes(w: dict) -> list:
    path = "data/verdicts/verdicts.jsonl"
    before = {common.dumps(r) for r in _jsonl(w["base"], path)}
    return [r for r in _jsonl(w["head"], path) if common.dumps(r) not in before]


# ---------------------------------------------------------------- the digest

def build(since_days: int = 7, to: str | None = None) -> dict:
    w = window(since_days, to)
    base_rels = _jsonl(w["base"], "data/canonical/relationships.jsonl")
    head_rels = _jsonl(w["head"], "data/canonical/relationships.jsonl")
    base_projects = {p["oss_id"]: p for p in _jsonl(w["base"], "data/canonical/oss_projects.jsonl")}
    head_projects = {p["oss_id"]: p for p in _jsonl(w["head"], "data/canonical/oss_projects.jsonl")}
    gone_ids = (set(base_projects) - set(head_projects)) | {r["oss_id"] for r in base_rels} - set(head_projects)
    dataset = {p["oss_id"]: p for p in _jsonl(w["head"], "data/dataset/oss_projects.jsonl")
               if p["oss_id"] in gone_ids} if gone_ids else {}
    m_before = lineage._json(w["base"], MANIFEST)
    m_after = lineage._json(w["head"], MANIFEST)
    full = {**recommendation_changes(base_rels, head_rels, head_projects, dataset, base_projects),
            **project_changes(base_projects, head_projects, dataset),
            "momentum": momentum_movers(_jsonl(w["head"], "data/momentum/projects.jsonl"), set(head_projects)),
            **intake_changes(w),
            "keith_verdicts_recorded": verdict_changes(w)}
    counts = {"newly_recommended": len(full["newly_recommended"]),
              "no_longer_recommended": len(full["no_longer_recommended"]),
              "newly_scored": len(full["newly_scored"]), "newly_health_scored": len(full["newly_health_scored"]),
              "dropped_projects": len(full["dropped_projects"]), "joined_projects": len(full["joined_projects"]),
              "fit_moves": len(full["fit_moves"]), "momentum_surging": len(full["momentum"]["surging"]),
              "momentum_declining": len(full["momentum"]["declining"]),
              "intake_accepted": len(full["intake_accepted"]), "corpus_added": full["corpus_added"],
              "cycles": full["cycles"], "cycles_not_succeeded": len(full["cycles_not_succeeded"]),
              "keith_verdicts_recorded": len(full["keith_verdicts_recorded"])}
    review_items = sum(counts[k] for k in ("newly_recommended", "no_longer_recommended", "newly_scored",
                                           "dropped_projects", "fit_moves", "momentum_surging", "momentum_declining",
                                           "intake_accepted", "cycles_not_succeeded", "keith_verdicts_recorded"))
    shown = dict(full)
    for k, n in LIMITS.items():
        if k.startswith("momentum_"):
            band = k.split("_", 1)[1]
            shown["momentum"] = {**shown["momentum"], band: full["momentum"][band][:n]}
        else:
            shown[k] = full[k][:n]
    return {
        "digest_schema": DIGEST_SCHEMA, "week": w["week"],
        "window": {k: w[k] for k in ("base", "head", "base_at", "head_at", "since_days", "note")},
        "dataset_version": {"before": m_before["dataset_version"], "after": m_after["dataset_version"]},
        "canonical_counts": {"before": m_before.get("counts"), "after": m_after.get("counts")},
        "counts": counts, "review_items": review_items, "limits": LIMITS,
        "thresholds": {"fit_move_min": FIT_MOVE_MIN, "mover_min_stars": MOVER_MIN_STARS},
        **{k: shown[k] for k in sorted(shown)},
    }


def _more(full_n: int, shown: list) -> str:
    return f"\n_{full_n} in all; the first {len(shown)} are listed (limits in the JSON twin)._\n" if full_n > len(shown) else ""


def render(d: dict) -> str:
    c, w = d["counts"], d["window"]
    cb, ca = d["canonical_counts"]["before"] or {}, d["canonical_counts"]["after"] or {}
    L = [f"# Review digest {d['week']}", "",
         f"What the daily cycles committed between `{w['base'][:12]}` ({w['base_at']}) and `{w['head'][:12]}` "
         f"({w['head_at']}), dataset `{d['dataset_version']['before']}` → `{d['dataset_version']['after']}`. "
         "Generated by `python -m source_pipeline review-digest` (docs/OPERATIONS.md, Human review digest). "
         "Everything below is machine-derived; nothing here is Keith's verdict unless it says so. "
         "Review, comment, and merge to keep the digest on record; the dataset itself is already live.", ""]
    if w["note"]:
        L += [f"> {w['note']}", ""]
    L += ["## Summary", "", "| | count |", "|---|---:|"]
    for label, k in (("Newly recommended", "newly_recommended"), ("No longer recommended", "no_longer_recommended"),
                     ("Newly scored, not recommended", "newly_scored"), ("Projects dropped out", "dropped_projects"),
                     ("Projects joined", "joined_projects"), (f"Replacement Fit moves ≥ {FIT_MOVE_MIN:g}", "fit_moves"),
                     ("Momentum: surging (7 d)", "momentum_surging"), ("Momentum: declining (7 d)", "momentum_declining"),
                     ("Intake acceptances", "intake_accepted"), ("Corpus admissions", "corpus_added"),
                     ("Cycles", "cycles"), ("Cycles not succeeded", "cycles_not_succeeded"),
                     ("Keith verdicts recorded", "keith_verdicts_recorded")):
        L.append(f"| {label} | {c[k]} |")
    L += ["", f"Canonical: {cb.get('recommended_relationships')} → {ca.get('recommended_relationships')} recommended "
          f"relationships, {cb.get('oss_projects')} → {ca.get('oss_projects')} projects, "
          f"{cb.get('catalogue_ok')} → {ca.get('catalogue_ok')} catalogue products ok.", ""]

    def section(title, rows, fmt, total):
        L.extend([f"## {title}", ""])
        if not rows:
            L.extend(["None.", ""])
            return
        L.extend(fmt(r) for r in rows)
        L.append(_more(total, rows))

    section("Newly recommended", d["newly_recommended"],
            lambda r: f"- `{r['oss_id']}` for **{r['saas_id']}**: Replacement Fit {r['replacement_fit']} "
                      f"(confidence {r['confidence']})" + (f", Keith: {r['keith_verdict']}" if r["keith_verdict"] else ""),
            c["newly_recommended"])
    section("No longer recommended", d["no_longer_recommended"],
            lambda r: f"- `{r['oss_id']}` for **{r['saas_id']}**: {r['reason']} "
                      f"(Fit {r['replacement_fit_before']} → {r['replacement_fit_now'] if r['replacement_fit_now'] is not None else 'n/a'})",
            c["no_longer_recommended"])
    section("Projects dropped out", d["dropped_projects"],
            lambda r: f"- `{r['oss_id']}` ({r['name']}): {r['reason']}", c["dropped_projects"])
    section("Newly scored, not recommended", d["newly_scored"],
            lambda r: f"- `{r['oss_id']}` for **{r['saas_id']}**: Replacement Fit {r['replacement_fit']}",
            c["newly_scored"])
    section(f"Replacement Fit moves on recommendations (≥ {FIT_MOVE_MIN:g} points)", d["fit_moves"],
            lambda r: f"- `{r['oss_id']}` for **{r['saas_id']}**: {r['replacement_fit_before']} → "
                      f"{r['replacement_fit_now']} ({r['change']:+g})", c["fit_moves"])
    m = d["momentum"]
    mover = (lambda r: f"- `{r['key']}`{' (canonical)' if r['canonical'] else ''}: {r['stars_delta_7d']:+d} stars in 7 d "
                       f"to {r['stars']}, {r['growth_pct_per_30d']:+g} %/30 d, {r['acceleration'] or 'acceleration n/a'}")
    section(f"Momentum: surging (7-day window, ≥ {MOVER_MIN_STARS} stars, as of {m['on']})", m["surging"], mover,
            c["momentum_surging"])
    section("Momentum: declining (7-day window)", m["declining"], mover, c["momentum_declining"])
    section("Intake acceptances", d["intake_accepted"],
            lambda r: f"- `{r['oss_id']}` (request {r['request_id']}, hint {r['saas_hint']}, {r['assessed_at']})",
            c["intake_accepted"])
    section(f"Corpus admissions: top new by stars ({c['corpus_added']} admitted over {c['cycles']} cycles)",
            d["corpus_admissions"], lambda r: f"- `{r['oss_id']}`: {r['stars']} stars ({r['run']})",
            len(d["corpus_admissions"]))
    if d["cycles_not_succeeded"]:
        section("Cycles that did not succeed", d["cycles_not_succeeded"], lambda r: f"- `{r}`",
                c["cycles_not_succeeded"])
    section("Keith verdicts recorded", d["keith_verdicts_recorded"],
            lambda r: f"- `{r['oss_id']}`{' for **' + r['saas_id'] + '**' if r['saas_id'] else ''}: {r['verdict']}"
                      f"{' (tested ' + r['tested_at'] + ')' if r['keith_tested'] else ''}",
            c["keith_verdicts_recorded"])
    return "\n".join(L).rstrip() + "\n"


def write(d: dict, out: Path | None = None) -> dict:
    out = Path(out) if out else REVIEWS
    md, js = out / f"digest-{d['week']}.md", out / f"digest-{d['week']}.json"
    out.mkdir(parents=True, exist_ok=True)
    md.write_text(render(d), encoding="utf-8")
    common.write_json(js, d)
    return {"markdown": str(md), "json": str(js)}


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="source_pipeline review-digest", description=__doc__.split("\n\n")[0])
    ap.add_argument("--since-days", type=int, default=7, help="window length in days (default 7)")
    ap.add_argument("--to", help="end revision (default HEAD); the window ends at its commit time")
    ap.add_argument("--out", help="output directory (default reviews/)")
    args = ap.parse_args(argv)
    if args.since_days < 1:
        ap.error("--since-days must be at least 1")
    try:
        d = build(args.since_days, args.to)
    except lineage.LineageError as exc:
        print(f"review-digest: {exc}", file=sys.stderr)
        return 1
    paths = write(d, args.out)
    print(json.dumps({"ok": True, "week": d["week"], "review_items": d["review_items"], "counts": d["counts"],
                      "window": d["window"], **paths}, indent=2, sort_keys=True))
    return 0
