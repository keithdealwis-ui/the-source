"""KEI-919 S1.1 / S1.7: project-level licence classification of flagged corpus rows. No network."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from source_pipeline import common, corpus as C
from source_pipeline import licence_classify as L

ROOT = Path(__file__).resolve().parent.parent
TEXTS = ROOT / "config" / "licence_texts"
MIT = (TEXTS / "MIT.txt").read_text()
AGPL = (TEXTS / "AGPL-3.0.txt").read_text()
APACHE = (TEXTS / "Apache-2.0.txt").read_text()
SHA = "a" * 40
AT = "2026-10-17T00:00:00Z"
COMMERCIAL = """The Example Enterprise Licence

This software may only be used in production with a valid Example Enterprise subscription.
You may not copy, distribute or sell the Software. All rights reserved by Example, Inc.
""" * 3
BSL = """Business Source License 1.1

Licensor: Example, Inc.
Licensed Work: Example. The Licensed Work is (c) 2026 Example, Inc.
Additional Use Grant: You may make use of the Licensed Work, provided that you do not use it
in a production environment that offers a managed service competing with Example.
Change Date: 2030-01-01
Change License: Apache License, Version 2.0
"""


def tree(*entries):
    """'dir/' entries are trees, the rest blobs; parents are added."""
    out = set()
    for e in entries:
        is_dir = e.endswith("/")
        e = e.rstrip("/")
        parts = e.split("/")
        for i in range(1, len(parts)):
            out.add(("/".join(parts[:i]), "tree"))
        out.add((e, "tree" if is_dir else "blob"))
    return sorted(out)


def run(paths, texts, **kw):
    return L.classify(paths, texts, commit=kw.pop("commit", SHA), checked_at=AT, **kw)


# --------------------------------------------------------------------------- the six classes


def test_open_standard_licence_over_the_whole_repository():
    c = run(tree("LICENSE", "src/main.py", "Dockerfile"), {"LICENSE": MIT})
    assert c["class"] == "open" and c["spdx"] == "MIT"
    e = L.eligibility(c)
    assert e["status"] == "eligible" and e["condition"] is None and e["self_hostable"] is True
    assert e["self_hosting_evidence"] == ["Dockerfile"]


def test_open_core_with_an_ee_directory_named_in_the_root_licence():
    notice = ("Portions of this software are licensed as follows:\n\n"
              "* All content that resides under the \"ee/\" directory of this repository is licensed under the "
              "license defined in \"ee/LICENSE\".\n"
              "* Content outside of the above-mentioned directories is available under the MIT license "
              "as defined below.\n\n")
    paths = tree("LICENSE", "ee/LICENSE", "ee/billing.ts", "src/app.ts", "docker-compose.yml")
    c = run(paths, {"LICENSE": notice + MIT, "ee/LICENSE": COMMERCIAL})
    assert c["class"] == "open_core", c["reason"]
    assert c["core_spdx"] == "MIT" and c["excluded"]["paths"] == ["ee"]
    e = L.eligibility(c)
    assert e["status"] == "conditional"
    assert e["condition"] == "core only; edition check required before recommendation"
    assert e["follow_up"] == "edition_check_required_before_recommendation"


def test_open_core_from_a_standard_root_text_and_an_enterprise_directory_with_its_own_licence():
    paths = tree("LICENSE.txt", "server/enterprise/LICENSE", "server/main.go")
    c = run(paths, {"LICENSE.txt": AGPL, "server/enterprise/LICENSE": COMMERCIAL})
    assert c["class"] == "open_core" and c["core_spdx"] == "AGPL-3.0"
    assert c["excluded"]["paths"] == ["server/enterprise"]


def test_open_core_delimited_by_an_exact_file_header_marker():
    notice = ("This project is mostly licensed under the GNU Affero General Public License v3.0 (AGPLv3) as "
              "described below.\n\n1. Certain files are licensed under a commercial license. These files are "
              "clearly marked with the following comment at the top of the file: /* @license Enterprise */\n\n")
    c = run(tree("LICENSE", "packages/server/a.ts"), {"LICENSE": notice + AGPL})
    assert c["class"] == "open_core", c["reason"]
    assert c["excluded"]["markers"] == ["/* @license Enterprise */"]


def test_source_available_business_source_licence():
    c = run(tree("LICENSE", "src/x.go"), {"LICENSE": BSL})
    assert c["class"] == "source_available"
    assert L.eligibility(c)["status"] == "not_eligible"


def test_source_available_core_with_an_open_sdk_is_not_open_core():
    """The inverse shape: the core is source-available and only a named part is open."""
    wrapper = ("This repository is licensed under the Business Source License 1.1. Code in the "
               "\"packages/sdk/\" directory is available under the MIT license; see LICENSE-MIT.\n")
    c = run(tree("LICENSE", "LICENSE-MIT", "packages/sdk/index.ts"), {"LICENSE": wrapper, "LICENSE-MIT": MIT})
    assert c["class"] == "source_available", c["reason"]


def test_restricted_standard_text_with_prohibitive_terms_added():
    text = MIT + "\n\nAdditional restriction: the Software may not be used for any military purpose.\n"
    c = run(tree("LICENSE"), {"LICENSE": text})
    assert c["class"] == "restricted", c["reason"]
    assert L.eligibility(c)["status"] == "not_eligible"


def test_missing_no_licence_file_at_root_even_if_a_manifest_declares_one():
    c = run(tree("README.md", "package.json"), {"package.json": '{"license": "MIT"}'})
    assert c["class"] == "missing"
    assert c["supporting"] == {"package.json": "MIT"}  # supporting evidence only, never the class
    assert L.eligibility(c)["status"] == "not_eligible"


# --------------------------------------------------------------------------- fail closed: unclear


def test_unclear_enterprise_directory_with_no_stated_licence():
    c = run(tree("LICENSE", "ee/feature.ts"), {"LICENSE": MIT})
    assert c["class"] == "unclear"
    assert "ee" in c["reason"]


def test_unclear_when_a_licence_file_could_not_be_read():
    c = run(tree("LICENSE", "src/a.py"), {})
    assert c["class"] == "unclear" and "could not be read" in c["reason"]


def test_unclear_when_the_repository_could_not_be_read():
    c = L.classify([], {}, commit=None, checked_at=AT, found=False, error="not_found")
    assert c["class"] == "unclear" and c["provenance"]["commit"] is None
    assert L.eligibility(c)["status"] == "not_eligible"


def test_unclear_unrecognised_licence_text():
    c = run(tree("LICENSE"), {"LICENSE": "Do whatever you like with this, but ask me first. Signed, Bob.\n" * 4})
    assert c["class"] == "unclear"


def test_unclear_open_text_with_separate_parts_but_no_default_statement():
    """Parts are licensed separately, but no notice says the open licence covers the rest."""
    notice = "Files in the \"cloud/\" directory are licensed under the Example Cloud License.\n\n"
    c = run(tree("LICENSE", "cloud/a.go"), {"LICENSE": notice + MIT})
    assert c["class"] == "unclear", c["reason"]


# --------------------------------------------------------------------------- provenance (S1.7) and row fields


def test_every_classification_carries_source_commit_and_checked_at():
    c = run(tree("LICENSE", "LICENSE_AGPL.txt"), {"LICENSE": MIT, "LICENSE_AGPL.txt": AGPL})
    prov = c["provenance"]
    assert set(prov) == {"source", "commit", "checked_at"}
    assert prov["commit"] == SHA and prov["checked_at"] == AT
    assert prov["source"]["files"] == ["LICENSE", "LICENSE_AGPL.txt"]


def test_source_files_named_license_are_not_licence_files():
    assert not L._is_licence_file("license.ts") and not L._is_licence_file("LICENSE_FAQ.md")
    assert L._is_licence_file("LICENSE.txt") and L._is_licence_file("COPYING")


def test_row_fields_validate_in_the_corpus_layer(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    shutil.copy(ROOT / "config" / "corpus.yaml", tmp_path / "config" / "corpus.yaml")

    def row(rid, licence):
        return {"id": rid, "host_repo_id": 1, "name": rid.split("/")[-1], "full_name": rid.split("/", 1)[1],
                "url": f"https://{rid}", "aliases": [], "description": None, "homepage": None, "topics": [],
                "languages": [], "licence": licence, "maintenance": {"status": "active",
                                                                    "last_meaningful_activity_at": None},
                "lanes": ["discover"], "provenance": [{"strategy": "topic_search", "source": "github-topic:x",
                                                       "commit": None, "locator": None, "depth": 1, "via": None}]}

    def live(rid):
        return {"id": rid, "found": True, "full_name": rid.split("/", 1)[1], "stars": 1, "forks": 0,
                "pushed_at": None, "archived": False, "latest_release": None, "licence_spdx": None,
                "observed_as_of": AT, "unresolved": None}

    issue = {"spdx": "NOASSERTION", "status": "issue", "issue": {"category": "unrecognised", "reason": "x"}}
    oc = run(tree("LICENSE.txt", "server/enterprise/LICENSE"),
             {"LICENSE.txt": AGPL, "server/enterprise/LICENSE": COMMERCIAL})
    projects = [row("github.com/a/oc", {**issue, **L.row_fields(oc)}),
                row("github.com/b/ok", {"spdx": "MIT", "status": "recognised", "issue": None})]
    out = tmp_path / "corpus"
    m = C.write_layer(out, projects, [live(p["id"]) for p in projects], {})
    assert C.validate(out)["failed_checks"] == []
    assert m["counts"]["licence_classification"] == {"open_core": 1}
    assert m["counts"]["licence_eligibility"] == {"conditional": 1}

    # An open_core row that claims full eligibility is refused: the gate is not relaxed.
    bad = {**issue, **L.row_fields(oc)}
    bad["eligibility"] = {**bad["eligibility"], "status": "eligible"}
    with pytest.raises(C.CorpusError):
        C.write_layer(tmp_path / "corpus2", [row("github.com/a/oc", bad)], [live("github.com/a/oc")], {})
