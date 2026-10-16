import io
import json
import os
from pathlib import Path
from copy import deepcopy
from unittest.mock import Mock

import pytest

from core.updating.releases import IMAGE_REPOSITORY, Release, UpdateBusy, UpdateError
from core.updating.packaged import PackagedUpdater
from core.updating.runtime import Updater, APPLICATIONS, _GATE_PROGRAM, _DRAIN_PROBE, _BUSY_PROBE, _SCHEMA_PROBE, _DATABASE_PROBE
from core.updating.state import atomic_json, read_json, installation_lock

OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
RELEASE = Release("2.0.0", "v2.0.0", NEW_SHA, "https://example.invalid/release", "today", "stable")


class FakeCommands:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.busy_counts = [0, 0]
        self.dirty = False
        self.bad_image = False
        self.schema_ok = True
        self.endpoint = "unix:///fake/docker.sock"
        self.daemon = "local-daemon"
        self.git_sha = NEW_SHA
        self.gate = None
        self.drain_ok = True
        self.fail_switch = False
        self.fail_rollback = False
        self.before = {"name": "operator-one", "volumes": {"operator_state": {"name": "operator-one_operator_state"}},
                       "services": {}}
        for name, image in APPLICATIONS.items():
            self.before["services"][name] = {
                "image": f"{IMAGE_REPOSITORY}:{image}", "build": {"context": str(root)},
                "environment": {"COMPUTE_NODE_ID": "pc-1", "DATABASE_URL": "SECRET_DATABASE",
                                "MINIO_ENDPOINT": "nas:9000", "MINIO_ROOT_PASSWORD": "VERY_PRIVATE"},
                "volumes": [{"type": "volume", "source": "operator_state", "target": "/var/lib/printer-companion"}],
            }
        for name in ("postgres", "minio", "redis"):
            self.before["services"][name] = {"image": name + ":pinned"}
        self.after = deepcopy(self.before)
        self.containers = {}
        self.images = {}
        for index, name in enumerate(self.before["services"]):
            image_id = "sha256:" + f"{index+1:064x}"
            labels = {"com.docker.compose.service": name, "com.docker.compose.project": "operator-one",
                      "com.docker.compose.project.working_dir": str(root),
                      "com.docker.compose.project.config_files": str(root / "docker-compose.yml"),
                      "org.opencontainers.image.version": "1.7.0", "org.opencontainers.image.revision": OLD_SHA}
            self.containers[name] = {"Id": "id-" + name, "Image": image_id,
                                     "Config": {"Labels": labels, 'Env': [f'{key}={value}' for key, value in self.before['services'][name].get('environment', {}).items()]},
                                     "State": {"Running": name not in {'scheduler', 'mcp'}, "Paused": False}}
            self.containers[name]['Mounts'] = [{'Destination': volume['target'], 'Type': 'volume', 'Name': 'operator-one_operator_state', 'RW': True}
                                              for volume in self.before['services'][name].get('volumes', [])]
            self.images[image_id] = {"Id": image_id, "Config": {"Labels": labels}}

    def run(self, arguments, *, timeout=120, check=True):
        self.calls.append(list(arguments))
        if arguments[0] == "git":
            args = arguments[1:]
            if args[:1] == ["-C"]:
                return "" if "status" in args else NEW_SHA
            if args[0] == "status":
                return " M edited.py" if self.dirty else ""
            if args[:2] == ["remote", "get-url"]:
                return "https://github.com/ArtemIvanchenko/Printers-companion.git"
            if args[0] == "rev-parse":
                return self.git_sha
            if args[:2] == ["worktree", "add"]:
                directory = Path(args[-2])
                directory.mkdir(parents=True)
                (directory / "VERSION").write_text("2.0.0\n")
                (directory / "docker-compose.yml").write_text("# fake\n")
            return ""
        assert arguments[0] == "docker"
        args = arguments[1:]
        if args == ["context", "show"]:
            return "default"
        if args[:2] == ["context", "inspect"]:
            return json.dumps([{"Endpoints": {"docker": {"Host": self.endpoint}}}])
        assert args[:2] == ["--context", "default"]
        args = args[2:]
        if args[0] == "info":
            return self.daemon
        if args[:2] == ["compose", "version"]:
            return "2.30.0"
        if args[0] == "ps":
            return " ".join(item["Id"] for name, item in self.containers.items()
                            if "label=com.docker.compose.service=api" not in args or name == "api")
        if args[0] == "inspect":
            return json.dumps([item for item in self.containers.values() if item["Id"] in args[1:]])
        if args[0] == 'start':
            for item in self.containers.values():
                if item['Id'] in args[1:]:
                    item['State']['Running'] = True
            return ''
        if args[0] == "pull":
            tag = args[-1]
            service = tag.split(":")[-1].split("-v")[0]
            digest = "sha256:" + {"api": "c", "worker": "d", "watcher": "e", "scheduler": "f", "mcp": "9"}[service] * 64
            image = {"Id": digest, "Os": "linux", "Architecture": "amd64", "RepoDigests": [IMAGE_REPOSITORY + "@" + digest],
                     "Config": {"Labels": {"org.opencontainers.image.version": "2.0.0",
                                           "org.opencontainers.image.revision": "incorrect" if self.bad_image else NEW_SHA,
                                           "io.printers-companion.source-state": "clean"}}}
            self.images[tag] = self.images[digest] = self.images[IMAGE_REPOSITORY + "@" + digest] = image
            return ""
        if args[:2] == ["image", "inspect"]:
            return json.dumps([self.images[args[2]]])
        if args[0] == "exec":
            if _DRAIN_PROBE in args:
                if not self.drain_ok:
                    raise UpdateError('write drain timeout')
                return '{"drained":true}'
            if _BUSY_PROBE in args:
                count = self.busy_counts.pop(0) if self.busy_counts else 0
                return json.dumps({"running_jobs": count})
            if _GATE_PROGRAM in args:
                action, token = args[-2:]
                if action == "claim":
                    if not self.gate:
                        self.gate = token
                elif self.gate == token:
                    self.gate = None
                return json.dumps({'owned': not self.gate or self.gate == token})
        if args[0] in {"pause", "unpause", "kill"}:
            item = next(item for item in self.containers.values() if item["Id"] == args[-1])
            if args[0] == "kill":
                assert item["State"]["Paused"]
                item["State"].update(Running=False, Paused=False)
            else:
                item["State"]["Paused"] = args[0] == "pause"
            return ""
        if args[0] == "compose":
            if "config" in args:
                return json.dumps(self.after if any("/releases/" in arg.replace("\\", "/") for arg in args) else self.before)
            if "run" in args:
                assert _SCHEMA_PROBE in args or _DATABASE_PROBE in args
                if not self.schema_ok:
                    raise UpdateError("schema mismatch")
                return '{"database": true, "schema": true}'
            path = Path(args[args.index("-f") + 1])
            snapshot = read_json(path)
            if "up" in args:
                target = "candidate.compose" in path.name
                if (target and self.fail_switch) or (not target and self.fail_rollback):
                    raise UpdateError("compose failure")
                services = args[args.index("never") + 1:]
                services = [name for name in services if name in APPLICATIONS]
                for name in services:
                    config = snapshot["services"][name]
                    image = self.images[config["image"]]
                    self.containers[name]["Image"] = image["Id"]
                    self.containers[name]["Config"]["Labels"].update(image["Config"]["Labels"])
                    self.containers[name]['Config']['Env'] = [f'{key}={value}' for key, value in config.get('environment', {}).items()]
                    self.containers[name]["State"].update(Running=True, Paused=False)
                return ""
            if "stop" in args:
                for name in args[args.index("30") + 1:]:
                    self.containers[name]["State"]["Running"] = False
                return ""
        raise AssertionError(arguments)


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "Printer's companion с пробелами"
    root.mkdir()
    (root / "docker-compose.yml").write_text("# fixture\n")
    commands = FakeCommands(root)
    updater = Updater(root, commands=commands, release_loader=lambda version: RELEASE,
                      readiness=lambda url, sha: True, progress=lambda message: None)
    monkeypatch.setattr(updater, "_notify", Mock())
    updater.configure()
    return updater, commands


