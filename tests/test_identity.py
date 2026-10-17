"""KEI-919 S1.8: renames, forks, mirrors and sibling repositories resolve to one entity. No network."""
from __future__ import annotations

from pathlib import Path

import pytest

from source_pipeline import common, corpus as C
from source_pipeline import identity as I

ROOT = Path(__file__).resolve().parent.parent
LIST = "list:punkpeye/awesome-mcp-servers"


def _p(pid, desc=None, aliases=(), full_name=None, hid=None, lanes=("discover",), line=1):
    host, slug = pid.split("/", 1)
    return {"id": pid, "host_repo_id": hid, "name": slug.split("/")[-1], "full_name": full_name or slug,
            "url": f"https://{pid}", "aliases": sorted(aliases), "description": desc, "homepage": None,
            "topics": [], "languages": [],
            "licence": {"spdx": "MIT", "status": "recognised", "issue": None},
            "maintenance": {"status": "active", "last_meaningful_activity_at": None}, "lanes": list(lanes),
            "provenance": [{"strategy": "curated_list", "source": LIST, "commit": "c" * 40,
                            "locator": f"line {line}", "depth": 1, "via": None}]}


def _l(pid, stars=0, full_name=None):
    return {"id": pid, "found": True, "full_name": full_name or pid.split("/", 1)[1], "stars": stars, "forks": 0,
            "pushed_at": None, "archived": False, "latest_release": None, "licence_spdx": "MIT",
            "observed_as_of": "2026-10-10T00:00:00Z", "unresolved": None}


def _fact(pid, nwo=None, dbid=None, fork_parent=None, mirror=False, mirror_url=None, found=True):
    return {"id": pid, "asked": pid.split("/", 1)[1], "observed_at": "2026-10-17T00:00:00Z", "found": found,
            "error": None if found else "not_found", "name_with_owner": nwo or (pid.split("/", 1)[1] if found else None),
            "database_id": dbid, "is_fork": bool(fork_parent),
            "parent": {"slug": fork_parent, "database_id": 1} if fork_parent else None, "is_mirror": mirror,
            "mirror_url": mirror_url}


def _run(projects, facts=(), canonical=(), stars=None):
    stars = stars or {}
    live = [_l(p["id"], stars.get(p["id"], 0), p["full_name"]) for p in projects]
    return I.resolve(projects, live, {f["id"]: f for f in facts}, list(canonical))


def _by(rows):
    return {r["id"]: r for r in rows}


def test_a_rename_is_one_project_and_both_names_resolve():
    rows, live, rep = _run([_p("github.com/old/tool", hid=7), _p("github.com/new/tool", hid=7, line=2)],
                           facts=[_fact("github.com/old/tool", "New/Tool", 7), _fact("github.com/new/tool", "New/Tool", 7)])
    assert [r["id"] for r in rows] == ["github.com/new/tool"] and [r["id"] for r in live] == ["github.com/new/tool"]
    one = rows[0]
    assert "github.com/old/tool" in one["aliases"]
    assert one["identity"]["merged"] == [{"id": "github.com/old/tool", "reason": "renamed"}]
    assert len(one["provenance"]) == 2                                     # both routes kept
    assert rep["collapsed"] == 1


def test_a_renamed_row_alone_gains_its_current_name_as_an_alias():
    rows, _, rep = _run([_p("github.com/old/tool")], facts=[_fact("github.com/old/tool", "moved/Tool", 9)])
    r = rows[0]
    assert r["identity"]["renamed_to"] == "github.com/moved/tool" and "github.com/moved/tool" in r["aliases"]
    assert rep["renamed"] == 1


def test_the_same_host_repository_id_under_two_names_is_one_row():
    rows, _, _ = _run([_p("github.com/a/x", hid=5), _p("github.com/b/y", hid=5)])
    assert len(rows) == 1 and rows[0]["identity"]["merged"][0]["reason"] == "same_repository"


def test_a_fork_of_a_corpus_project_is_marked_not_collapsed():
    rows, _, rep = _run([_p("github.com/up/tool", hid=1), _p("github.com/me/tool", hid=2), _p("github.com/me/other", hid=3)],
                        facts=[_fact("github.com/me/tool", dbid=2, fork_parent="up/tool"),
                               _fact("github.com/me/other", dbid=3, fork_parent="far/away")])
    by = _by(rows)
    assert len(rows) == 3
    assert by["github.com/me/tool"]["identity"]["fork_of"] == "github.com/up/tool"
    assert by["github.com/me/tool"]["identity"]["fork_parent_in_corpus"] is True
    assert by["github.com/me/other"]["identity"]["fork_of"] == "github.com/far/away"
    assert by["github.com/me/other"]["identity"]["fork_parent_in_corpus"] is False
    assert by["github.com/up/tool"]["identity"] is None
    assert rep["forks"] == 2 and rep["forks_of_corpus_projects"] == 1


