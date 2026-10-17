"""Edition check for open-core corpus rows: can the core be self-hosted without the excluded parts?

licence_classify.py labels a repository `open_core` when a recognised open licence covers the
core and every part under other terms is delimited (directories, or file-header markers). That
made the row `conditional` with follow-up `edition_check_required_before_recommendation`, which
until now meant a human reading the repository and hand-writing config/licence_exceptions.yaml.

This module does that reading. At the commit the classification was made (so the excluded parts
are the ones the classification named), it confirms three things:

  1. core licence   the classification is open_core with a core SPDX and named excluded parts
                    (already established by licence_classify; re-checked, never re-derived);
  2. core-only path a self-hosting deployment in the repository (docker-compose, or a Helm chart)
                    that runs the project itself and does not need an excluded part: no build
                    context, Dockerfile, volume, env file or chart path inside an excluded path,
                    no image, value or switch carrying an enterprise marker, and no Dockerfile it
                    builds that copies an excluded path explicitly;
  3. disclosure     what the excluded parts cover (each path, why it is excluded, what it holds),
                    so a recommendation can say what it does not cover.

Outcomes, failing closed:
    pass       all three hold: the row becomes eligible (core only), and a licence-exception entry
               is generated for it (config/licence_exceptions.generated.yaml) with this provenance;
    fail       the repository shows that every deployment it ships needs an excluded part;
    ambiguous  anything that cannot be established: the repository or a deployment file cannot be
               read, the deployment lives outside the repository, or no deployment runs the project.
fail and ambiguous keep the row `conditional`, with a reason code.

Provenance has the shape of the licence classification's: {source: {api, files}, commit, checked_at}.
The check never relaxes anything else: recommendation still needs the canonical build, the KEI-805
quality bar and the scoring gate, unchanged.
"""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import yaml

from . import common
from . import licence_classify as lc

METHOD = "the-source.edition-check/1"
RESULTS = ("pass", "fail", "ambiguous")
APPROVER = "edition-check"
GENERATED = common.CONFIG / "licence_exceptions.generated.yaml"
CACHE = common.RAW / "edition_check"
LIST_DEPTH = 3
DEPLOY_DIR = re.compile(r"^\.?(docker|deploy|deployment|deployments|helm|charts?|k8s|kubernetes|compose|install|"
                        r"self[-_]?host(ed|ing)?|ops|infra|containers?|\.docker|.+[-_]docker|docker[-_].+)$", re.I)
COMPOSE = re.compile(r"^(docker-)?compose([.-][\w.-]+)?\.ya?ml$", re.I)
NOT_DEPLOYMENT = re.compile(r"(^|[./_-])(ci|test|tests|e2e|testing|monitoring)([./_-]|$)", re.I)
# Development, debugging, test-harness and demo stacks: recorded, never the self-hosting path that passes.
DEV = re.compile(r"(^|[./_-])(dev|devel|develop|development|local|debug|bdd|bench|benchmark|demo|sample|samples|"
                 r"example|examples|override|playground|sandbox|devcontainer|hack|tools?)([./_-]|$)", re.I)
MARKER = re.compile(r"(enterprise|commercial|premium|proprietary|(^|[-_./:])(ee|pro)($|[-_./:]))", re.I)
# The file names a self-hosting stack usually has, preferred when reporting which deployment passed.
PRIMARY = re.compile(r"^((docker-)?compose(\.(prod|production|selfhost|self-host|hobby|oss))?\.ya?ml|chart\.yaml)$", re.I)
FALSY = {"", "false", "0", "no", "off", "none", "null"}
MAX_FILES = 30
# Where Reader.at keeps the files that carry each excluded-edition header marker (GitHub code search).
MARKER_FILES_KEY = "__marker_files__"


def _rel(base: str, ref: str) -> str | None:
    """A path a deployment file names, relative to the repository root; None when it is not a path."""
    if not isinstance(ref, str) or not ref or "://" in ref or ref.startswith(("$", "~", "/")):
        return None
    parts = []
    for part in PurePosixPath(base, ref.split(":", 1)[0]).parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part not in (".", ""):
            parts.append(part)
    return "/".join(parts)


