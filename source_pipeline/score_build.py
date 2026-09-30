"""Scoring (KEI-806) — Project Health, Replacement Fit and Evidence Confidence.

    score-build   no network. Two stages, deliberately separable:

      1. extract   evidence -> data/score/signals.jsonl
                   Every raw signal the rubric reads, per project and per SaaS pair, each
                   with its value, source, provenance ref and observation time. Unknown is
                   stored as null, never as zero.
      2. score     signals + config/scoring.yaml -> project_health.jsonl,
                   replacement_fit.jsonl, records.jsonl; appends to history/scores.jsonl.

    Rescoring after a rubric change is stage 2 alone (`rescore`): no re-harvest, no schema
    change. Refreshed evidence (a new `score-harvest --refresh`) is stage 1 + 2 with the same
    code and schema.

Three outputs, never blended (rubric rule 3). Disqualifiers sit outside the numbers
(rule 2). Scoring is automatic and is never represented as Keith-tested or endorsed (rule 8).
"""
from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timezone

from . import common

SCORE = common.DATA / "score"
GRAPH = common.DATA / "graph"
EVIDENCE = SCORE / "evidence"
CFG_PATH = common.CONFIG / "scoring.yaml"
JOBS_PATH = common.CONFIG / "saas_jobs.yaml"
OVERRIDES_PATH = common.CONFIG / "score_overrides.yaml"

MAINTAINER_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}
EXTERNAL_ASSOC = {"CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE"}

ROOT_PATTERNS = {
    "docs": re.compile(r"^(docs?|documentation|website|site|book|guide|mkdocs\.ya?ml|docusaurus\.config\.[jt]s)$", re.I),
    "examples": re.compile(r"^(examples?|samples?|demos?|example-.*|tutorials?)$", re.I),
    "contributing": re.compile(r"^contributing(\.|$)", re.I),
    "container": re.compile(r"^(dockerfile.*|containerfile|docker-compose.*\.ya?ml|compose\.ya?ml|helm|charts?|k8s|kubernetes|deploy(ment)?s?|docker|\.devcontainer)$", re.I),
    "ci_other": re.compile(r"^(\.gitlab-ci\.yml|\.circleci|\.travis\.yml|jenkinsfile|azure-pipelines\.yml|\.drone\.yml|\.woodpecker.*|appveyor\.yml|\.buildkite)$", re.I),
    "package": re.compile(r"^(package\.json|pyproject\.toml|setup\.py|setup\.cfg|cargo\.toml|go\.mod|pom\.xml|build\.gradle(\.kts)?|composer\.json|gemfile|.*\.gemspec|mix\.exs|pubspec\.yaml|.*\.csproj|.*\.sln|cmakelists\.txt|deno\.json|flake\.nix|meson\.build)$", re.I),
    "upgrade": re.compile(r"^(changelog|history|changes|upgrading|upgrade|migration|migrating|release[-_]?notes|news)(\.|$)", re.I),
    "security": re.compile(r"^security(\.|$)", re.I),
    "governance": re.compile(r"^(codeowners|governance|maintainers|code_of_conduct|code-of-conduct|owners)(\.|$)", re.I),
    "dependency": re.compile(r"^(dependabot\.ya?ml|renovate\.json5?|\.renovaterc(\.json)?)$", re.I),
    "readme": re.compile(r"^readme(\.|$)", re.I),
}
IDENTITY_README_CHARS = 2000
STABLE_VERSION = re.compile(r"(\d+)(?:\.\d+)*")


# ---------------------------------------------------------------- helpers

def parse_ts(s):
    if not s:
        return None
    return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def days_between(a, b):
    return (parse_ts(b) - parse_ts(a)).total_seconds() / 86400.0


def curve(points, x):
    if x is None:
        return None
    if x <= points[0][0]:
        return float(points[0][1])
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return float(y0 + (y1 - y0) * (x - x0) / (x1 - x0)) if x1 != x0 else float(y1)
    return float(points[-1][1])


def r1(x):
    return None if x is None else round(float(x), 1)


def term_re(term: str):
    # Separators are interchangeable: "web analytics" matches the topic slug "web-analytics"
    # and "file_sync"; "self-host" matches "self host" and "selfhost".
    t = r"[\s\-_]*".join(re.escape(part) for part in re.split(r"[\s\-_]+", term.lower()))
    # Prefix boundary always (so "ai" does not match "maintain"); a short term also needs a
    # suffix boundary, a longer one may be a stem ("collaborat" matches "collaborative").
    return re.compile(r"(?<![a-z0-9])" + t + (r"(?![a-z0-9])" if len(term.strip()) <= 3 else ""))


_TERM_CACHE: dict = {}


def has_term(text: str, term: str) -> bool:
    rx = _TERM_CACHE.get(term)
    if rx is None:
        rx = _TERM_CACHE[term] = term_re(term)
    return bool(rx.search(text))


def sig(value, source, ref, observed_at):
    return {"value": value, "source": source, "ref": ref, "observed_at": observed_at}


def load_cfg():
    return common.load_yaml(CFG_PATH)


def load_jobs():
    return common.load_yaml(JOBS_PATH)["categories"]


def load_overrides():
    if OVERRIDES_PATH.exists():
        return common.load_yaml(OVERRIDES_PATH) or {}
    return {}


# ---------------------------------------------------------------- stage 1: extract

def _entries(obj):
    return ((obj or {}).get("entries")) or []


def _licence_usability(spdx, status, cfg):
    lu = cfg["project_health"]["licence_usability"]
    if status != "recognised" or not spdx or spdx in ("NOASSERTION", "NONE", "OTHER"):
        return lu["unclear"]
    for fam in ("permissive", "copyleft", "network_copyleft"):
        if spdx in lu[fam]["ids"]:
            return lu[fam]["value"]
    return lu["other_recognised"]


