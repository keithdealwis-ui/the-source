"""AC11/AC12 probe (KEI-806): rescore against REFRESHED metadata with the same code and schema.

In an isolated copy of the inputs (THE_SOURCE_ROOT pointed at a temp dir, so the real
data/score/ is untouched), it:

  1. re-observes a fixed sample of repositories on GitHub under a new as_of
     (`score-harvest --refresh`, the same code path a weekly refresh uses),
  2. rebuilds scores with `score-build`,
  3. checks every record still validates against schema/the-source.score.schema.json,
     the sample carries the new evaluation date, and the history now holds BOTH
     evaluations of each sampled project rather than only the latest.

Writes evidence/KEI-806-refresh-probe.json. Needs network (step 1). Run from the repo root:
    .venv/bin/python scripts/score_refresh_probe.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ["github.com/plausible/analytics", "github.com/grafana/grafana", "github.com/keycloak/keycloak",
          "github.com/authelia/authelia", "github.com/just-every/mcp-read-website-fast", "github.com/louislam/uptime-kuma",
          "github.com/documenso/documenso", "github.com/linkwarden/linkwarden"]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kei806-refresh-"))
    try:
        for d in ("config", "schema", "source_pipeline"):
            shutil.copytree(ROOT / d, tmp / d, ignore=shutil.ignore_patterns("__pycache__"))
        for f in ("data/graph/views/scoring_inputs.jsonl", "data/graph/edges.jsonl", "data/dataset/saas_products.jsonl",
                  "data/dataset/oss_projects.jsonl"):
            (tmp / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / f, tmp / f)
        (tmp / "data/discover").mkdir(parents=True, exist_ok=True)
        os.symlink(ROOT / "data/discover/live.json", tmp / "data/discover/live.json")
        shutil.copytree(ROOT / "data/score/evidence", tmp / "data/score/evidence")
        shutil.copytree(ROOT / "data/score/history", tmp / "data/score/history")
        before = json.loads((tmp / "data/score/evidence/github_meta.json").read_text())["as_of"]
        env = {**os.environ, "THE_SOURCE_ROOT": str(tmp), "PYTHONPATH": str(tmp)}
        py = str(ROOT / ".venv/bin/python")
        code = ("import json,sys; from source_pipeline import score_harvest as h; "
                f"print(json.dumps(h.run(refresh=True, only={SAMPLE!r})))")
        harvest = json.loads(subprocess.run([py, "-c", code], env=env, cwd=tmp, check=True,
                                            capture_output=True, text=True).stdout)
        build = subprocess.run([py, "-c", "import json; from source_pipeline import score_build as b; "
                                          "print(json.dumps(b.run()['counts']))"],
                               env={**env, "THE_SOURCE_NO_NETWORK": "1"}, cwd=tmp, check=True, capture_output=True, text=True)
        counts = json.loads(build.stdout)
        sys.path.insert(0, str(ROOT))
        import jsonschema

        schema = json.loads((ROOT / "schema/the-source.score.schema.json").read_text())
        v = jsonschema.Draft7Validator(schema)
        recs = [json.loads(l) for l in open(tmp / "data/score/records.jsonl")]
        errs = [e.message for r in recs for e in v.iter_errors(r)]
        after = harvest["as_of"]
        sampled = {r["canonical"]: r for r in recs if r["canonical"] in SAMPLE}
        hist = [json.loads(l) for l in open(tmp / "data/score/history/scores.jsonl")]
        evals = {}
        for h in hist:
            evals.setdefault(h["project"], set()).add(h["evidence_as_of"])
        pid = {r["canonical"]: r["project"] for r in recs}
        both = {c: sorted(evals.get(pid[c], [])) for c in SAMPLE}
        obs = [json.loads(l) for l in open(tmp / "data/score/history/observations.jsonl")]
        obs_both = {c: sorted({o["observed_at"] for o in obs if o["project"] == c}) for c in SAMPLE}
        code_same = all(
            (ROOT / "source_pipeline" / p.name).read_bytes() == p.read_bytes()
            for p in (tmp / "source_pipeline").glob("*.py"))
        ok = (after != before and not errs and all(r["evaluated_at"] == after for r in sampled.values())
              and all(len(x) >= 2 for x in both.values()) and all(len(x) >= 2 for x in obs_both.values()) and code_same)
        out = {"ok": ok, "previous_as_of": before, "refreshed_as_of": after, "sample": SAMPLE,
               "harvest": harvest, "build_counts": counts, "schema_errors": errs[:5], "records_validated": len(recs),
               "code_and_schema_unchanged": code_same,
               "sample_after_refresh": {c: {"health": r["project_health"]["score"], "evaluated_at": r["evaluated_at"],
                                            "trajectory": r["trajectory"],
                                            "fits": {f["saas"]: f["score"] for f in r["replacement_fit"]}}
                                        for c, r in sampled.items()},
               "score_history_evaluations": both, "observation_history": obs_both}
        (ROOT / "evidence").mkdir(exist_ok=True)
        (ROOT / "evidence/KEI-806-refresh-probe.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
        print(json.dumps({"ok": ok, "refreshed_as_of": after, "schema_errors": len(errs)}, indent=2))
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