@pytest.mark.parametrize("desc,mirror", [
    ("Mirror of https://git.ffmpeg.org/ffmpeg.git", True),
    ("Read-only mirror of https://gitlab.gnome.org/GNOME/gimp", True),
    ("(Mirror) S3-compatible object store. Main repo: https://git.deuxfleurs.fr/Deuxfleurs/garage", True),
    ("Git Source Code Mirror - This is a publish-only repository", True),
    ("pagure is a light-weight git-centered forge (MIRROR)", True),
    ("Real-time dashboard with live chat mirroring", False),
    ("Run any process as if it were a pod: mirrord", False),
    ("Self-hosted huggingface mirror service.", False),
    (None, False),
])
def test_a_mirror_is_what_its_description_says(desc, mirror):
    assert I.says_mirror(desc) is mirror


def test_a_mirror_of_a_held_upstream_collapses_into_it_and_an_outside_upstream_stays():
    rows, _, rep = _run([_p("gitlab.com/proj/app", lanes=("replace",)),
                         _p("github.com/proj/app", desc="Mirror of https://gitlab.com/proj/app"),
                         _p("github.com/postgres/postgres", desc="Mirror of the official PostgreSQL GIT repository"),
                         _p("github.com/gnome/gimp", desc="Read-only mirror of https://gitlab.gnome.org/GNOME/gimp")])
    by = _by(rows)
    assert "github.com/proj/app" not in by and "github.com/proj/app" in by["gitlab.com/proj/app"]["aliases"]
    assert by["gitlab.com/proj/app"]["identity"]["merged"] == [{"id": "github.com/proj/app", "reason": "mirror"}]
    assert by["gitlab.com/proj/app"]["lanes"] == ["discover", "replace"]
    assert by["github.com/postgres/postgres"]["identity"]["mirror"] is True     # the only copy here: kept
    assert by["github.com/postgres/postgres"]["identity"]["mirror_of"] is None
    assert by["github.com/gnome/gimp"]["identity"]["mirror_of"] == "gitlab.gnome.org/gnome/gimp"
    assert rep["collapsed_by_reason"] == {"mirror": 1}


def test_a_bare_owner_name_upstream_is_trusted_only_when_held():
    rows, _, _ = _run([_p("github.com/x/mcp", desc="Public mirror of packages/mcp from the monorepo")])
    assert rows[0]["identity"]["mirror"] is True and rows[0]["identity"]["mirror_of"] is None


def test_github_mirror_flag_and_the_canonical_layer_pick_the_primary():
    canonical = [{"oss_id": "gitlab.com/base/row", "repo_url": "https://gitlab.com/base/row", "github_url": None}]
    rows, _, _ = _run([_p("github.com/base/row", hid=10), _p("gitlab.com/base/row", hid=20)], canonical=canonical)
    assert [r["id"] for r in rows] == ["gitlab.com/base/row"]
    assert rows[0]["identity"]["merged"] == [{"id": "github.com/base/row", "reason": "mirror"}]
    flagged, _, _ = _run([_p("github.com/m/r")], facts=[_fact("github.com/m/r", mirror=True,
                                                              mirror_url="https://example.org/git/r.git")])
    assert flagged[0]["identity"]["mirror"] is True and flagged[0]["identity"]["mirror_of"] == "example.org/git/r"


def test_sibling_repositories_name_one_primary():
    rows, _, rep = _run([_p("github.com/bitwarden/server"), _p("github.com/bitwarden/clients"),
                         _p("github.com/acme/widget"), _p("github.com/acme/widget-docs"), _p("github.com/acme/widget-cli"),
                         _p("github.com/acme/unrelated"), _p("github.com/solo/thing-docs")],
                        stars={"github.com/bitwarden/server": 100, "github.com/bitwarden/clients": 50})
    by = _by(rows)
    assert by["github.com/acme/widget-docs"]["identity"]["sibling_primary"] == "github.com/acme/widget"
    assert by["github.com/acme/widget"]["identity"]["siblings"] == ["github.com/acme/widget-cli",
                                                                    "github.com/acme/widget-docs"]
    assert by["github.com/bitwarden/clients"]["identity"]["sibling_primary"] == "github.com/bitwarden/server"
    assert by["github.com/acme/unrelated"]["identity"] is None and by["github.com/solo/thing-docs"]["identity"] is None
    assert rep["sibling_groups"] == 2 and rep["sibling_repositories"] == 5