def extract_project(row, meta, deep, owner_is_org, cfg):
    """Raw signals for one project. Sources, in order of preference:
    github_meta (this ticket's GraphQL pass), kei811 deep record, graph view observation."""
    key = row["canonical"]
    rec = meta.get(key) or {}
    r = rec.get("repo") or {}
    obs_at = rec.get("observed_at")
    mref = f"score:evidence/github_meta.json#{key}@{obs_at}"
    view_obs = (row.get("observations") or [{}])[-1]
    vref = view_obs.get("ref") and f"graph:views/scoring_inputs.jsonl#{row['project']}:{view_obs.get('ref')}"
    d = deep or {}
    dref = d and f"kei811:live.json#deep/{d.get('canonical_id')}"
    S = {}
    facts = {"meta_error": rec.get("error") if rec else "not_observed", "is_fork": r.get("isFork", d.get("is_fork"))}
    has_meta = bool(r)
    as_of = obs_at if has_meta else (view_obs.get("as_of") or d.get("fetched_at"))
    facts["observed_at"] = as_of

    tgt = ((r.get("defaultBranchRef") or {}).get("target")) or {}
    c90 = (tgt.get("c90") or {}).get("totalCount")
    c180 = (tgt.get("c180") or {}).get("totalCount")
    c365n = tgt.get("c365") or {}
    c365 = c365n.get("totalCount")
    cprev = (tgt.get("cprev") or {}).get("totalCount")
    nodes = c365n.get("nodes") or []
    created = r.get("createdAt") or d.get("created_at")
    age_years = days_between(created, as_of) / 365.25 if (created and as_of) else None
    facts.update({"c90": c90, "c180": c180, "c365": c365, "cprev": cprev, "created_at": created,
                  "age_years": round(age_years, 2) if age_years is not None else None})

    # --- maintenance & momentum
    last_commit = max((n["committedDate"] for n in nodes if n.get("committedDate")), default=None)
    if has_meta and tgt:
        if last_commit:
            dsl = max(0.0, days_between(last_commit, as_of))
            S["days_since_last_commit"] = sig(r1(dsl), "host_api", mref + ":defaultBranchRef.target.c365.nodes[0].committedDate", obs_at)
        elif c365 == 0:
            base = 730.0 if not cprev else 365.0
            pushed = r.get("pushedAt")
            dsl = max(base, days_between(pushed, as_of)) if pushed else base
            S["days_since_last_commit"] = sig(r1(dsl), "host_api", mref + ":defaultBranchRef.target.c365.totalCount=0", obs_at)
        S["commits_90d"] = sig(c90, "host_api", mref + ":defaultBranchRef.target.c90.totalCount", obs_at)
        S["commits_365d"] = sig(c365, "host_api", mref + ":defaultBranchRef.target.c365.totalCount", obs_at)
        if cprev:
            ratio = c365 / cprev
        elif c365 and (age_years or 0) >= 1.2:
            ratio = 1.5
        elif not c365 and not cprev and (age_years or 0) >= 1.0:
            ratio = 0.0
        else:
            ratio = None
        S["activity_ratio_yoy"] = sig(r1(ratio) if ratio is not None else None, "host_api",
                                      mref + ":c365/cprev", obs_at)
    elif view_obs:
        pushed = view_obs.get("pushed_at")
        if pushed:
            S["days_since_last_commit"] = sig(r1(max(0.0, days_between(pushed, view_obs["as_of"]))),
                                              "host_api_via_listing", vref + ":pushed_at", view_obs["as_of"])
        if view_obs.get("commits_in_12m_window") is not None:
            S["commits_365d"] = sig(view_obs["commits_in_12m_window"], "host_api_via_listing",
                                    vref + ":commits_in_12m_window", view_obs["as_of"])

    releases = r.get("releases") or {}
    rel_nodes = [n for n in (releases.get("nodes") or []) if not n.get("isDraft")]
    rel_count = releases.get("totalCount") if has_meta else None
    if rel_count is None and d:
        rel_count = len((d.get("observations") or {}).get("releases") or [])
        rel_count = rel_count or None
    latest_rel = max((n.get("publishedAt") or n.get("createdAt") for n in rel_nodes if (n.get("publishedAt") or n.get("createdAt"))), default=None)
    if not latest_rel and d:
        tags = (d.get("observations") or {}).get("tags") or []
        latest_rel = max((t["at"] for t in tags if t.get("at")), default=None)
    if latest_rel and as_of:
        S["days_since_last_release"] = sig(r1(max(0.0, days_between(latest_rel, as_of))), "host_api",
                                           (mref + ":releases.nodes") if rel_nodes else (dref + ":observations.tags"), as_of)
    facts["release_count"] = rel_count
    facts["latest_release_at"] = latest_rel

    # --- community & contributors
    logins = [((n.get("author") or {}).get("user") or {}).get("login") for n in nodes]
    logins = [x for x in logins if x]
    if logins:
        counts = {}
        for x in logins:
            counts[x] = counts.get(x, 0) + 1
        S["distinct_authors_recent"] = sig(len(counts), "host_api", mref + ":c365.nodes[].author.user.login", obs_at)
        S["top_author_share"] = sig(round(max(counts.values()) / len(logins), 3), "host_api",
                                    mref + ":c365.nodes[].author.user.login", obs_at)
    prs = ((r.get("recentPRs") or {}).get("nodes")) or []
    if len(prs) >= 3:
        ext = sum(1 for p in prs if p.get("authorAssociation") in EXTERNAL_ASSOC)
        S["external_pr_share"] = sig(round(ext / len(prs), 3), "host_api", mref + ":recentPRs.nodes[].authorAssociation", obs_at)
    stars = r.get("stargazerCount", view_obs.get("stars") if view_obs else None)
    forks = r.get("forkCount", view_obs.get("forks") if view_obs else None)
    src = "host_api" if has_meta else "host_api_via_listing"
    ref = mref if has_meta else vref
    if stars is not None:
        S["stars_log10"] = sig(round(math.log10(stars + 1), 3), src, ref + ":stargazerCount", as_of)
    if forks is not None:
        S["forks_log10"] = sig(round(math.log10(forks + 1), 3), src, ref + ":forkCount", as_of)
    facts.update({"stars": stars, "forks": forks})

    # --- issue / PR responsiveness
    issues = ((r.get("recentIssues") or {}).get("nodes")) or []
    elig = [i for i in issues if i.get("authorAssociation") not in MAINTAINER_ASSOC
            and as_of and days_between(i["createdAt"], as_of) >= 7]
    if has_meta and r.get("hasIssuesEnabled") is not False and len(elig) >= 3:
        waits = []
        for i in elig:
            first = [c["createdAt"] for c in ((i.get("comments") or {}).get("nodes") or [])
                     if c.get("authorAssociation") in MAINTAINER_ASSOC]
            if i.get("closedAt"):
                first.append(i["closedAt"])
            if first:
                waits.append(max(0.0, days_between(i["createdAt"], min(first)) * 24))
        S["responded_share"] = sig(round(len(waits) / len(elig), 3), "host_api", mref + ":recentIssues.nodes", obs_at)
        if waits:
            S["median_first_response_hours"] = sig(r1(statistics.median(waits)), "host_api", mref + ":recentIssues.nodes", obs_at)
    pr_elig = [p for p in prs if as_of and days_between(p["createdAt"], as_of) >= 7]
    if len(pr_elig) >= 3:
        done = [p for p in pr_elig if p.get("mergedAt") or p.get("closedAt")]
        S["pr_resolution_share"] = sig(round(len(done) / len(pr_elig), 3), "host_api", mref + ":recentPRs.nodes", obs_at)
        if done:
            hrs = [days_between(p["createdAt"], p.get("mergedAt") or p["closedAt"]) * 24 for p in done]
            S["median_pr_resolution_hours"] = sig(r1(statistics.median(hrs)), "host_api", mref + ":recentPRs.nodes", obs_at)
    oi = (r.get("openIssues") or {}).get("totalCount")
    ci = (r.get("closedIssues") or {}).get("totalCount")
    if oi is not None and ci is not None and (oi + ci) >= 5 and r.get("hasIssuesEnabled") is not False:
        S["open_issue_ratio"] = sig(round(oi / (oi + ci), 3), "host_api", mref + ":openIssues/closedIssues", obs_at)

    # --- maturity & continuity
    if age_years is not None:
        S["age_years"] = sig(round(age_years, 2), "host_api" if has_meta else "host_api_via_listing",
                             (mref + ":createdAt") if has_meta else (dref + ":created_at"), as_of)
    if rel_count is not None:
        S["release_count"] = sig(rel_count, "host_api", mref + ":releases.totalCount", as_of)
    stable_src = [n for n in rel_nodes if not n.get("isPrerelease")]
    if stable_src:
        m = STABLE_VERSION.search(stable_src[0].get("tagName") or "")
        if m:
            S["version_stable"] = sig(1 if int(m.group(1)) >= 1 else 0, "host_api", mref + ":releases.nodes[0].tagName", obs_at)
    if has_meta and tgt:
        if cprev is not None and (cprev > 0 or (age_years or 0) >= 1.0):
            S["active_in_prior_year"] = sig(1 if cprev > 0 else 0, "host_api", mref + ":cprev.totalCount", obs_at)

    # --- documentation, operational readiness, security & governance (from listings)
    root = _entries(r.get("root"))
    dotgh = _entries(r.get("dotgithub"))
    wf = _entries(r.get("workflows"))
    if root:
        names = [e["name"] for e in root]
        ghn = [e["name"] for e in dotgh]
        docs_names = [e["name"] for e in _entries(r.get("root")) if e.get("type") == "tree"]
        any_ = lambda pat, pool: any(ROOT_PATTERNS[pat].match(n) for n in pool)
        lref = mref + ":root.entries"
        readme = [e for e in root if e.get("type") == "blob" and ROOT_PATTERNS["readme"].match(e["name"])]
        rb = max(((e.get("object") or {}).get("byteSize") or 0) for e in readme) if readme else 0
        S["readme_bytes"] = sig(rb, "host_api", lref + "[readme].byteSize", obs_at)
        S["docs_present"] = sig(int(any_("docs", names)), "host_api", lref, obs_at)
        S["homepage_present"] = sig(int(bool((r.get("homepageUrl") or "").strip())), "host_api", mref + ":homepageUrl", obs_at)
        S["examples_present"] = sig(int(any_("examples", docs_names)), "host_api", lref, obs_at)
        S["contributing_present"] = sig(int(any_("contributing", names + ghn)), "host_api", lref + "+.github", obs_at)
        S["container_present"] = sig(int(any_("container", names)), "host_api", lref, obs_at)
        ci_count = len([n for n in (e["name"] for e in wf) if n.lower().endswith((".yml", ".yaml"))])
        if not ci_count and any_("ci_other", names):
            ci_count = 1
        S["ci_workflows"] = sig(ci_count, "host_api", mref + ":workflows.entries", obs_at)
        S["package_manifest"] = sig(int(any_("package", names)), "host_api", lref, obs_at)
        S["upgrade_path_evidence"] = sig(int(any_("upgrade", names) or (rel_count or 0) >= 3), "host_api", lref + "+releases", obs_at)
        S["security_policy"] = sig(int(bool(r.get("isSecurityPolicyEnabled")) or any_("security", names + ghn)),
                                   "host_api", mref + ":isSecurityPolicyEnabled+root/.github", obs_at)
        S["dependency_automation"] = sig(int(any_("dependency", names + ghn)), "host_api", mref + ":.github.entries", obs_at)
        S["codeowners_or_governance"] = sig(int(any_("governance", names + ghn)), "host_api", lref + "+.github", obs_at)
        facts["root_listing"] = sorted(names)
    if row.get("owner"):
        S["organisation_owned"] = sig(int(owner_is_org), "inferred_from_graph", "graph:edges.jsonl#" + row["owner"][0]["edge"],
                                      view_obs.get("as_of") or as_of)

    spdx = (r.get("licenseInfo") or {}).get("spdxId") or row["licence"].get("spdx")
    S["licence_usability"] = sig(_licence_usability(row["licence"].get("spdx") or spdx, row["licence"]["status"], cfg),
                                 "host_api_via_listing", f"graph:views/scoring_inputs.jsonl#{row['project']}:licence", view_obs.get("as_of") or as_of)
    facts.update({"licence_spdx": row["licence"].get("spdx"), "licence_status": row["licence"]["status"],
                  "archived": r.get("isArchived", view_obs.get("archived") if view_obs else d.get("archived")),
                  "disabled": r.get("isDisabled", d.get("disabled")),
                  "homepage": r.get("homepageUrl"), "description": r.get("description"),
                  "topics": sorted(t["topic"]["name"] for t in (((r.get("repositoryTopics") or {}).get("nodes")) or [])),
                  "security_policy_url": r.get("securityPolicyUrl")})

    # --- cross-source agreement: this pass against the KEI-811 REST record / graph view
    agree = []
    other = d or {}
    tol = cfg["evidence_confidence"]["agreement_tolerance"]
    if has_meta and (other or view_obs):
        o_stars = other.get("stars", view_obs.get("stars"))
        o_forks = other.get("forks", view_obs.get("forks"))
        o_arch = other.get("archived", view_obs.get("archived"))
        if o_stars is not None and stars is not None:
            agree.append(abs(stars - o_stars) <= max(5, tol["stars_rel"] * max(stars, o_stars)))
        if o_forks is not None and forks is not None:
            agree.append(abs(forks - o_forks) <= max(3, tol["forks_rel"] * max(forks, o_forks)))
        if o_arch is not None:
            agree.append(bool(o_arch) == bool(r.get("isArchived")))
        g_spdx = (r.get("licenseInfo") or {}).get("spdxId")
        if g_spdx and row["licence"].get("spdx") and g_spdx not in ("NOASSERTION", "OTHER"):
            agree.append(g_spdx == row["licence"]["spdx"])
    facts["agreement_checks"] = agree
    return {"project": row["project"], "canonical": key, "signals": S, "facts": facts}