def _inside(path: str | None, excluded: list[str]) -> str | None:
    if not path:
        return None
    return next((x for x in excluded if path == x or path.startswith(x + "/")), None)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _app_names(slug: str) -> set[str]:
    """What the project's own image is called: the repository name (and a bare owner name, the
    usual org/app pattern), never the owner alone, so `acme/postgres-dev` is not mistaken for it."""
    owner, name = slug.lower().split("/", 1)
    return {n for n in (_norm(name),) if len(n) >= 4} or {_norm(owner)}


def resolve_image(image: str) -> str | None:
    """An image reference with compose variables resolved to their defaults; None when one has no default."""
    def sub(m):
        d = re.match(r"^[A-Za-z_][A-Za-z0-9_]*(:?-)(.*)$", m.group(1))
        if not d:
            raise KeyError(m.group(1))
        return d.group(2)
    try:
        prev = None
        while prev != image:
            prev, image = image, re.sub(r"\$\{([^${}]*)\}", sub, image)
    except KeyError:
        return None
    return None if "$" in image else image


def image_name(image: str) -> str | None:
    """The image's own name (`twenty` in `twentycrm/twenty:${TAG}`): registry, namespace and tag are dropped,
    so a variable there does not hide it; a variable in the name itself must have a default."""
    image = image.split("@")[0]
    if re.fullmatch(r"\$\{[^${}]*\}", image):  # the whole reference is one variable: use its default
        image = resolve_image(image) or ""
    last = re.split(r"/(?![^{]*\})", image)[-1]
    last = re.sub(r":(?![^{]*\}).*$", "", last)
    return resolve_image(last) or None


def runs_image(image: str, slug: str) -> bool:
    repo = image_name(image)
    if not repo:
        return False
    return any(n in _norm(repo) for n in _app_names(slug))


def _truthy(v) -> bool:
    return v is not None and str(v).strip().lower() not in FALSY


def _env_items(env):
    if isinstance(env, dict):
        return list(env.items())
    if isinstance(env, list):
        return [tuple(str(e).split("=", 1)) if "=" in str(e) else (str(e), None) for e in env]
    return []


def _copies(dockerfile: str, base: str, excluded: list[str]) -> list[str]:
    hits = []
    for line in dockerfile.splitlines():
        m = re.match(r"^\s*(COPY|ADD)\s+(.+)$", line, re.I)
        if not m:
            continue
        args = [a for a in m.group(2).split() if not a.startswith("--")]
        for src in args[:-1]:
            hit = _inside(_rel(base, src), excluded)
            if hit:
                hits.append(f"Dockerfile copies {src} (inside excluded {hit})")
    return hits


def development(path: str) -> bool:
    """A development, test or demo stack, or a file inside the source tree rather than at the root or under a
    deployment directory (docker/, deploy/, charts/, ...): recorded, never the path that passes."""
    dirs = path.split("/")[:-1]
    return bool(DEV.search(path)) or not (not dirs or any(DEPLOY_DIR.match(d) for d in dirs))


