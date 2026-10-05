import hashlib
import json
import os
import subprocess
import sys

import pytest

from core.updating.packaged import RESOURCE_FILES, PackagedUpdater, configure_nas, finish_setup, load_bundle, public_status
from core.updating.releases import IMAGE_REPOSITORY, UpdateError
from core.updating.runtime import _GATE_PROGRAM
from core.updating.state import atomic_json


@pytest.fixture
def bundle(tmp_path):
    directory = tmp_path / 'bundle'
    for name in RESOURCE_FILES:
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# test-resource\n')
    manifest = {'schema_version': 1, 'update_protocol': 1, 'version': '2.0.0', 'sha': 'b' * 40,
                'images': {name: IMAGE_REPOSITORY + '@sha256:' + char * 64 for name, char in zip(('api','worker','watcher'), ('c','d','e'), strict=True)},
                'source_state': 'clean', 'files': {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in RESOURCE_FILES}}
    atomic_json(directory / 'runtime-manifest.json', manifest)
    return directory


def test_bundled_resources_verified_and_no_git_required(bundle, tmp_path):
    updater = PackagedUpdater(tmp_path / 'operator', bundle)
    updater._clean_sources()
    source = updater._stage(updater.release_loader(None))
    assert (source / 'docker-compose.yml').read_bytes() == (bundle / 'docker-compose.yml').read_bytes()
    (bundle / 'docker-compose.yml').write_text('tampered')
    with pytest.raises(UpdateError, match='повреждён'):
        load_bundle(bundle)


def test_package_without_verified_image_digests_cannot_deploy_mutable_aliases(bundle, tmp_path):
    manifest_path = bundle / 'runtime-manifest.json'
    saved = json.loads(manifest_path.read_text())
    saved.pop('images')
    atomic_json(manifest_path, saved)
    updater = PackagedUpdater(tmp_path / 'operator', bundle)
    with pytest.raises(UpdateError, match='закреплённых образов'):
        updater._clean_sources()


def test_configuration_preserves_special_secrets_and_does_not_overwrite(bundle, tmp_path):
    root = tmp_path / 'пользователь с пробелами'
    settings = {'host': '100.78.114.66', 'db_password': "p$#'\\", 'access_key': 'access', 'secret_key': "s$#'\\"}
    configure_nas(root, bundle, settings)
    path = root / 'deploy/nas/.env.operator'
    original = path.read_bytes()
    assert 'operator-' in original.decode()
    assert 'change-me' not in original.decode()
    with pytest.raises(UpdateError, match='уже настроена'):
        configure_nas(root, bundle, dict(settings, db_password='new'))
    assert path.read_bytes() == original
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('host', ['http://nas:9000', 'nas/x', 'nas\nDATABASE_URL=evil', ''])
def test_bad_settings_cannot_inject_env(bundle, tmp_path, host):
    with pytest.raises(UpdateError):
        configure_nas(tmp_path / 'operator', bundle, {'host': host})


def test_public_status_never_contains_env_or_paths():
    result = public_status({'phase': 'verifying', 'current': {'version': '2.0.0', 'sha': 'a'*40,
                            'compose_path': '/PRIVATE', 'password': 'SECRET'}, 'maintenance_token': 'TOKEN'})
    assert result['can_rollback']
    assert all(secret not in json.dumps(result) for secret in ('PRIVATE', 'SECRET', 'TOKEN'))


@pytest.mark.skipif(os.name != 'posix', reason='Gate runs inside the Linux API container, not native Windows')
def test_gate_is_idempotent_and_never_deletes_foreign_owner(tmp_path):
    environment = dict(os.environ, OPERATOR_INSTANCE_FILE=str(tmp_path / 'instance-id'))
    def invoke(action, token):
        result = subprocess.run([sys.executable, '-c', _GATE_PROGRAM, action, token], env=environment,
                                text=True, capture_output=True, check=True)
        return json.loads(result.stdout)
    assert invoke('claim', 'ours')['owned']
    assert invoke('claim', 'ours')['owned']  # retry after unknown commit
    assert not invoke('claim', 'theirs')['owned']
    assert not invoke('release', 'theirs')['owned']
    assert (tmp_path / 'operator-update.json').exists()
    assert invoke('release', 'ours')['owned']
    assert not (tmp_path / 'operator-update.json').exists()


def test_crash_mid_setup_recovers_original_passwords_and_identity(bundle, tmp_path, monkeypatch):
    import core.updating.packaged as packaged
    root = tmp_path / 'operator'
    original_replace = packaged.os.replace
    def crash_once(source, destination):
        if destination == root / 'deploy/nas/.env.operator':
            raise OSError('simulated interruption')
        return original_replace(source, destination)
    monkeypatch.setattr(packaged.os, 'replace', crash_once)
    with pytest.raises(OSError):
        configure_nas(root, bundle, {'host': 'nas', 'db_password': 'original', 'access_key': 'key', 'secret_key': 'original-secret'})
    staged = root / '.update-state/setup-pending/deploy/nas/.env.operator'
    expected = staged.read_bytes()
    monkeypatch.setattr(packaged.os, 'replace', original_replace)
    finish_setup(root)
    assert (root / 'deploy/nas/.env.operator').read_bytes() == expected
    finish_setup(root)
    assert (root / 'deploy/nas/.env.operator').read_bytes() == expected


def test_foreign_file_is_not_overwritten_during_setup_recovery(bundle, tmp_path, monkeypatch):
    import core.updating.packaged as packaged
    root = tmp_path / 'operator'
    monkeypatch.setattr(packaged, '_finish_setup', lambda *args: None)
    configure_nas(root, bundle, {'host': 'nas', 'db_password': 'original', 'access_key': 'key', 'secret_key': 'secret'})
    (root / 'docker-compose.yml').write_text('operator changes')
    monkeypatch.undo()
    with pytest.raises(UpdateError, match='не перезаписан'):
        finish_setup(root)
    assert (root / 'docker-compose.yml').read_text() == 'operator changes'