def mutating_commands(commands):
    return [call for call in commands.calls if ("pause" in call or "kill" in call or "up" in call)]


def test_failed_native_stop_keeps_recovery_journal_until_launch(installation, monkeypatch):
    updater, commands = installation
    updater.launch(timeout=0)
    original_run = commands.run

    def fail_api_stop(arguments, **kwargs):
        if 'compose' in arguments and 'stop' in arguments:
            raise UpdateError('Остановка API не подтверждена')
        return original_run(arguments, **kwargs)

    monkeypatch.setattr(commands, 'run', fail_api_stop)
    with pytest.raises(UpdateError, match='Остановка API'):
        PackagedUpdater.stop(updater)
    interrupted = updater.status()
    assert interrupted['phase'] == 'switching'
    assert commands.gate == interrupted['maintenance_token']
    assert not commands.containers['worker']['State']['Running']
    assert not commands.containers['watcher']['State']['Running']
    assert commands.containers['api']['State']['Running']

    monkeypatch.setattr(commands, 'run', original_run)
    recovered = updater.launch(timeout=0)
    assert recovered['phase'] == 'rolled_back'
    assert recovered['current'] == interrupted['current']
    assert recovered['maintenance_token'] is None
    assert commands.gate is None
    assert all(commands.containers[name]['State']['Running'] for name in ('api', 'worker', 'watcher'))


