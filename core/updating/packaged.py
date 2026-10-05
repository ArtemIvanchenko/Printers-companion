"""Installed desktop adapter: bundled resources, no Git or host Python install.

The desktop package is updated first by its native installer. This adapter then
converges the local Docker backend to that exact package's version/SHA. Neither
a moving branch nor an unsigned remotely downloaded script is executed.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
from urllib.parse import quote
from uuid import uuid4

from core.updating.releases import IMAGE_REPOSITORY, Release, UpdateError, validate_sha, version_tuple
from core.updating.runtime import REQUIRED, Updater
from core.updating.state import atomic_json, atomic_text, installation_lock, private_directory, read_json

RESOURCE_FILES = ('docker-compose.yml', 'deploy/nas/docker-compose.operator.yml')


def load_bundle(directory: Path) -> dict:
    manifest = read_json(directory / 'runtime-manifest.json')
    if manifest.get('schema_version') != 1 or manifest.get('update_protocol') != 1:
        raise UpdateError('Манифест настольного пакета не поддерживается.')
    version_tuple(manifest.get('version', ''))
    validate_sha(manifest.get('sha', ''))
    if set(manifest.get('files', {})) != set(RESOURCE_FILES):
        raise UpdateError('Неполный состав ресурсов настольного пакета.')
    for name in RESOURCE_FILES:
        path = directory / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != manifest['files'][name]:
            raise UpdateError('Ресурс настольного пакета повреждён; запуск запрещён.')
    images = manifest.get('images', {})
    if not isinstance(images, dict):
        raise UpdateError('Некорректный список контейнеров в настольном пакете.')
    if images and (set(images) != {'api', 'worker', 'watcher'} or not all(
            isinstance(value, str) and re.fullmatch(re.escape(IMAGE_REPOSITORY) + r'@sha256:[0-9a-f]{64}', value)
            for value in images.values())):
        raise UpdateError('Некорректные закреплённые контейнеры в настольном пакете.')
    return manifest


def _env_value(value: str) -> str:
    if not isinstance(value, str) or any(char in value for char in ('\r', '\n', '\x00')):
        raise UpdateError('Настройка содержит недопустимый перенос строки.')
    # Double quotes handle trailing backslashes and backslash+quote sequences.
    # Compose's dotenv escape for a literal $ is \$, not shell substitution.
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$') + '"'


def _finish_setup(root: Path, state: dict, journal: Path) -> None:
    expected = set(RESOURCE_FILES) | {'deploy/nas/.env.operator'}
    if state.get('schema_version') != 1 or set(state.get('files', {})) != expected:
        raise UpdateError('Повреждён журнал первой настройки. Исходные файлы не изменялись.')
    for name in sorted(expected):
        destination = root / name
        source = root / '.update-state/setup-pending' / name
        if destination.is_file():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != state['files'][name]:
                raise UpdateError('Файл настройки изменён вне установщика. Он не перезаписан.')
            continue
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != state['files'][name]:
            raise UpdateError('Не найден проверенный файл первой настройки. Пароли не заменялись.')
        private_directory(destination.parent)
        # All parent directories are private; a partial rename is resumable from
        # the journal, including the original compute identity and credentials.
        os.replace(source, destination)
    private_directory(root / '.operator-state')
    private_directory(root / 'raw_logs')
    atomic_json(journal, dict(state, phase='completed'))


def configure_nas(root: Path, bundle: Path, values: dict) -> None:
    """One-time configuration only; never replace existing secrets/compute ID."""
    root = root.resolve()
    load_bundle(bundle)
    private_directory(root)
    env_path = root / 'deploy/nas/.env.operator'
    journal = root / '.update-state/setup.json'
    with installation_lock(root / '.update-state'):
        saved = read_json(journal)
        if saved.get('phase') == 'prepared':
            _finish_setup(root, saved, journal)
            return
        if env_path.exists() or (root / '.update-state/installation.json').exists() or saved:
            raise UpdateError('Установка уже настроена. Её пароли и идентификатор не заменялись.')
    host = values.get('host', '').strip()
    if not host or any(char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for char in host):
        raise UpdateError('Укажите IP или имя NAS без протокола и пути.')
    username, password = values.get('db_user', 'printer_logs'), values.get('db_password', '')
    database = values.get('database', 'printer_logs')
    access, secret = values.get('access_key', ''), values.get('secret_key', '')
    if not all(isinstance(value, str) and value for value in (username, password, database, access, secret)):
        raise UpdateError('Заполните реквизиты PostgreSQL и файлового хранилища NAS.')
    settings = {
        'DATABASE_URL': f'postgresql+psycopg://{quote(username, safe="")}:{quote(password, safe="")}@{host}:5433/{quote(database, safe="")}',
        'COMPUTE_NODE_ID': 'operator-' + uuid4().hex,
        'MINIO_ENDPOINT': host + ':9000', 'MINIO_ROOT_USER': access, 'MINIO_ROOT_PASSWORD': secret,
        'API_SERVICE_TOKEN': uuid4().hex + uuid4().hex,
        'AGENT_API_TOKEN': uuid4().hex + uuid4().hex,
        'RAW_LOGS_HOST_PATH': str(root / 'raw_logs'),
        'OPERATOR_STATE_HOST_PATH': str(root / '.operator-state'),
        'API_BIND_ADDRESS': '127.0.0.1', 'STARTUP_IMPORT_ENABLED': 'false',
    }
    content = '\n'.join(key + '=' + _env_value(value) for key, value in settings.items()) + '\n'
    with installation_lock(root / '.update-state'):
        if env_path.exists() or read_json(journal):
            raise UpdateError('Другой запуск уже сохранил настройки. Они не перезаписаны.')
        for name in RESOURCE_FILES:
            path = root / name
            private_directory(path.parent)
            if path.exists():
                raise UpdateError('В новой папке уже есть Compose-файлы; выберите импорт установки.')
        pending = root / '.update-state/setup-pending'
        # Staged secrets are host-private and never part of package resources.
        files = {}
        for name in RESOURCE_FILES:
            text = (bundle / name).read_text(encoding='utf-8')
            atomic_text(pending / name, text)
            files[name] = hashlib.sha256((pending / name).read_bytes()).hexdigest()
        atomic_text(pending / 'deploy/nas/.env.operator', content)
        files['deploy/nas/.env.operator'] = hashlib.sha256(content.encode()).hexdigest()
        saved = {'schema_version': 1, 'phase': 'prepared', 'files': files}
        atomic_json(journal, saved)
        _finish_setup(root, saved, journal)


def finish_setup(root: Path) -> None:
    """Recover only this installer's prepared/completed setup, without new input."""
    root = root.resolve()
    journal = root / '.update-state/setup.json'
    with installation_lock(root / '.update-state'):
        saved = read_json(journal)
        if saved.get('phase') not in {'prepared', 'completed'}:
            raise UpdateError('Нет завершённой или восстанавливаемой первой настройки.')
        _finish_setup(root, saved, journal)


