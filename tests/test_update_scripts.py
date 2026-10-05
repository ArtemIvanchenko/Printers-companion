"""Thin compatibility wrappers delegate to the same transactional host engine."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize('exit_code', [0, 3])
def test_bash_wrapper_preserves_arguments_and_failure_without_git_or_docker(tmp_path, exit_code):
    bash = shutil.which('bash')
    if not bash:
        pytest.skip('bash is unavailable on this Windows runner')
    root = tmp_path / "Printer's companion с пробелами"
    (root / 'scripts/maintenance').mkdir(parents=True)
    (root / '.venv/bin').mkdir(parents=True)
    shutil.copy2(ROOT / 'update.sh', root / 'update.sh')
    (root / '.venv/bin/python').symlink_to(sys.executable)
    script = root / 'scripts/maintenance/update_runtime.py'
    script.write_text('import json,os,sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(int(os.environ["TEST_EXIT"]))\n')
    result = subprocess.run([bash, str(root / 'update.sh'), 'configure', '--env-file', 'папка с пробелами/.env'],
                            env=dict(os.environ, TEST_EXIT=str(exit_code)), text=True, capture_output=True)
    assert result.returncode == exit_code
    assert json.loads(result.stdout) == ['--root', str(root), 'configure', '--env-file', 'папка с пробелами/.env']


def test_wrappers_do_not_update_branches_delete_storage_or_install_host_daemons():
    for name in ('update.sh', 'update.ps1'):
        source = (ROOT / name).read_text()
        assert 'scripts' in source and 'update_runtime.py' in source
        executable_source = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('#'))
        for forbidden in ('git pull', 'reset --hard', 'down -v', 'docker prune', 'launchctl', 'schtasks'):
            assert forbidden not in executable_source
    source = (ROOT / 'update.ps1').read_text()
    assert 'Get-FileHash' in source and 'SetAccessRuleProtection' in source
    assert 'exit $LASTEXITCODE' in source


def test_local_import_and_desktop_runtime_data_are_not_copied_into_images():
    patterns = (ROOT / '.dockerignore').read_text().splitlines()
    for name in ('.operator-state/', '.update-state/', '.codex-tmp/', 'backups/', 'raw_logs/', '.env.secrets', 'desktop/'):
        assert name in patterns