def _text_blob(readme_rec, facts, upstream_cats, graph_terms):
    parts = [(readme_rec or {}).get("text") or "", facts.get("description") or "",
             " ".join(facts.get("topics") or []), " ".join(upstream_cats), " ".join(graph_terms)]
    return "\n".join(parts).lower()


def extract_pair(edge, proj, row, readme_rec, saas, job, upstream_cats, graph_terms, cfg):
    """Raw evidence for one SaaS pair. Stage 1 stores the TEXT the pair is judged on (identity
    text and full text) plus the structural and relationship facts; stage 2 (`match_pair`)
    does the term matching, so a change to config/saas_jobs.yaml or the term families is a
    rescore, not a re-harvest."""
    facts = proj["facts"]
    readme_ok = bool(readme_rec and readme_rec.get("text"))
    rref = f"score:evidence/readmes.json#{proj['canonical']}@{(readme_rec or {}).get('observed_at')}"
    head = ((readme_rec or {}).get("text") or "")[:IDENTITY_README_CHARS]
    ident = "\n".join([head, facts.get("description") or "", " ".join(facts.get("topics") or []),
                       " ".join(upstream_cats), " ".join(graph_terms)]).lower()
    s = proj["signals"]
    val = lambda k: (s.get(k) or {}).get("value")
    return {
        "edge": edge["id"], "project": proj["project"], "canonical": proj["canonical"],
        "saas": edge["to"], "saas_name": saas["name"], "category": saas["category"],
        "relationship": {"confidence": edge["confidence"], "status": edge["attrs"].get("kei805_status"),
                         "corroborated": edge["attrs"].get("corroborated"), "source_count": edge["attrs"].get("source_count"),
                         "recommendation_eligible": edge["attrs"].get("recommendation_eligible"),
                         "ref": "graph:edges.jsonl#" + edge["id"], "evidence": edge.get("evidence")},
        "text": {"readme_available": readme_ok, "readme_ref": rref if readme_ok else None,
                 "readme_chars": len((readme_rec or {}).get("text") or "") if readme_ok else 0,
                 "readme_observed_at": (readme_rec or {}).get("observed_at"),
                 "identity": ident, "full": _text_blob(readme_rec, facts, upstream_cats, graph_terms)},
        "structural": {k: val(k) for k in ("container_present", "docs_present", "readme_bytes", "version_stable",
                                           "release_count", "licence_usability", "homepage_present")},
    }


