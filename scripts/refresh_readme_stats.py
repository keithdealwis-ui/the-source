"""Refresh only the dated README coverage block from published manifests."""
import argparse
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
START = '<!-- source-stats:start -->'
END = '<!-- source-stats:end -->'


def render(canonical, corpus):
    def count(data, name):
        value = data['counts'][name]
        if type(value) is not int or value < 0:
            raise ValueError(f'Invalid count: {name}')
        return f'{value:,}'

    canonical_date = datetime.fromisoformat(canonical['data_as_of'].replace('Z', '+00:00')).strftime('%d %B %Y').lstrip('0')
    corpus_date = datetime.fromisoformat(corpus['as_of'].replace('Z', '+00:00')).strftime('%d %B %Y').lstrip('0')
    return f'''{START}
| Published coverage | What it means |
|---|---|
| **{count(corpus, 'projects')} discovered projects** | The broad discovery corpus: public project records with evidence dates and licensing information or warnings. |
| **{count(canonical, 'oss_projects')} projects in the assessed layer** | Projects with the fuller assessment of health and replacement fit where applicable. |
| **{count(canonical, 'recommended_relationships')} recommended product–project pairings** | Specific relationships that pass the recommendation rules. One project can replace more than one product. |
| **{count(canonical, 'supported_saas')} products with published alternative records** | Product coverage in the read API; the number of alternatives varies. |

Assessment snapshot: **{canonical_date}**. Discovery snapshot: **{corpus_date}**. Individual observation dates can differ.
[Discovery counts](data/corpus/MANIFEST.json) · [Assessment and recommendation counts](data/canonical/MANIFEST.json)
{END}'''


def refresh(root=ROOT, check=False):
    canonical = json.loads((root / 'data/canonical/MANIFEST.json').read_text())
    corpus = json.loads((root / 'data/corpus/MANIFEST.json').read_text())
    path = root / 'README.md'
    text = path.read_text()
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError('README must have one coverage block')
    a, b = text.index(START), text.index(END) + len(END)
    if b <= a:
        raise ValueError('Coverage markers are out of order')
    updated = text[:a] + render(canonical, corpus) + text[b:]
    if check:
        if updated != text:
            raise ValueError('README coverage is out of date; run scripts/refresh_readme_stats.py')
    else:
        path.write_text(updated)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    refresh(check=parser.parse_args().check)