def compose_unit(path: str, text: str, excluded: list[str], slug: str, texts: dict) -> dict:
    """One docker-compose file: does it run the project, and does it need an excluded part?"""
    base = str(PurePosixPath(path).parent) if "/" in path else ""
    unit = {"path": path, "kind": "docker_compose", "development": development(path), "services": [],
            "runs_project": False, "hits": [], "notes": [], "files": [path]}
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        unit["error"] = f"unparseable YAML: {str(exc).splitlines()[0][:120]}"
        return unit
    services = (doc or {}).get("services") if isinstance(doc, dict) else None
    if not isinstance(services, dict):
        unit["error"] = "no services: block"
        return unit
    if MARKER.search(path):
        unit["hits"].append(f"{path}: the file itself is named for an enterprise edition")
    for sname, svc in sorted(services.items()):
        if not isinstance(svc, dict):
            continue
        image = svc.get("image") if isinstance(svc.get("image"), str) else None
        build = svc.get("build")
        ctx, dockerfile = (build, None) if isinstance(build, str) else \
            ((build or {}).get("context"), (build or {}).get("dockerfile")) if isinstance(build, dict) else (None, None)
        runs = bool(build) or bool(image and runs_image(image, slug))
        unit["services"].append({"name": sname, "image": image, "build": bool(build), "runs_project": runs})
        unit["runs_project"] |= runs
        if image and MARKER.search(image_name(image) or ""):
            unit["hits"].append(f"service {sname}: image {image} carries an enterprise marker")
        ctx_path = _rel(base, ctx) if ctx else None
        for what, ref, rel_to in (("build context", ctx, base),
                                  ("Dockerfile", dockerfile, ctx_path if ctx_path is not None else base)):
            hit = _inside(_rel(rel_to, ref) if ref else None, excluded)
            if hit:
                unit["hits"].append(f"service {sname}: {what} {ref} is inside excluded {hit}")
        if build:
            df = _rel(ctx_path if ctx_path is not None else base, dockerfile or "Dockerfile")
            if df in texts:
                unit["files"].append(df)
                unit["hits"] += [f"service {sname}: {h}" for h in _copies(texts[df], ctx_path or "", excluded)]
                if re.search(r"^\s*COPY\s+\.\s", texts[df], re.M):
                    unit["notes"].append(f"{df} copies the whole build context; the excluded parts are in the "
                                         "image but the deployment does not name them")
            else:
                unit["notes"].append(f"service {sname}: Dockerfile {df} not read")
        vols = svc.get("volumes") or []
        for v in vols if isinstance(vols, list) else []:
            src = v.get("source") if isinstance(v, dict) else str(v).split(":", 1)[0] if ":" in str(v) else None
            hit = _inside(_rel(base, src) if src and src.startswith(".") else None, excluded)
            if hit:
                unit["hits"].append(f"service {sname}: volume {src} is inside excluded {hit}")
        env_files = svc.get("env_file") or []
        for ef in [env_files] if isinstance(env_files, str) else env_files if isinstance(env_files, list) else []:
            hit = _inside(_rel(base, ef if isinstance(ef, str) else (ef or {}).get("path")), excluded)
            if hit:
                unit["hits"].append(f"service {sname}: env_file {ef} is inside excluded {hit}")
        for k, v in _env_items(svc.get("environment")):
            if MARKER.search(str(k)) and _truthy(v) and not str(v).startswith("${"):
                unit["hits"].append(f"service {sname}: environment {k} switches on an enterprise feature")
    return unit


def _walk(node, key=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, str(k))
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)
    else:
        yield key, node


def helm_unit(chart_path: str, texts: dict, excluded: list[str], slug: str) -> dict:
    base = str(PurePosixPath(chart_path).parent)
    unit = {"path": base, "kind": "helm_chart", "development": development(chart_path), "services": [],
            "runs_project": False, "hits": [], "notes": [], "files": [chart_path]}
    hit = _inside(base, excluded)
    if hit:
        unit["hits"].append(f"chart {base} is inside excluded {hit}")
    try:
        chart = yaml.safe_load(texts[chart_path]) or {}
    except yaml.YAMLError:
        unit["error"] = "unparseable Chart.yaml"
        return unit
    names = _app_names(slug)
    cname = _norm(str((chart or {}).get("name") or ""))
    if MARKER.search(base):
        unit["hits"].append(f"{base}: the chart is named for an enterprise edition")
    values_path = f"{base}/values.yaml"
    values = {}
    if values_path in texts:
        unit["files"].append(values_path)
        try:
            values = yaml.safe_load(texts[values_path]) or {}
        except yaml.YAMLError:
            unit["error"] = "unparseable values.yaml"
            return unit
    else:
        unit["notes"].append(f"{values_path} not read")
    images = [str(v) for k, v in _walk(values) if k in ("repository", "image") and isinstance(v, str)]
    unit["services"] = [{"name": cname or base, "image": i} for i in images[:10]]
    unit["runs_project"] = any(n in cname for n in names) or any(runs_image(i, slug) for i in images)
    for i in images:
        if MARKER.search(image_name(i) or ""):
            unit["hits"].append(f"values: image {i} carries an enterprise marker")
    for k, v in _walk(values):
        if MARKER.search(k) and isinstance(v, bool) and v:
            unit["hits"].append(f"values: {k} is switched on")
    return unit


