"""Updater checks: no project DB, Docker daemon, or live network."""
import io
import json
import ssl

import pytest

from core.updating import releases

SHA = "a" * 40


def wire(monkeypatch, objects):
    calls = []

    def response(request, timeout, context):
        assert timeout <= 15
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert context.cert_store_stats()["x509_ca"] > 0
        url = request.full_url
        calls.append(url)
        data = objects[url.split('/Printers-companion/', 1)[1]]
        return io.BytesIO(json.dumps(data).encode())

    monkeypatch.setattr(releases, "urlopen", response)
    return calls


def test_github_appends_public_bundle_to_default_verified_context(monkeypatch):
    original = ssl.create_default_context()
    original_roots = set(original.get_ca_certs(binary_form=True))
    loads = []

    class Context:
        def load_verify_locations(self, **kwargs):
            loads.append(kwargs)
            original.load_verify_locations(**kwargs)

    context = Context()
    monkeypatch.setattr(releases.ssl, "create_default_context", lambda: context)

    def response(request, timeout, context):
        assert timeout == 15
        assert context is expected_context
        assert original.verify_mode == ssl.CERT_REQUIRED
        assert original.check_hostname is True
        assert original_roots <= set(original.get_ca_certs(binary_form=True))
        return io.BytesIO(b"{}")

    expected_context = context
    monkeypatch.setattr(releases, "urlopen", response)
    assert releases.github_json("releases/latest") == {}
    assert loads == [{"cafile": releases.certifi.where()}]


def test_github_has_verified_public_trust_without_host_ca_paths(monkeypatch):
    # Portable empty context: unlike environment variables, it also excludes
    # Windows automatic system-store loading without mutating host settings.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    assert context.cert_store_stats()["x509_ca"] == 0
    monkeypatch.setattr(releases.ssl, "create_default_context", lambda: context)
    wire(monkeypatch, {"releases/latest": {}})
    assert releases.github_json("releases/latest") == {}
    assert context.cert_store_stats()["x509_ca"] > 0
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_missing_portable_bundle_fails_closed_before_network(monkeypatch, tmp_path):
    monkeypatch.setattr(releases.certifi, "where", lambda: str(tmp_path / "missing-ca.pem"))
    calls = wire(monkeypatch, {"releases/latest": {}})
    with pytest.raises(releases.UpdateError, match="Не удалось проверить релиз"):
        releases.github_json("releases/latest")
    assert calls == []


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


def release_with_assets(*assets):
    return releases.Release("1.8.0", "v1.8.0", SHA, "", "", "", tuple(assets))


def asset(name, **values):
    return {"name": name, "state": "uploaded", "size": 123, **values}


@pytest.mark.parametrize("platform,arch,suffix", [
    ("darwin", "arm64", "mac-arm64.dmg"), ("darwin", "x64", "mac-x64.dmg"),
    ("win32", "x64", "win-x64.exe"),
])
def test_installer_selection_matches_only_native_platform_and_arch(platform, arch, suffix):
    native = [asset(f"Printer-Companion-1.8.0-{item}") for item in (
        "mac-arm64.dmg", "mac-x64.dmg", "win-x64.exe", "mac-arm64.zip",
    )]
    selected = releases.compatible_installer(release_with_assets(*native), platform=platform, arch=arch)
    assert selected == {
        "name": f"Printer-Companion-1.8.0-{suffix}", "size": 123, "test_build": False,
        "url": f"https://github.com/{releases.REPOSITORY}/releases/download/v1.8.0/Printer-Companion-1.8.0-{suffix}",
    }
    assert releases.compatible_installer(release_with_assets(*native), platform="win32", arch="arm64") is None


def test_assets_are_bounded_projected_and_remote_urls_are_not_trusted(monkeypatch):
    wire(monkeypatch, {
        "releases/latest": {"tag_name": "v1.8.0", "published_at": "today", "assets": [
            asset("Printer-Companion-1.8.0-mac-arm64.dmg", digest="sha256:" + "c" * 64,
                  browser_download_url="https://evil.invalid/run", body="not retained"),
        ]},
        "git/ref/tags/v1.8.0": {"object": {"type": "commit", "sha": SHA}},
    })
    release = releases.stable_release()
    selected = releases.compatible_installer(release, platform="darwin", arch="arm64")
    assert selected["sha256"] == "c" * 64
    assert selected["url"].startswith(f"https://github.com/{releases.REPOSITORY}/releases/download/v1.8.0/")
    assert set(release.assets[0]) == {"name", "state", "size", "digest"}
    assert releases.Release("1.8.0", "v1.8.0", SHA, "", "", "").assets == ()


@pytest.mark.parametrize("values", [{"state": "starter"}, {"size": 0}, {"size": -1},
                                    {"size": True}, {"size": 2.5}, {"size": "123"}])
def test_incomplete_or_unknown_size_native_asset_is_not_installable(values):
    release = release_with_assets(asset("Printer-Companion-1.8.0-mac-arm64.dmg", **values))
    assert releases.compatible_installer(release, platform="darwin", arch="arm64") is None


@pytest.mark.parametrize("name", ["source.zip", "operator-images.json", "SHA256SUMS",
                                "Printer-Companion-1.8.0-mac-arm64.zip",
                                "Printer-Companion-1.7.0-mac-arm64.dmg",
                                "Printer-Companion-1.8.0-mac-x64.dmg"])
def test_source_archives_container_assets_and_other_identity_are_not_installers(name):
    assert releases.compatible_installer(release_with_assets(asset(name)), platform="darwin", arch="arm64") is None


def test_duplicate_selected_native_kind_fails_closed():
    native = asset("Printer-Companion-1.8.0-mac-arm64.dmg")
    with pytest.raises(releases.UpdateError, match="неоднозначный"):
        releases.compatible_installer(release_with_assets(native, dict(native, state="starter")), platform="darwin", arch="arm64")


def test_test_artifact_requires_exact_release_sha_suffix_and_prefers_production():
    name = f"Printer-Companion-1.8.0-test.{SHA[:12]}.1234abcd-mac-arm64.dmg"
    selected = releases.compatible_installer(release_with_assets(asset(name)), platform="darwin", arch="arm64")
    assert selected["test_build"] is True
    assert selected["name"] == name
    other = asset(name.replace("1234abcd", "5678abcd"))
    with pytest.raises(releases.UpdateError, match="неоднозначный"):
        releases.compatible_installer(release_with_assets(asset(name), other), platform="darwin", arch="arm64")
    native = asset("Printer-Companion-1.8.0-mac-arm64.dmg")
    assert releases.compatible_installer(release_with_assets(native, asset(name), other), platform="darwin", arch="arm64")["test_build"] is False
    for invalid in (name.replace(SHA[:12], "b" * 12), name.replace("1234abcd", "1234ABCD"),
                    name.replace(".dmg", ".zip"), name.replace("1.8.0-test", "1.8.0-beta")):
        assert releases.compatible_installer(release_with_assets(asset(invalid)), platform="darwin", arch="arm64") is None


@pytest.mark.parametrize("assets", [{}, [asset("x")] * 257])
def test_unbounded_or_malformed_asset_list_is_rejected(monkeypatch, assets):
    wire(monkeypatch, {"releases/latest": {"tag_name": "v1.8.0", "published_at": "today", "assets": assets}})
    with pytest.raises(releases.UpdateError, match="список файлов"):
        releases.stable_release()