def test_success_preserves_sources_and_storage_and_pins_digest(installation):
    updater, commands = installation
    state = updater.apply(timeout=0)
    assert state["phase"] == "completed"
    assert state["current"]["sha"] == NEW_SHA
    assert state["previous"]["sha"] == OLD_SHA
    assert not commands.gate
    assert not any("merge" in call or "pull" in call and call[0] == "git" or "reset" in call for call in commands.calls)
    up = next(call for call in commands.calls if "up" in call)
    assert "--no-deps" in up and "--no-build" in up
    assert not {"postgres", "minio", "redis"}.intersection(up)
    assert all(commands.containers[name]["Image"] == "sha256:" + f"{i+8:064x}"
               for i, name in enumerate(("postgres", "minio", "redis")))
    assert "VERY_PRIVATE" not in json.dumps(state)
    configuration = read_json(Path(state["current"]["compose_path"]))
    assert all(configuration["services"][name]["image"].startswith(IMAGE_REPOSITORY + "@sha256:") for name in state["current"]["services"])
    if os.name != "nt":
        assert Path(state["current"]["compose_path"]).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", ["dirty", "schema", "image", "sha", "network"])
def test_preparation_failures_leave_running_app_untouched(installation, failure):
    updater, commands = installation
    commands.dirty = failure == "dirty"
    commands.schema_ok = failure != "schema"
    commands.bad_image = failure == "image"
    if failure == "sha":
        commands.git_sha = "c" * 40
    if failure == "network":
        updater.release_loader = Mock(side_effect=UpdateError("offline"))
    with pytest.raises(UpdateError):
        updater.apply(timeout=0)
    assert not mutating_commands(commands)
    assert not commands.gate
    assert commands.containers["api"]["Image"] == "sha256:" + f"{1:064x}"


@pytest.mark.parametrize("counts", [[2], [0, 1]])
def test_running_or_just_claimed_job_is_not_killed(installation, counts):
    updater, commands = installation
    commands.busy_counts = counts
    with pytest.raises(UpdateError, match="выполняет"):
        updater.apply(timeout=0)
    assert not any("kill" in call or "up" in call for call in commands.calls)
    assert not any(item["State"]["Paused"] for item in commands.containers.values())
    assert not commands.gate


def test_failed_new_readiness_restores_old_version_not_false_success(installation):
    updater, commands = installation
    updater.readiness = lambda url, sha: sha == OLD_SHA
    with pytest.raises(UpdateError, match="готовность"):
        updater.apply(timeout=0)
    state = updater.status()
    assert state["phase"] == "rolled_back"
    assert state["current"]["sha"] == OLD_SHA
    assert not commands.gate
    assert commands.containers["api"]["Image"] == "sha256:" + f"{1:064x}"


def test_active_http_write_is_never_interrupted(installation):
    updater, commands = installation
    commands.drain_ok = False
    with pytest.raises(UpdateError, match='загрузки'):
        updater.apply(timeout=0)
    assert not mutating_commands(commands)
    assert not commands.gate
    assert updater.status()['phase'] == 'blocked'


def test_write_drain_precedes_freezing_and_switching(installation):
    updater, commands = installation
    updater.apply(timeout=0)
    drain = next(index for index, call in enumerate(commands.calls) if _DRAIN_PROBE in call)
    pause = next(index for index, call in enumerate(commands.calls) if 'pause' in call)
    kill = next(index for index, call in enumerate(commands.calls) if 'kill' in call)
    assert drain < pause < kill


def test_native_restart_drains_and_freezes_but_never_switches_images(installation):
    updater, commands = installation
    updater.apply(timeout=0)
    commands.calls.clear()
    original_images = {name: item['Image'] for name, item in commands.containers.items()}
    state = PackagedUpdater.prepare_restart(updater)
    assert state['phase'] == 'restart_ready'
    assert commands.gate
    assert not any('kill' in call or 'stop' in call or 'up' in call for call in commands.calls)
    assert {name: item['Image'] for name, item in commands.containers.items()} == original_images
    assert commands.containers['api']['State']['Running']
    drain = next(index for index, call in enumerate(commands.calls) if _DRAIN_PROBE in call)
    pause = next(index for index, call in enumerate(commands.calls) if 'pause' in call)
    assert drain < pause


def test_native_restart_cancellation_releases_only_its_gate_and_resumes(installation):
    updater, commands = installation
    updater.apply(timeout=0)
    PackagedUpdater.prepare_restart(updater)
    state = PackagedUpdater.cancel_restart(updater)
    assert state['phase'] == 'interrupted'
    assert not commands.gate
    assert not any(item['State']['Paused'] for item in commands.containers.values())
    assert all(commands.containers[name]['State']['Running'] for name in state['current']['services'])