def excluded_scope(cls: dict, paths: list[tuple[str, str]], texts: dict) -> list[dict]:
    """What each excluded part covers, from the classification's reasons and the tree itself."""
    why = {e["path"]: e["why"] for e in (cls.get("evidence") or {}).get("excluded_paths") or []}
    out = []
    for x in (cls.get("excluded") or {}).get("paths") or []:
        children = sorted({p[len(x) + 1:].split("/")[0] for p, _ in paths if p.startswith(x + "/")})
        readme = next((texts[p] for p in sorted(texts) if p.lower().startswith((x + "/readme").lower())), None)
        summary = None
        if readme:
            line = next((ln.strip("# ").strip() for ln in readme.splitlines() if ln.strip().strip("#").strip()), None)
            summary = (line or "")[:200] or None
        out.append({"path": x, "why": why.get(x) or "named by the licence classification as under other terms",
                    "contains": children[:15], "readme_summary": summary})
    for mk in (cls.get("excluded") or {}).get("markers") or []:
        found = (texts.get(MARKER_FILES_KEY) or {}).get(mk) if isinstance(texts.get(MARKER_FILES_KEY), dict) else None
        out.append({"marker": mk, "why": "files carrying this header are under the enterprise terms",
                    "contains": sorted({f.split("/")[-2] if "/" in f else f for f in found or []})[:40],
                    "files": (found or [])[:1000], "files_listed": found is not None,
                    "readme_summary": None})
    return out


def disclosure(core_spdx: str, scope: list[dict]) -> str:
    parts = [s["path"] + "/" for s in scope if s.get("path")] + [s["marker"] for s in scope if s.get("marker")]
    return (f"open core: recommendation covers the {core_spdx} core; enterprise features excluded"
            + (f" ({', '.join(parts)})" if parts else ""))


def check(row: dict, rec: dict | None, checked_at: str) -> dict:
    """Pure: the edition check for one corpus row, given what was read at the classification commit."""
    cls = row["licence"].get("classification") or {}
    commit = (cls.get("provenance") or {}).get("commit")
    files_read = sorted(k for k in ((rec or {}).get("texts") or {}) if k != MARKER_FILES_KEY)
    prov = {"source": {"api": "github: GraphQL tree at the classification commit (3 levels; deployment "
                              "directories two more) + Blob.text", "files": files_read},
            "commit": commit, "checked_at": (rec or {}).get("fetched_at") or checked_at}
    out = {"method": METHOD, "result": "ambiguous", "reason_code": None, "reason": None,
           "core_spdx": cls.get("core_spdx"), "deployment": [], "excluded_scope": [], "disclosure": None,
           "provenance": prov}

    def done(result, code, reason):
        out.update(result=result, reason_code=code, reason=reason)
        return out

    excl = cls.get("excluded") or {}
    if cls.get("class") != "open_core" or not cls.get("core_spdx") or not (excl.get("paths") or excl.get("markers")):
        return done("ambiguous", "not_classified_open_core", "the licence classification does not name an open "
                                                            "core and its excluded parts")
    if not commit:
        return done("ambiguous", "no_classification_commit", "the classification carries no commit to read at")
    if rec is None or not rec.get("found") or rec.get("commit") != commit:
        return done("ambiguous", "repository_unreadable",
                    f"the repository could not be read at {commit[:12]}: {(rec or {}).get('error') or 'not read'}")
    paths = [tuple(p) for p in rec.get("paths") or []]
    texts = rec.get("texts") or {}
    excluded = list(excl.get("paths") or [])
    out["excluded_scope"] = excluded_scope(cls, paths, texts)
    out["disclosure"] = disclosure(cls["core_spdx"], out["excluded_scope"])
    slug = rec["slug"]
    units = []
    for p in deployment_files(paths):
        if p not in texts:
            units.append({"path": p, "kind": "helm_chart" if p.endswith("Chart.yaml") else "docker_compose",
                          "error": "file not read", "hits": [], "runs_project": False, "services": [], "notes": [],
                          "files": [p], "development": development(p)})
        elif p.split("/")[-1].lower() == "chart.yaml":
            units.append(helm_unit(p, texts, excluded, slug))
        else:
            unit = compose_unit(p, texts[p], excluded, slug, texts)
            hit = _inside(p, excluded)
            if hit:
                unit["hits"].append(f"{p} is inside excluded {hit}")
            units.append(unit)
    out["deployment"] = units
    if rec.get("list_depth", LIST_DEPTH) < LIST_DEPTH:
        return done("ambiguous", "tree_truncated", "the repository tree could only be read "
                                                   f"{rec.get('list_depth')} level(s) deep")
    runners = [u for u in units if u["runs_project"] and not u.get("error") and not u["development"]]
    clean = [u for u in runners if not u["hits"]]
    if clean:
        best = sorted(clean, key=lambda u: (not PRIMARY.match(u["files"][0].split("/")[-1]),
                                            u["kind"] != "docker_compose", u["path"].count("/"), u["path"]))[0]
        out["core_deployment"] = best["path"]
        return done("pass", "core_only_deployment",
                    f"{best['path']} ({best['kind'].replace('_', ' ')}) runs the project without the excluded parts")
    if runners:
        return done("fail", "deployment_requires_excluded_parts",
                    "every self-hosting deployment in the repository that runs the project needs an excluded part: "
                    + "; ".join(h for u in runners for h in u["hits"])[:600])
    if any(u["runs_project"] and not u.get("error") for u in units):
        return done("ambiguous", "only_development_deployments",
                    "the only deployments that run the project are development, test or demo stacks: "
                    + ", ".join(u["path"] for u in units if u["runs_project"])[:400])
    if any(u.get("error") for u in units):
        return done("ambiguous", "deployment_file_unreadable", "; ".join(f"{u['path']}: {u['error']}"
                                                                         for u in units if u.get("error"))[:600])
    if units:
        return done("ambiguous", "no_deployment_runs_the_project",
                    f"{len(units)} deployment file(s) found, none runs the project itself (dependencies only)")
    return done("ambiguous", "no_deployment_files_in_repository",
                "no docker-compose file or Helm chart in the repository; any deployment lives elsewhere")


