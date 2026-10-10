"""Latency and memory of the MCP engine at full corpus size (KEI-912 acceptance criterion 7).

    python3 -S scripts/corpus_bench.py [--root DIR] [--rounds N] [--url https://host/mcp]

In-process (default): loads the layer the way the service does, records peak RSS after the load and after a
forced reload (the moment two layers coexist), then times every tool over a fixed call set and reports p50/p95
per tool and overall. With --url it times the same calls against a live Streamable HTTP endpoint instead
(stdlib urllib; obeys the endpoint's rate limit by pacing). Standard library only.
"""
from __future__ import annotations

import argparse
import json
import resource
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CALLS = [
    ("source_search", {"query": "MCP server for web scraping", "mode": "hybrid"}),
    ("source_search", {"query": "self-hosted analytics", "mode": "hybrid"}),
    ("source_search", {"query": "vector database"}),
    ("source_search", {"query": "terminal file manager", "tier": "discovery"}),
    ("source_get_project", {"project": "github.com/firecrawl/firecrawl"}),
    ("source_get_project", {"project": "Docmost"}),
    ("source_compare_projects", {"projects": ["github.com/umami-software/umami", "github.com/plausible/analytics"]}),
    ("source_get_alternatives", {"saas": "Notion"}),
    ("source_get_alternatives", {"saas": "Google Analytics", "include_discovery": True}),
    ("source_recommend", {"need": "team wiki and notes like Notion"}),
    ("source_recommend", {"need": "password manager", "include_discovery": True}),
    ("source_emerging_projects", {"window": "7"}),
    ("source_emerging_projects", {"window": "7", "query": "ai agents"}),
    ("source_get_momentum", {"project": "github.com/firecrawl/firecrawl"}),
    ("source_dataset_info", {}),
]


def rss_mb():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(r / (1024 * 1024) if sys.platform == "darwin" else r / 1024, 1)


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] * 1000, 1)


def http_call(url, sid, name, args, mid):
    body = json.dumps({"jsonrpc": "2.0", "id": mid, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2025-06-18"}
    if sid:
        headers["Mcp-Session-Id"] = sid
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        doc = json.loads(r.read().decode())
    if doc.get("error") or doc["result"].get("isError"):
        raise RuntimeError(f"{name}: {doc}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(ROOT))
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--url")
    ap.add_argument("--pace", type=float, default=1.1, help="--url: seconds between calls (rate limit)")
    a = ap.parse_args(argv)
    times = {}
    out = {"calls_per_round": len(CALLS), "rounds": a.rounds}
    if a.url:
        init = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "corpus-bench", "version": "1"}}})
        req = urllib.request.Request(a.url, data=init.encode(), headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=60) as r:
            sid = r.headers.get("Mcp-Session-Id")
        mid = 1
        for _ in range(a.rounds):
            for name, args in CALLS:
                t = time.perf_counter()
                http_call(a.url, sid, name, args, mid)
                times.setdefault(name, []).append(time.perf_counter() - t)
                mid += 1
                time.sleep(a.pace)
        out["target"] = a.url
    else:
        from source_pipeline import mcp_server as M

        t = time.perf_counter()
        server = M.Server(Path(a.root), datetime.now(timezone.utc))
        out["load_seconds"] = round(time.perf_counter() - t, 2)
        out["rss_after_load_mb"] = rss_mb()
        old = server.layer
        t = time.perf_counter()
        server.layer = M.Layer(Path(a.root))  # a reload: the old layer is still referenced, as during a switch
        out["reload_seconds"] = round(time.perf_counter() - t, 2)
        out["peak_rss_during_reload_mb"] = rss_mb()
        del old
        out["corpus"] = server.layer.corpus_state["status"]
        out["tiers"] = server.layer.tier_counts
        for _ in range(a.rounds):
            for name, args in CALLS:
                fn = M.TOOL_BY_NAME[name][4]
                t = time.perf_counter()
                fn(server.layer, dict(args), server)
                times.setdefault(name, []).append(time.perf_counter() - t)
        out["target"] = "in-process"
    alls = [x for v in times.values() for x in v]
    out["per_tool_ms"] = {k: {"p50": pct(v, 50), "p95": pct(v, 95), "max": round(max(v) * 1000, 1)}
                          for k, v in sorted(times.items())}
    out["overall_ms"] = {"p50": pct(alls, 50), "p95": pct(alls, 95), "max": round(max(alls) * 1000, 1)}
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
