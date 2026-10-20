"""Intake and top-result noise (KEI-923, V2.1 WP6 S4.3).

Two separate questions, measured separately:

  intake noise       of the repositories admitted into the discovery corpus, how many are not software a person
                     would run: curated lists, courses and tutorials, templates and examples, personal
                     configuration, prompt packs. `intake_reasons` names them and admission refuses them
                     (corpus_admit, `intake_noise:<code>`).
  top-result noise   of what the MCP tools actually put in front of someone, how much is irrelevant: the top ten
                     discovery results for a category need, and the top five discovery candidates for every
                     supported product.

The measurement (`measure`, the `obs-noise` command) uses `audit`, a second classifier written independently of
`intake_reasons` and of the server's own filters, so that neither the gate nor the ranker grades itself. It also
counts integrations (SDKs, clients, plugins, MCP servers) among the top five for supported products, and among
published recommendations, where the target is zero (KEI-919).

Thresholds live in config/expansion.yaml (`noise`), calibrated against observability/noise-baseline.json: a measure
fails only when it is worse than max(threshold, baseline + tolerance).
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

from . import common

BASELINE = common.ROOT / "observability" / "noise-baseline.json"
K_TOP = 10
K_SUPPORTED = 5
INTAKE_NOISE_FLAGS = {"material", "undescribed"}

# ---------------------------------------------------------------- the intake gate


def _norm(text) -> str:
    return " ".join(re.sub(r"[^a-z0-9+#.]+", " ", str(text or "").lower()).split())


_LIST_NAME = re.compile(r"^(awesome|list|lists)([-_.]|$)|[-_.](awesome|list|lists|resources|links)$")
_LIST_DESC = re.compile(r"^(an? )?(curated |awesome |comprehensive |collection |community curated )*"
                        r"(list|lists|collection|compilation|directory) of\b")
_LEARN = re.compile(r"\b(tutorials?|courses?|coursework|bootcamp|workshop|lessons?|homework|assignments?|"
                    r"exercises?|leetcode|interview (questions|prep|preparation)|study notes|cheat ?sheets?|"
                    r"roadmap to|learning path|book source|source code (for|of) (the )?book|code (for|from) "
                    r"(the )?book|100 days of|30 days of|solutions to)\b")
_LEARN_NAME = re.compile(r"(^|[-_.])(tutorials?|course|bootcamp|workshop|homework|exercises|leetcode|"
                         r"interview|cheatsheets?|learn|learning|notes)([-_.]|$)")
_TEMPLATE = re.compile(r"\b((?<!low )(?<!no )(?<!less )boilerplate|starter kit|starter template|project template|templates? for|"
                       r"example (app|project|code|application)|sample (app|project|code|application)|"
                       r"demo (app|project|application)|hello world|proof of concept|playground for)\b")
_TEMPLATE_NAME = re.compile(r"(^|[-_.])(boilerplate|starter|template|templates|examples?|samples?|demo|"
                            r"playground|poc|hello-world)([-_.]|$)")
_PERSONAL = re.compile(r"^(my|our) (own |personal |private )?(home ?lab|home-lab|homelab|dotfiles|config(uration)?s?|"
                       r"setup|infra(structure)?|servers?|k8s|kubernetes|cluster|nixos|website|blog|portfolio|"
                       r"personal|gitops)\b")
_PERSONAL_NAME = re.compile(r"(^|[-_.])(dotfiles|nixos-config|nix-config|home-ops|homelab-ops|"
                            r"k8s-gitops|infra-config)([-_.]|$)|^(dotfiles|homelab|home-lab|home-ops|infra|"
                            r"infrastructure|config|configs|setup)$")
_PROMPTS = re.compile(r"\b(prompt (pack|collection|library)|collection of prompts|system prompts? (for|of))\b|"
                      r"\b(agent|claude code|claude|codex|llm) skills?\b|\bskills? (for|from) [a-z0-9 .'-]{0,40}"
                      r"\b(claude|codex|agents?|llms?|models?|chatgpt|gpt)\b|\bskills for the [a-z0-9 .-]{1,30} models?\b")
# A registry, app or server for skills is software; only the skills themselves are a pack.
_SKILL_SOFTWARE = re.compile(r"\b(registry|desktop|app|application|platform|workbench|server|manager|marketplace|"
                             r"hub|cli|runtime|engine|framework|tool to|ide)\b")
_PROMPT_TOPICS = {"prompts"}
_SKILL_NAME = re.compile(r"[-_.](skills|skill-pack|prompts)$")  # a skills tag alone also marks agent tools that use skills
# Desk review, 2026-10-20 (docs/ACCEPTANCE-KEI-923.md): a reviewer reading the admissions found these kinds the
# first gate let through.
_SITE = re.compile(r"\b(my|our) (own )?(portfolio|website|web site|site|homepage|blog)\b|"
                   r"\bpersonal (portfolio|website|web site)\b|\bdeveloper portfolio\b|\bportfolio (website|site|"
                   r"template|for (developers|programmers)|example|para|web repo)\b|\b(meu|mon|mi) portf?olio\b|"
                   r"\bweb ?page for\b|\bcode of the [a-z0-9.-]+ website\b")
_SITE_NAME = re.compile(r"(^|[-_.])(portfolio|porfolio|personal-website)([-_.]|$)|\.github\.io$")
# Code that accompanies a paper. A conference tag alone is not enough: many tools people run carry one.
_RESEARCH = re.compile(r"\b((official |companion )?(code|implementation|pytorch implementation) (for|of) (the |our )?"
                       r"([a-z0-9 .:-]{0,40} )?paper)\b")
_DOCS = re.compile(r"^(the |official |community |public )*(manifest and )?(documentation|docs)( repo(sitory)?| site|"
                   r" website)? (for|of)\b|\bwiki pages\b|\b(rfcs?|proposals|standards) (and|&) (drafts|discussions)\b|"
                   r"\bepisodes? (for|of) the\b")
_MORE_TEMPLATE = re.compile(r"\b(admin|dashboard|landing page|website|ui|vault|system|mvp|prototype) templates?\b|"
                            r"\b(templates?|mvp) (for|with|built|based)\b|\bsample [a-z]+ app\b|\ban example of\b|"
                            r"\b(rebuilt|recreated|clone) (using|in|with) [a-z]+\b.*\btemplate\b|\bbookshelf made in\b")
_MORE_LEARN = re.compile(r"\b(study guide|exam objectives|exam prep|course notes|day wise notes|hands on resources|"
                         r"checklist (helps|for)|comparison of|books (related|about|on))\b")
_MORE_LEARN_NAME = re.compile(r"(^|[-_.])(cheat-?sheets?|study-guide|books)([-_.]|$)")
# Second desk review, 2026-10-21: only the signals that refused no product anywhere in the 25k corpus. Venue tags,
# "reference implementation", "sandbox project" and dataset/survey topics were tried and dropped: each also marks
# software people run.
_SURVEY = re.compile(r"\ba (comprehensive |systematic )?survey (on|of)\b")
_SPEC_DOCS = re.compile(r"^(the )?(specification|spec) (for|of)\b|^(user |official )?documentation( for| of| repo|,|$)|"
                        r"\bto host the [a-z0-9 .-]{1,30} wiki\b")
_DATASET = re.compile(r"\bdataset (for|of)\b|^dataset\b")
_SAMPLE_OWNER = re.compile(r"[-_](samples|examples)$")
_NOISE_TOPICS = {"awesome", "awesome-list", "awesome-lists", "list", "tutorial", "tutorials", "course",
                 "courses", "learning", "education-material", "interview-questions", "interview-preparation",
                 "leetcode", "cheatsheet", "cheatsheets", "boilerplate", "starter-kit", "starter-template",
                 "template", "templates", "examples", "example", "demo", "dotfiles", "homelab-ops",
                 "prompts", "prompt-engineering", "homework", "book", "ebook-source"}


def intake_reasons(name, full_name, description, topics) -> list[str]:
    """Why a repository is not software a person would run, from its own name, description and topics.

    Empty when there is no such reason. Codes: curated_list, learning_material, template_or_example,
    personal_configuration, prompt_pack (prompts and agent skill packs), personal_site, research_code,
    documentation."""
    repo = str(full_name or "").split("/")[-1].lower()
    nm = str(name or "").lower()
    desc = _norm(description)
    tops = {str(t).lower() for t in topics or []}
    out = []
    if _LIST_NAME.search(repo) or _LIST_NAME.search(nm) or _LIST_DESC.search(desc) or tops & {"awesome", "awesome-list", "awesome-lists", "list"}:
        out.append("curated_list")
    if _LEARN.search(desc) or _LEARN_NAME.search(repo) or tops & {"tutorial", "tutorials", "course", "courses",
                                                                  "interview-questions", "interview-preparation",
                                                                  "leetcode", "cheatsheet", "cheatsheets", "homework"}:
        out.append("learning_material")
    if _TEMPLATE.search(desc) or _TEMPLATE_NAME.search(repo) or tops & {"boilerplate", "starter-kit", "starter-template",
                                                                        "template", "templates", "examples", "example",
                                                                        "demo"}:
        out.append("template_or_example")
    if _PERSONAL.search(desc) or _PERSONAL_NAME.search(repo) or "dotfiles" in tops:
        out.append("personal_configuration")
    if (_PROMPTS.search(desc) and not _SKILL_SOFTWARE.search(desc)) or _SKILL_NAME.search(repo) or tops & _PROMPT_TOPICS:
        out.append("prompt_pack")
    if _SITE.search(desc) or _SITE_NAME.search(repo):
        out.append("personal_site")
    if _RESEARCH.search(desc):
        out.append("research_code")
    if _DOCS.search(desc):  # a repository named docs or *-wiki is as often a document or wiki product
        out.append("documentation")
    if "template_or_example" not in out and _MORE_TEMPLATE.search(desc):
        out.append("template_or_example")
    if "learning_material" not in out and (_MORE_LEARN.search(desc) or _MORE_LEARN_NAME.search(repo)
                                           or "skills-course" in tops):
        out.append("learning_material")
    if "research_code" not in out and (_SURVEY.search(desc) or _DATASET.search(desc)):
        out.append("research_code")
    if "documentation" not in out and _SPEC_DOCS.search(desc):
        out.append("documentation")
    owner = str(full_name or "").split("/")[0].lower()
    if "template_or_example" not in out and _SAMPLE_OWNER.search(owner):
        out.append("template_or_example")
    return out


# ---------------------------------------------------------------- the independent audit

# Written from a different reading of "not software a person would run", deliberately not sharing a pattern with
# intake_reasons: the measurement must be able to catch what the gate lets through.
_A_MATERIAL = re.compile(r"(^| )(a |an |the )?(list|collection|compilation|index) of (awesome |useful |the best |"
                         r"curated |free |open source |self hosted )*[a-z]|\bawesome\b|\btutorials?\b|\bcourses?\b|"
                         r"\bguide to\b|\blearn (how|to)\b|\bstep by step\b|\bbootcamp\b|\binterview (questions|prep)|"
                         r"\bleetcode\b|\bmy (home ?lab|dotfiles|setup|config)\b|\bdotfiles\b|\bboilerplate\b|"
                         r"\bstarter( kit| template)?\b|\btemplate for\b|\bexamples? (for|of)\b|\bcheat ?sheets?\b|"
                         r"\blectures?\b|\bclass project\b|\bassignments?\b|\bhomework\b|"
                         r"\bportfolio\b(?! (management|tracker|tracking|manager|analytics|optimi))|"
                         r"\bpersonal (web ?site|blog|page)\b|\bpaper\b|\bconference\b|\bdocs? (site|repo)|"
                         r"\bwiki pages\b|\b(skills|prompts) for\b|\bstudy\b|\bexam\b|\bsample\b|\bdemo\b|"
                         r"\btemplate\b|\bnotes\b")
_A_INTEGRATION = re.compile(r"(mcp server|model context protocol|sdk (for|to)|bot for|plugin for|extension for|"
                            r"integration (for|with)|integrates with|connector (for|to)|client (for|library)|"
                            r"wrapper (for|around)|library for|bindings for|api for)")
_A_INTEGRATION_NAME = re.compile(r"(^|[-_.])(mcp|sdk|bot|plugin|connector|client|bindings)([-_.]|$)")


def audit(row: dict, category: str | None = None) -> list[str]:
    """Independent irrelevance audit of one result row: 'material' (not software), 'integration' (part of, or a
    client of, another product; not judged for API tooling, where a client IS the product) and 'undescribed' (no
    description and no topics: nothing tells a reader what it is)."""
    name = str(row.get("full_name") or row.get("id") or "").split("/")[-1].lower()
    desc = " " + _norm(row.get("description")) + " "
    topics = [str(t).lower() for t in row.get("topics") or []]
    out = []
    if _A_MATERIAL.search(desc) or _A_MATERIAL.search(name.replace("-", " ")) or set(topics) & _NOISE_TOPICS:
        out.append("material")
    if category != "API tooling" and (_A_INTEGRATION.search(desc) or _A_INTEGRATION_NAME.search(name)):
        out.append("integration")
    if not desc.strip() and not topics:
        out.append("undescribed")
    return out


# ---------------------------------------------------------------- measurement


def _server(root: Path):
    from . import mcp_server as m
    srv = m.Server(root)
    return m, srv


def _call(m, srv, tool, args):
    try:
        return m.TOOL_BY_NAME[tool][4](srv.layer, args, srv)
    except m.ToolError as exc:
        return {"error": str(exc)}


def measure(root: Path = common.ROOT, sample_seed: int = 923, sample_n: int = 400) -> dict:
    """Every noise measure on the layer under `root`, with the rows behind each so a reviewer can check them."""
    m, srv = _server(root)
    layer = srv.layer
    corpus_rows = common.read_jsonl(root / "data" / "corpus" / "projects.jsonl")
    by_id = {r["id"]: r for r in corpus_rows}

    # top ten discovery results for a need in every category the catalogue names
    cats = sorted({s["category"] for s in layer.saas.values() if s.get("category")})
    top10 = []
    for cat in cats:
        r = _call(m, srv, "source_search", {"query": cat, "tier": "discovery", "limit": K_TOP})
        rows = [x for x in (r.get("results") or []) if x.get("kind") == "project"][:K_TOP]
        for x in rows:
            c = by_id.get(x["id"]) or {}
            v = {"id": x["id"], "full_name": c.get("full_name") or x["id"].split("/", 1)[-1],
                 "description": c.get("description"), "topics": c.get("topics")}
            top10.append({"category": cat, **v, "flags": audit(v, cat)})

    # top five discovery candidates for every supported product (KEI-919's integration surface)
    idx = json.loads((root / "api" / "v1" / "index.json").read_text())
    top5 = []
    for p in idx["supported_saas"]:
        sid = p["saas_id"]
        cat = layer.saas.get(sid, {}).get("category")
        r = _call(m, srv, "source_get_alternatives", {"saas": sid, "include_discovery": True, "limit": K_SUPPORTED})
        for x in ((r.get("discovery_candidates") or {}).get("candidates") or [])[:K_SUPPORTED]:
            c = by_id.get(x["id"]) or {}
            v = {"id": x["id"], "full_name": x.get("full_name") or c.get("full_name"),
                 "description": x.get("description") or c.get("description"), "topics": c.get("topics")}
            top5.append({"saas": sid, "category": cat, **v, "flags": audit(v, cat)})

    # integrations among published recommendations: a recommendation that is an integration is a false positive
    rec_fp = []
    for p in idx["supported_saas"]:
        page = json.loads((root / "api" / "v1" / p["path"]).read_text())
        cat = page.get("category") or layer.saas.get(p["saas_id"], {}).get("category")
        for rec in page.get("recommendations") or []:
            v = {"id": rec["oss_id"], "full_name": rec.get("repo_url", "").split("github.com/")[-1] or rec["oss_id"],
                 "description": rec.get("description") or (by_id.get(rec["oss_id"]) or {}).get("description"),
                 "topics": (by_id.get(rec["oss_id"]) or {}).get("topics")}
            flags = audit(v, cat)
            if "integration" in flags:
                rec_fp.append({"saas": p["saas_id"], **v, "flags": flags})

    # intake noise: the whole corpus and, separately, what expansion admitted
    def share(rows):
        # Integrations (MCP servers, SDKs, clients) are deliberate corpus members, labelled as such: at intake only
        # material and undescribed rows are noise. They are noise in top results, measured above.
        flags = {r["id"]: audit(r) for r in rows}
        flagged = [r for r in rows if set(flags[r["id"]]) & INTAKE_NOISE_FLAGS]
        return {"rows": len(rows), "flagged": len(flagged),
                "share": round(len(flagged) / len(rows), 4) if rows else None,
                "by_flag": _tally(f for r in flagged for f in flags[r["id"]] if f in INTAKE_NOISE_FLAGS),
                "integrations_labelled": sum(1 for r in rows if "integration" in flags[r["id"]])}

    expanded = [r for r in corpus_rows if any(p.get("strategy") == "corpus_expansion" for p in r.get("provenance") or [])]
    rnd = random.Random(sample_seed)
    sample_pool = expanded or corpus_rows
    sample = rnd.sample(sample_pool, min(sample_n, len(sample_pool)))

    def irrelevant_share(rows, flag_filter=None):
        n = len(rows)
        bad = [r for r in rows if (r["flags"] if flag_filter is None else [f for f in r["flags"] if f in flag_filter])]
        return {"rows": n, "irrelevant": len(bad), "share": round(len(bad) / n, 4) if n else None}

    return {
        "schema": "the-source.noise/1",
        "corpus_version": (common.read_json(root / "data" / "corpus" / "MANIFEST.json") or {}).get("corpus_version"),
        "corpus_members": len(corpus_rows),
        "measures": {
            "top10_discovery_irrelevant": {**irrelevant_share(top10), "queries": len(cats)},
            "top5_supported_irrelevant": {**irrelevant_share(top5), "products": len(idx["supported_saas"])},
            "top5_supported_integrations": irrelevant_share(top5, {"integration"}),
            "intake_irrelevant": share(expanded) if expanded else share(corpus_rows),
            "corpus_irrelevant": share(corpus_rows),
            "integration_false_positives_in_recommendations": {"count": len(rec_fp), "rows": rec_fp},
        },
        "intake_scope": "corpus_expansion admissions" if expanded else "whole corpus (no expansion admissions yet)",
        "rows": {"top10": top10, "top5": top5},
        "desk_review_sample": [{"id": r["id"], "full_name": r.get("full_name"), "description": r.get("description"),
                                "topics": r.get("topics")} for r in sample],
    }


def _tally(values) -> dict:
    out = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def check(result: dict, baseline: dict | None, cfg: dict) -> list[str]:
    """Measures worse than max(threshold, baseline + tolerance). Empty means the noise bound holds."""
    th, tol = cfg["thresholds"], float(cfg.get("tolerance", 0.02))
    failures = []
    for key in ("top10_discovery_irrelevant", "top5_supported_irrelevant", "intake_irrelevant"):
        got = (result["measures"].get(key) or {}).get("share")
        if got is None:
            continue
        base = ((baseline or {}).get("measures", {}).get(key) or {}).get("share")
        limit = max(float(th[key]), (base + tol) if base is not None else 0.0)
        if got > limit:
            failures.append(f"{key} {got:.3f} > bound {limit:.3f} (threshold {th[key]}, baseline {base})")
    # Recommendations: no audit flag that was not already in the baseline. Every baseline flag was reviewed by hand
    # and is the audit's own false alarm (docs/EXPANSION.md); a new one is a candidate false positive.
    got = {(r["saas"], r["id"]) for r in result["measures"]["integration_false_positives_in_recommendations"]["rows"]}
    base = {(r["saas"], r["id"]) for r in ((baseline or {}).get("measures", {})
                                           .get("integration_false_positives_in_recommendations") or {}).get("rows", [])}
    new = sorted(got - base)
    if len(new) > int(cfg.get("integration_false_positives_in_recommendations", 0)):
        failures.append(f"recommendations newly flagged as integrations: {new}")
    return failures


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="obs-noise")
    ap.add_argument("--out", type=Path, help="write the full result (rows included) here")
    ap.add_argument("--baseline", type=Path, default=BASELINE)
    ap.add_argument("--write-baseline", action="store_true", help="record this result as the baseline")
    ap.add_argument("--check", action="store_true", help="exit 1 when a measure is outside its bound")
    a = ap.parse_args(argv)
    cfg = common.load_yaml(common.CONFIG / "expansion.yaml")["noise"]
    res = measure()
    if a.out:
        common.write_json(a.out, res)
    summary = {k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in res["measures"].items()}
    print(json.dumps({"corpus_members": res["corpus_members"], "corpus_version": res["corpus_version"],
                      "intake_scope": res["intake_scope"], "measures": summary}, indent=1))
    if a.write_baseline:
        common.write_json(a.baseline, {k: v for k, v in res.items() if k not in ("rows", "desk_review_sample")})
        print(f"baseline written: {a.baseline}")
    if a.check:
        base = common.read_json(a.baseline) if a.baseline.exists() else None
        failures = check(res, base, cfg)
        for f in failures:
            print("FAIL", f)
        return 1 if failures else 0
    return 0
