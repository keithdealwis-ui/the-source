"""Second-opinion licence detection (KEI-805 criterion 10).

A host's own detector sometimes reports "Other" for a repository whose licence file is
a standard open-source licence with a short preamble. This module re-reads the licence
text itself. It is deliberately conservative: it would rather leave a good project
excluded (it then appears as an exception candidate for human review) than admit a
source-available or open-core one.

A repository is recognised only if ALL of these hold:
  1. every licence file at the repository root matches a reference licence text;
  2. whatever text is left over after the match is short;
  3. the left-over text contains none of the restriction markers;
  4. the repository root has no enterprise/commercial directory.

Pure functions; reference texts are pinned under config/licence_texts/.
"""
from __future__ import annotations

import re
from functools import lru_cache

from . import common

LICENCE_FILE = re.compile(r"^(licen[sc]e|copying|unlicense|copyright)([.\-_ ].*)?$", re.I)
ENTERPRISE_DIR = re.compile(r"^\.?(ee|enterprise|enterprise[-_]edition|commercial|pro|premium|proprietary)$", re.I)

RESTRICTION_MARKERS = [
    # a different licence applies to part of the repository
    "portions of", "as follows", "directory", "directories", "folder", "resides under",
    "license defined in", "licence defined in", "separate license", "different license",
    "dual license", "dual licensed", "multi license",
    # commercial or source-available terms
    "enterprise", "commercial", "proprietary", "commons clause", "sustainable use",
    "business source", "elastic license", "server side public", "functional source",
    "fair source", "fair code", "non commercial", "noncommercial", "source available",
    "subscription", "paid plan", "license key", "licence key",
    "purchase", "employees", "hosted", "managed service", "as a service", "saas",
    "revenue", "competing", "compete", "production use", "evaluation", "trial",
    # terms added to or subtracted from the standard licence
    "additional terms", "additional restriction", "additional permission", "may not",
    "shall not", "must not", "cannot", "not permitted", "not allowed", "only", "requires",
    "require", "required", "unless", "provided that you", "applies", "apply",
    "except", "exception", "exceptions", "prohibited", "restricted", "restriction",
    "files in", "files under", "tree",
]
# "licensed under <something>" is a restriction unless that something is an open licence.
FOREIGN_LICENCE = re.compile(
    r"licen[sc]ed under (?!(the )?(terms of )?(the )?(same|gnu|gpl|agpl|lgpl|mit|apache|bsd|mozilla|mpl|"
    r"creative commons|cc by|cc0|isc|eclipse|european union)\b)")
DIRECTORY_REFERENCE = re.compile(r"(^|[\s\"'`(/])\.?(ee|enterprise)(/|\s+(tree|directory|folder|edition))", re.I)

MIN_COVERAGE = 0.90
MAX_LEFTOVER_CHARS = 1200
MIN_BLOCK_WORDS = 4


def _norm(text: str) -> str:
    text = text.lower().replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _paragraphs(text: str) -> list[str]:
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        n = _norm(block)
        if n:
            out.append(n)
    return out