def test_native_restart_cancellation_resumes_and_verifies_exited_consumer_before_releasing_gate(installation):
    updater, commands = installation
    updater.apply(timeout=0)
    prepared = PackagedUpdater.prepare_restart(updater)
    selected_sha = prepared['current']['sha']
    token = prepared['maintenance_token']
    commands.containers['worker']['State'].update(Running=False, Paused=False)
    commands.calls.clear()
    verified = []

    def readiness(url, sha):
        assert commands.gate == token
        assert updater.status()['phase'] == 'restart_ready'
        assert all(commands.containers[name]['State']['Running'] for name in prepared['current']['services'])
        verified.append(sha)
        return True

    updater.readiness = readiness
    recovered = PackagedUpdater.cancel_restart(updater)

    assert recovered['phase'] == 'interrupted'
    assert recovered['current']['sha'] == selected_sha
    assert commands.containers['worker']['State']['Running']
    assert verified == [selected_sha]
    assert commands.gate is None
    start = next(index for index, call in enumerate(commands.calls) if call[-2:] == ['start', 'id-worker'])
    release = next(index for index, call in enumerate(commands.calls) if _GATE_PROGRAM in call and call[-2] == 'release')
    assert start < release
    assert not mutating_commands(commands)


@pytest.mark.parametrize('failure', ['start_failure', 'worker_stays_exited', 'wrong_image', 'readiness'])
def test_native_restart_failed_restore_preserves_gate_and_retryable_journal(installation, monkeypatch, failure):
    updater, commands = installation
    updater.apply(timeout=0)
    prepared = PackagedUpdater.prepare_restart(updater)
    token = prepared['maintenance_token']
    current = deepcopy(prepared['current'])
    image = commands.containers['worker']['Image']
    commands.containers['worker']['State'].update(Running=False, Paused=False)
    commands.calls.clear()
    run = commands.run

    def failed_start(arguments, **kwargs):
        if arguments[-2:] == ['start', 'id-worker']:
            commands.calls.append(list(arguments))
            if failure == 'start_failure':
                raise UpdateError('exact consumer start failed')
            return ''  # Docker success without a running consumer is not proof.
        return run(arguments, **kwargs)

    if failure in {'start_failure', 'worker_stays_exited'}:
        monkeypatch.setattr(commands, 'run', failed_start)
    elif failure == 'wrong_image':
        commands.containers['worker']['Image'] = 'sha256:unexpected-version'
    else:
        updater.readiness = lambda url, sha: False
    verify = updater._verify
    monkeypatch.setattr(updater, '_verify', lambda snapshot, options, timeout: verify(snapshot, options, timeout=0))

    with pytest.raises(UpdateError):
        PackagedUpdater.cancel_restart(updater)

    saved = updater.status()
    assert saved['phase'] == 'restart_ready'
    assert saved['current'] == current
    assert saved['maintenance_token'] == token == commands.gate
    assert not any(_GATE_PROGRAM in call and call[-2] == 'release' for call in commands.calls)
    assert not mutating_commands(commands)
    if failure == 'wrong_image':
        assert not any('start' in call for call in commands.calls)
    monkeypatch.setattr(commands, 'run', run)
    commands.containers['worker']['Image'] = image
    updater.readiness = lambda url, sha: sha == current['sha']

    recovered = PackagedUpdater.cancel_restart(updater)
    assert recovered['phase'] == 'interrupted'
    assert recovered['current'] == current
    assert commands.gate is None
    assert commands.containers['worker']['State']['Running']


@pytest.mark.parametrize('failure', ['api_status', 'schema', 'sha'])
def test_native_restart_cancellation_checks_current_readiness_contract_before_gate_release(installation, monkeypatch, failure):
    updater, commands = installation
    updater.apply(timeout=0)
    prepared = PackagedUpdater.prepare_restart(updater)
    token = prepared['maintenance_token']
    healthy = {'status': 'ready', 'build': {'git_sha': prepared['current']['sha']},
               'checks': {name: True for name in ('database', 'schema', 'redis', 'minio', 'minio_buckets')}}
    evidence = deepcopy(healthy)
    if failure == 'api_status':
        evidence['status'] = 'unavailable'
    elif failure == 'schema':
        evidence['checks']['schema'] = False
    else:
        evidence['build']['git_sha'] = OLD_SHA

    def response(url, timeout):
        assert url.endswith('/health/ready')
        assert commands.gate == token
        return io.BytesIO(json.dumps(evidence).encode())

    monkeypatch.setattr('core.updating.runtime.urlopen', response)
    updater.readiness = updater._readiness
    verify = updater._verify
    monkeypatch.setattr(updater, '_verify', lambda snapshot, options, timeout: verify(snapshot, options, timeout=0))
    with pytest.raises(UpdateError, match='готовность'):
        PackagedUpdater.cancel_restart(updater)
    assert updater.status()['phase'] == 'restart_ready'
    assert updater.status()['maintenance_token'] == token == commands.gate

    evidence = healthy
    recovered = PackagedUpdater.cancel_restart(updater)
    assert recovered['phase'] == 'interrupted'
    assert recovered['current']['sha'] == healthy['build']['git_sha']
    assert commands.gate is None


