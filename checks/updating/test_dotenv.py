"""Actual Compose interpolation, without a Docker daemon or NAS connection."""
import json
import os
import shutil
import subprocess

import pytest

from core.updating.packaged import _env_value
from core.updating.state import atomic_json, atomic_text


@pytest.mark.skipif(not shutil.which('docker'), reason='Compose CLI unavailable; no daemon required')
@pytest.mark.parametrize('value', ["p$#'\\", 'C:\\Users\\Оператор\\AppData\\state', ' spaces # dollars $ ', 'двойная"кавычка', "slash\\'quote", 'two\\\\slashes', '${NOT_A_VARIABLE}'])
def test_generated_credentials_and_windows_paths_survive_real_compose(tmp_path, value):
    env_file = tmp_path / '.env.operator'
    atomic_text(env_file, 'INPUT=' + _env_value(value) + '\n')
    compose = tmp_path / 'compose.json'
    atomic_json(compose, {'name': 'pc-parse-fixture', 'services': {'fixture': {'image': 'hello-world', 'environment': {'OUTPUT': '${INPUT}'}}}})
    environment = {key: item for key, item in os.environ.items() if key not in {'DOCKER_HOST', 'DOCKER_CONTEXT', 'COMPOSE_FILE', 'COMPOSE_PROJECT_NAME', 'INPUT'}}
    result = subprocess.run(['docker', 'compose', '--project-directory', str(tmp_path), '--env-file', str(env_file), '-f', str(compose), 'config', '--format', 'json'],
                            env=environment, text=True, encoding='utf-8', capture_output=True, timeout=15)
    assert result.returncode == 0, 'Offline Compose interpolation failed (output omitted)'
    parsed = json.loads(result.stdout)
    assert parsed['services']['fixture']['environment']['OUTPUT'].replace('$$', '$') == value
    # The private snapshot must survive a second Compose parse unchanged too.
    atomic_json(compose, parsed)
    again = subprocess.run(['docker', 'compose', '--project-directory', str(tmp_path), '-f', str(compose), 'config', '--format', 'json'],
                           env=environment, text=True, encoding='utf-8', capture_output=True, timeout=15)
    assert again.returncode == 0
    assert json.loads(again.stdout)['services']['fixture']['environment']['OUTPUT'].replace('$$', '$') == value
