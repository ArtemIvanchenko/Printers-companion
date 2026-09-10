"""Exercise the updater without touching Git remotes or Docker services."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize('running,expected', [
    ('api\nworker\npostgres\n', {'api', 'worker', 'postgres', 'estimator', 'nas-sync'}),
    ('api\nestimator\nnas-sync\n', {'api', 'estimator', 'nas-sync'}),
    ('postgres\nminio\nredis\n', {'postgres', 'minio', 'redis'}),
    ('', {'api', 'worker', 'estimator', 'nas-sync', 'watcher', 'scheduler'}),
])
def test_update_completes_operator_workers_but_preserves_storage_only(tmp_path, running, expected):
    bash = shutil.which('bash')
    if not bash:
        pytest.skip('bash is not available')
    root = Path(__file__).parents[1]
    shutil.copyfile(root / 'update.sh', tmp_path / 'update.sh')
    commands = {
        'git': '''#!/bin/sh
case "$*" in
  'rev-parse --short HEAD') echo abc1234 ;;
  'rev-parse HEAD'|'rev-parse origin/main') echo abc123456789 ;;
esac
''',
        'docker': '''#!/bin/sh
case "$*" in
  'compose ps --services --filter status=running') printf '%s' "$TEST_RUNNING" ;;
  'compose up -d --build '*) printf '%s\\n' "$@" > "$TEST_CAPTURE" ;;
  *) exit 1 ;;
esac
''',
        'curl': '#!/bin/sh\nexit 0\n',
    }
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, source in commands.items():
        executable = bin_dir / name
        executable.write_text(source)
        executable.chmod(0o700)
    capture = tmp_path / 'compose-arguments'
    env = {**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
           'TEST_RUNNING': running, 'TEST_CAPTURE': str(capture)}
    result = subprocess.run([bash, str(tmp_path / 'update.sh')], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    arguments = capture.read_text().splitlines()
    assert arguments[:4] == ['compose', 'up', '-d', '--build']
    assert set(arguments[4:]) == expected
    assert len(arguments[4:]) == len(expected)
    assert (tmp_path / '.last_deployed').read_text().strip() == 'abc123456789'


def test_powershell_updater_completes_workers_and_stamps_build():
    source = (Path(__file__).parents[1] / 'update.ps1').read_text()
    assert '$Services -contains "api" -or $Services -contains "worker"' in source
    assert 'foreach ($Required in @("estimator", "nas-sync"))' in source
    assert '$Services -notcontains $Required' in source
    assert '$env:GIT_COMMIT = (git rev-parse --short HEAD).Trim()' in source
    assert 'git pull --ff-only origin main -q' in source


def test_local_import_data_is_not_copied_into_images():
    patterns = (Path(__file__).parents[1] / '.dockerignore').read_text().splitlines()
    for name in ('.operator-state/', '.codex-tmp/', 'backups/', 'raw_logs/', '.env.secrets'):
        assert name in patterns