def deployment_files(paths: list[tuple[str, str]]) -> list[str]:
    out = []
    for p, k in paths:
        if k != "blob":
            continue
        parts = p.split("/")
        name = parts[-1]
        if any(x.lower() in lc.NON_RUNTIME_DIRS or (x.startswith(".") and x != ".docker") for x in parts[:-1]):
            continue
        if NOT_DEPLOYMENT.search(p):
            continue
        if COMPOSE.match(name) and len(parts) <= 5:
            out.append(p)
        elif name.lower() == "chart.yaml" and len(parts) <= 6:
            out.append(p)
    return sorted(out)[:12]


# ---------------------------------------------------------------- network


class Reader(lc.Fetcher):
    """Reads one repository at a given commit: the tree (deployment directories deeper), the deployment
    files, the Dockerfiles they build, Helm values, and READMEs of the excluded directories."""

    def __init__(self, api=None, cache: Path = CACHE):
        super().__init__(api, cache)

    def at(self, slug: str, commit: str, excluded: list[str], use_cache: bool = True) -> dict:
        cp = self.cache / (slug.replace("/", "__") + f"@{commit[:12]}.json")
        if use_cache and cp.exists():
            return common.read_json(cp)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rec = {"slug": slug, "found": False, "commit": None, "error": None, "fetched_at": now, "paths": [],
               "texts": {}, "list_depth": LIST_DEPTH}
        for depth in range(LIST_DEPTH, 0, -1):
            _, repo, err = self._repo(slug, f"object(expression:{json.dumps(commit + ':')}){{{lc._entries(depth)}}}",
                                      tries=2 if depth > 1 else 4)
            if not (err and err.startswith(("graphql_http", "graphql_error"))):
                break
        rec["list_depth"] = depth
        if err or not (repo or {}).get("object"):
            rec["error"] = err or "commit_not_found"
            return self._save(cp, rec, err)
        rec["found"], rec["commit"] = True, commit
        paths = lc._flatten(repo["object"].get("entries"))
        # Deployment directories at the listed depth, and excluded directories, opened two levels more.
        deeper = sorted({p for p, k in paths if k == "tree" and p.count("/") >= depth - 1 and (
            DEPLOY_DIR.match(p.split("/")[-1]) or any(a for a in p.split("/") if DEPLOY_DIR.match(a)))
                         and not NOT_DEPLOYMENT.search(p)})[:12]
        deeper += [x for x in excluded if x not in deeper and any(p == x and k == "tree" for p, k in paths)]
        if deeper:
            fields = [f"d{i}: object(expression:{json.dumps(commit + ':' + d)}){{{lc._entries(2)}}}"
                      for i, d in enumerate(deeper)]
            _, got, err = self._repo(slug, " ".join(fields))
            have = set(paths)
            for i, d in enumerate(deeper):
                for p, k in lc._flatten(((got or {}).get(f"d{i}") or {}).get("entries"), d + "/"):
                    if (p, k) not in have:
                        paths.append((p, k))
                        have.add((p, k))
        want = deployment_files(paths)
        dirs = {str(PurePosixPath(p).parent) for p in want}
        blobs = {p for p, k in paths if k == "blob"}
        want += [f"{d}/values.yaml" for d in dirs if f"{d}/values.yaml" in blobs and any(
            w.endswith("Chart.yaml") and w.startswith(d + "/") for w in want)]
        want += [p for p in sorted(blobs) if p.split("/")[-1].lower().startswith("dockerfile")
                 and str(PurePosixPath(p).parent) in dirs | {"", "."} | {d.split("/")[0] for d in dirs}]
        want += [p for p in sorted(blobs) for x in excluded
                 if p.lower() in (f"{x}/readme.md".lower(), f"{x}/readme".lower())]
        want = list(dict.fromkeys(want))[:MAX_FILES]
        if want:
            fields = [f"f{i}: object(expression:{json.dumps(commit + ':' + p)}){{... on Blob{{text isBinary}}}}"
                      for i, p in enumerate(want)]
            _, got, err = self._repo(slug, " ".join(fields))
            if err:
                rec["error"] = f"blobs_{err}"
            for i, p in enumerate(want):
                o = (got or {}).get(f"f{i}")
                if o and o.get("text") is not None and not o.get("isBinary"):
                    rec["texts"][p] = o["text"][:200000]
        # Dockerfiles named by a compose build block that the first pass did not reach.
        extra = []
        for p in [w for w in want if COMPOSE.match(w.split("/")[-1]) and w in rec["texts"]]:
            try:
                svcs = (yaml.safe_load(rec["texts"][p]) or {}).get("services") or {}
            except (yaml.YAMLError, AttributeError):
                continue
            base = str(PurePosixPath(p).parent) if "/" in p else ""
            for svc in svcs.values() if isinstance(svcs, dict) else []:
                b = (svc or {}).get("build") if isinstance(svc, dict) else None
                ctx, df = (b, None) if isinstance(b, str) else ((b or {}).get("context"), (b or {}).get("dockerfile")) \
                    if isinstance(b, dict) else (None, None)
                if b:
                    cpath = _rel(base, ctx) if ctx else base
                    target = _rel(cpath if cpath is not None else base, df or "Dockerfile")
                    if target and target in blobs and target not in rec["texts"]:
                        extra.append(target)
        if extra:
            extra = sorted(set(extra))[:10]
            fields = [f"f{i}: object(expression:{json.dumps(commit + ':' + p)}){{... on Blob{{text isBinary}}}}"
                      for i, p in enumerate(extra)]
            _, got, _ = self._repo(slug, " ".join(fields))
            for i, p in enumerate(extra):
                o = (got or {}).get(f"f{i}")
                if o and o.get("text") is not None and not o.get("isBinary"):
                    rec["texts"][p] = o["text"][:200000]
        rec["paths"] = sorted(paths)
        return self._save(cp, rec)

    def marker_files(self, slug: str, markers: list[str], rec: dict) -> dict:
        """The files carrying each header marker, from GitHub code search (default branch, at checked_at):
        what a marker-delimited enterprise edition covers. Not cached with the tree; None when search fails."""
        from urllib.parse import urlencode

        out = {}
        for mk in markers:
            words = re.sub(r"[/*]+", " ", mk).strip()
            files, total = [], None
            for page in range(1, 11):  # code search serves at most 1,000 results
                st, doc = self.api.rest("search/code?" + urlencode(
                    {"q": f'"{words}" repo:{slug}', "per_page": 100, "page": page}))
                if st != 200 or not isinstance(doc, dict):
                    files = None
                    break
                total = doc.get("total_count")
                items = doc.get("items") or []
                files += [i["path"] for i in items]
                if len(items) < 100 or len(files) >= (total or 0):
                    break
            # an incomplete list cannot say what the edition excludes: treated as unlisted (fail closed)
            out[mk] = sorted(set(files)) if files is not None and not (doc or {}).get("incomplete_results") \
                and total is not None and len(set(files)) >= total else None
        rec.setdefault("texts", {})[MARKER_FILES_KEY] = out
        return out