@pytest.mark.parametrize('phase', ['restart_ready', 'committed'])
def test_recovery_preserves_journal_token_when_gate_release_has_a_foreign_owner(installation, phase):
    updater, commands = installation
    updater.apply(timeout=0)
    prepared = PackagedUpdater.prepare_restart(updater)
    token = prepared['maintenance_token']
    current = deepcopy(prepared['current'])
    if phase == 'committed':
        updater._unfreeze(prepared)
        updater._save(prepared, phase)
    commands.gate = 'foreign-owner'

    with pytest.raises(UpdateError, match='владелец|обслуживания'):
        if phase == 'restart_ready':
            PackagedUpdater.cancel_restart(updater)
        else:
            updater.launch(timeout=0)

    saved = updater.status()
    assert saved['phase'] == phase
    assert saved['current'] == current
    assert saved['maintenance_token'] == token
    assert commands.gate == 'foreign-owner'
    # The foreign owner releases its own marker; our journal stays retryable.
    commands.gate = None
    recovered = PackagedUpdater.cancel_restart(updater) if phase == 'restart_ready' else updater.launch(timeout=0)
    assert recovered['phase'] == ('interrupted' if phase == 'restart_ready' else 'completed')
    assert recovered['current'] == current
    assert recovered['maintenance_token'] is None


def test_crash_after_native_restart_preparation_is_recovered_on_launch(installation):
    updater, commands = installation
    updater.apply(timeout=0)
    PackagedUpdater.prepare_restart(updater)
    commands.calls.clear()
    state = updater.launch(timeout=0)
    assert state['current']['sha'] == NEW_SHA
    assert state['phase'] == 'interrupted'
    assert not commands.gate
    assert not any(item['State']['Paused'] for item in commands.containers.values())
    assert not any('up' in call or 'kill' in call for call in commands.calls)


def test_native_restart_does_not_close_running_computation(installation):
    updater, commands = installation
    updater.apply(timeout=0)
    commands.calls.clear()
    commands.busy_counts = [1]
    with pytest.raises(UpdateBusy):
        PackagedUpdater.prepare_restart(updater)
    assert not commands.gate
    assert not mutating_commands(commands)


def test_native_restart_write_timeout_restores_old_runtime(installation):
    updater, commands = installation
    updater.apply(timeout=0)
    commands.calls.clear()
    commands.drain_ok = False
    with pytest.raises(UpdateBusy):
        PackagedUpdater.prepare_restart(updater)
    assert updater.status()['phase'] == 'blocked'
    assert not commands.gate
    assert not mutating_commands(commands)


@pytest.mark.parametrize('failure', ['drain_timeout', 'job_claimed_during_drain'])
def test_failed_native_stop_restores_admission_and_consumers(installation, failure):
    updater, commands = installation
    updater.apply(timeout=0)
    commands.calls.clear()
    original_images = {name: item['Image'] for name, item in commands.containers.items()}
    if failure == 'drain_timeout':
        commands.drain_ok = False
    else:
        commands.busy_counts = [0, 1]

    with pytest.raises(UpdateBusy):
        PackagedUpdater.stop(updater)

    state = updater.status()
    assert state['phase'] == 'blocked'
    assert state['current']['sha'] == NEW_SHA
    assert not commands.gate
    assert not any(item['State']['Paused'] for item in commands.containers.values())
    assert all(commands.containers[name]['State']['Running'] for name in state['current']['services'])
    assert {name: item['Image'] for name, item in commands.containers.items()} == original_images
    assert not any('kill' in call or 'stop' in call or 'up' in call for call in commands.calls)


def test_failed_stop_cleanup_keeps_recoverable_journal(installation, monkeypatch):
    updater, commands = installation
    updater.apply(timeout=0)
    commands.busy_counts = [0, 1]
    run = commands.run

    def fail_unpause(arguments, **kwargs):
        if 'unpause' in arguments:
            raise UpdateError('unpause unavailable')
        return run(arguments, **kwargs)

    monkeypatch.setattr(commands, 'run', fail_unpause)
    with pytest.raises(UpdateError, match='unpause unavailable'):
        PackagedUpdater.stop(updater)

    state = updater.status()
    assert state['phase'] == 'quiescing'
    assert state['maintenance_token'] == commands.gate
    assert state['frozen']
    monkeypatch.setattr(commands, 'run', run)
    recovered = PackagedUpdater.cancel_restart(updater)
    assert recovered['phase'] == 'interrupted'
    assert not commands.gate
    assert not any(item['State']['Paused'] for item in commands.containers.values())


