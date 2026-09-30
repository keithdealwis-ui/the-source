"""Stage 2 — normalise and reconcile (KEI-805 criteria 2 and 5).

Pure functions over staged claims. No network, no clock: the same claims and the same
config always produce byte-identical output.

Identity rules
  OSS project   canonical key `host/owner/repo`, lowercased, `.git` and any trailing
                path, query or fragment removed. After live verification the key is
                replaced by the host's own canonical name, so a renamed or transferred
                repository listed under two URLs collapses to one project.
  SaaS product  normalised name key, resolved through the reviewed catalogue.
  Relationship  the pair (saas_id, oss_id). Every upstream statement of the same pair
                is merged into one edge and kept as a separate provenance entry.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from . import common

# Path prefixes on a forge that are never a repository owner.
RESERVED_OWNERS = {
    "orgs", "topics", "sponsors", "marketplace", "features", "about", "apps",
    "collections", "explore", "settings", "users", "groups",
}


def clean_saas_name(name: str) -> str:
    """Drop markup debris an upstream left in a name (e.g. a stray `</a>[`)."""
    name = re.sub(r"<[^>]*>", " ", name)
    name = re.sub(r"^[^A-Za-z0-9]*(/?a>)?[\[\]\s]*", "", name)
    return name.strip()


LICENCE_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9.+-]*(?:[ -](?:[0-9][0-9.]*|only|or-later|Clause|License|Licence))*")


def clean_declared_licence(value: str | None) -> str | None:
    """Keep the licence identifier an upstream declared; drop its commentary."""
    if not value:
        return None
    head = re.split(r"\s*[(\u2014\u2013;]|\s+-\s+|\s*/\s*", value.strip(), maxsplit=1)[0].strip()
    head = re.sub(r"^[^A-Za-z0-9]+", "", head)
    return head[:60] or None


def saas_key(name: str) -> str:
    name = re.sub(r"\([^)]*\)", " ", name)
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def repo_key(url: str) -> tuple[str | None, str | None]:
    """Return (canonical key, None) or (None, rejection reason)."""
    if not url:
        return None, "repo_url_missing"
    url = url.strip().strip("\"'")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in parsed.path.split("/") if p]
    if host == "gitlab.com":
        # GitLab allows nested groups; the project path ends at "/-/" if present.
        if "-" in parts:
            parts = parts[: parts.index("-")]
        if len(parts) < 2:
            return None, "repo_url_not_a_repository"
        path = "/".join(parts)
    else:
        if len(parts) < 2:
            return None, "repo_url_not_a_repository"
        path = "/".join(parts[:2])
    if parts[0].lower() in RESERVED_OWNERS:
        return None, "repo_url_not_a_repository"
    path = re.sub(r"\.git$", "", path, flags=re.I).lower()
    if not host or "." not in host:
        return None, "repo_url_not_a_repository"
    return f"{host}/{path}", None


def _url_key(url: str) -> str:
    return re.sub(r"/+$", "", (url or "").strip().strip("\"'").lower())


def load_overrides() -> dict[str, str]:
    path = common.CONFIG / "repo_overrides.yaml"
    if not path.exists():
        return {}
    doc = common.load_yaml(path) or {}
    return {_url_key(k): v for k, v in (doc.get("overrides") or {}).items()}


def name_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"\([^)]*\)", " ", name).lower())


def choose_name_entry(oss_id: str, names: list[dict]) -> dict:
    value = choose_name(oss_id, names)
    return next(n for n in names if n["value"] == value)


def choose_name(oss_id: str, names: list[dict]) -> str:
    """Pick the display name deterministically.

    Upstreams sometimes label an entry editorially ("X stack (A + B)", "X (self-host)").
    Prefer the spelling that matches the repository's own name; otherwise the spelling
    from the highest-priority source. `names` is already in source-priority order.
    """
    repo = name_key(oss_id.rsplit("/", 1)[-1])
    owner = name_key(oss_id.split("/")[1])
    exact = [n["value"] for n in names if name_key(n["value"]) in (repo, owner)]
    if exact:
        return exact[0]
    plain = [n["value"] for n in names if not re.search(r"[+(/]", n["value"])]
    return (plain or [n["value"] for n in names])[0]


def product_group(oss_id: str, name: str) -> str:
    """Repositories of one product (same owner, same product name) share a group."""
    host, owner = oss_id.split("/")[:2]
    return f"{host}/{owner}:{name_key(name)}"


def load_catalogue() -> tuple[dict, dict]:
    """Return (products by saas_id, alias key -> saas_id). Fails on ambiguous aliases."""
    doc = common.load_yaml(common.CONFIG / "saas_catalogue.yaml")
    products: dict[str, dict] = {}
    index: dict[str, str] = {}
    for name, domain, category, aliases in doc["products"]:
        sid = slug(name)
        if sid in products:
            raise ValueError(f"duplicate catalogue id {sid}")
        products[sid] = {
            "saas_id": sid,
            "name": name,
            "domain": domain,
            "category": category,
            "aliases": sorted(set(aliases)),
        }
        for label in [name, *aliases]:
            key = saas_key(label)
            if index.get(key, sid) != sid:
                raise ValueError(f"alias {label!r} maps to both {index[key]} and {sid}")
            index[key] = sid
    return products, index


def reconcile(claims: list[dict], sources_cfg: dict) -> dict:
    """Merge raw claims into candidate projects, products and edges."""
    products, alias_index = load_catalogue()
    overrides = load_overrides()
    priority = {s["source_id"]: s["priority"] for s in sources_cfg["sources"]}
    share_alike = {s["source_id"]: bool(s.get("share_alike")) for s in sources_cfg["sources"]}

    oss: dict[str, dict] = {}
    edges: dict[tuple[str, str], dict] = {}
    saas_seen: dict[str, set] = {}
    rejections: list[dict] = []
    uncatalogued: dict[str, dict] = {}

    def reject(claim, stage, reason, **extra):
        rejections.append(
            {
                "stage": stage,
                "reason": reason,
                "source_id": claim["source_id"],
                "source_commit": claim["source_commit"],
                "locator": claim["locator"],
                "oss_name": claim["oss_name"],
                "repo_url": claim["repo_url"],
                **extra,
            }
        )

    for claim in sorted(claims, key=lambda c: (priority[c["source_id"]], c["locator"], c["oss_name"])):
        if not claim["saas_names"]:
            continue  # harvested, but states no relationship: nothing to build an edge from
        resolved = overrides.get(_url_key(claim["repo_url"]))
        key, why = repo_key(resolved or claim["repo_url"])
        if key is None:
            reject(claim, "normalise", why, saas_names=claim["saas_names"])
            continue
        sid = claim["source_id"]
        prov = {
            "source_id": sid,
            "source_commit": claim["source_commit"],
            "locator": claim["locator"],
        }
        if resolved:
            prov["resolved_by"] = "reviewed_override"
            prov["upstream_url"] = claim["repo_url"]
        project = oss.setdefault(
            key,
            {
                "oss_id": key,
                "imported": {"names": [], "repo_urls": [], "declared_licences": [], "categories": []},
                "provenance": [],
            },
        )
        imp = project["imported"]
        for field, value in (
            ("names", claim["oss_name"]),
            ("repo_urls", claim["repo_url"]),
            ("declared_licences", clean_declared_licence(claim["declared_licence"])),
            ("categories", claim["category"]),
        ):
            if value:
                entry = {"value": value, "source_id": sid}
                if entry not in imp[field]:
                    imp[field].append(entry)
        if prov not in project["provenance"]:
            project["provenance"].append(prov)

        for raw_name in claim["saas_names"]:
            raw_name = clean_saas_name(raw_name)
            if not raw_name:
                continue
            saas_id = alias_index.get(saas_key(raw_name))
            if saas_id is None:
                k = saas_key(raw_name) or raw_name
                u = uncatalogued.setdefault(k, {"key": k, "spellings": set(), "claims": 0, "sources": set()})
                u["spellings"].add(raw_name)
                u["claims"] += 1
                u["sources"].add(sid)
                reject(claim, "normalise", "saas_not_in_catalogue", saas_name=raw_name)
                continue
            saas_seen.setdefault(saas_id, set()).add(sid)
            edge = edges.setdefault(
                (saas_id, key),
                {"saas_id": saas_id, "oss_id": key, "relation": "open_source_alternative_to", "provenance": []},
            )
            entry = dict(prov, claimed_saas_name=raw_name, claimed_oss_name=claim["oss_name"])
            if entry not in edge["provenance"]:
                edge["provenance"].append(entry)

    def order(plist):
        return sorted(plist, key=lambda p: (priority[p["source_id"]], p["locator"], p.get("claimed_saas_name", "")))

    for project in oss.values():
        project["provenance"] = order(project["provenance"])
        for field in project["imported"]:
            project["imported"][field].sort(key=lambda e: (priority[e["source_id"]], e["value"]))
        # Display name: the spelling from the highest-priority source.
        project["name"] = choose_name(project["oss_id"], project["imported"]["names"])
    for edge in edges.values():
        edge["provenance"] = order(edge["provenance"])
        srcs = sorted({p["source_id"] for p in edge["provenance"]}, key=lambda s: priority[s])
        edge["source_ids"] = srcs
        edge["source_count"] = len(srcs)
        edge["share_alike_only"] = all(share_alike[s] for s in srcs)

    return {
        "oss": dict(sorted(oss.items())),
        "edges": [edges[k] for k in sorted(edges)],
        "saas": {k: dict(products[k], source_ids=sorted(v, key=lambda s: priority[s])) for k, v in sorted(saas_seen.items())},
        "rejections": rejections,
        "uncatalogued": [
            {"key": u["key"], "spellings": sorted(u["spellings"]), "claims": u["claims"], "sources": sorted(u["sources"])}
            for u in sorted(uncatalogued.values(), key=lambda u: (-u["claims"], u["key"]))
        ],
    }


def merge_renamed(rec: dict, canonical: dict[str, str]) -> dict:
    """Collapse projects whose live canonical identity is the same repository.

    `canonical` maps a harvested key to the key the host reports today. Two upstream
    URLs that resolve to one repository (rename, transfer, case difference) become one
    project; their imported fields, provenance and edges are merged.
    """
    oss: dict[str, dict] = {}
    for key in sorted(rec["oss"]):
        project = rec["oss"][key]
        target = canonical.get(key, key)
        if target not in oss:
            merged = {
                "oss_id": target,
                "name": project["name"],
                "imported": {f: list(v) for f, v in project["imported"].items()},
                "provenance": list(project["provenance"]),
                "harvested_as": [key],
            }
            oss[target] = merged
            continue
        merged = oss[target]
        merged["harvested_as"] = sorted(set(merged["harvested_as"]) | {key})
        for field, values in project["imported"].items():
            for v in values:
                if v not in merged["imported"][field]:
                    merged["imported"][field].append(v)
        for p in project["provenance"]:
            if p not in merged["provenance"]:
                merged["provenance"].append(p)

    edges: dict[tuple[str, str], dict] = {}
    for edge in rec["edges"]:
        target = canonical.get(edge["oss_id"], edge["oss_id"])
        k = (edge["saas_id"], target)
        if k not in edges:
            edges[k] = dict(edge, oss_id=target, provenance=list(edge["provenance"]))
            continue
        for p in edge["provenance"]:
            if p not in edges[k]["provenance"]:
                edges[k]["provenance"].append(p)

    sources_cfg = common.load_sources()
    priority = {s["source_id"]: s["priority"] for s in sources_cfg["sources"]}
    share_alike = {s["source_id"]: bool(s.get("share_alike")) for s in sources_cfg["sources"]}
    for project in oss.values():
        project["provenance"].sort(key=lambda p: (priority[p["source_id"]], p["locator"]))
        for field in project["imported"]:
            project["imported"][field].sort(key=lambda e: (priority[e["source_id"]], e["value"]))
        project["name"] = choose_name(project["oss_id"], project["imported"]["names"])
    for edge in edges.values():
        edge["provenance"].sort(
            key=lambda p: (priority[p["source_id"]], p["locator"], p.get("claimed_saas_name", ""))
        )
        srcs = sorted({p["source_id"] for p in edge["provenance"]}, key=lambda s: priority[s])
        edge["source_ids"] = srcs
        edge["source_count"] = len(srcs)
        edge["share_alike_only"] = all(share_alike[s] for s in srcs)

    return dict(rec, oss=dict(sorted(oss.items())), edges=[edges[k] for k in sorted(edges)])