# ---------------------------------------------------------------- the corpus pass


def _scope_text(scope: list[dict]) -> str:
    bits = []
    for x in scope:
        bits += [x.get("path") or "", *(x.get("contains") or []), *(x.get("files") or []), x.get("readme_summary") or ""]
    return " " + re.sub(r"[^a-z0-9]+", " ", " ".join(bits).lower()) + " "


def _hit(term: str, text: str) -> bool:
    t = re.sub(r"[^a-z0-9]+", " ", term.lower()).strip()
    return bool(t) and f" {t}" in text


def pair_rights(scope: list[dict] | None, job: dict | None) -> dict:
    """Does replacing a product with this open-core project rely on its excluded parts?

    The job is the product category's (config/saas_jobs.yaml): its defining group says what the product IS. If a
    defining phrase names an excluded part, the recommended functionality is enterprise-only: `enterprise_job`.
    Matching breadth features are listed (they narrow the disclosure) but do not block. If the excluded parts
    cannot be read (a marker whose files could not be listed) or the category has no job, `unknown`. Only
    `covered` lets the pair be served. Fail closed."""
    if not job or not job.get("core"):
        return {"status": "unknown", "reason": "the product category has no job definition", "core_hits": [],
                "feature_hits": []}
    if scope is None or any(x.get("marker") and not x.get("files_listed") for x in scope):
        return {"status": "unknown", "reason": "what the excluded parts cover could not be listed", "core_hits": [],
                "feature_hits": []}
    text = _scope_text(scope)
    core = sorted({t for t in job["core"][0] if _hit(t, text)})
    feats = sorted({t for t in job.get("features") or [] if _hit(t, text)})
    if core:
        return {"status": "enterprise_job", "reason": f"the excluded parts name the product's job: {', '.join(core)}",
                "core_hits": core, "feature_hits": feats}
    return {"status": "covered", "reason": "the excluded parts name none of the product's defining job"
            + (f"; enterprise-only features: {', '.join(feats)}" if feats else ""), "core_hits": [],
            "feature_hits": feats}