@pytest.mark.parametrize('action', ['prepare_restart', 'stop', 'apply', 'rollback'])
@pytest.mark.parametrize('error_type', [UpdateError, KeyboardInterrupt])
def test_abort_quiesce_restores_selected_runtime_before_releasing_gate(installation, monkeypatch, action, error_type):
    updater, commands = installation
    if action != 'apply':
        updater.apply(timeout=0)
    selected_sha = OLD_SHA if action == 'apply' else NEW_SHA
    original_images = {name: item['Image'] for name, item in commands.containers.items()}
    commands.calls.clear()
    run = commands.run
    pause_error = error_type('Cannot pause exited worker')

    def exited_worker(arguments, **kwargs):
        if arguments[-2:] == ['pause', 'id-worker']:
            commands.calls.append(list(arguments))
            commands.containers['worker']['State'].update(Running=False, Paused=False)
            raise pause_error
        return run(arguments, **kwargs)

    monkeypatch.setattr(commands, 'run', exited_worker)
    verified = []

    def readiness(url, sha):
        state = updater.status()
        assert state['phase'] == 'quiescing'
        assert state['maintenance_token'] == commands.gate
        assert commands.gate is not None
        assert all(commands.containers[name]['State']['Running'] for name in state['current']['services'])
        verified.append(sha)
        return True

    updater.readiness = readiness
    verify = updater._verify
    timeouts = []

    def bounded_verify(snapshot, options, timeout):
        timeouts.append(timeout)
        return verify(snapshot, options, timeout=0)

    monkeypatch.setattr(updater, '_verify', bounded_verify)
    with pytest.raises(error_type) as caught:
        if action in {'apply', 'rollback'}:
            getattr(updater, action)(timeout=0)
        else:
            getattr(PackagedUpdater, action)(updater)

    assert caught.value is pause_error  # successful cleanup preserves the rejection
    state = updater.status()
    assert state['phase'] == 'blocked'
    assert state['current']['sha'] == selected_sha
    assert state['candidate'] is None
    assert state['frozen'] == []
    assert state['maintenance_token'] is None
    assert commands.gate is None
    assert commands.containers['worker']['State']['Running']
    assert not any(item['State']['Paused'] for item in commands.containers.values())
    assert verified == [selected_sha]
    assert timeouts == [0 if action in {'apply', 'rollback'} else 180]
    assert {name: item['Image'] for name, item in commands.containers.items()} == original_images
    start = next(index for index, call in enumerate(commands.calls) if call[-2:] == ['start', 'id-worker'])
    release = next(index for index, call in enumerate(commands.calls) if _GATE_PROGRAM in call and call[-2] == 'release')
    assert start < release
    assert not any('kill' in call or 'stop' in call or 'up' in call for call in commands.calls)
    assert PackagedUpdater.cancel_restart(updater)['phase'] == 'blocked'


def test_abort_restart_ready_recovers_before_marking_blocked(installation):
    updater, commands = installation
    updater.apply(timeout=0)
    state = PackagedUpdater.prepare_restart(updater)
    token = state['maintenance_token']
    commands.containers['worker']['State'].update(Running=False, Paused=False)
    commands.calls.clear()
    verified = []

    def readiness(url, sha):
        assert updater.status()['phase'] == 'restart_ready'
        assert commands.gate == token
        verified.append(sha)
        return True

    updater.readiness = readiness
    updater._abort_quiesce(state, options=updater._options(), timeout=0, error='restart rejected')

    assert verified == [NEW_SHA]
    assert state['phase'] == 'blocked'
    assert state['error'] == 'restart rejected'
    assert state['maintenance_token'] is None
    assert commands.gate is None
    assert commands.containers['worker']['State']['Running']
    assert not any(item['State']['Paused'] for item in commands.containers.values())
    assert not mutating_commands(commands)