class PackagedUpdater(Updater):
    def __init__(self, root: Path, bundle: Path, **kwargs):
        self.bundle = bundle.resolve()
        self.manifest = load_bundle(self.bundle)
        release = Release(self.manifest['version'], 'v' + self.manifest['version'], self.manifest['sha'],
                          '', self.manifest.get('built_at', ''), 'Установленный настольный пакет')
        super().__init__(root, release_loader=lambda version: release, **kwargs)

    def _clean_sources(self) -> None:
        self.manifest = load_bundle(self.bundle)
        if self.manifest.get('source_state') != 'clean':
            raise UpdateError('Это тестовый пакет из изменённых исходников: развёртывание релизных образов запрещено. Доступен просмотр интерфейса.')
        if not self.manifest.get('images'):
            raise UpdateError('В тестовом пакете ещё нет закреплённых образов этого SHA. Развёртывание контейнеров запрещено; рабочая установка не менялась.')

    def _pin_images(self, candidate: dict, services: list[str], release: Release) -> None:
        self._clean_sources()
        super()._pin_images(candidate, services, release, expected_images=self.manifest['images'])

    def _stage(self, release: Release) -> Path:
        self._clean_sources()
        if release.sha != self.manifest['sha'] or release.version != self.manifest['version']:
            raise UpdateError('Контейнеры должны соответствовать установленному настольному пакету.')
        directory = self.directory / 'releases' / f'{release.tag}-{release.sha[:12]}'
        private_directory(directory)
        for name in RESOURCE_FILES:
            target = directory / name
            private_directory(target.parent)
            if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != self.manifest['files'][name]:
                raise UpdateError('Сохранённый ресурс установки изменён; замена запрещена.')
            if not target.exists():
                shutil.copyfile(self.bundle / name, target)
        return directory

    def adopt(self) -> dict:
        # Validate before the desktop saves its root binding. No containers are
        # started/stopped, no sources or data are copied. Wrong folders can be
        # rejected and selected again without trapping the setup screen.
        options = self.configure()
        configuration = self._json_docker(*self._base(options), 'config', '--format', 'json')
        if not REQUIRED.issubset(configuration.get('services', {})):
            raise UpdateError('В папке нет полного операторского проекта Printer’s Companion.')
        self._running(options['project_name'])  # ownership check if containers exist
        return {'phase': 'configured'}

    def launch(self, *, timeout=180) -> dict:
        if not self.settings_path.exists():
            self.configure()
        options = self._options()
        # Initial install has no previous backend. Stage/check everything before
        # creating API/workers. Failed first launch can be retried, no data deleted.
        with installation_lock(self.directory):
            state = self.status()
            if (not state.get('current') and state.get('phase') not in {'initializing', 'initial_failed'}
                    and not self._running(options['project_name']).get('api')):
                self._clean_sources()
                release = self.release_loader(None)
                source = self._stage(release)
                config = self._json_docker(*self._base(options, source), 'config', '--format', 'json')
                # This entrypoint only creates NAS-mode compute nodes, not storage.
                if config['services']['api'].get('environment', {}).get('APP_ENV') != 'production':
                    raise UpdateError('Для новой установки выберите NAS; автономное хранилище автоматически не создаётся.')
                services = sorted(REQUIRED)
                self._pin_images(config, services, release)
                directory = self.directory / 'runs' / uuid4().hex
                private_directory(directory)
                target = self._snapshot(config, directory / 'candidate.compose.json', source=source,
                                        services=services, sha=release.sha, version=release.version)
                self._check_schema(target)
                self._save(state, 'initializing', candidate=target)
                try:
                    self._deploy(target, start_dependencies=True)
                    self._verify(target, options, timeout)
                except BaseException:
                    self._save(state, 'initial_failed', error='Первый запуск не подтверждён. Настройки сохранены; повторите запуск.')
                    raise
                self._save(state, 'completed', current=target, candidate=None, error=None)
                return state
            if state.get('phase') in {'initializing', 'initial_failed'} and state.get('candidate'):
                target = state['candidate']
                self._resume_containers(target)
                self._verify(target, options, timeout)
                self._save(state, 'completed', current=target, candidate=None, error=None)
                return state
        state = super().launch(timeout=timeout)
        current = state['current']
        if current.get('version') == self.manifest['version'] and current.get('sha') == self.manifest['sha']:
            return state
        # The native window can show the verified old dashboard while busy and
        # retry later; never hide it behind an hour-long child-process loop.
        return self.apply(timeout=timeout)

    def prepare_restart(self) -> dict:
        """Drain accepted writes BEFORE the installer closes the renderer.

        Only idle consumers are frozen. No image/container is changed here.
        The durable restart_ready phase lets the next app (or cancellation)
        resume the old version even if the native installer never completed.
        """
        with installation_lock(self.directory):
            options = self._options()
            state = self.status()
            self._recover(state, options, 180)
            current = state.get('current')
            if not current:
                return state
            try:
                self._quiesce(state, current, self._running(options['project_name']))
                self._save(state, 'restart_ready')
            except BaseException:
                self._abort_quiesce(state)
                raise
            return state

    def cancel_restart(self) -> dict:
        with installation_lock(self.directory):
            state = self.status()
            if state.get('phase') in {'quiescing', 'restart_ready'}:
                self._recover(state, self._options(), 180)
            return state

    def stop(self) -> dict:
        with installation_lock(self.directory):
            options = self._options()
            state = self.status()
            self._recover(state, options, 180)
            current = state.get('current')
            if not current:
                raise UpdateError('Нет зарегистрированной установки для остановки.')
            try:
                self._quiesce(state, current, self._running(options['project_name']))
            except BaseException:
                self._abort_quiesce(state)
                raise
            self._stop_frozen(state)
            self._docker(*self._snapshot_args(current), 'stop', '--timeout', '30', 'api')
            self._save(state, 'stopped')
            return state

    def _recover(self, state: dict, options: dict, timeout: int) -> None:
        if state.get('phase') == 'stopped':
            self._resume_containers(state['current'])
            self._verify(state['current'], options, timeout)
            self._gate(state, 'release')
            self._save(state, 'current')
        else:
            super()._recover(state, options, timeout)


def public_status(state: dict) -> dict:
    """Whitelisted response: no paths, Compose secrets, tokens or raw CLI output."""
    current = state.get('current') or {}
    return {'phase': state.get('phase', 'unconfigured'), 'version': current.get('version'),
            'sha': current.get('sha'), 'message': state.get('error'),
            'can_rollback': bool(state.get('previous')) or state.get('phase') in {
                'quiescing', 'restart_ready', 'switching', 'verifying', 'rolling_back', 'needs_recovery', 'committed'}}
