"""The Source pipeline.

    python -m source_pipeline run             harvest -> verify live -> build -> validate
    python -m source_pipeline run --offline   rebuild from pinned checkouts and the stored
                                              live snapshot; no network; byte-identical
    python -m source_pipeline harvest|verify|build|validate|export-public

  Discover lane (KEI-811):
    python -m source_pipeline discover-harvest     run the discovery strategies (network)
    python -m source_pipeline discover-verify      verify every candidate (network)
                              [--only-failed]      re-ask only what failed last time
    python -m source_pipeline discover-build       build the corpus from the records; no network
    python -m source_pipeline discover-validate

  Scoring (KEI-806):
    python -m source_pipeline score-harvest        observe the scoring evidence on GitHub (network)
                              [--refresh]          re-observe everything under a new as_of
    python -m source_pipeline score-build          score every project from the stored evidence; no network
    python -m source_pipeline score-validate       run the calibration set and the acceptance checks

  Canonical repository (KEI-807):
    python -m source_pipeline canonical-build      data/canonical/ + api/v1/ from the dataset; no network
    python -m source_pipeline canonical-validate   schema, integrity, read-surface and determinism checks
    python -m source_pipeline scores-extract       compact the KEI-806 lane's scores into data/scores/
    python -m source_pipeline research             advance upstream pins where the licence is unchanged
    python -m source_pipeline intake-assess --url URL --request-id ID [--saas-hint ID] [--submitted-by WHO]
    python -m source_pipeline intake-status --request-id ID
    python -m source_pipeline cycle --trigger schedule|manual   the daily intelligence cycle (KEI-848)
    python -m source_pipeline cycle-due            is a scheduled cycle due now, and why
    python -m source_pipeline runs-latest [--runs-dir DIR]   the newest runs/*.json cycle summary

  Daily intelligence (KEI-848):
    python -m source_pipeline history-days         the dated daily snapshots held (data/history/)
    python -m source_pipeline history-delta --days N [--on YYYY-MM-DD] [--key KEY]
                                                   change over N days (1, 7, 30, 90...) from the snapshots
    python -m source_pipeline discovery-pool       the reconciled candidate pool (data/discovery/)

  Momentum and acceleration (KEI-849, docs/MOMENTUM.md):
    python -m source_pipeline momentum-build [--on YYYY-MM-DD]   data/momentum/ from data/history/; no network
    python -m source_pipeline momentum-validate    rebuild-equals-committed, schema, released methodology
    python -m source_pipeline momentum [--window 1|7|30|90] [--key KEY]... [--sort growth|stars_per_day|acceleration]
                              [--momentum surging|rising|flat|declining] [--acceleration accelerating|steady|decelerating]
                              [--limit N]          read the committed momentum, with the window's coverage

  Community Radar (KEI-850, docs/RADAR.md):
    python -m source_pipeline radar-collect        ask Hacker News and Forem, resolve, rank (network)
    python -m source_pipeline radar-build [--as-of TS]   data/radar/radar.jsonl from what is held; no network
    python -m source_pipeline radar-validate       rebuild-equals-committed, schema, released methodology
    python -m source_pipeline radar [--key KEY]... [--source hn|forem]
                              [--status canonical|corpus|candidate|not_found|unresolved] [--limit N]
                                                   read the committed radar, with what each source answered

  Knowledge graph (KEI-844):
    python -m source_pipeline graph-build          build data/graph/ from the corpus; no network
    python -m source_pipeline graph-validate       run the query suite and the acceptance checks
    python -m source_pipeline graph-query <plan>   run one plan (YAML/JSON file, or a query id from
                                                   config/graph_queries.yaml) and print the evidence

  MCP retrieval server (KEI-851, docs/MCP.md):
    python -m source_pipeline mcp-serve [--root DIR] [--now TS] [--check]
                                                   serve the canonical read layer over MCP (stdio)
    python -m source_pipeline mcp-http [--host H] [--port P] [--telemetry FILE]
                                                   the same server over Streamable HTTP (POST /mcp)
"""
from __future__ import annotations

