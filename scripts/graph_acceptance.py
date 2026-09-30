"""Write docs/GRAPH-ACCEPTANCE.md: KEI-844 criterion by criterion, from the built graph.

Every figure is read from data/graph/, never typed by hand.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
G = ROOT / "data" / "graph"
summary = json.loads((G / "summary.json").read_text())
report = json.loads((G / "reports" / "validation_report.json").read_text())
queries = json.loads((G / "reports" / "query_results.json").read_text())
checks = {c["id"]: c for c in report["checks"]}
n = summary["counts"]


def ck(criterion):
    return "\n".join(f"- `{c['id']}` **{'PASS' if c['ok'] else 'FAIL'}** — "
                     f"`{json.dumps(c['detail'], sort_keys=True)[:240]}`"
                     for c in report["checks"] if c["criterion"] == criterion)


def fmt(x):
    return f"{x:,}" if isinstance(x, int) else str(x)


ents = "\n".join(f"| {k} | {fmt(v)} |" for k, v in n["entities"].items())
edges = "\n".join(f"| `{k}` | {fmt(v.get('explicit', 0))} | {fmt(v.get('inferred', 0))} |" for k, v in n["edges"].items())
excl = "\n".join(f"| `{k}` | {fmt(v)} |" for k, v in n["excluded_claims"].items())
qrows = "\n".join(
    f"| {r['id']} | {r['question']} | {r['kind']} | {'yes' if r['multi_hop'] else ''} | {fmt(r['total'])} | "
    f"{fmt(r['evidence_refs_resolved'])} | {'PASS' if r['ok'] else 'FAIL'} |" for r in queries["report"])


def example(qid, k=3):
    r = next(x for x in queries["report"] if x["id"] == qid)
    lines = [f"**{r['id']}: {r['question']}**", ""]
    for lim in r["limitations"]:
        lines.append(f"> Limitation reported with the result: {lim}")
    if r["limitations"]:
        lines.append("")
    for res in r["results"][:k]:
        lines.append(f"- **{res['name']}** (`{res['id']}`)")
        for e in res["evidence"]["edges"][:6]:
            src = e["sources"][0]
            lines.append(f"  - `{e['relation']}` → `{e['edge'].split('|')[-1]}` · {e['assertion']} · "
                         f"confidence {e['confidence']} · evidence `{src['ref']}`"
                         + (f" (`{src.get('field')}` = `{str(src.get('value'))[:60]}`)" if src.get("field") else ""))
        for f in res["evidence"]["facets"][:3]:
            lines.append(f"  - facet `{f['facet']}` = `{f['value']}` from `{f['ref']}` (`{f['source_field']}`)")
        for s in res["evidence"]["semantic"][:1]:
            lines.append(f"  - semantic {s['semantic_score']} ({s['model']}) on {', '.join(s['matched_terms'][:6])}")
    return "\n".join(lines)


doc = f"""# KEI-844 — acceptance map

Graph `as_of` {summary['as_of']} (the corpus clock). Schema `{summary['schema']}`.
Validation: **{sum(c['ok'] for c in report['checks'])}/{len(report['checks'])} checks pass.**
Query suite: **{queries['queries']} questions, {queries['multi_hop']} multi-hop, all {'pass' if queries['ok'] else 'NOT passing'}.**
Status: internal, not published, nothing endorsed.

Regenerate with `python3 scripts/graph_acceptance.py` after `python -m source_pipeline graph-build`.

## The graph

| Entity type | Count |
|---|---|
{ents}

| Relationship | explicit | inferred |
|---|---|---|
{edges}

Defined with no edges yet (the evidence is not collected): {', '.join(f'`{r}`' for r in n['relations_defined_without_edges']) or 'none'}.
Identity aliases resolved: {fmt(n['identity_aliases'])}. Observations: {fmt(n['observations'])}.

Claims seen and not made into edges:

| Claim : reason | Count |
|---|---|
{excl}

## Criterion by criterion

### AC1 — the retained corpus ingests deterministically
{ck('AC1')}

### AC2 — canonical identities; no obvious duplicates
{ck('AC2')}

### AC3 — relationship types defined with direction, semantics and evidence requirements
Defined in `source_pipeline/graph_model.py` and documented in `docs/GRAPH.md` (reference section generated from it).
{ck('AC3')}

### AC4 — provenance on every material edge; confidence where inferred
{ck('AC4')}

### AC5 — Replace and Discover lanes stay distinguishable
{ck('AC5')}

### AC6 — traversal across project, organisation, maintainer, category, capability, use case, technology and licence
{ck('AC6')}

### AC7 — longitudinal observations, not latest-state overwrite
{ck('AC7')}

The model keeps every observation. The current data has at most two observations per project, less than 28 days apart, so every trend is `insufficient_history`: trend questions are supported but not yet answerable, and are reported as such.

### AC8 — a retrieval representation for hybrid search
{ck('AC8')}

### AC9 — at least 20 natural-language questions exercised, including multi-hop
{ck('AC9')}

| # | Question | Kind | Multi-hop | Results | Evidence refs resolved | |
|---|---|---|---|---|---|---|
{qrows}

### AC10 — results carry evidence back to canonical source records
{ck('AC10')}

{example('q01')}

{example('q20', 2)}

{example('q11', 2)}

### AC11 — implementation-independent
{ck('AC11')}

### AC12 — KEI-806 scoring consumes graph entities, edges and confidence
{ck('AC12')}

`data/graph/views/scoring_inputs.jsonl`: one row per project. `replacement_fit_applicable` is true only where a `REPLACES` edge exists.

## Known limits (recorded, not hidden)

- `DEPENDS_ON` has no edges: dependency manifests are not read yet.
- `MAINTAINED_BY` covers only personal repositories. For organisation-owned projects the maintainers are unknown.
- Trends need a second observation at least 28 days after the first. The next verify run starts that series.
- Price ("expensive SaaS") is not in the reviewed catalogue, so no query can constrain on it yet.
- Questions are compiled into plans by hand (`config/graph_queries.yaml`). Automatic compilation is later work.
- Category near-synonyms that differ by more than form ("Password manager" vs "Password management") are separate entities. The ontology can fold them when someone reviews them.
"""
(ROOT / "docs" / "GRAPH-ACCEPTANCE.md").write_text(doc)
print("wrote docs/GRAPH-ACCEPTANCE.md")