@pytest.mark.parametrize('action', ['prepare_restart', 'stop'])
@pytest.mark.parametrize('failure', ['start_failure', 'worker_stays_exited', 'wrong_image', 'readiness'])
def test_abort_quiesce_failed_restore_keeps_gate_for_cancel_retry(installation, monkeypatch, action, failure):
    updater, commands = installation
    updater.apply(timeout=0)
    current = deepcopy(updater.status()['current'])
    worker_image = commands.containers['worker']['Image']
    commands.calls.clear()
    run = commands.run

    def failed_recovery(arguments, **kwargs):
        if arguments[-2:] == ['pause', 'id-worker']:
            commands.calls.append(list(arguments))
            commands.containers['worker']['State'].update(Running=False, Paused=False)
            if failure == 'wrong_image':
                commands.containers['worker']['Image'] = 'sha256:unexpected-version'
            raise UpdateError('Cannot pause exited worker')
        if arguments[-2:] == ['start', 'id-worker'] and failure in {'start_failure', 'worker_stays_exited'}:
            commands.calls.append(list(arguments))
            if failure == 'start_failure':
                raise UpdateError('exact consumer start failed')
            return ''
        return run(arguments, **kwargs)

    monkeypatch.setattr(commands, 'run', failed_recovery)
    updater.readiness = lambda url, sha: failure != 'readiness'
    verify = updater._verify
    monkeypatch.setattr(updater, '_verify', lambda snapshot, options, timeout: verify(snapshot, options, timeout=0))
    with pytest.raises(UpdateError):
        getattr(PackagedUpdater, action)(updater)

    state = updater.status()
    token = state['maintenance_token']
    assert state['phase'] == 'quiescing'
    assert state['current'] == current
    assert token is not None and token == commands.gate
    assert not any(_GATE_PROGRAM in call and call[-2] == 'release' for call in commands.calls)
    with pytest.raises(UpdateError):
        PackagedUpdater.cancel_restart(updater)
    assert updater.status()['phase'] == 'quiescing'
    assert updater.status()['maintenance_token'] == token == commands.gate

    monkeypatch.setattr(commands, 'run', run)
    commands.containers['worker']['Image'] = worker_image
    updater.readiness = lambda url, sha: sha == current['sha']
    recovered = PackagedUpdater.cancel_restart(updater)
    assert recovered['phase'] == 'interrupted'
    assert recovered['current'] == current
    assert commands.containers['worker']['State']['Running']
    assert commands.gate is None
    assert not any('kill' in call or 'stop' in call or 'up' in call for call in commands.calls)


def test_abort_preparation_cleanup_does_not_resume_or_load_options(installation, monkeypatch):
    updater, commands = installation
    updater.apply(timeout=0)
    state = updater.status()
    updater._save(state, 'preparing', candidate=None)
    commands.calls.clear()
    monkeypatch.setattr(updater, '_recover', Mock(side_effect=AssertionError('preparation is not quiescing')))
    monkeypatch.setattr(updater, '_options', Mock(side_effect=AssertionError('preparation needs no recovery options')))

    updater._abort_quiesce(state, error='offline')

    assert state['phase'] == 'blocked'
    assert state['error'] == 'offline'
    assert not commands.calls


def test_live_storage_env_drift_never_uses_file_defaults(installation):
    updater, commands = installation
    commands.containers['api']['Config']['Env'] = ['DATABASE_URL=other-database']
    with pytest.raises(UpdateError, match='Фактические настройки'):
        updater.apply(timeout=0)
    assert not mutating_commands(commands)


def test_live_storage_mount_drift_never_creates_a_new_empty_volume(installation):
    updater, commands = installation
    commands.containers['api']['Mounts'][0]['Name'] = 'other_state'
    with pytest.raises(UpdateError, match='том отличается'):
        updater.apply(timeout=0)
    assert not mutating_commands(commands)


def test_windows_docker_desktop_bind_translation_is_not_a_false_migration():
    assert Updater._mount_source('C:\\Users\\Оператор\\state') == Updater._mount_source('/run/desktop/mnt/host/c/Users/Оператор/state')


def test_literal_dollar_secret_in_compose_snapshot_matches_live_environment():
    Updater._validate_live({'Config': {'Env': ['MINIO_ROOT_PASSWORD=one$secret']}, 'Mounts': []},
                           {'environment': {'MINIO_ROOT_PASSWORD': 'one$$secret'}}, {'name': 'fixture'})


def test_additional_developer_writer_is_not_blindly_killed(installation):
    updater, commands = installation
    commands.containers['mcp']['State']['Running'] = True
    with pytest.raises(UpdateError, match='MCP'):
        updater.apply(timeout=0)
    assert not mutating_commands(commands)


def test_failed_compose_then_failed_rollback_requires_explicit_recovery(installation):
    updater, commands = installation
    commands.fail_switch = commands.fail_rollback = True
    with pytest.raises(UpdateError):
        updater.apply(timeout=0)
    assert updater.status()["phase"] == "needs_recovery"
    commands.fail_switch = commands.fail_rollback = False
    recovered = updater.rollback(timeout=0)
    assert recovered["phase"] == "rolled_back"
    assert not commands.gate


def test_crash_during_switch_is_recovered_before_new_network_request(installation):
    updater, commands = installation
    updater.apply(timeout=0)
    state = updater.status()
    state.update(phase="verifying", current=state["previous"], candidate=state["current"], frozen=[])
    atomic_json(updater.state_path, state)
    updater.release_loader = Mock(side_effect=UpdateError("offline"))
    with pytest.raises(UpdateError, match="offline"):
        updater.apply(timeout=0)
    assert commands.containers["api"]["Image"] == "sha256:" + f"{1:064x}"


def test_context_changed_never_restores_other_docker_installation(installation):
    updater, commands = installation
    updater.apply(timeout=0)
    commands.calls.clear()
    commands.daemon = "other-daemon"
    with pytest.raises(UpdateError, match="движок"):
        updater.rollback(timeout=0)
    assert not mutating_commands(commands)