def match_pair(pair, job, cfg):
    """Stage 2 term matching: which job, feature and family terms the stored text evidences.
    Core groups record where a term was found (identity text vs deeper README)."""
    T = cfg["replacement_fit"]["terms"]
    text, ident = pair["text"]["full"], pair["text"]["identity"]
    hits = lambda terms, t=text: sorted({x for x in terms if has_term(t, x)})
    core_groups = [{"terms": g, "hits": hits(g), "identity_hits": hits(g, ident)} for g in job["core"]]
    name = pair["saas_name"].lower()
    incumbent = has_term(text, name) if len(name) > 2 else False
    migration = False
    if incumbent and pair["text"]["readme_available"]:
        for m in re.finditer(re.escape(name), text):
            window = text[max(0, m.start() - 120): m.end() + 120]
            if re.search(r"import|migrat|export|switch from|move from|alternative", window):
                migration = True
                break
    return {"core_groups": core_groups, "feature_hits": hits(job["features"]), "feature_total": len(job["features"]),
            "families": {k: hits(v) for k, v in T.items()}, "incumbent_mentioned": incumbent,
            "migration_from_incumbent": migration}


def rejob(pair, job, category):
    """The same pair judged against another category's job (negative control)."""
    return {**pair, "category": category, "_job": job}


_JOBS_CACHE = {}


def _job_for(pair):
    if "_job" in pair:
        return pair["_job"]
    if "jobs" not in _JOBS_CACHE:
        _JOBS_CACHE["jobs"] = load_jobs()
    return _JOBS_CACHE["jobs"][pair["category"]]


def extract(cfg=None):
    cfg = cfg or load_cfg()
    jobs = load_jobs()
    rows = common.read_jsonl(GRAPH / "views" / "scoring_inputs.jsonl")
    meta_doc = common.read_json(EVIDENCE / "github_meta.json")
    meta = meta_doc["records"]
    readmes = common.read_json(EVIDENCE / "readmes.json")["records"] if (EVIDENCE / "readmes.json").exists() else {}
    deep_all = common.read_json(common.DATA / "discover" / "live.json")["deep"]
    deep = {v["canonical_id"]: v for v in deep_all.values() if v.get("canonical_id")}
    saas = {f"saas:{s['saas_id']}": s for s in common.read_jsonl(common.DATASET / "saas_products.jsonl")}
    upstream = {}
    for o in common.read_jsonl(common.DATASET / "oss_projects.jsonl"):
        upstream[o["oss_id"]] = sorted({c["value"] for c in (o.get("imported") or {}).get("categories") or []})
    edges = {}
    for line in open(GRAPH / "edges.jsonl", encoding="utf-8"):
        e = common.json.loads(line)
        if e["relation"] == "REPLACES":
            edges[e["id"]] = e
    projects, pairs = [], []
    for row in rows:
        owner_is_org = bool(row.get("owner")) and row["owner"][0]["to"].startswith("org:")
        p = extract_project(row, meta, deep.get(row["canonical"]), owner_is_org, cfg)
        projects.append(p)
        graph_terms = [c["to"].split(":", 1)[1].replace("-", " ") for c in row["capabilities"] + row["use_cases"]]
        for rep in row["replaces"]:
            e = edges[rep["edge"]]
            s = saas[e["to"]]
            pairs.append(extract_pair(e, p, row, readmes.get(row["canonical"]), s, jobs.get(s["category"]),
                                      upstream.get(row["canonical"], []), graph_terms, cfg)
                         if jobs.get(s["category"]) else {"edge": e["id"], "missing_job": s["category"]})
    return {"evidence_as_of": meta_doc["as_of"], "projects": projects, "pairs": pairs,
            "view_rows": {r["project"]: r for r in rows}}