import argparse
import json
import sys

from . import build, common, enrich, export, harvest, normalise, validate


def _verify():
    claims = common.read_jsonl(common.STAGING / "claims.jsonl")
    rec = normalise.reconcile(claims, common.load_sources())
    keys = sorted(rec["oss"])
    print(f"verifying {len(keys)} repositories against their hosts", file=sys.stderr)
    snap = enrich.run(keys, progress=lambda i, n: print(f"  {i}/{n}", file=sys.stderr))
    found = sum(1 for p in snap["projects"].values() if p.get("found"))
    return {"as_of": snap["as_of"], "checked": len(keys), "found": found}


def _discover(args) -> int:
    from . import discover

    out = {}
    if args.command == "discover-harvest":
        out["harvest"] = discover.harvest()
    elif args.command == "discover-verify":
        out["verify"] = discover.verify(only_failed=args.only_failed)
    if args.command in ("discover-build", "discover-validate"):
        from . import discover_build, discover_validate
    if args.command == "discover-build":
        out["summary"] = discover_build.run()["counts"]
    if args.command in ("discover-build", "discover-validate"):
        report = discover_validate.run()
        out["validation"] = {"ok": report["ok"], "checks": len(report["checks"]),
                             "failed": [c["id"] for c in report["checks"] if not c["ok"]]}
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


