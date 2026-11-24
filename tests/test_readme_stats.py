import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('readme_stats', Path(__file__).parents[1] / 'scripts/refresh_readme_stats.py')
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)


def fixture(root):
    for layer, value in [('canonical', {'data_as_of': '2026-11-24T00:00:00Z', 'counts': {'oss_projects': 662, 'recommended_relationships': 1256, 'supported_saas': 377}}),
                         ('corpus', {'as_of': '2026-11-23T00:00:00Z', 'counts': {'projects': 25294}})]:
        path = root / 'data' / layer / 'MANIFEST.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(value))
    (root / 'README.md').write_text('Keep the introduction.\n'+stats.START+'old'+stats.END+'\nKeep the ending.\n')


def test_updates_only_coverage_and_keeps_independent_dates(tmp_path):
    fixture(tmp_path)
    stats.refresh(tmp_path)
    text = (tmp_path / 'README.md').read_text()
    assert text.startswith('Keep the introduction.\n') and text.endswith('\nKeep the ending.\n')
    assert '25,294 discovered projects' in text
    assert 'Assessment snapshot: **24 November 2026**' in text
    assert 'Discovery snapshot: **23 November 2026**' in text
    stats.refresh(tmp_path, check=True)


def test_invalid_manifest_never_replaces_existing_text(tmp_path):
    fixture(tmp_path)
    before = (tmp_path / 'README.md').read_bytes()
    path = tmp_path / 'data/corpus/MANIFEST.json'
    path.write_text('{"as_of":"2026-11-24", "counts":{}}')
    with pytest.raises(KeyError):
        stats.refresh(tmp_path)
    assert (tmp_path / 'README.md').read_bytes() == before


def test_missing_markers_refuses_to_overwrite(tmp_path):
    fixture(tmp_path)
    (tmp_path / 'README.md').write_text('A different README')
    with pytest.raises(ValueError):
        stats.refresh(tmp_path)
    assert (tmp_path / 'README.md').read_text() == 'A different README'