# ---------------------------------------------------------------- stage 2: score

def _band(score, cfg):
    for b in cfg["project_health"]["bands"]:
        if score >= b["min"]:
            return b["label"]
    return cfg["project_health"]["bands"][-1]["label"]


def score_health(p, cfg):
    dims = {}
    total_w = known_w = acc = 0.0
    for dname, dim in cfg["project_health"]["dimensions"].items():
        sw = sk = sacc = 0.0
        sigs = {}
        for sname, sc in dim["signals"].items():
            v = (p["signals"].get(sname) or {}).get("value")
            pts = curve(sc["curve"], v)
            sigs[sname] = {"value": v, "points": r1(pts), "weight": sc["weight"]}
            sw += sc["weight"]
            if pts is not None:
                sk += sc["weight"]
                sacc += sc["weight"] * pts
        dscore = sacc / sk if sk else None
        share = sk / sw
        dims[dname] = {"score": r1(dscore), "weight": dim["weight"], "known_share": round(share, 3), "signals": sigs}
        total_w += dim["weight"]
        known_w += dim["weight"] * share
        if dscore is not None:
            acc += dim["weight"] * dscore
    denom = sum(d["weight"] for d in dims.values() if d["score"] is not None)
    score = r1(acc / denom) if denom else None  # the band is read from the number as displayed
    ks = known_w / total_w
    return {"score": score, "band": _band(score, cfg) if score is not None else None,
            "known_weight_share": round(ks, 3),
            "evidence_insufficient": ks < cfg["project_health"]["min_known_weight_share"],
            "dimensions": dims}


def disqualifiers(p, cfg, overrides):
    f, s = p["facts"], p["signals"]
    out = []
    q = cfg["disqualifiers"]
    if f.get("meta_error") == "not_found":
        out.append({"id": "not_found", "reason": q["not_found"], "evidence": f"score:evidence/github_meta.json#{p['canonical']}"})
    if f.get("archived"):
        out.append({"id": "archived", "reason": q["archived"], "evidence": f"score:evidence/github_meta.json#{p['canonical']}:isArchived"})
    if f.get("disabled"):
        out.append({"id": "disabled", "reason": q["disabled"], "evidence": f"score:evidence/github_meta.json#{p['canonical']}:isDisabled"})
    if f.get("licence_status") != "recognised":
        out.append({"id": "licence_unclear", "reason": q["licence_unclear"], "evidence": s["licence_usability"]["ref"]})
    dsl = (s.get("days_since_last_commit") or {}).get("value")
    if (f.get("c365") == 0 and f.get("cprev") == 0) or (dsl is not None and dsl >= 730):
        out.append({"id": "demonstrably_dead", "reason": q["demonstrably_dead"],
                    "evidence": (s.get("days_since_last_commit") or {}).get("ref")})
    for o in (overrides.get("security_concern") or []):
        if o.get("project") in (p["project"], p["canonical"]):
            out.append({"id": "security_concern", "reason": o.get("reason") or q["security_concern"], "evidence": o.get("evidence")})
    return out


def watch_outs(p, cfg, readme_text=""):
    w, s, f = cfg["watch_outs"], p["signals"], p["facts"]
    v = lambda k: (s.get(k) or {}).get("value")
    out = []
    if (v("top_author_share") or 0) >= w["single_maintainer_risk"]["top_author_share_min"] and \
            (v("distinct_authors_recent") or 99) <= w["single_maintainer_risk"]["distinct_authors_max"]:
        out.append({"id": "single_maintainer_risk", "detail": f"{v('distinct_authors_recent')} recent commit author(s); top author {int(v('top_author_share') * 100)}% of recent commits",
                    "evidence": s["top_author_share"]["ref"]})
    if (v("median_first_response_hours") or 0) >= w["slow_responses"]["median_first_response_hours_min"]:
        out.append({"id": "slow_responses", "detail": f"median first maintainer response {round(v('median_first_response_hours') / 24)} days",
                    "evidence": s["median_first_response_hours"]["ref"]})
    if v("release_count") == 0:
        out.append({"id": "no_releases", "detail": "no published releases", "evidence": s["release_count"]["ref"]})
    if v("version_stable") == 0:
        out.append({"id": "pre_1_0", "detail": "latest stable release is 0.x", "evidence": s["version_stable"]["ref"]})
    if f.get("is_fork"):
        out.append({"id": "fork_of_another", "detail": "repository is a fork", "evidence": f"score:evidence/github_meta.json#{p['canonical']}:isFork"})
    if readme_text:
        hits = [t for t in w["open_core_markers"]["readme_terms"] if t in readme_text]
        if hits:
            out.append({"id": "open_core_markers", "detail": "README mentions " + ", ".join(sorted(hits)),
                        "evidence": f"score:evidence/readmes.json#{p['canonical']}"})
    return out


def trajectory(p, history_rows, cfg):
    t, f = cfg["trajectory"], p["facts"]
    c180, c365, cprev = f.get("c180"), f.get("c365"), f.get("cprev")
    basis = {"method": "dated host commit history (half-year and year-on-year windows)"}
    stars_basis = None
    obs = sorted((h for h in history_rows if h.get("stars") is not None), key=lambda h: h["observed_at"])
    if len(obs) >= 2 and days_between(obs[0]["observed_at"], obs[-1]["observed_at"]) >= t["min_observation_span_days"]:
        span = days_between(obs[0]["observed_at"], obs[-1]["observed_at"])
        stars_basis = {"first": obs[0]["stars"], "last": obs[-1]["stars"], "span_days": r1(span),
                       "per_30d": r1((obs[-1]["stars"] - obs[0]["stars"]) * 30 / span)}
    if c365 is None or cprev is None or c180 is None:
        return {"label": "Insufficient history", "basis": {**basis, "reason": "no dated commit history observed"}, "stars": stars_basis}
    events = c365 + cprev
    if events < t["min_events_24m"]:
        return {"label": "Insufficient history", "basis": {**basis, "commits_24m": events,
                "reason": f"fewer than {t['min_events_24m']} commits in 24 months"}, "stars": stars_basis}
    # A window is compared only when the project existed through all of it: a repository
    # younger than a year has an empty prior year by construction, which is not growth.
    age = f.get("age_years")
    ratios = []
    prev_half = c365 - c180
    if age is not None and age >= t["min_age_years_half"]:
        ratios.append(c180 / prev_half if prev_half > 0 else (2.0 if c180 > 0 else 1.0))
    if age is not None and age >= t["min_age_years_yoy"]:
        ratios.append(c365 / cprev if cprev > 0 else (2.0 if c365 > 0 else 1.0))
    if not ratios:
        return {"label": "Insufficient history", "basis": {**basis, "age_years": age,
                "reason": f"younger than {t['min_age_years_half']} years: no complete prior window"}, "stars": stars_basis}
    ratio = math.exp(sum(math.log(max(x, 0.01)) for x in ratios) / len(ratios)) if ratios else 0.0
    label = "Rising" if ratio >= t["rising_ratio"] else "Declining" if ratio <= t["declining_ratio"] else "Stable"
    return {"label": label, "basis": {**basis, "commits_last_180d": c180, "commits_prior_180d": prev_half,
                                      "commits_last_365d": c365, "commits_prior_365d": cprev, "ratio": round(ratio, 3)},
            "stars": stars_basis}


