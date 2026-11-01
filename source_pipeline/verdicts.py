"""Keith's personal verdicts: the human layer beside the machine scores (KEI-783 AC4).

    data/verdicts/verdicts.jsonl          one row per verdict Keith recorded; append-only
    schema/the-source.verdict.schema.json the row format

    python -m source_pipeline verdict-add --oss-id ID --verdict recommended|conditional|not_recommended
                              [--saas-id ID] [--tested [--tested-at YYYY-MM-DD]] [--notes TEXT] [--episode-url URL]

Only Keith runs verdict-add. No pipeline stage, cycle, workflow or agent writes this file: every
row is recorded_by "keith". Replacement Fit, Project Health and every other number The Source
publishes are computed automatically; a verdict is what Keith concluded after looking at (and,
when keith_tested is true, running) the project himself.

The two never mix. The canonical build reads the verdicts and shows the one that applies as
`keith_verdict` beside the machine scores, and derives `endorsement` from it alone; a verdict never
changes a score, a rank, a recommendation or a catalogue status. With no row, `keith_verdict` is
null and every endorsement flag is false.

Which verdict applies: a verdict naming a SaaS product applies to that relationship (and its
api/v1 recommendation row); a verdict without one applies to the project record and to every
relationship of the project that has no verdict of its own. For the same (project, product) the
newest recorded_at wins, so a changed mind is a new row, never an edit.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import common

VERDICTS = common.DATA / "verdicts" / "verdicts.jsonl"
SCHEMA_FILE = common.SCHEMA / "the-source.verdict.schema.json"
VERDICT_SCHEMA = "the-source.verdict/1"
VERDICTS_ENUM = ("recommended", "conditional", "not_recommended")
# The fields the read surface shows. recorded_by is always "keith"; it stays so a reader never has to infer it.
PUBLIC_FIELDS = ("saas_id", "verdict", "keith_tested", "tested_at", "notes", "episode_url", "recorded_by",
                 "recorded_at")
NO_ENDORSEMENT = {"keith_tested": False, "keith_endorsed": False, "editorial_recommendation": False}
# score_build: the statement a scored record carries when Keith has recorded a verdict on the project.
STATEMENT_WITH_VERDICT = ("Scores are automatically computed from public repository metadata. Keith de Alwis has "
                          "recorded his own verdict on this project separately (data/verdicts/); it does not change "
                          "any score, and no score is an editorial recommendation.")


def load(path: Path | None = None) -> list:
    """Every recorded verdict, in file order. A missing or empty file is no verdicts."""
    path = Path(path) if path else VERDICTS
    return common.read_jsonl(path) if path.is_file() else []


def index(rows: list) -> dict:
    """(oss_id, saas_id or None) -> the newest verdict. Later rows win ties, so the file is the order of record."""
    out = {}
    for r in rows:
        key = (r["oss_id"], r.get("saas_id"))
        if key not in out or out[key]["recorded_at"] <= r["recorded_at"]:
            out[key] = r
    return out


def public(row: dict | None) -> dict | None:
    return None if row is None else {k: row.get(k) for k in PUBLIC_FIELDS}


def for_project(idx: dict, oss_id: str) -> dict | None:
    """The verdict on the project itself (no SaaS product named), as the read surface shows it."""
    return public(idx.get((oss_id, None)))


def for_pair(idx: dict, saas_id: str, oss_id: str) -> dict | None:
    """The verdict on this relationship, else the project's own verdict, else None."""
    return public(idx.get((oss_id, saas_id)) or idx.get((oss_id, None)))


def endorsement(verdict: dict | None) -> dict:
    """Endorsement is derived from Keith's verdict and nothing else. editorial_recommendation stays false: The
    Source has no editorial layer, and a verdict is Keith's own, labelled as his."""
    if not verdict:
        return dict(NO_ENDORSEMENT)
    return {"keith_tested": bool(verdict["keith_tested"]), "keith_endorsed": verdict["verdict"] == "recommended",
            "editorial_recommendation": False}


def score_endorsement(base: dict, verdict: dict | None) -> dict:
    """A scored record's endorsement block (score_build): the configured all-false block and its statement without a
    verdict; with one, the flags derived from it and a statement that says the verdict is separate from the scores."""
    if not verdict:
        return base
    return {**base, **endorsement(verdict), "statement": STATEMENT_WITH_VERDICT}


# ---------------------------------------------------------------- checks

def _validator():
    import jsonschema

    schema = common.read_json(SCHEMA_FILE)
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def schema_errors(rows: list, limit: int = 5) -> list:
    v = _validator()
    out = []
    for i, r in enumerate(rows, 1):
        for err in v.iter_errors(r):
            out.append(f"verdicts.jsonl:{i}: {'/'.join(str(p) for p in err.absolute_path)}: {err.message[:160]}")
            if len(out) >= limit:
                return out
    return out


def known_projects(root: Path | None = None) -> set:
    """Projects a verdict may name: the canonical projects and the public discovery corpus."""
    root = Path(root) if root else common.ROOT
    out = set()
    for path, key in ((root / "data" / "canonical" / "oss_projects.jsonl", "oss_id"),
                      (root / "data" / "corpus" / "projects.jsonl", "id")):
        if path.is_file():
            out |= {r[key] for r in common.read_jsonl(path)}
    return out


def known_saas(root: Path | None = None) -> set:
    root = Path(root) if root else common.ROOT
    path = root / "data" / "dataset" / "saas_products.jsonl"
    return {r["saas_id"] for r in common.read_jsonl(path)} if path.is_file() else set()


