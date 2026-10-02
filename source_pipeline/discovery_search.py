"""Daily discovery lanes (KEI-848): find candidate repositories, nothing more.

Each lane in config/discovery.yaml asks one provider one question. Results are returned as
candidates for daily.reconcile_candidates; nothing here decides that a candidate is new,
and nothing here touches the canonical dataset. A lane that fails is recorded and the
cycle continues (discovery is additive: missing a day's candidates loses nothing, the
look-back window overlaps), so a provider outage degrades discovery, never canonical truth.
"""
from __future__ import annotations

import urllib.parse
from datetime import datetime, timedelta

from . import common

CONFIG = "discovery.yaml"


def load_cfg() -> dict:
    return common.load_yaml(common.CONFIG / CONFIG)


def _github_search(http, query: str, lane: str, pages: int = 3) -> tuple[list[dict], str | None]:
    out = []
    for page in range(1, pages + 1):
        url = ("https://api.github.com/search/repositories?" +
               urllib.parse.urlencode({"q": query, "sort": "stars", "order": "desc", "per_page": 100, "page": page}))
        st, doc = http.call(url, auth=True)
        if st != 200 or not isinstance(doc, dict):
            return out, f"{lane}: github search http {st}"
        for r in doc.get("items") or []:
            out.append({
                "key": "github.com/" + r["full_name"].lower(), "provider": "github_search", "query": lane,
                "host_repo_id": r.get("id"), "canonical_name": r.get("full_name"),
                "stars": r.get("stargazers_count"), "created_at": r.get("created_at"),
                "pushed_at": r.get("pushed_at"), "archived": r.get("archived"),
                "licence_spdx": ((r.get("license") or {}).get("spdx_id")),
                "description": (r.get("description") or "")[:300] or None,
                "topics": sorted(r.get("topics") or []) or None,
            })
        if len(doc.get("items") or []) < 100:
            break
    return out, None


def _ecosystems_awesome(http, list_url: str, lane: str, pages: int = 30) -> tuple[list[dict], str | None]:
    """awesome.ecosyste.ms: an awesome list parsed by ecosyste.ms, as a discovery seed only.
    Its values are CC BY-SA 4.0, so only the repository address and id are carried forward;
    every metric the pool records comes from GitHub (the light refresh or search)."""
    st, meta = http.call("https://awesome.ecosyste.ms/api/v1/lists/lookup?" + urllib.parse.urlencode({"url": list_url}))
    if st != 200 or not isinstance(meta, dict) or not meta.get("projects_url"):
        return [], f"{lane}: awesome.ecosyste.ms lookup http {st}"
    out = []
    for page in range(1, pages + 1):
        st, rows = http.call(meta["projects_url"] + f"?per_page=100&page={page}")
        if st != 200 or not isinstance(rows, list):
            return out, f"{lane}: awesome.ecosyste.ms projects page {page} http {st}"
        for r in rows:
            url = (r.get("url") or "").rstrip("/")
            parts = urllib.parse.urlparse(url)
            path = parts.path.strip("/").split("/")
            if parts.netloc.lower() != "github.com" or len(path) != 2:
                continue
            repo = r.get("repository") or {}
            out.append({"key": "github.com/" + "/".join(path).lower(), "provider": "ecosystems_awesome", "query": lane,
                        "host_repo_id": int(repo["uuid"]) if str(repo.get("uuid") or "").isdigit() else None,
                        "stars": None})
        if len(rows) < 100:
            break
    return out, None


PROVIDERS = {"github_search": _github_search, "ecosystems_awesome": _ecosystems_awesome}


def run(as_of: datetime, http=None) -> dict:
    from .enrich import Http

    cfg = load_cfg()
    http = http or Http()
    since = (as_of - timedelta(days=int(cfg["lookback_days"]))).date().isoformat()
    candidates, errors, lanes = [], [], []
    for lane in cfg["lanes"]:
        q = lane.get("list_url") or lane["query"].format(since=since, min_stars=cfg["min_stars"])
        fn = PROVIDERS.get(lane["provider"])
        if fn is None:
            errors.append(f"{lane['id']}: unknown provider {lane['provider']}")
            lanes.append({"id": lane["id"], "status": "failed", "returned": 0})
            continue
        try:
            got, err = fn(http, q, lane["id"])
        except Exception as exc:  # a lane failure is recorded, never fatal
            got, err = [], f"{lane['id']}: {type(exc).__name__}: {exc}"
        if lane["provider"] == "github_search":  # seeds carry no metric of their own (see above)
            got = [c for c in got if (c.get("stars") or 0) >= int(cfg["min_stars"])]
        candidates += got
        lanes.append({"id": lane["id"], "provider": lane["provider"], "query": q,
                      "status": "failed" if err and not got else "degraded" if err else "ok", "returned": len(got)})
        if err:
            errors.append(err)
    status = "ok" if not errors else "failed" if all(l["status"] == "failed" for l in lanes) else "degraded"
    return {"status": status, "lanes": lanes, "candidates": candidates, "errors": errors}