def _graph(args) -> int:
    from . import graph_build, graph_queries, graph_validate

    if args.command == "graph-query":
        from pathlib import Path

        from .graph_retrieval import Executor, Graph, RetrievalIndex
        if args.target and Path(args.target).exists():
            plan = common.load_yaml(Path(args.target))
            plan = plan.get("plan", plan)
        else:
            plan = next(q for q in common.load_yaml(graph_queries.QUERIES)["queries"] if q["id"] == args.target)["plan"]
        res = Executor(Graph.load(), RetrievalIndex.load(graph_build.GRAPH / "retrieval")).run(plan)
        print(json.dumps(res, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    out = {}
    if args.command == "graph-build":
        out["summary"] = graph_build.run()["counts"]
    q = graph_queries.run()
    report = graph_validate.run(q)
    out["queries"] = {"count": q["queries"], "ok": q["ok"], "multi_hop": q["multi_hop"]}
    out["validation"] = {"ok": report["ok"], "checks": len(report["checks"]),
                         "failed": [c["id"] for c in report["checks"] if not c["ok"]]}
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def _score(args) -> int:
    from . import score_harvest

    out = {}
    if args.command == "score-harvest":
        out["harvest"] = score_harvest.run(refresh=args.refresh)
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    from . import score_build, score_validate
    if args.command == "score-build":
        out["summary"] = score_build.run()["counts"]
    report = score_validate.run()
    out["validation"] = {"ok": report["ok"], "checks": len(report["checks"]),
                         "failed": [c["id"] for c in report["checks"] if not c["ok"]]}
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


def _canonical(args) -> int:
    from . import canonical, canonical_validate, cycle, intake, research

    c = args.command
    if c == "canonical-build":
        m = canonical.build()
        print(json.dumps({k: m[k] for k in ("dataset_version", "counts", "live_checked_at")}, indent=2, sort_keys=True))
        c = "canonical-validate"
    if c == "canonical-validate":
        r = canonical_validate.run()
        print(json.dumps({"ok": r["ok"], "dataset_version": r["dataset_version"], "warnings": r["warnings"],
                          "checks": len(r["checks"]),
                          "failed": [{"id": x["id"], "detail": x["detail"]} for x in r["checks"] if not x["ok"]]},
                         indent=2, sort_keys=True))
        return 0 if r["ok"] else 1
    if c == "scores-extract":
        print(json.dumps(canonical.extract_scores(), indent=2, sort_keys=True))
        return 0
    if c == "research":
        print(json.dumps(research.advance_pins(), indent=2, sort_keys=True))
        return 0
    if c == "intake-assess":
        if not args.url or not args.request_id:
            raise SystemExit("intake-assess needs --url and --request-id")
        o = intake.assess(args.url, args.request_id, submitted_by=args.submitted_by, saas_hint=args.saas_hint,
                          note=args.note)
        print(json.dumps(o, indent=2, sort_keys=True, ensure_ascii=False))
        return 0 if o["outcome"] != "failed" else 3
    if c == "intake-status":
        print(json.dumps(intake.status(args.request_id), indent=2, sort_keys=True))
        return 0
    if c == "cycle-due":
        print(json.dumps(cycle.due(), indent=2, sort_keys=True))
        return 0
    if c == "cycle":
        r = cycle.run(args.trigger)
        return 0 if r.get("skipped") or r.get("status") == "succeeded" else 1
    if c == "cycle-work":
        from pathlib import Path
        return cycle.work(args.run_id, Path(args.result))
    raise SystemExit(f"unknown command {c}")

def _radar(args, ap) -> int:
    from datetime import datetime, timezone

    from . import radar

    if args.command == "radar-collect":
        # the cycle's own step, run against the live copy: what an operator runs to see it work
        from zoneinfo import ZoneInfo

        from .cycle import load_schedule

        now = datetime.now(timezone.utc).replace(microsecond=0)
        day = now.astimezone(ZoneInfo(load_schedule()["timezone"])).date().isoformat()
        light = (common.read_json(common.LIVE / "snapshot.json")["projects"]
                 if (common.LIVE / "snapshot.json").exists() else {})
        r = radar.guarded_cycle_step(now, day, light)
        out = {"ok": r["ok"], "failed_checks": r["failed_checks"], **r["strategy"]}
    elif args.command == "radar-build":
        try:
            m = radar.build(args.as_of)
        except ValueError as exc:
            ap.error(str(exc))
        out = {"ok": True, **{k: m[k] for k in ("as_of", "methodology", "counts", "files")}}
    elif args.command == "radar-validate":
        out = radar.validate()
    else:
        try:
            out = radar.retrieve(args.key, args.source, args.status, args.limit)
        except (ValueError, FileNotFoundError) as exc:
            ap.error(str(exc))
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if out.get("ok", True) else 1


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["mcp-serve"]:
        from . import mcp_server

        return mcp_server.main(argv[1:])
    if argv[:1] == ["mcp-http"]:
        from . import mcp_http

        return mcp_http.main(argv[1:])
    ap = argparse.ArgumentParser(prog="source_pipeline", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "harvest", "verify", "build", "validate", "export-public",
                                        "discover-harvest", "discover-verify", "discover-build",
                                        "discover-validate", "graph-build", "graph-validate", "graph-query",
                                        "score-harvest", "score-build", "score-validate",
                                        "canonical-build", "canonical-validate", "scores-extract", "research",
                                        "intake-assess", "intake-status", "cycle", "cycle-due", "cycle-work",
                                        "runs-latest",
                                        "history-days", "history-delta", "discovery-pool",
                                        "momentum-build", "momentum-validate", "momentum",
                                        "radar-collect", "radar-build", "radar-validate", "radar"])
    ap.add_argument("target", nargs="?", help="graph-query: a plan file or a query id")
    ap.add_argument("--only-failed", action="store_true", help="discover-verify: retry only failed lookups")
    ap.add_argument("--refresh", action="store_true", help="score-harvest: re-observe everything")
    ap.add_argument("--offline", action="store_true", help="no network: reuse pinned checkouts and live snapshot")
    ap.add_argument("--include-share-alike", action="store_true",
                    help="export-public: include CC BY-SA-only material (export is then CC BY-SA)")
    ap.add_argument("--out", help="export-public: output directory")
    ap.add_argument("--url", help="intake-assess: repository URL")
    ap.add_argument("--request-id", help="intake-*: the submission's request id")
    ap.add_argument("--saas-hint", help="intake-assess: SaaS id the submitter believes it replaces (advisory)")
    ap.add_argument("--submitted-by", help="intake-assess: who or what submitted it")
    ap.add_argument("--note", help="intake-assess: free-text note from the submitter")
    ap.add_argument("--trigger", default="manual", choices=["schedule", "manual", "local"], help="cycle: what started it")
    ap.add_argument("--days", type=int, help="history-delta: window in days")
    ap.add_argument("--on", help="history-delta: end day (default: newest held)")
    ap.add_argument("--key", action="append", help="history-delta, momentum, radar: restrict to these project keys")
    ap.add_argument("--window", type=int, default=7, help="momentum: window in days")
    ap.add_argument("--sort", choices=["growth", "stars_per_day", "acceleration"], help="momentum: rank by")
    ap.add_argument("--momentum", dest="momentum_band", help="momentum: only this momentum band")
    ap.add_argument("--acceleration", help="momentum: only this acceleration label")
    ap.add_argument("--limit", type=int, help="momentum, radar: at most this many entries")
    ap.add_argument("--as-of", help="radar-build: rank as of this UTC timestamp (default: the last collection's)")
    ap.add_argument("--source", help="radar: only entries mentioned on this source")
    ap.add_argument("--status", help="radar: only entries with this reconciliation status")
    ap.add_argument("--runs-dir", help="runs-latest: read cycle records from here (default: runs/)")
    ap.add_argument("--run-id", help="cycle-work: internal")
    ap.add_argument("--result", help="cycle-work: internal")
    args = ap.parse_args(argv)

    out = {}
    if args.command.startswith("radar"):
        return _radar(args, ap)
    if args.command.startswith("momentum"):
        from . import momentum

        if args.command == "momentum-build":
            out = momentum.build(args.on)
            out = {"ok": True, **{k: out[k] for k in ("methodology", "on", "counts", "files")},
                   "coverage": momentum.summary(out)}
        elif args.command == "momentum-validate":
            out = momentum.validate()
        else:
            try:
                out = momentum.retrieve(args.key, args.window, args.sort, args.momentum_band, args.acceleration,
                                        args.limit)
            except ValueError as exc:
                ap.error(str(exc))
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if out.get("ok", True) else 1
    if args.command.startswith(("history-", "discovery-")):
        from . import daily

        if args.command == "history-days":
            out = {"days": daily.history_days()}
        elif args.command == "discovery-pool":
            pool = daily.load_pool()
            out = {"size": len(pool), "candidates": list(pool.values())}
        else:
            if not args.days:
                ap.error("history-delta needs --days")
            out = daily.delta(args.days, args.on, args.key)
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if out.get("ok", True) else 1
    if args.command == "runs-latest":
        from . import cycle

        out = cycle.latest(args.runs_dir)
        if out is None:
            print(f"runs-latest: no cycle summaries (*.json) in {args.runs_dir or cycle.RUNS}", file=sys.stderr)
            return 1
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0
    if args.command.startswith(("canonical-", "intake-", "cycle")) or args.command in ("scores-extract", "research"):
        return _canonical(args)
    if args.command.startswith("score-"):
        return _score(args)
    if args.command.startswith("graph-"):
        return _graph(args)
    if args.command.startswith("discover-"):
        return _discover(args)
    if args.command in ("run", "harvest"):
        out["harvest"] = harvest.run(offline=args.offline)
    if args.command == "verify" or (args.command == "run" and not args.offline):
        out["verify"] = _verify()
    if args.command in ("run", "build"):
        out["summary"] = build.run()
    if args.command in ("run", "build", "validate"):
        report = validate.run()
        out["validation"] = {"ok": report["ok"], "checks": len(report["checks"]),
                             "failed": [c["id"] for c in report["checks"] if not c["ok"]]}
        print(json.dumps(out, indent=2, sort_keys=True))
        return 0 if report["ok"] else 1
    if args.command == "export-public":
        out["export"] = export.run(args.out, include_share_alike=args.include_share_alike)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