def generated_entry(row: dict, res: dict) -> dict:
    prov = res["provenance"]
    return {"oss_id": row["id"], "kind": "edition_check",
            "licence_as_found": f"{res['core_spdx']} core; excluded: "
                                + ", ".join(s.get("path") or s.get("marker") for s in res["excluded_scope"]),
            "approved_by": APPROVER, "approval_ref": f"{METHOD}@{prov['commit']}",
            "reviewed_at": prov["checked_at"][:10], "rationale": res["reason"], "disclosure": res["disclosure"],
            "core_spdx": res["core_spdx"], "excluded_scope": res["excluded_scope"],
            "edition_check": {"method": METHOD, "commit": prov["commit"], "files": prov["source"]["files"],
                              "checked_at": prov["checked_at"], "core_deployment": res.get("core_deployment")}}


def write_generated(entries: list[dict], path: Path = GENERATED) -> None:
    head = ("# GENERATED by `python -m source_pipeline corpus-edition-check` (source_pipeline/edition_check.py).\n"
            "# Do not edit by hand: the next run rewrites it. Each entry is an open-core project whose automated\n"
            "# edition check passed at the commit named: a self-hosting deployment in the repository runs the\n"
            "# core without the excluded parts. A recommendation of such a project always carries `disclosure`.\n"
            "# Hand-approved exceptions stay in config/licence_exceptions.yaml.\n")
    body = yaml.safe_dump({"schema": "the-source.licence-exceptions-generated/1",
                           "exceptions": sorted(entries, key=lambda e: e["oss_id"])},
                          sort_keys=False, allow_unicode=True, width=110)
    path.write_text(head + body, encoding="utf-8")


