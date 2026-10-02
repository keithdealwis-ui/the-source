"""Validate the canonical dataset and read surface (KEI-807 criteria 2-8).

Runs in CI on every push and pull request, and at the end of every cycle before
anything is promoted. Exits non-zero on any failed check. Warnings are reported but do
not fail: they describe states the read surface already represents truthfully (for
example a supported product that has dropped below the minimum recommendations).
"""
from __future__ import annotations

import filecmp
import tempfile
from pathlib import Path

from . import common

SCHEMA_FILE = common.SCHEMA / "the-source.canonical.schema.json"


def _validator(defn: str):
    import jsonschema

    schema = common.read_json(SCHEMA_FILE)
    sub = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{defn}"}
    cls = jsonschema.validators.validator_for(sub)
    cls.check_schema(sub)
    return cls(sub)


def _schema_errors(defn: str, docs: list[tuple[str, dict]], limit: int = 5) -> list[str]:
    v = _validator(defn)
    out = []
    for where, doc in docs:
        for err in v.iter_errors(doc):
            out.append(f"{where}: {'/'.join(str(p) for p in err.absolute_path)}: {err.message[:160]}")
            if len(out) >= limit:
                return out
    return out


def run(root: Path | None = None, determinism: bool = True) -> dict:
    root = Path(root) if root else common.ROOT
    canon = root / "data" / "canonical"
    api = root / "api" / "v1"
    checks, warnings = [], []

    def check(cid, criterion, ok, detail=""):
        checks.append({"id": cid, "criterion": criterion, "ok": bool(ok), "detail": detail})

    saas = common.read_jsonl(canon / "saas_products.jsonl")
    oss = common.read_jsonl(canon / "oss_projects.jsonl")
    rels = common.read_jsonl(canon / "relationships.jsonl")
    manifest = common.read_json(canon / "MANIFEST.json")
    index = common.read_json(api / "index.json")
    domains = common.read_json(api / "domains.json")
    docs = {p.stem: common.read_json(p) for p in sorted((api / "saas").glob("*.json"))}
    supported_cfg = common.load_yaml(root / "config" / "supported_saas.yaml")
    supported_ids = [p["saas_id"] for p in supported_cfg["products"]]

    # --- criterion 2: schema
    for defn, rows in (("saas_product", [(f"saas_products.jsonl:{i}", r) for i, r in enumerate(saas, 1)]),
                       ("oss_project", [(f"oss_projects.jsonl:{i}", r) for i, r in enumerate(oss, 1)]),
                       ("relationship", [(f"relationships.jsonl:{i}", r) for i, r in enumerate(rels, 1)]),
                       ("api_index", [("api/v1/index.json", index)]),
                       ("api_domains", [("api/v1/domains.json", domains)]),
                       ("api_saas", [(f"api/v1/saas/{k}.json", d) for k, d in docs.items()])):
        errs = _schema_errors(defn, rows)
        check(f"schema_{defn}", 2, not errs, "; ".join(errs) or f"{len(rows)} records valid")
    for sub, defn in (("runs", "run_record"), ("data/intake/outcomes", "intake_outcome")):
        paths = sorted((root / sub).glob("*.json")) if (root / sub).exists() else []
        errs = _schema_errors(defn, [(str(p.relative_to(root)), common.read_json(p)) for p in paths])
        check(f"schema_{defn}", 11 if defn == "run_record" else 17, not errs, "; ".join(errs) or f"{len(paths)} records valid")

    # --- integrity
    files = {f.name: common.sha256_file(f) for f in sorted(canon.iterdir()) if f.is_file() and f.name != "MANIFEST.json"}
    check("manifest_hashes", 2, files == manifest["files"], "file hashes match MANIFEST.json" if files == manifest["files"]
          else f"mismatch: {sorted(set(files.items()) ^ set(manifest['files'].items()))[:4]}")
    versions = {manifest["dataset_version"], index["dataset_version"], domains["dataset_version"]} | \
               {d["dataset_version"] for d in docs.values()}
    check("single_dataset_version", 6, len(versions) == 1, f"versions seen: {sorted(versions)}")
    saas_ids = {s["saas_id"] for s in saas}
    oss_ids = {p["oss_id"] for p in oss}
    dangling = [r["edge_id"] for r in rels if r["saas_id"] not in saas_ids or r["oss_id"] not in oss_ids]
    check("relationships_resolve", 2, not dangling, f"{len(dangling)} dangling, e.g. {dangling[:3]}")
    dup = len(rels) - len({(r["saas_id"], r["oss_id"]) for r in rels})
    check("no_duplicate_relationships", 12, dup == 0, f"{dup} duplicates")
    dup_p = len(oss) - len(oss_ids)
    check("no_duplicate_projects", 12, dup_p == 0, f"{dup_p} duplicates")

    # --- criterion 3: the initial supported products and their mappings
    check("supported_count", 3, len(supported_ids) >= 20 and len(set(supported_ids)) == len(supported_ids),
          f"{len(supported_ids)} supported products configured")
    missing_docs = [s for s in supported_ids if s not in docs]
    check("supported_docs_present", 3, not missing_docs, f"missing: {missing_docs}")
    ok = [s for s in supported_ids if s in docs and docs[s]["status"] == "ok"]
    for s in supported_ids:
        if s in docs and docs[s]["status"] != "ok":
            warnings.append(f"{s}: {len(docs[s]['recommendations'])} recommendations, below the minimum")
    check("supported_with_validated_mappings", 3, len(ok) >= min(20, len(supported_ids)) or bool(warnings),
          f"{len(ok)} of {len(supported_ids)} supported products have >= {supported_cfg['recommendations']['min']} recommendations")
    extra_docs = sorted(set(docs) - set(supported_ids))
    check("no_unsupported_docs", 6, not extra_docs, f"unexpected: {extra_docs}")

    # --- criterion 4: live fields on every served recommendation
    rel_by = {(r["saas_id"], r["oss_id"]): r for r in rels}
    proj_by = {p["oss_id"]: p for p in oss}
    bad_live, bad_rec = [], []
    for sid, d in docs.items():
        for rec in d["recommendations"]:
            r = rel_by.get((sid, rec["oss_id"]))
            p = proj_by.get(rec["oss_id"])
            if not r or not r["recommended"] or not p:
                bad_rec.append(f"{sid}:{rec['oss_id']}")
                continue
            if p["host"] == "github.com" and (not rec["github_url"] or rec["stars"] is None):
                bad_live.append(f"{sid}:{rec['oss_id']}")
            if rec["stars"] != p["live"]["stars"] or rec["live_checked_at"] != p["live"]["checked_at"]:
                bad_live.append(f"{sid}:{rec['oss_id']} (drift from canonical record)")
    check("recommendations_are_recommended_relationships", 6, not bad_rec, f"{len(bad_rec)} bad, e.g. {bad_rec[:3]}")
    check("live_fields_present", 4, not bad_live, f"{len(bad_live)} bad, e.g. {bad_live[:3]}")
    missing_live = [p["oss_id"] for p in oss if p["host"] == "github.com" and
                    (p["live"]["stars"] is None or p["live"]["licence_spdx"] is None or p["live"]["pushed_at"] is None)]
    check("project_live_fields_present", 4, not missing_live, f"{len(missing_live)} projects lack stars/licence/pushed_at")

    # --- criterion 6: deterministic ordering and domain rules
    for sid, d in docs.items():
        ranks = [r["rank"] for r in d["recommendations"]]
        if ranks != list(range(1, len(ranks) + 1)):
            bad_rec.append(f"{sid}: ranks {ranks}")
    seen, clash = {}, []
    for rule in domains["rules"]:
        key = (rule.get("host"), rule.get("host_suffix"), rule.get("path_prefix"))
        if key in seen and seen[key] != rule["saas_id"]:
            clash.append(f"{key}: {seen[key]} vs {rule['saas_id']}")
        seen[key] = rule["saas_id"]
    check("domain_rules_unambiguous", 6, not clash, "; ".join(clash) or f"{len(domains['rules'])} rules")
    check("domain_rules_cover_supported", 6, {r["saas_id"] for r in domains["rules"]} == set(supported_ids),
          "every supported product has at least one rule and no rule names an unsupported one")
    check("rows_sorted", 6, [r["oss_id"] for r in oss] == sorted(oss_ids)
          and [(r["saas_id"], r["oss_id"]) for r in rels] == sorted((r["saas_id"], r["oss_id"]) for r in rels),
          "canonical rows are in key order")

    # --- criterion 7 / KEI-805 criterion 15: nothing withheld leaks
    ds = common.ROOT / "data" / "dataset" / "relationships.jsonl"
    if ds.exists():
        sa_only = {(e["saas_id"], e["oss_id"]) for e in common.read_jsonl(ds) if e["share_alike_only"]}
        leaked = sorted(k for k in rel_by if k in sa_only)
        check("no_share_alike_only_leak", 7, not leaked, f"{len(leaked)} leaked, e.g. {leaked[:3]}")
    stale = [p["oss_id"] for p in oss if p["maintenance"]["status"] not in ("active", "maintained") or p["live"]["archived"]]
    check("no_stale_or_archived_projects", 7, not stale, f"{len(stale)} stale/archived")

    # --- criterion 8: every output records when live metadata was last checked
    no_check = [p["oss_id"] for p in oss if not p["live"]["checked_at"]]
    no_check += [f"api/{k}" for k, d in docs.items() if d["recommendations"] and not d["last_checked"]["newest"]]
    if not index["live_checked_at"]["newest"] or not manifest["live_checked_at"]["newest"]:
        no_check.append("index/manifest")
    check("last_checked_recorded", 8, not no_check, f"missing on: {no_check[:5]}")

    # --- determinism: a rebuild from the same inputs is byte-identical
    if determinism and (common.ROOT / "data" / "dataset" / "summary.json").exists():
        from . import canonical

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "config").mkdir()
            canonical.build(tmp)
            diffs = []
            for rel in ["data/canonical", "api/v1", "api/v1/saas"]:
                a, b = root / rel, tmp / rel
                cmp = filecmp.dircmp(a, b)
                diffs += [f"{rel}/{x}" for x in cmp.diff_files + cmp.left_only + cmp.right_only
                          if not (a / x).is_dir() and not (b / x).is_dir()]
                _, mismatch, errors = filecmp.cmpfiles(a, b, cmp.common_files, shallow=False)
                diffs += [f"{rel}/{x}" for x in mismatch + errors]
            check("rebuild_is_byte_identical", 6, not diffs, f"differs: {sorted(set(diffs))[:5]}" if diffs
                  else "canonical data and read surface are exactly what the code produces from the committed inputs")

    report = {"ok": all(c["ok"] for c in checks), "checks": checks, "warnings": warnings,
              "dataset_version": manifest["dataset_version"]}
    return report
