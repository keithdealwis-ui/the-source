"""Upstream parsers.

Each parser reads one pinned upstream checkout and yields raw *claims*:

    {"oss_name", "repo_url", "declared_licence", "category", "saas_names": [...],
     "locator"}

Facts only (see config/sources.yaml import_policy): no description, tagline or other
upstream prose is carried into a claim. `locator` says where in the upstream the
claim was read, so every relationship can be traced to a file and line.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def _split_names(text: str) -> list[str]:
    """Split "A, B and C" / "A / B" / "A or B" into names."""
    text = text.strip().strip(".")
    parts = re.split(r"\s*,\s*|\s+and\s+|\s+or\s+|\s*/\s*|\s*&\s*", text)
    return [p.strip() for p in parts if p and p.strip()]


def parse_solvohq(src: Path, parse_path: str):
    path = src / parse_path
    category = None
    saas = None
    in_contents = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("## "):
            category = line[3:].strip()
            in_contents = category.lower() == "contents"
            saas = None
            continue
        if in_contents:
            continue
        if line.startswith("### "):
            m = MD_LINK.search(line)
            saas = (m.group(1) if m else line[4:]).strip()
            continue
        m = re.match(r"^- \[\*\*(.+?)\*\*\]\(([^)\s]+)\)", line)
        if m and saas:
            lic = re.search(r"_\(([^)]*(?:\([^)]*\))?[^)]*)\)_\s*$", line)
            yield {
                "oss_name": m.group(1).strip(),
                "repo_url": m.group(2).strip(),
                "declared_licence": lic.group(1).strip() if lic else None,
                "category": category,
                "saas_names": [saas],
                "locator": f"{parse_path}#L{n}",
            }


def parse_opensaasdirectory(src: Path, parse_path: str):
    path = src / parse_path
    category = None
    current = None
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("## "):
            category = line[3:].strip()
            current = None
            continue
        if line.startswith("### "):
            m = MD_LINK.search(line)
            current = {"name": m.group(1).strip(), "url": m.group(2).strip()} if m else None
            continue
        m = re.match(r"^\*\*.*?alternative to:\*\*\s*(.+)$", line, re.I)
        if m and current:
            yield {
                "oss_name": current["name"],
                "repo_url": current["url"],
                "declared_licence": None,
                "category": category,
                "saas_names": [s.strip() for s in m.group(1).split(",") if s.strip()],
                "locator": f"{parse_path}#L{n}",
            }


def parse_runacapital(src: Path, parse_path: str):
    for path in sorted((src / parse_path).glob("*.yaml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            yield {"_unparseable": f"{parse_path}/{path.name}"}
            continue
        if not isinstance(doc, dict):
            continue
        names = doc.get("alts_names") or []
        if isinstance(names, str):
            names = [names]
        yield {
            "oss_name": str(doc.get("company_name") or path.stem).strip(),
            "repo_url": str(doc.get("gh_link") or "").strip().strip("\"'"),
            "declared_licence": None,
            "category": (str(doc.get("category")).strip() if doc.get("category") else None),
            "saas_names": [str(x).strip() for x in names if str(x).strip()],
            "locator": f"{parse_path}/{path.name}",
        }


def parse_emmraan(src: Path, parse_path: str):
    products = json.loads((src / parse_path).read_text(encoding="utf-8"))
    for idx, p in enumerate(products):
        if not p.get("openSource") or not p.get("repo"):
            continue
        cats = p.get("categories") or []
        yield {
            "oss_name": p["name"].strip(),
            "repo_url": "https://github.com/" + p["repo"].strip(),
            "declared_licence": p.get("license"),
            "category": cats[0] if cats else None,
            "saas_names": [s.strip() for s in (p.get("replaces") or []) if s.strip()],
            "locator": f"{parse_path}#/{idx}/slug={p['slug']}",
        }


ALT_RE = re.compile(r"\(\s*alternatives? to ([^)]+)\)", re.I)


def parse_awesome_selfhosted(src: Path, parse_path: str):
    for path in sorted((src / parse_path).glob("*.yml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            yield {"_unparseable": f"{parse_path}/{path.name}"}
            continue
        if not isinstance(doc, dict):
            continue
        # The description is read ONLY to extract the named alternatives; the text
        # itself is discarded.
        m = ALT_RE.search(doc.get("description") or "")
        tags = doc.get("tags") or []
        lic = doc.get("licenses") or []
        yield {
            "oss_name": str(doc.get("name") or path.stem).strip(),
            "repo_url": str(doc.get("source_code_url") or "").strip(),
            "declared_licence": ", ".join(str(x) for x in lic) or None,
            "category": tags[0] if tags else None,
            "saas_names": _split_names(m.group(1)) if m else [],
            "locator": f"{parse_path}/{path.name}",
        }


def parse_btwso(src: Path, parse_path: str):
    path = src / parse_path
    saas: list[str] = []
    category = None
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("#"):
            heading = line.lstrip("#").strip().rstrip(":")
            m = re.match(r"^(.*?)\((.+?)\s+alternatives?\)\s*$", heading, re.I)
            if m:
                category = m.group(1).strip()
                saas = _split_names(m.group(2))
            else:
                category, saas = heading, []
            continue
        m = re.match(r"^\|?\s*\[([^\]]+)\]\((https?://[^)\s]+)\)\s*\|", line)
        if m and saas:
            yield {
                "oss_name": m.group(1).strip(),
                "repo_url": m.group(2).strip(),
                "declared_licence": None,
                "category": category,
                "saas_names": list(saas),
                "locator": f"{parse_path}#L{n}",
            }


def parse_diegoleme(src: Path, parse_path: str):
    path = src / parse_path
    saas = None
    in_resources = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("## "):
            in_resources = line[3:].strip().lower() == "resources"
            saas = None
            continue
        if not in_resources:
            continue
        if line.startswith("### "):
            m = MD_LINK.search(line)
            saas = (m.group(1) if m else line[4:]).strip()
            continue
        m = re.match(r"^\s*[-*]\s*\[([^\]]+)\]\((https?://[^)\s]+)\)", line)
        if m and saas:
            yield {
                "oss_name": m.group(1).strip(),
                "repo_url": m.group(2).strip(),
                "declared_licence": None,
                "category": None,
                "saas_names": _split_names(saas),
                "locator": f"{parse_path}#L{n}",
            }


def parse_altstack(src: Path, parse_path: str):
    tools = json.loads((src / parse_path).read_text(encoding="utf-8"))
    by_slug = {t.get("slug"): t for t in tools}
    replaced_by: dict[str, list] = {}
    for idx, t in enumerate(tools):
        if t.get("is_open_source"):
            continue
        for alt in t.get("alternatives") or []:
            replaced_by.setdefault(alt, []).append((t["name"].strip(), idx, t["slug"]))
    for slug_, claims in sorted(replaced_by.items()):
        oss = by_slug.get(slug_)
        if not oss or not oss.get("is_open_source"):
            continue
        repo = (oss.get("github_repo") or "").strip()
        yield {
            "oss_name": oss["name"].strip(),
            "repo_url": ("https://github.com/" + repo) if repo else "",
            "declared_licence": oss.get("license"),
            "category": oss.get("category"),
            "saas_names": [c[0] for c in claims],
            "locator": f"{parse_path}#slug={slug_};named_by=" + ",".join(c[2] for c in claims),
        }


def parse_fossalt(src: Path, parse_path: str):
    path = src / parse_path
    saas: list[str] = []
    category = None
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            m = re.match(r"^(.*?)\((.+?)\s+alternatives?\)\s*$", heading, re.I)
            if m:
                category = m.group(1).strip()
                saas = [x.rstrip(".").strip() for x in _split_names(m.group(2).replace("...", ""))]
                saas = [x for x in saas if x]
            else:
                category, saas = heading, []
            continue
        m = re.match(r"^\s*[-*]\s*(?:\*\*)?\[([^\]]+)\]\((https?://[^)\s]+)\)", line)
        if m and saas:
            yield {
                "oss_name": m.group(1).strip(),
                "repo_url": m.group(2).strip(),
                "declared_licence": None,
                "category": category,
                "saas_names": list(saas),
                "locator": f"{parse_path}#L{n}",
            }


PARSERS = {
    "fossalt": parse_fossalt,
    "btwso": parse_btwso,
    "diegoleme": parse_diegoleme,
    "altstack": parse_altstack,
    "solvohq": parse_solvohq,
    "opensaasdirectory": parse_opensaasdirectory,
    "runacapital": parse_runacapital,
    "emmraan": parse_emmraan,
    "awesome_selfhosted": parse_awesome_selfhosted,
}