def relationship_claims() -> dict[str, list[str]]:
    """SaaS products each licence-exception-lane project is listed as replacing in the KEI-805 dataset, from
    licence-cleared, non-share-alike sources only (the export guard's own source rules)."""
    ds = common.DATASET
    if not (ds / "relationships.jsonl").is_file():
        return {}
    sources = {s["source_id"]: s for s in common.read_json(ds / "sources.json")}
    out: dict[str, set] = {}
    for e in common.read_jsonl(ds / "relationships.jsonl"):
        if e.get("lane") != "licence_exception" or e.get("share_alike_only"):
            continue
        if any(sources.get(s, {}).get("decision") not in ("allow", "allow_with_conditions") for s in e["source_ids"]):
            continue
        out.setdefault(e["oss_id"].lower(), set()).add(e["saas_id"])
    return {k: sorted(v) for k, v in out.items()}


def apply(row: dict, res: dict, claims: dict) -> None:
    """Write the check onto a corpus row's licence and, on pass, make the row eligible core-only."""
    lic = row["licence"]
    lic["edition_check"] = res
    e = dict(lic["eligibility"])
    if res["result"] == "pass":
        e.update(status="eligible", condition=res["disclosure"], follow_up=None, basis="edition_check",
                 reason_code=None)
    else:
        e.update(status="conditional", condition=lc.OPEN_CORE_CONDITION, follow_up=lc.OPEN_CORE_FOLLOW_UP,
                 basis="edition_check", reason_code=res["reason_code"])
    lic["eligibility"] = e
    if claims.get(row["id"]):
        lic["listed_alternative_to"] = claims[row["id"]]


def run(out: Path | None = None, workers: int = 6, api=None, generated: Path | None = None) -> dict:
    from . import corpus
    from .discover import log

    out = out or corpus.OUT
    m = common.read_json(out / "MANIFEST.json")
    if corpus.validate(out)["failed_checks"]:
        raise corpus.CorpusError("data/corpus does not validate before the edition check")
    projects = common.read_jsonl(out / "projects.jsonl")
    live = common.read_jsonl(out / "live.jsonl")
    rows = [p for p in projects if ((p["licence"].get("classification") or {}).get("class") == "open_core")]
    reader = Reader(api)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    full = {r["id"]: (r.get("full_name") or "").lower() for r in live}

    def read(p):
        cls = p["licence"]["classification"]
        commit = (cls.get("provenance") or {}).get("commit")
        if not commit or not p["id"].startswith("github.com/"):
            return None
        slug = full.get(p["id"]) or p["full_name"].lower()
        rec = reader.at(slug, commit, (cls.get("excluded") or {}).get("paths") or [])
        markers = (cls.get("excluded") or {}).get("markers") or []
        if markers and rec.get("found") and MARKER_FILES_KEY not in (rec.get("texts") or {}):
            reader.marker_files(slug, markers, rec)
        return rec

    log(f"edition-check: reading {len(rows)} open-core repositories at their classification commits")
    with ThreadPoolExecutor(workers) as pool:
        recs = list(pool.map(read, rows))
    claims = relationship_claims()
    tally, entries, report = {r: 0 for r in RESULTS}, [], []
    for p, rec in zip(rows, recs):
        res = check(p, rec, stamp)
        apply(p, res, claims)
        tally[res["result"]] += 1
        report.append({"id": p["id"], "result": res["result"], "reason_code": res["reason_code"],
                       "reason": res["reason"], "core_deployment": res.get("core_deployment"),
                       "disclosure": res["disclosure"]})
        if res["result"] == "pass":
            entries.append(generated_entry(p, res))
    write_generated(entries, generated or GENERATED)
    extra = {k: m[k] for k in ("membership", "publication", "refresh", "licence_classification") if k in m}
    extra["edition_check"] = {"method": METHOD, "run_at": stamp, "scope": "rows classified open_core",
                              "results": tally, "generated_exceptions": "config/licence_exceptions.generated.yaml",
                              "rules": "source_pipeline/edition_check.py; docs/LICENCE-EDITION-CHECK.md"}
    manifest = corpus.write_layer(out, projects, live, extra)
    return {"checked": len(rows), "results": tally, "corpus_version": manifest["corpus_version"],
            "projects": sorted(report, key=lambda r: (r["result"], r["id"]))}


def main() -> int:
    from . import corpus

    try:
        print(json.dumps(run(), indent=2))
    except corpus.CorpusError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    return 0