def _confidence(components, cfg):
    w = cfg["evidence_confidence"]["weights"]
    num = sum(w[k] * v for k, v in components.items() if v is not None)
    den = sum(w[k] for k, v in components.items() if v is not None)
    return r1(num / den) if den else None


def health_confidence(p, health, evaluated_at, cfg):
    ec = cfg["evidence_confidence"]
    q = ec["source_quality"]
    srcs = [q.get(s["source"], 50) for s in p["signals"].values() if s.get("value") is not None]
    obs_at = p["facts"].get("observed_at")
    fresh = curve(ec["freshness_curve"], max(0.0, days_between(obs_at, evaluated_at))) if obs_at else None
    agree = p["facts"].get("agreement_checks") or []
    comps = {"completeness": health["known_weight_share"] * 100, "freshness": fresh,
             "source_quality": sum(srcs) / len(srcs) if srcs else None,
             "relationship_confidence": None,  # not applicable to Project Health
             "cross_source_agreement": 100.0 * sum(agree) / len(agree) if agree else None}
    return {"score": _confidence(comps, cfg), "components": {k: r1(v) for k, v in comps.items()},
            "not_applicable": ["relationship_confidence"]}


def score_pair(pair, proj_health, disq, evaluated_at, cfg, job=None):
    rf = cfg["replacement_fit"]
    pair = {**pair, **match_pair(pair, job or _job_for(pair), cfg)}
    T = pair["families"]
    st = pair["structural"]
    readme = pair["text"]["readme_available"]
    # Text-only evidence. A term that is present counts. A term that is ABSENT counts as
    # evidence of absence only when the README is substantial documentation: a short README
    # that defers to an external docs site says nothing about what it does not mention, and
    # rule 1 forbids reading crawler reach as a project weakness. Unknown, not zero.
    full_doc = readme and (pair["text"].get("readme_chars") or 0) >= rf["text_absence_min_chars"]
    ind = lambda present: 100.0 if present else (0.0 if full_doc else None)
    mean = lambda xs: (sum(x for x in xs if x is not None) / len([x for x in xs if x is not None])) if any(x is not None for x in xs) else None
    groups = pair["core_groups"]
    gate = rf["core_gate"]
    matched = sum(1 for g in groups if g["hits"])
    credit = lambda g: 1.0 if g["identity_hits"] else (gate["deep_readme_credit"] if g["hits"] else 0.0)
    core = 100.0 * sum(credit(g) for g in groups) / len(groups) if groups else None
    if not readme and matched == 0:
        core = None  # absence in a one-line description is not evidence of absence
    # The defining group (what the product IS) must be in the project's own identity text.
    defining_ok = bool(groups and groups[0]["identity_hits"])
    feat_share = len(pair["feature_hits"]) / pair["feature_total"] if pair["feature_total"] else None
    feat = curve([[0, 0], [0.3, 50], [0.6, 85], [0.8, 100]], feat_share) if (readme or pair["feature_hits"]) else None
    if feat is not None and not full_doc and not pair["feature_hits"]:
        feat = None
    lic = st.get("licence_usability")
    dims = {
        "core_use_case_coverage": core,
        "feature_equivalence": feat,
        "ease_of_adoption": mean([ind(bool(T["quickstart"])), ind(bool(T["demo"])),
                                  None if st.get("container_present") is None else 100.0 * st["container_present"],
                                  None if st.get("docs_present") is None else 100.0 * st["docs_present"],
                                  curve([[0, 0], [2000, 50], [8000, 100]], st.get("readme_bytes"))]),
        "deployment_ownership": mean([100.0 if (T["self_host"] or st.get("container_present")) else (0.0 if full_doc else None),
                                      ind(bool(T["cloud_option"])),
                                      ind(bool(T["data_ownership"])),
                                      None if lic is None else 100.0 * lic]),
        "migration_friction": mean([ind(bool(T["import"])), ind(pair["migration_from_incumbent"]),
                                    ind(bool(T["api"]))]),
        "integrations_extensibility": mean([ind(bool(T["api"])), ind(bool(T["webhook"])),
                                            ind(bool(T["plugins"])), ind(bool(T["sso"]))]),
        "production_commercial_readiness": mean([ind(bool(T["multi_user"])), ind(bool(T["sso"])), ind(bool(T["support"])),
                                                 None if st.get("version_stable") is None else 100.0 * st["version_stable"],
                                                 None if st.get("release_count") is None else (100.0 if st["release_count"] > 0 else 0.0)]),
    }
    self_host = bool(T["self_host"] or st.get("container_present"))
    econ = rf["economic"]
    if readme or st.get("container_present") is not None:
        base = econ["base_self_hostable"] if self_host else econ["base_hosted_only"]
        dims["economic_advantage"] = max(0.0, base - (econ["open_core_penalty"] if pair.get("open_core") else 0)) * (lic if lic is not None else 1)
    else:
        dims["economic_advantage"] = None
    weights = {k: v["weight"] for k, v in rf["dimensions"].items()}
    known = {k: v for k, v in dims.items() if v is not None}
    den = sum(weights[k] for k in known)
    raw = sum(weights[k] * v for k, v in known.items()) / den if den else None
    gate_state = "passed"
    score = raw
    if core is None:
        gate_state = "unproven"
    elif core < gate["gate_min"] or (gate["defining_group_required"] and not defining_ok):
        gate_state = "failed"
    if gate_state != "passed" and score is not None:
        score = min(score, gate["gate_cap"])
    # --- confidence
    ec = cfg["evidence_confidence"]
    q = ec["source_quality"]
    rel = pair["relationship"]
    rel_conf = 100.0 * (rel["confidence"] or 0)
    if rel.get("corroborated"):
        rel_conf = min(100.0, rel_conf + 10)
    sc = rel.get("source_count") or 1
    agreement = {1: 40.0, 2: 70.0}.get(sc, 100.0)
    if pair["incumbent_mentioned"]:
        agreement = min(100.0, agreement + 20)
    ro = pair["text"].get("readme_observed_at")
    comps = {"completeness": 100.0 * sum(weights[k] for k in known) / sum(weights.values()),
             "freshness": curve(ec["freshness_curve"], max(0.0, days_between(ro, evaluated_at))) if ro else None,
             "source_quality": (q["readme_text"] + q["host_api"]) / 2 if readme else q["upstream_list_claim"],
             "relationship_confidence": rel_conf, "cross_source_agreement": agreement}
    conf = _confidence(comps, cfg)
    th = cfg["thresholds"]
    ca, pm = th["credible_alternative"], th["publish_mapping"]
    credible = (score is not None and score >= ca["replacement_fit_min"] and core is not None
                and core >= ca["core_coverage_min"] and (conf or 0) >= ca["confidence_min"] and not disq)
    refusals = []
    if (conf or 0) < pm["confidence_min"]:
        refusals.append(f"evidence confidence {conf} below {pm['confidence_min']}")
    if rel.get("status") not in pm["relationship_statuses"]:
        refusals.append(f"relationship status '{rel.get('status')}' is not publishable")
    if disq and pm["requires_no_disqualifier"]:
        refusals.append("project carries a hard disqualifier: " + ", ".join(d["id"] for d in disq))
    if pm.get("requires_core_gate_passed") and gate_state != "passed":
        refusals.append(f"core use case not evidenced (core gate {gate_state})")
    hl = th["high_score_low_confidence"]
    label = hl["label"] if (score or 0) >= hl["score_min"] and (conf or 0) < hl["confidence_below"] else None
    return {
        "edge": pair["edge"], "project": pair["project"], "canonical": pair["canonical"], "saas": pair["saas"],
        "saas_name": pair["saas_name"], "category": pair["category"],
        "score": r1(score), "uncapped_score": r1(raw), "core_gate": gate_state,
        "dimensions": {k: {"score": r1(v), "weight": weights[k]} for k, v in dims.items()},
        "core_coverage": {"score": r1(core), "defining_group_in_identity": defining_ok,
                          "groups": [{**g, "credit": credit(g)} for g in pair["core_groups"]]},
        "feature_hits": pair["feature_hits"],
        "confidence": {"score": conf, "components": {k: r1(v) for k, v in comps.items()}},
        "credible_alternative": credible, "publish": not refusals, "refusal_reasons": refusals,
        "presentation_label": label,
        "project_health_ref": {"score": proj_health.get("score"), "band": proj_health.get("band")},
        "relationship": {k: rel[k] for k in ("confidence", "status", "corroborated", "source_count", "ref")},
        "evidence": {"readme": pair["text"]["readme_ref"], "relationship": rel["ref"],
                     "incumbent_mentioned": pair["incumbent_mentioned"],
                     "migration_from_incumbent": pair["migration_from_incumbent"],
                     "families": {k: v for k, v in pair["families"].items() if v}},
    }


