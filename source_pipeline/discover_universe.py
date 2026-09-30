"""Discover lane (KEI-811) — pure functions over what discovery recorded.

Nothing here touches the network. Given the frontier that `discover-harvest` recorded,
these functions decide which repositories are candidates, by which routes, and in what
order. The harvest stage uses them to choose the depth-2 frontier; the build stage uses
them again to rebuild the universe, so both agree by construction.
"""
from __future__ import annotations

import re

GITHUB_LINK = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})",
    re.I,
)

# First path segments on github.com that are not repository owners.
NOT_OWNERS = {
    "about", "apps", "blog", "collections", "contact", "customer-stories", "enterprise", "events",
    "explore", "features", "issues", "login", "marketplace", "new", "notifications", "orgs",
    "pricing", "pulls", "readme", "search", "security", "settings", "site", "sponsors", "topics",
    "trending", "users", "codespaces", "copilot", "resources", "solutions", "team", "join",
    "signup", "password_reset", "dashboard", "account", "user-attachments", "assets",
}
NOT_REPOS = {"followers", "following", "repositories", "stars", "projects", "packages", "sponsors"}


def slug_key(owner: str, repo: str) -> str | None:
    """`owner/repo`, lower-cased, or None if the pair cannot name a repository."""
    owner = owner.strip()
    repo = repo.strip().rstrip(".")
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo or owner.lower() in NOT_OWNERS or repo.lower() in NOT_REPOS:
        return None
    if repo in (".", "..") or repo.startswith("."):
        return None
    return f"{owner}/{repo}".lower()


def extract_github_repos(text: str) -> list[tuple[str, int]]:
    """Every github.com repository linked in a text, once, with the first line it is on."""
    seen: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in GITHUB_LINK.finditer(line):
            key = slug_key(m.group(1), m.group(2))
            if key and key not in seen:
                seen[key] = lineno
    return sorted(seen.items())


def parse_seed_file(text: str) -> list[dict]:
    """Rows of Keith's seed corpus.

    A markdown table row yields one seed: its name, its category and the repository it
    links to, or `unresolved` with the reason if it links to none. Bare URLs outside a
    table are seeds too, so more can be pasted in any form.
    """
    rows = []
    for lineno, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 2 or set(s) <= set("|-: ") or cells[0].lower() == "project":
                continue
            links = extract_github_repos(s)
            row = {"line": lineno, "name": cells[0], "source_text": cells[1],
                   "category": cells[2] if len(cells) > 2 else None}
            if links:
                row["slug"] = links[0][0]
            else:
                row["slug"] = None
                row["unresolved_reason"] = "no_repository_url_in_seed_row"
            rows.append(row)
        else:
            for slug, _ in extract_github_repos(s):
                rows.append({"line": lineno, "name": slug, "source_text": s, "category": None, "slug": slug})
    return rows


def _route(strategy, source, depth, via=None, locator=None) -> dict:
    return {"strategy": strategy, "source": source, "depth": depth, "via": via, "locator": locator}


def depth1_routes(frontier: dict, cfg: dict) -> dict[str, list[dict]]:
    """Depth-0 seeds and every depth-1 candidate with the routes that reached it."""
    routes: dict[str, list[dict]] = {}

    def add(slug, route):
        if slug:
            routes.setdefault(slug, []).append(route)

    seeds = frontier["seeds"]
    for row in seeds["keith"]:
        add(row.get("slug"), _route("keith_seed", cfg["seeds"]["keith_seed_file"], 0,
                                    locator=f"line {row['line']}"))
    for login, acct in sorted(seeds["accounts"].items()):
        for kind in ("starred", "watching"):
            for slug in acct.get(kind) or []:
                add(slug, _route(f"github_{kind}", f"github-account:{login}", 0))
    for slug in seeds["replace"]:
        add(slug, _route("replace_lane", "kei805-dataset", 0))

    st = frontier["strategies"]
    for owner, rec in sorted(st["owner_neighbourhood"].items()):
        for slug in rec.get("repos") or []:
            add(slug, _route("owner_neighbourhood", f"github-owner:{owner}", 1, via=rec.get("via")))
    for seed, rec in sorted(st["readme_links"].items()):
        for slug, line in rec.get("links") or []:
            if slug != seed:
                add(slug, _route("readme_links", f"readme:{seed}", 1, via=[seed], locator=f"line {line}"))
    for topic, rec in sorted(st["topic_search"]["results"].items()):
        for slug in rec.get("repos") or []:
            add(slug, _route("topic_search", f"github-topic:{topic}", 1))
    for name, rec in sorted(st["curated_lists"].items()):
        if rec.get("decision") != "allow":
            continue
        for slug, line in rec.get("links") or []:
            if slug != name.lower():
                add(slug, _route("curated_list", f"list:{name}@{rec['commit']}", 1, locator=f"line {line}"))
    for qid, rec in sorted(st["trending"].items()):
        for slug in rec.get("repos") or []:
            add(slug, _route("trending", f"search:{qid}", 1))
    return routes


def distinct_routes(rs: list[dict]) -> int:
    return len({(r["strategy"], r["source"]) for r in rs})


def recursion_frontier(routes: dict[str, list[dict]], cfg: dict) -> list[str]:
    """Depth-1 candidates that more than one independent route reached, bounded."""
    rec = cfg["recursion"]
    pool = []
    for slug, rs in routes.items():
        if min(r["depth"] for r in rs) != 1:
            continue
        n = distinct_routes(rs)
        if n >= int(rec["frontier_min_routes"]):
            pool.append((-n, slug))
    pool.sort()
    return [slug for _, slug in pool[: int(rec["frontier_max"])]]


def all_routes(frontier: dict, cfg: dict) -> dict[str, list[dict]]:
    """The whole universe: depth 0, depth 1 and the recorded depth-2 expansion."""
    routes = depth1_routes(frontier, cfg)
    rec = frontier["strategies"].get("recursion") or {}
    for owner, r in sorted((rec.get("owner_neighbourhood") or {}).items()):
        for slug in r.get("repos") or []:
            routes.setdefault(slug, []).append(
                _route("owner_neighbourhood", f"github-owner:{owner}", 2, via=r.get("via")))
    for seed, r in sorted((rec.get("readme_links") or {}).items()):
        for slug, line in r.get("links") or []:
            if slug != seed:
                routes.setdefault(slug, []).append(
                    _route("readme_links", f"readme:{seed}", 2, via=[seed], locator=f"line {line}"))
    for slug in routes:
        routes[slug].sort(key=lambda r: (r["depth"], r["strategy"], r["source"], r["locator"] or ""))
    return dict(sorted(routes.items()))


def order_for_cap(routes: dict[str, list[dict]]) -> list[str]:
    """Evaluation order when the universe is capped: depth, then route count, then id."""
    return [s for _, _, s in sorted(
        (min(r["depth"] for r in rs), -distinct_routes(rs), s) for s, rs in routes.items())]


def rank_topics(seed_meta: dict, cfg: dict) -> list[list]:
    """Topics carried by the seeds, most common first, generic topics removed."""
    stop = set(cfg["strategies"]["topic_search"]["stop_topics"])
    counts: dict[str, int] = {}
    for meta in seed_meta.values():
        for t in meta.get("topics") or []:
            if t not in stop:
                counts[t] = counts.get(t, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [[t, n] for t, n in ranked[: int(cfg["strategies"]["topic_search"]["max_topics"])]]
