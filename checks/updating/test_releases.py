"""Stdlib updater checks: no project DB, Docker daemon, or live network."""
import io
import json

import pytest

from core.updating import releases

SHA = "a" * 40


def wire(monkeypatch, objects):
    calls = []

    def response(request, timeout):
        assert timeout <= 15
        url = request.full_url
        calls.append(url)
        data = objects[url.split('/Printers-companion/', 1)[1]]
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(releases, "urlopen", response)
    return calls


def test_annotated_release_resolves_tag_not_target_branch(monkeypatch):
    calls = wire(monkeypatch, {
        "releases/latest": {"tag_name": "v1.7.0", "target_commitish": "main",
                            "published_at": "2026-09-30", "draft": False, "prerelease": False},
        "git/ref/tags/v1.7.0": {"object": {"type": "tag", "sha": "b" * 40}},
        "git/tags/" + "b" * 40: {"object": {"type": "commit", "sha": SHA}},
    })
    assert releases.stable_release().sha == SHA
    assert all("commits/main" not in url for url in calls)


@pytest.mark.parametrize("flag", ["draft", "prerelease"])
def test_unpublished_candidates_never_install(monkeypatch, flag):
    wire(monkeypatch, {"releases/latest": {"tag_name": "v1.7.0", "published_at": "today", flag: True}})
    with pytest.raises(releases.UpdateError, match="Черновик"):
        releases.stable_release()


@pytest.mark.parametrize("tag", ["v1.7.0-beta", "../main", "v01.7.0", "main", "v1.7.0;whoami"])
def test_unsafe_or_nonstable_versions_never_reach_network(tag):
    with pytest.raises(releases.UpdateError):
        releases.stable_release(tag)


def test_same_version_another_commit_is_identity_conflict_not_newer_release():
    release = releases.Release("1.7.0", "v1.7.0", SHA, "", "", "")
    report = releases.update_comparison("1.7.0", "b" * 40, release)
    assert report["identity_conflict"]
    assert not report["update_available"]
    assert not releases.update_comparison("1.8.0", SHA, release)["update_available"]
    assert releases.update_comparison("1.6.0", SHA, release)["update_available"]


def test_unknown_identity_is_not_green_latest():
    report = releases.update_comparison("1.7.0", "unknown", releases.Release("1.7.0", "v1.7.0", SHA, "", "", ""))
    assert report["version_unknown"]
    assert not report["update_available"]