def why_health(p, health):
    s = p["signals"]
    v = lambda k: (s.get(k) or {}).get("value")
    ref = lambda k: (s.get(k) or {}).get("ref")
    out = []
    if v("days_since_last_commit") is not None:
        out.append({"text": f"Last commit {int(v('days_since_last_commit'))} days before evaluation; "
                            f"{v('commits_90d') if v('commits_90d') is not None else 'unknown'} commits in 90 days",
                    "evidence": ref("days_since_last_commit")})
    if v("distinct_authors_recent") is not None:
        out.append({"text": f"{v('distinct_authors_recent')} distinct commit authors in the last 12 months (of the latest 100 commits)",
                    "evidence": ref("distinct_authors_recent")})
    if v("median_first_response_hours") is not None:
        h = v("median_first_response_hours")
        out.append({"text": f"Median first maintainer response {h:.0f} hours across recent issues"
                            f" ({int(100 * (v('responded_share') or 0))}% answered)", "evidence": ref("median_first_response_hours")})
    if v("release_count") is not None:
        out.append({"text": f"{v('release_count')} published releases; project age {v('age_years')} years",
                    "evidence": ref("release_count")})
    if v("container_present") is not None:
        bits = [n for n, k in (("container/deploy files", "container_present"), ("CI workflows", "ci_workflows"),
                               ("security policy", "security_policy"), ("docs", "docs_present")) if v(k)]
        if bits:
            out.append({"text": "Repository has " + ", ".join(bits), "evidence": ref("container_present")})
    return out


