"""Opt-in real Docker lifecycle; never uses an operator/NAS database.

The tiny fixture substitutes only release transport and SQL readiness/job reads.
Pause/KILL, image replacement, full-SHA HTTP verification, admission-file drain,
rollback and bind preservation use the actual engine. This is not a full
analytics/NAS end-to-end assertion. Run with COMPANION_DOCKER_CHECKS=1.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest

from core.updating.releases import Release, UpdateBusy, UpdateError
from core.updating.runtime import Commands, REQUIRED, Updater, _DATABASE_PROBE
from core.updating.state import atomic_json

pytestmark = pytest.mark.skipif(os.environ.get('COMPANION_DOCKER_CHECKS') != '1', reason='Opt-in isolated Docker fixture')
OLD_SHA, NEW_SHA = 'a' * 40, 'b' * 40
ROOT = Path(__file__).resolve().parents[2]

SERVER = r'''import json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from core.maintenance import write_admission
state=Path('/state')
if os.environ['ROLE']!='api':
    while True: time.sleep(1)
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        value={'status':'ready','build':{'git_sha':os.environ['TEST_SHA']},
               'checks':{key:True for key in ('database','schema','redis','minio','minio_buckets')}}
        self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(value).encode())
    def do_POST(self):
        with write_admission() as admitted:
            if not admitted:
                self.send_response(503); self.end_headers(); return
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            if self.path=='/hold':
                (state/'write-started').write_text('started')
                deadline=time.monotonic()+30
                while not (state/'write-release').exists():
                    if time.monotonic()>deadline: raise TimeoutError('fixture writer')
                    time.sleep(0.05)
                (state/'write-completed').write_text('durable result')
            self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
ThreadingHTTPServer(('0.0.0.0',8000),Handler).serve_forever()
'''


class TraceCommands(Commands):
    def __init__(self, root):
        super().__init__(root)
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append(args)
        if _DATABASE_PROBE in args:
            return '{"database":true,"schema":true}'  # fixture has no NAS/SQL
        return super().run(args, **kwargs)


class FixtureUpdater(Updater):
    def _clean_sources(self):
        pass  # fixture resources are generated inside tmp_path, not a Git repo

    def _stage(self, release):
        return self.root / 'candidate'

    def _pin_images(self, candidate, services, release):
        for name in services:
            image = self._json_docker('image', 'inspect', candidate['services'][name]['image'])[0]
            assert image['Config']['Labels']['org.opencontainers.image.revision'] == release.sha
            candidate['services'][name]['image'] = image['Id']

    def _check_schema(self, target):
        pass  # DB schema/readiness has independent application/migration CI

    def _busy(self, api_id):
        count = int(self._docker('exec', api_id, 'python', '-c', "from pathlib import Path; print(Path('/state/running-jobs').read_text())"))
        if count:
            raise UpdateBusy('fixture running job')


@pytest.fixture(scope='module')
def docker_installation(tmp_path_factory):
    if not shutil.which('docker'):
        pytest.fail('Opt-in Docker check requires a local daemon')
    root = tmp_path_factory.mktemp('companion-docker')
    project = 'pc-update-fixture-' + uuid4().hex[:12]
    context = subprocess.check_output(['docker', 'context', 'show'], text=True).strip()
    # The production engine also validates local unix/npipe context before use.
    compose = root / 'compose.json'
    state = root / 'operator-state'
    state.mkdir()
    (state / 'running-jobs').write_text('0')
    (state / 'instance-id').write_text('original-instance')
    (state / 'sentinel').write_bytes(b'original outbox and settings')
    (root / 'core').mkdir()
    shutil.copy2(ROOT / 'core/maintenance.py', root / 'core/maintenance.py')
    (root / 'server.py').write_text(SERVER)
    (root / 'Dockerfile').write_text('FROM python:3.11-alpine\nWORKDIR /app\nCOPY core /app/core\nCOPY server.py /app/server.py\nARG SHA\nARG VERSION\nENV TEST_SHA=$SHA\nLABEL org.opencontainers.image.revision=$SHA org.opencontainers.image.version=$VERSION\n')
    def docker(*args):
        return subprocess.check_output(['docker', '--context', context, *args], cwd=root, text=True, stderr=subprocess.STDOUT).strip()
    tags = [project + ':old', project + ':new']
    try:
        for tag, sha, version in zip(tags, (OLD_SHA, NEW_SHA), ('1.7.0', '2.0.0'), strict=True):
            docker('build', '-t', tag, '--build-arg', 'SHA=' + sha, '--build-arg', 'VERSION=' + version, '.')
        configuration = {'name': project, 'services': {}}
        for name in sorted(REQUIRED):
            configuration['services'][name] = {
                'image': tags[0], 'command': ['python', '/app/server.py'], 'restart': 'unless-stopped',
                'environment': {'ROLE': name, 'COMPUTE_NODE_ID': 'fixture-pc', 'OPERATOR_INSTANCE_FILE': '/state/instance-id'},
                'volumes': [{'type': 'bind', 'source': str(state), 'target': '/state'}],
            }
        configuration['services']['api']['ports'] = [{'target': 8000, 'published': '0', 'host_ip': '127.0.0.1', 'protocol': 'tcp'}]
        atomic_json(compose, configuration)
        docker('compose', '-f', str(compose), 'up', '-d', '--no-build', '--pull', 'never')
        host_port = docker('compose', '-f', str(compose), 'port', 'api', '8000').rsplit(':', 1)[-1]
        url = 'http://127.0.0.1:' + host_port
        deadline = time.monotonic() + 30
        while not Updater._readiness(url, OLD_SHA):
            if time.monotonic() > deadline:
                pytest.fail('Docker fixture API did not start')
            time.sleep(0.1)
        candidate = deepcopy(configuration)
        # Keep the resolved randomly allocated port throughout replacement.
        configuration['services']['api']['ports'][0]['published'] = host_port
        candidate['services']['api']['ports'][0]['published'] = host_port
        atomic_json(compose, configuration)
        for service in candidate['services'].values():
            service['image'] = tags[1]
        atomic_json(root / 'candidate/compose.json', candidate)
        commands = TraceCommands(root)
        updater = FixtureUpdater(root, commands=commands,
                                 release_loader=lambda version: Release('2.0.0', 'v2.0.0', NEW_SHA, '', '', 'fixture'),
                                 progress=lambda message: None)
        updater.configure(compose_files=['compose.json'], url=url, context=context)
        yield updater, commands, state, url
    finally:
        if compose.exists():
            docker('compose', '-f', str(compose), 'down', '--remove-orphans')
        for tag in tags:
            docker('image', 'rm', tag)


def test_docker_freeze_kill_switch_and_data_preservation(docker_installation):
    updater, commands, state, _ = docker_installation
    result = updater.apply(timeout=15)
    assert result['phase'] == 'completed' and result['current']['sha'] == NEW_SHA
    assert any('pause' in call for call in commands.calls)
    assert any('kill' in call for call in commands.calls)
    assert (state / 'sentinel').read_bytes() == b'original outbox and settings'
    assert (state / 'instance-id').read_text() == 'original-instance'
    assert not (state / 'operator-update.json').exists()
    updater.rollback(timeout=15)
    assert updater.status()['current']['sha'] == OLD_SHA


def test_docker_old_image_is_restored_after_real_new_start(docker_installation):
    updater, _, state, _ = docker_installation
    original = updater.readiness
    updater.readiness = lambda url, sha: sha != NEW_SHA and original(url, sha)
    try:
        with pytest.raises(UpdateError, match='готовность'):
            updater.apply(timeout=1)
    finally:
        updater.readiness = original
    assert updater.status()['phase'] == 'rolled_back'
    assert updater.status()['current']['sha'] == OLD_SHA
    assert not (state / 'operator-update.json').exists()
    assert (state / 'sentinel').read_bytes() == b'original outbox and settings'


def test_docker_running_job_is_not_killed(docker_installation):
    updater, commands, state, _ = docker_installation
    (state / 'running-jobs').write_text('1')
    commands.calls.clear()
    try:
        with pytest.raises(UpdateBusy):
            updater.apply(timeout=15)
    finally:
        (state / 'running-jobs').write_text('0')
    assert not any('kill' in call or 'pause' in call for call in commands.calls)


def test_docker_real_inflight_http_write_finishes_before_replacement(docker_installation):
    updater, _, state, url = docker_installation
    def upload():
        with urlopen(Request(url + '/hold', data=b'input', method='POST'), timeout=30) as response:
            return response.status
    entered_drain = threading.Event()
    updater.progress = lambda message: entered_drain.set() if 'принятых' in message else None
    with ThreadPoolExecutor(max_workers=2) as pool:
        request = pool.submit(upload)
        deadline = time.monotonic() + 10
        while not (state / 'write-started').exists():
            if time.monotonic() > deadline:
                pytest.fail('Fixture upload did not start')
            time.sleep(0.05)
        switch = pool.submit(updater.apply, timeout=15)
        assert entered_drain.wait(10)
        assert not switch.done()
        (state / 'write-release').write_text('continue')
        assert request.result() == 200
        assert switch.result()['phase'] == 'completed'
    assert (state / 'write-completed').read_text() == 'durable result'
    assert (state / 'sentinel').read_bytes() == b'original outbox and settings'