@lru_cache(maxsize=1)
def references() -> dict[str, dict]:
    refs = {}
    for path in sorted((common.CONFIG / "licence_texts").glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        refs[path.stem] = {
            "words": _norm(text).split(),
            "paragraphs": [p for p in _paragraphs(text) if len(p) >= 40 and not re.search(r"\byear\b.*\bfullname\b", p)],
        }
    return refs


def _shortlist(flat: str, limit: int = 3) -> list[str]:
    """Rank references by how many of their paragraphs appear verbatim; cheap pre-filter."""
    scored = []
    for spdx, ref in references().items():
        total = sum(len(p) for p in ref["paragraphs"]) or 1
        hit = sum(len(p) for p in ref["paragraphs"] if p in flat)
        scored.append((hit / total, spdx))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [spdx for score, spdx in scored[:limit] if score > 0]


def match_text(text: str) -> dict:
    """Match one licence file, word by word, against the closest reference texts."""
    from difflib import SequenceMatcher

    flat = _norm(text)
    words = flat.split()
    best = None
    for spdx in _shortlist(flat):
        ref_words = references()[spdx]["words"]
        sm = SequenceMatcher(None, ref_words, words, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size >= MIN_BLOCK_WORDS]
        covered = sum(b.size for b in blocks)
        coverage = covered / len(ref_words)
        keep = [True] * len(words)
        for b in blocks:
            for i in range(b.b, b.b + b.size):
                keep[i] = False
        leftover = " ".join(w for w, k in zip(words, keep) if k)
        # Closest reference wins: BSD-3-Clause text also contains all of BSD-2-Clause,
        # so coverage alone cannot separate them; the smaller remainder does.
        rank = (round(coverage, 2), -len(leftover), spdx)
        if best is None or rank > best["rank"]:
            best = {"spdx": spdx, "coverage": coverage, "leftover": leftover, "rank": rank}
    if best is None:
        best = {"spdx": None, "coverage": 0.0, "leftover": flat}
    leftover = best["leftover"]
    padded = f" {leftover} "
    markers = sorted({m for m in RESTRICTION_MARKERS if f" {m} " in padded})
    if FOREIGN_LICENCE.search(leftover):
        markers.append("licensed under something that is not a recognised open licence")
    if DIRECTORY_REFERENCE.search(text):
        markers.append("reference to an ee/ or enterprise/ path")
    if best["spdx"] == "BSD-2-Clause" and "neither the name" in leftover:
        best["spdx"] = "BSD-3-Clause"  # the third clause is what is left over
    result = {
        "spdx": best["spdx"],
        "coverage": round(best["coverage"], 4),
        "leftover_chars": len(leftover),
        "restriction_markers": markers,
    }
    # Long licences legitimately carry longer notices, so the allowance scales with
    # the reference: the larger of a fixed floor and 10% of the standard text.
    ref_chars = len(" ".join(references()[best["spdx"]]["words"])) if best["spdx"] else 0
    allowance = max(MAX_LEFTOVER_CHARS, ref_chars // 10)
    result["leftover_allowance"] = allowance
    result["matched"] = bool(
        best["spdx"] and best["coverage"] >= MIN_COVERAGE and len(leftover) <= allowance and not markers
    )
    return result


def detect(root_entries: list[dict], texts: dict[str, str]) -> dict:
    """Decide from a repository's root listing and its licence file texts.

    root_entries: [{"name": str, "type": "file"|"dir"}]
    texts: licence file name -> content
    """
    enterprise = sorted(e["name"] for e in root_entries if e["type"] == "dir" and ENTERPRISE_DIR.match(e["name"]))
    files = sorted(e["name"] for e in root_entries if e["type"] == "file" and LICENCE_FILE.match(e["name"]))
    for name in texts:
        if name not in files:
            files.append(name)
    files.sort()
    per_file = {}
    for name in files:
        if name not in texts:
            per_file[name] = {"matched": False, "reason": "text_not_retrieved"}
        else:
            per_file[name] = match_text(texts[name])
    reasons = []
    if not files:
        reasons.append("no_licence_file_at_root")
    if enterprise:
        reasons.append("enterprise_directory_at_root")
    unmatched = [n for n, r in per_file.items() if not r["matched"]]
    if unmatched:
        reasons.append("licence_file_not_a_standard_text")
    spdx = sorted({r["spdx"] for r in per_file.values() if r.get("matched")})
    ok = not reasons
    return {
        "method": "licence_text_match",
        "recognised": ok,
        "spdx": (spdx[0] if len(spdx) == 1 else " AND ".join(spdx)) if ok else None,
        "spdx_components": spdx if ok else [],
        "licence_files": files,
        "enterprise_directories": enterprise,
        "per_file": per_file,
        "refusal_reasons": reasons,
    }