def score_all(ext, cfg, history_obs=None, overrides=None):
    overrides = overrides if overrides is not None else load_overrides()
    evaluated_at = ext["evidence_as_of"]
    history_obs = history_obs or {}
    readme_text = {}
    rpath = EVIDENCE / "readmes.json"
    if rpath.exists():
        for k, v in common.read_json(rpath)["records"].items():
            readme_text[k] = (v.get("text") or "").lower()
    health_rows, records, fit_rows = [], [], []
    by_project = {}
    for p in ext["projects"]:
        h = score_health(p, cfg)
        dq = disqualifiers(p, cfg, overrides)
        wo = watch_outs(p, cfg, readme_text.get(p["canonical"], ""))
        tr = trajectory(p, history_obs.get(p["canonical"], []), cfg)
        conf = health_confidence(p, h, evaluated_at, cfg)
        by_project[p["project"]] = (p, h, dq, wo, tr, conf)
        health_rows.append({"project": p["project"], "canonical": p["canonical"], "score_version": cfg["version"],
                            "evaluated_at": evaluated_at, **h, "trajectory": tr, "confidence": conf,
                            "disqualifiers": dq, "watch_outs": wo})
    for pair in ext["pairs"]:
        if pair.get("missing_job"):
            continue
        p, h, dq, wo, tr, conf = by_project[pair["project"]]
        pair["open_core"] = any(w["id"] == "open_core_markers" for w in wo)
        fr = score_pair(pair, h, dq, evaluated_at, cfg)
        fr.update({"score_version": cfg["version"], "evaluated_at": evaluated_at})
        fit_rows.append(fr)
    fits_by_project = {}
    for fr in fit_rows:
        fits_by_project.setdefault(fr["project"], []).append(fr)
    th = cfg["thresholds"]["recommendation_eligible"]
    hl = cfg["thresholds"]["high_score_low_confidence"]
    for pid, (p, h, dq, wo, tr, conf) in by_project.items():
        row = ext["view_rows"][pid]
        reasons = []
        if (h["score"] or 0) < th["health_min"]:
            reasons.append(f"Project Health {h['score']} below {th['health_min']}")
        if (conf["score"] or 0) < th["health_confidence_min"]:
            reasons.append(f"Evidence Confidence {conf['score']} below {th['health_confidence_min']}")
        if dq:
            reasons.append("hard disqualifier: " + ", ".join(d["id"] for d in dq))
        if th["requires_graph_eligibility"] and not row["recommendation_eligible"]:
            reasons.append(f"graph outcome '{row['outcome']}' (licence/lane) is not recommendation-eligible")
        label = hl["label"] if (h["score"] or 0) >= hl["score_min"] and (conf["score"] or 0) < hl["confidence_below"] else None
        fits = sorted(fits_by_project.get(pid, []), key=lambda f: f["saas"])
        records.append({
            "project": pid, "canonical": p["canonical"], "name": row["name"], "url": row["url"],
            "lanes": row["lanes"], "score_version": cfg["version"], "evaluated_at": evaluated_at,
            "endorsement": cfg["endorsement"],
            "project_health": {"score": h["score"], "band": h["band"], "evidence_insufficient": h["evidence_insufficient"],
                               "band_note": cfg["project_health"]["band_note"]},
            "trajectory": tr["label"],
            "evidence_confidence": conf["score"],
            "presentation_label": label,
            "replacement_fit": [{"saas": f["saas"], "saas_name": f["saas_name"], "score": f["score"],
                                 "core_gate": f["core_gate"], "confidence": f["confidence"]["score"],
                                 "credible_alternative": f["credible_alternative"], "publish": f["publish"],
                                 "refusal_reasons": f["refusal_reasons"], "presentation_label": f["presentation_label"]}
                                for f in fits],
            "replacement_fit_applicable": bool(fits),
            "why": why_health(p, h),
            "disqualifiers": dq, "watch_outs": wo,
            "recommendation_eligible": not reasons, "recommendation_blockers": reasons,
            "evidence": {"signals": f"score:signals.jsonl#{pid}", "health": f"score:project_health.jsonl#{pid}",
                         "replacement_fit": [f"score:replacement_fit.jsonl#{f['edge']}" for f in fits]},
        })
    return health_rows, fit_rows, records


# ---------------------------------------------------------------- orchestration

def _history_obs():
    out = {}
    path = SCORE / "history" / "observations.jsonl"
    if path.exists():
        for h in common.read_jsonl(path):
            out.setdefault(h["project"], []).append(h)
    return out


def _append_history(cfg, evaluated_at, health_rows, fit_rows):
    path = SCORE / "history" / "scores.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = set()
    if path.exists():
        for h in common.read_jsonl(path):
            seen.add((h["project"], h["score_version"], h["evidence_as_of"]))
    fits = {}
    for f in fit_rows:
        fits.setdefault(f["project"], {})[f["saas"]] = {"score": f["score"], "confidence": f["confidence"]["score"]}
    added = 0
    with open(path, "a", encoding="utf-8") as fh:
        for h in health_rows:
            k = (h["project"], cfg["version"], evaluated_at)
            if k in seen:
                continue
            fh.write(common.dumps({"project": h["project"], "score_version": cfg["version"], "evidence_as_of": evaluated_at,
                                   "health": h["score"], "band": h["band"], "confidence": h["confidence"]["score"],
                                   "trajectory": h["trajectory"]["label"],
                                   "disqualifiers": [d["id"] for d in h["disqualifiers"]],
                                   "replacement_fit": fits.get(h["project"], {})}) + "\n")
            added += 1
    return added


def write_scores(ext, cfg, out_dir=SCORE, history=True):
    health_rows, fit_rows, records = score_all(ext, cfg, _history_obs())
    common.write_jsonl(out_dir / "project_health.jsonl", health_rows)
    common.write_jsonl(out_dir / "replacement_fit.jsonl", sorted(fit_rows, key=lambda f: f["edge"]))
    common.write_jsonl(out_dir / "records.jsonl", records)
    added = _append_history(cfg, ext["evidence_as_of"], health_rows, fit_rows) if history else 0
    return health_rows, fit_rows, records, added


def rescore(cfg=None, out_dir=SCORE, history=True):
    """Stage 2 only: stored signals + rubric -> scores. No evidence is re-read."""
    cfg = cfg or load_cfg()
    doc = common.read_json(SCORE / "signals_meta.json")
    ext = {"evidence_as_of": doc["evidence_as_of"],
           "projects": common.read_jsonl(SCORE / "signals.jsonl"),
           "pairs": common.read_jsonl(SCORE / "pair_signals.jsonl"),
           "view_rows": {r["project"]: r for r in common.read_jsonl(GRAPH / "views" / "scoring_inputs.jsonl")}}
    return write_scores(ext, cfg, out_dir, history)


def run():
    cfg = load_cfg()
    ext = extract(cfg)
    common.write_jsonl(SCORE / "signals.jsonl", ext["projects"])
    common.write_jsonl(SCORE / "pair_signals.jsonl", ext["pairs"])
    common.write_json(SCORE / "signals_meta.json", {"evidence_as_of": ext["evidence_as_of"],
                                                    "graph_view_sha256": common.sha256_file(GRAPH / "views" / "scoring_inputs.jsonl")})
    health_rows, fit_rows, records, added = write_scores(ext, cfg)
    bands = {}
    for h in health_rows:
        bands[h["band"]] = bands.get(h["band"], 0) + 1
    traj = {}
    for h in health_rows:
        traj[h["trajectory"]["label"]] = traj.get(h["trajectory"]["label"], 0) + 1
    counts = {"projects_scored": len(health_rows), "pairs_scored": len(fit_rows),
              "pairs_missing_job": sum(1 for p in ext["pairs"] if p.get("missing_job")),
              "bands": bands, "trajectory": traj,
              "credible_alternatives": sum(1 for f in fit_rows if f["credible_alternative"]),
              "mappings_publishable": sum(1 for f in fit_rows if f["publish"]),
              "recommendation_eligible": sum(1 for r in records if r["recommendation_eligible"]),
              "disqualified": sum(1 for h in health_rows if h["disqualifiers"]),
              "history_rows_added": added, "score_version": cfg["version"], "evidence_as_of": ext["evidence_as_of"]}
    common.write_json(SCORE / "summary.json", counts)
    return {"counts": counts}
