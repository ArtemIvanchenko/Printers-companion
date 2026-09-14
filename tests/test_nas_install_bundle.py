"""Offline tests of initial-install packaging and preservation of credentials."""
import os
from pathlib import Path
import subprocess
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'deploy/nas/prepare-mac.command'


def setup_runner(tmp_path, *, download_ok=True):
    tools = tmp_path / 'tools'
    tools.mkdir()
    # Do not depend on GitHub or open Finder while testing.
    curl = tools / 'curl'
    curl.write_text(
        '#!/bin/bash\nfor last; do :; done\n'
        + ('cp "$COMPOSE_FIXTURE" "$last"\n' if download_ok else 'exit 22\n')
    )
    curl.chmod(0o700)
    opener = tools / 'open'
    opener.write_text('#!/bin/bash\nexit 0\n')
    opener.chmod(0o700)
    env = dict(os.environ, PATH=f'{tools}:{os.environ["PATH"]}',
               COMPOSE_FIXTURE=str(ROOT / 'deploy/nas/docker-compose.yml'))
    destination = tmp_path / 'bundle'

    def run():
        return subprocess.run(['/bin/bash', str(SCRIPT), str(destination)],
                              env=env, capture_output=True, text=True, timeout=20)
    return destination, run


def test_repeat_preserves_credentials_and_exact_archive_members(tmp_path):
    destination, run = setup_runner(tmp_path)
    first = run()
    assert first.returncode == 0, first.stderr
    archive = destination / 'NAS-files.zip'
    before = archive.read_bytes()
    secret = (destination / '.env').read_text()
    second = run()
    assert second.returncode == 0, second.stderr
    assert archive.read_bytes() == before
    with zipfile.ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == {'docker-compose.yml', '.env'}
        assert bundle.read('.env').decode() == secret
    for line in secret.splitlines():
        if 'PASSWORD=' in line:
            password = line.split('=', 1)[1]
            assert len(password) == 40
            assert password not in first.stdout + first.stderr + second.stdout + second.stderr


def test_modified_credentials_are_not_replaced(tmp_path):
    destination, run = setup_runner(tmp_path)
    assert run().returncode == 0
    secret_file = destination / '.env'
    secret_file.write_text('operator modification\n')
    assert run().returncode != 0
    assert secret_file.read_text() == 'operator modification\n'


def test_download_failure_does_not_publish_bundle(tmp_path):
    destination, run = setup_runner(tmp_path, download_ok=False)
    assert run().returncode != 0
    assert not destination.exists()