@pytest.mark.parametrize('action', ['prepare_restart', 'stop'])
@pytest.mark.parametrize('foreign_gate', [None, 'another-installation-token'])
def test_native_quiesce_cannot_modify_a_different_daemon(installation, action, foreign_gate):
    updater, commands = installation
    updater.apply(timeout=0)
    commands.calls.clear()
    commands.daemon = 'other-daemon'
    commands.gate = foreign_gate

    with pytest.raises(UpdateError, match='движок'):
        getattr(PackagedUpdater, action)(updater)

    assert not mutating_commands(commands)
    assert not any('exec' in call or 'stop' in call or 'unpause' in call for call in commands.calls)
    assert commands.gate == foreign_gate
    state = updater.status()
    assert state['current']['daemon_id'] == 'local-daemon'
    assert not state['frozen']
    assert state['maintenance_token'] is None


def test_normal_launch_does_not_recreate_running_compute_containers(installation):
    updater, commands = installation
    updater.apply(timeout=0)
    commands.calls.clear()
    commands.busy_counts = [2]  # a calculation can run through ordinary launch
    state = updater.launch(timeout=0)
    assert state['current']['sha'] == NEW_SHA
    assert not mutating_commands(commands)
    assert not any('stop' in call for call in commands.calls)


def test_normal_launch_starts_exact_stopped_ids_without_recreating_them(installation):
    updater, commands = installation
    updater.apply(timeout=0)
    commands.containers['worker']['State']['Running'] = False
    commands.calls.clear()
    updater.launch(timeout=0)
    assert any(call[-2:] == ['start', 'id-worker'] for call in commands.calls)
    assert not mutating_commands(commands)


@pytest.mark.parametrize('failure', ['missing', 'exited_after_start'])
def test_first_launch_cannot_register_ready_without_declared_estimator(installation, monkeypatch, failure):
    updater, commands = installation
    estimator = commands.containers['estimator']
    if failure == 'missing':
        del commands.containers['estimator']
    else:
        estimator['State']['Running'] = False
        original_run = commands.run

        def exits_after_start(arguments, **kwargs):
            result = original_run(arguments, **kwargs)
            if 'start' in arguments:
                estimator['State']['Running'] = False
            return result

        monkeypatch.setattr(commands, 'run', exits_after_start)
    with pytest.raises(UpdateError, match='сервис'):
        updater.launch(timeout=0)
    assert not updater.status().get('current')
    assert not mutating_commands(commands)
    assert commands.containers['api']['State']['Running']
    if failure == 'missing':
        commands.containers['estimator'] = estimator
    else:
        monkeypatch.setattr(commands, 'run', original_run)
    restored = updater.launch(timeout=0)
    assert restored['phase'] == 'current'
    assert 'estimator' in restored['current']['services']
    assert commands.containers['estimator']['State']['Running']


def test_saved_incomplete_service_list_does_not_bypass_readiness(installation):
    updater, commands = installation
    state = updater.launch(timeout=0)
    state['current']['services'].remove('estimator')
    updater._save(state, 'current')
    del commands.containers['estimator']
    commands.calls.clear()
    with pytest.raises(UpdateError, match='сервис'):
        updater.launch(timeout=0)
    assert updater.status()['current'] == state['current']
    assert not mutating_commands(commands)


@pytest.mark.parametrize('configuration', ['legacy_absent', 'explicitly_disabled'])
def test_readiness_does_not_require_services_outside_enabled_configuration(installation, configuration):
    updater, commands = installation
    del commands.containers['estimator']
    if configuration == 'legacy_absent':
        del commands.before['services']['estimator']
    else:
        commands.before['services']['estimator']['deploy'] = {'replicas': 0}
    state = updater.launch(timeout=0)
    assert state['phase'] == 'current'
    assert 'estimator' not in state['current']['services']
    assert not mutating_commands(commands)


@pytest.mark.parametrize("url", ["https://example.com", "http://100.78.114.66:8000", "http://user:pass@127.0.0.1:8000"])
def test_remote_api_addresses_are_not_update_targets(installation, url):
    updater, commands = installation
    with pytest.raises(UpdateError, match="локальный"):
        updater.configure(url=url)


def test_remote_docker_nas_is_not_update_target(installation):
    updater, commands = installation
    commands.endpoint = "ssh://nas"
    with pytest.raises(UpdateError, match="NAS"):
        updater.apply(timeout=0)
    assert not mutating_commands(commands)


def test_second_host_process_cannot_update_same_installation(tmp_path):
    with installation_lock(tmp_path):
        with pytest.raises(UpdateError, match="другое обновление"):
            with installation_lock(tmp_path):
                raise AssertionError("lock bypassed")
