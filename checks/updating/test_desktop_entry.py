"""Read-only release checks: real CLI/resource validation, no network or lifecycle."""
import hashlib
import io
import json
import ssl
import sys

import pytest

from core.updating import releases
from core.updating.packaged import RESOURCE_FILES
from core.updating.state import atomic_json
from scripts.maintenance import desktop_entry

CURRENT_SHA = 'b' * 40
LATEST_SHA = 'a' * 40
ASSET_SHA = 'f' * 64


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / 'bundle'
    for name in RESOURCE_FILES:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# isolated resource\n')
    atomic_json(directory / 'runtime-manifest.json', {
        'schema_version': 1, 'update_protocol': 1,
        'version': '2.0.0', 'sha': CURRENT_SHA, 'source_state': 'clean',
        'images': {},
        'files': {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                  for name in RESOURCE_FILES},
    })
    return directory


@pytest.fixture
def published_release(monkeypatch):
    assets = [
        {'name': f'Printer-Companion-2.1.0-{suffix}', 'state': 'uploaded',
         'size': 1024, 'digest': 'sha256:' + ASSET_SHA,
         'browser_download_url': 'https://untrusted.invalid/do-not-use'}
        for suffix in ('mac-arm64.dmg', 'mac-x64.dmg', 'win-x64.exe',
                       'mac-arm64.zip', 'win-x64.exe.blockmap')
    ]
    data = {
        'releases/latest': {
            'tag_name': 'v2.1.0', 'target_commitish': 'main',
            'published_at': '2026-10-14T00:00:00Z', 'name': 'Проверенный выпуск',
            'draft': False, 'prerelease': False, 'assets': assets,
        },
        'git/ref/tags/v2.1.0': {'object': {'type': 'commit', 'sha': LATEST_SHA}},
    }
    calls = []

    def response(request, timeout, *, context):
        assert timeout <= 15
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        endpoint = request.full_url.split('/Printers-companion/', 1)[1]
        calls.append(endpoint)
        return io.BytesIO(json.dumps(data[endpoint]).encode('utf-8'))

    monkeypatch.setattr(releases, 'urlopen', response)
    return calls, data


@pytest.fixture
def forbid_lifecycle(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Read-only release check touched lifecycle state')
    monkeypatch.setattr(desktop_entry, 'PackagedUpdater', forbidden)
    monkeypatch.setattr(desktop_entry, 'read_json', forbidden)


def invoke(monkeypatch, capsys, bundle, root, action='check-release', selection=()):
    monkeypatch.setattr(sys, 'argv', [
        'desktop_entry', action, '--root', str(root), '--bundle', str(bundle),
        *selection,
    ])
    code = desktop_entry.main()
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    return code, json.loads(lines[0])


@pytest.mark.parametrize(('platform', 'arch', 'suffix'), [
    ('darwin', 'arm64', 'mac-arm64.dmg'),
    ('darwin', 'x64', 'mac-x64.dmg'),
    ('win32', 'x64', 'win-x64.exe'),
])
def test_check_release_selects_requested_native_installer_without_root(
        bundle, tmp_path, monkeypatch, capsys, published_release, forbid_lifecycle,
        platform, arch, suffix):
    root = tmp_path / 'missing-original-root'
    code, output = invoke(monkeypatch, capsys, bundle, root,
                          selection=('--platform', platform, '--arch', arch))
    assert code == 0
    assert output['type'] == 'result'
    state = output['state']
    assert state['phase'] == 'release_checked'
    assert state['source_state'] == 'clean'
    assert state['channel'] == 'stable'
    assert state['update_available'] is True
    assert state['version_unknown'] is False
    assert state['ahead_of_release'] is False
    assert state['identity_conflict'] is False
    assert state['current_version'] == '2.0.0'
    assert state['current_commit'] == CURRENT_SHA[:8]
    assert state['latest_version'] == '2.1.0'
    assert state['latest_sha'] == LATEST_SHA
    assert 'comparison' not in state
    assert state['installer'] == {
        'name': 'Printer-Companion-2.1.0-' + suffix,
        'url': f'https://github.com/{releases.REPOSITORY}/releases/download/v2.1.0/'
               + 'Printer-Companion-2.1.0-' + suffix,
        'size': 1024, 'sha256': ASSET_SHA, 'test_build': False,
    }
    assert published_release[0] == ['releases/latest', 'git/ref/tags/v2.1.0']
    assert not root.exists()


@pytest.mark.parametrize('selection', [
    (), ('--platform', 'darwin'), ('--arch', 'x64'),
    ('--platform', 'linux', '--arch', 'x64'),
    ('--platform', 'darwin', '--arch', 'i386'),
])
def test_check_release_requires_supported_platform_and_arch(
        bundle, tmp_path, monkeypatch, capsys, published_release, forbid_lifecycle,
        selection):
    root = tmp_path / 'missing-original-root'
    with pytest.raises(SystemExit) as exc:
        invoke(monkeypatch, capsys, bundle, root, selection=selection)
    assert exc.value.code == 2
    assert published_release[0] == []
    assert not root.exists()


def test_lifecycle_status_still_dispatches_without_release_selections(
        bundle, tmp_path, monkeypatch, capsys, published_release):
    root = tmp_path / 'existing-installation'
    calls = []

    class FakeUpdater:
        def __init__(self, selected_root, selected_bundle, *, progress):
            calls.append((selected_root, selected_bundle))
            assert callable(progress)
            self.settings_path = tmp_path / 'absent-settings.json'

        def status(self):
            calls.append('status')
            return {'phase': 'idle'}

    monkeypatch.setattr(desktop_entry, 'PackagedUpdater', FakeUpdater)
    code, output = invoke(monkeypatch, capsys, bundle, root, action='status')
    assert code == 0
    assert output['type'] == 'result'
    assert output['state']['phase'] == 'idle'
    assert output['state']['url'] == 'http://127.0.0.1:8000'
    assert calls == [(root, bundle), 'status']
    assert published_release[0] == []


def test_corrupt_bundle_fails_before_network_or_lifecycle(
        bundle, tmp_path, monkeypatch, capsys, published_release, forbid_lifecycle):
    (bundle / RESOURCE_FILES[0]).write_text('corrupted')
    root = tmp_path / 'missing-original-root'
    code, output = invoke(monkeypatch, capsys, bundle, root,
                          selection=('--platform', 'darwin', '--arch', 'arm64'))
    assert code == 1
    assert output['type'] == 'error'
    assert output['code'] == 'unconfirmed'
    assert 'повреждён' in output['message']
    assert published_release[0] == []
    assert not root.exists()


def test_release_without_compatible_asset_reports_null_installer(
        bundle, tmp_path, monkeypatch, capsys, published_release, forbid_lifecycle):
    published_release[1]['releases/latest']['assets'] = []
    root = tmp_path / 'missing-original-root'
    code, output = invoke(monkeypatch, capsys, bundle, root,
                          selection=('--platform', 'win32', '--arch', 'x64'))
    assert code == 0
    assert output['state']['phase'] == 'release_checked'
    assert output['state']['update_available'] is True
    assert output['state']['installer'] is None
    assert not root.exists()