def test_resolution_is_deterministic_and_idempotent():
    projects = [_p("github.com/old/tool", hid=7), _p("github.com/new/tool", hid=7), _p("github.com/acme/widget"),
                _p("github.com/acme/widget-docs"), _p("gitlab.com/proj/app"),
                _p("github.com/proj/app", desc="Mirror of https://gitlab.com/proj/app")]
    facts = [_fact("github.com/old/tool", "new/tool", 7)]
    a, la, _ = _run(projects, facts)
    b, lb, _ = _run(list(reversed(projects)), facts)
    assert common.dumps(a) == common.dumps(b)
    again, _, _ = I.resolve(a, la, {f["id"]: f for f in facts}, [])
    assert common.dumps(again) == common.dumps(a)                          # merged history survives a re-run
    assert I.check(a) == []


def test_check_refuses_a_broken_identity():
    rows, _, _ = _run([_p("github.com/acme/widget"), _p("github.com/acme/widget-docs")])
    rows[1]["identity"]["sibling_primary"] = "github.com/acme/nowhere"
    assert I.check(rows)
    rows2, _, _ = _run([_p("github.com/a/x", hid=5), _p("github.com/b/y", hid=5)])
    rows2[0]["aliases"] = []
    assert any("merged" in m for m in I.check(rows2))


class FakeApi:
    """GraphQL answers by slug; REST follows a transfer."""

    def __init__(self, repos, moved=None):
        import types
        self.repos, self.moved = repos, moved or {}
        self.http = types.SimpleNamespace(calls=0, failures=[])
        self.queries = 0

    def gql(self, query, variables=None):
        import re
        self.http.calls += 1
        self.queries += 1
        data, errors = {}, []
        for alias, owner, name in re.findall(r'(r\d+): repository\(owner:"([^"]+)", name:"([^"]+)"\)', query):
            r = self.repos.get(f"{owner}/{name}")
            data[alias] = r
            if r is None:
                errors.append({"type": "NOT_FOUND", "path": [alias]})
        return 200, {"data": data, "errors": errors}

    def rest(self, path):
        self.http.calls += 1
        slug = path.split("repos/", 1)[1]
        if slug in self.moved:
            return 200, {"full_name": self.moved[slug], "id": 99, "fork": False, "mirror_url": None}
        return 404, None


def test_refresh_facts_is_batched_follows_transfers_and_feeds_corpus_validate(tmp_path, monkeypatch):
    out = tmp_path / "data" / "corpus"
    projects = [_p("github.com/a/one", hid=1), _p("github.com/a/one-docs", hid=2), _p("github.com/b/fork", hid=3),
                _p("github.com/c/gone", hid=4)]
    live = [_l(p["id"]) for p in projects]
    C.write_layer(out, projects, live, {})
    api = FakeApi({"a/one": {"nameWithOwner": "a/one", "databaseId": 1, "isFork": False, "isMirror": False,
                             "mirrorUrl": None, "parent": None},
                   "a/one-docs": {"nameWithOwner": "a/one-docs", "databaseId": 2, "isFork": False, "isMirror": False,
                                  "mirrorUrl": None, "parent": None},
                   "b/fork": {"nameWithOwner": "b/fork", "databaseId": 3, "isFork": True, "isMirror": False,
                              "mirrorUrl": None, "parent": {"nameWithOwner": "a/one", "databaseId": 1}}},
                  moved={"c/gone": "d/Gone"})
    monkeypatch.setattr("source_pipeline.discover.log", lambda *a, **k: None)
    got = I.apply(out, refresh=True, api=api)
    assert api.queries == 1                                               # four repositories, one batch
    assert got["facts_refresh"]["rest_follow"] == 1
    rows = {p["id"]: p for p in common.read_jsonl(out / "projects.jsonl")}
    assert rows["github.com/b/fork"]["identity"]["fork_of"] == "github.com/a/one"
    assert rows["github.com/c/gone"]["identity"]["renamed_to"] == "github.com/d/gone"
    assert "github.com/d/gone" in rows["github.com/c/gone"]["aliases"]
    assert rows["github.com/a/one-docs"]["identity"]["sibling_primary"] == "github.com/a/one"
    assert got["identity"]["forks_of_corpus_projects"] == 1
    report = C.validate(out)
    assert not report["failed_checks"], report
    m = common.read_json(out / "MANIFEST.json")
    assert m["identity"]["facts"]["repositories"] == 4 and m["counts"]["identity"]["renamed"] == 1


def test_committed_corpus_identity_validates():
    if not (ROOT / "data" / "corpus" / "MANIFEST.json").is_file():
        pytest.skip("no corpus committed")
    rows = common.read_jsonl(ROOT / "data" / "corpus" / "projects.jsonl")
    assert all("identity" in r for r in rows)
    assert I.check(rows) == []
    keys = {}
    for r in rows:
        for k in [r["id"], *r["aliases"]]:
            assert keys.setdefault(k, r["id"]) == r["id"], f"{k} names two projects"