def unknown_references(rows: list, oss_ids: set, saas_ids: set) -> list:
    bad = [f"{r['oss_id']}: unknown project" for r in rows if r["oss_id"] not in oss_ids]
    bad += [f"{r['oss_id']}: unknown SaaS product {r['saas_id']!r}" for r in rows
            if r.get("saas_id") is not None and r["saas_id"] not in saas_ids]
    return bad


def surface_errors(rows: list, projects: list, rels: list, docs: dict) -> list:
    """Every keith_verdict and endorsement on the canonical rows and the read surface is exactly what the verdict
    layer says: a row with no verdict claims no endorsement, and no verdict is dropped or invented. A row written
    before KEI-783 carries neither field, which is read as null and all-false."""
    idx = index(rows)
    bad = []

    def one(where, row, want):
        got = row.get("keith_verdict")
        if got != want:
            bad.append(f"{where}: keith_verdict {got!r} but the verdict layer says {want!r}")
        if row.get("endorsement", NO_ENDORSEMENT) != endorsement(want):
            bad.append(f"{where}: endorsement {row.get('endorsement')} not derived from its verdict")

    for p in projects:
        one(f"project {p['oss_id']}", p, for_project(idx, p["oss_id"]))
    for r in rels:
        one(f"relationship {r['saas_id']}:{r['oss_id']}", r, for_pair(idx, r["saas_id"], r["oss_id"]))
    for sid, d in docs.items():
        for rec in d["recommendations"]:
            one(f"api/v1/saas/{sid}.json {rec['oss_id']}", rec, for_pair(idx, sid, rec["oss_id"]))
    return bad


def score_errors(rels: list, projects: list, docs: dict, fit: dict, health: dict) -> list:
    """Verdicts never alter machine scores: every Replacement Fit and Project Health in the canonical rows and the
    read surface equals the scoring lane's own output (data/scores/), and `recommended` is the machine rule alone."""
    bad = []
    for r in rels:
        f = fit.get((r["saas_id"], r["oss_id"]))
        want = None if f is None else {k: f[k] for k in (r["replacement_fit"] or {})}
        if r["replacement_fit"] != want or (f is None and r["replacement_fit"] is not None):
            bad.append(f"relationship {r['saas_id']}:{r['oss_id']}: Replacement Fit differs from data/scores/fit.jsonl")
        if r["recommended"] != bool(f and f["publish"] and f["credible_alternative"]):
            bad.append(f"relationship {r['saas_id']}:{r['oss_id']}: recommended is not the machine rule")
    for p in projects:
        h = health.get(p["oss_id"])
        want = None if h is None else {k: h[k] for k in (p["project_health"] or {})}
        if p["project_health"] != want or (h is None and p["project_health"] is not None):
            bad.append(f"project {p['oss_id']}: Project Health differs from data/scores/health.jsonl")
    for sid, d in docs.items():
        for rec in d["recommendations"]:
            f = fit.get((sid, rec["oss_id"])) or {}
            if any(rec["replacement_fit"].get(k) != f.get(k) for k in rec["replacement_fit"]):
                bad.append(f"api/v1/saas/{sid}.json {rec['oss_id']}: Replacement Fit differs from the scoring lane")
    return bad


# ---------------------------------------------------------------- record (Keith only)

def make_row(oss_id: str, verdict: str, *, saas_id: str | None = None, keith_tested: bool = False,
             tested_at: str | None = None, notes: str | None = None, episode_url: str | None = None,
             now: str | None = None) -> dict:
    now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if keith_tested and not tested_at:
        tested_at = now[:10]
    return {"verdict_schema": VERDICT_SCHEMA, "oss_id": oss_id.strip().lower(),
            "saas_id": saas_id.strip().lower() if saas_id else None, "verdict": verdict,
            "keith_tested": bool(keith_tested), "tested_at": tested_at if keith_tested else None,
            "notes": notes or None, "episode_url": episode_url or None, "recorded_by": "keith", "recorded_at": now}


def add(row: dict, path: Path | None = None, root: Path | None = None) -> dict:
    """Validate one row and append it. Refuses (ValueError) a malformed row or one naming an unknown project."""
    path = Path(path) if path else VERDICTS
    errs = schema_errors([row]) + unknown_references([row], known_projects(root), known_saas(root))
    if errs:
        raise ValueError("; ".join(errs))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(common.dumps(row) + "\n")
    return row


def main(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="source_pipeline verdict-add",
                                 description="Record Keith's own verdict on a project. Only Keith runs this.")
    ap.add_argument("--oss-id", required=True, help="the project, e.g. github.com/owner/repo")
    ap.add_argument("--verdict", required=True, choices=VERDICTS_ENUM)
    ap.add_argument("--saas-id", help="the SaaS product the verdict is about (default: the project in general)")
    ap.add_argument("--tested", action="store_true", help="Keith ran the project himself")
    ap.add_argument("--tested-at", help="when he tested it, YYYY-MM-DD (default with --tested: today, UTC)")
    ap.add_argument("--notes", help="what Keith found")
    ap.add_argument("--episode-url", help="the episode or write-up where he covered it")
    args = ap.parse_args(argv)
    if args.tested_at and not args.tested:
        ap.error("--tested-at needs --tested")
    row = make_row(args.oss_id, args.verdict, saas_id=args.saas_id, keith_tested=args.tested,
                   tested_at=args.tested_at, notes=args.notes, episode_url=args.episode_url)
    try:
        add(row)
    except ValueError as exc:
        print(f"verdict-add: refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "recorded": row, "path": str(VERDICTS.relative_to(common.ROOT)),
                      "next": "python -m source_pipeline canonical-build"}, indent=2, sort_keys=True))
    return 0
