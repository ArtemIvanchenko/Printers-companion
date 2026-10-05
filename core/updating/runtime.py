"""Transactional host-side operator updates; never runs on the NAS or in API.

Source and images are staged first. Compose snapshots are private (they include
resolved environment values). Public state only contains identity and paths.
No git reset, volume deletion, schema migration or Docker socket mount is used.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from core.updating.releases import (
    IMAGE_REPOSITORY, REPOSITORY, Release, UpdateBusy, UpdateError, stable_release, version_tuple,
)
from core.updating.state import atomic_json, installation_lock, private_directory, read_json

APPLICATIONS = {"api": "api", "worker": "worker", "estimator": "api", "nas-sync": "worker",
                "watcher": "watcher", "scheduler": "scheduler", "mcp": "mcp"}
REQUIRED = {"api", "worker", "estimator", "nas-sync", "watcher"}
CRITICAL_ENV = ("COMPUTE_NODE_ID", "DATABASE_URL", "MINIO_ENDPOINT", "MINIO_ROOT_USER",
                "MINIO_ROOT_PASSWORD", "OPERATOR_INSTANCE_FILE", "NAS_OUTBOX_PATH")
_DATABASE_PROBE = """import json,sys
from core.config.settings import get_settings
from core.runtime_health import _database_checks
c=_database_checks(get_settings()); print(json.dumps(c)); sys.exit(0 if all(c.values()) else 1)
"""
_SCHEMA_PROBE = """import json,sys
from core.config.settings import get_settings
from core.runtime_health import _database_checks, _object_store_checks
from core.maintenance import UPDATE_PROTOCOL, WRITE_DRAIN_SUPPORTED
assert UPDATE_PROTOCOL==1 and WRITE_DRAIN_SUPPORTED
s=get_settings(); c=_database_checks(s); c['minio']=_object_store_checks(s)['minio']
print(json.dumps(c)); sys.exit(0 if all(c.values()) else 1)
"""
_GATE_PROGRAM = """import json,os,sys,tempfile
from pathlib import Path
p=Path(os.environ.get('OPERATOR_INSTANCE_FILE','/var/lib/printer-companion/instance-id')).parent/'operator-update.json'
token=sys.argv[2]
if sys.argv[1]=='claim':
    from core.maintenance import UPDATE_PROTOCOL, WRITE_DRAIN_SUPPORTED
    assert UPDATE_PROTOCOL==1 and WRITE_DRAIN_SUPPORTED
    p.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix='.maintenance-',dir=p.parent)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump({'token':token},f); f.flush(); os.fsync(f.fileno())
        try:
            os.link(temporary,p)
        except FileExistsError:
            pass
    finally:
        os.unlink(temporary)
owned=not p.exists() or json.loads(p.read_text()).get('token')==token
if sys.argv[1]=='release' and owned:
    p.unlink(missing_ok=True)
directory_fd=os.open(p.parent,os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
print(json.dumps({'owned':owned}))
"""
_DRAIN_PROBE = """import json
from core.maintenance import drain_writes
drain_writes(timeout=60)
print(json.dumps({'drained':True}))
"""
_BUSY_PROBE = """import json
from sqlalchemy import text
from core.config.settings import get_settings
from storage.db.session import engine
node=get_settings().compute_node_id
with engine.connect() as c:
    if c.dialect.name=='postgresql':
        c.execute(text('SET TRANSACTION READ ONLY'))
        c.execute(text('SET LOCAL statement_timeout=3000'))
    else:
        c.exec_driver_sql('PRAGMA query_only=ON')
    bg=c.scalar(text("SELECT count(*) FROM background_jobs WHERE owner_node_id=:node AND status='running'"), {'node':node})
    imp=c.scalar(text("SELECT count(*) FROM import_jobs WHERE owner_node_id=:node AND status IN ('importing','analyzing','reporting','processing')"), {'node':node})
print(json.dumps({'running_jobs':int(bg)+int(imp)}))
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Commands:
    def __init__(self, root: Path):
        self.root = root

    def run(self, args: list[str], *, timeout: int = 120, check: bool = True) -> str:
        # No shell, no repurposed HOME, no daemon implicitly overridden by env.
        environment = dict(os.environ)
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "COMPOSE_FILE", "COMPOSE_PROJECT_NAME"):
            environment.pop(key, None)
        try:
            result = subprocess.run(args, cwd=self.root, env=environment, text=True,
                                    capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise UpdateError(f"Не удалось выполнить {args[0]}: программа недоступна или истёк таймаут.") from None
        if check and result.returncode:
            # Docker/config/SQL output may contain .env values. Never echo it.
            raise UpdateError(f"Команда {args[0]} завершилась с кодом {result.returncode}; текущая стадия не подтверждена.")
        return result.stdout.strip()


class Updater:
    def __init__(self, root: Path, *, commands=None, release_loader=stable_release,
                 readiness=None, progress=print):
        self.root = root.resolve()
        self.directory = self.root / ".update-state"
        self.state_path = self.directory / "state.json"
        self.settings_path = self.directory / "installation.json"
        self.commands = commands or Commands(self.root)
        self.release_loader = release_loader
        self.readiness = readiness or self._readiness
        self.progress = progress
        self.context = ""
        self.daemon_id = ""

    def status(self) -> dict:
        state = read_json(self.state_path)
        if state and state.get("schema_version") != 1:
            raise UpdateError("Версия журнала обновлений не поддерживается.")
        return state

    def _save(self, state: dict, phase: str, **values) -> None:
        state.update(schema_version=1, phase=phase, updated_at=_now(), **values)
        atomic_json(self.state_path, state)

    def _docker(self, *args: str, timeout=120, check=True) -> str:
        return self.commands.run(["docker", "--context", self.context, *args],
                                 timeout=timeout, check=check)

    def _json_docker(self, *args: str) -> object:
        try:
            return json.loads(self._docker(*args))
        except ValueError:
            raise UpdateError("Docker вернул нераспознанное состояние; обновление запрещено.") from None

    def _host(self, configured: dict) -> None:
        self.context = configured.get("context") or self.commands.run(["docker", "context", "show"])
        info = self.commands.run(["docker", "context", "inspect", self.context])
        try:
            endpoint = json.loads(info)[0]["Endpoints"]["docker"]["Host"]
        except (KeyError, IndexError, ValueError, TypeError):
            raise UpdateError("Не удалось проверить локальный Docker context.") from None
        if not endpoint.startswith(("unix://", "npipe://")):
            raise UpdateError("Удалённый Docker/NAS не обновляется с операторского ПК. Выберите локальный context.")
        try:
            self.daemon_id = self._docker("info", "--format", "{{.ID}}", timeout=15)
        except UpdateError:
            raise UpdateError('Локальный Docker не запущен или недоступен. Запустите Docker Desktop/OrbStack и повторите. Настройки и данные не менялись.') from None
        version = self._docker("compose", "version", "--short")
        match = re.match(r"v?(\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (2, 24, 4):
            raise UpdateError("Для безопасного NAS merge нужен Docker Compose 2.24.4 или новее.")

    def _base(self, configured: dict, source: Path | None = None) -> list[str]:
        args = ["compose", "--project-directory", str(self.root)]
        if configured.get("project_name"):
            args.extend(["--project-name", configured["project_name"]])
        if configured.get("env_file"):
            args.extend(["--env-file", configured["env_file"]])
        for name in configured["compose_files"]:
            original = Path(name)
            candidate = (source / original.relative_to(self.root)) if source else original
            managed = original.name in {'docker-compose.yml', 'compose.yml', 'docker-compose.operator.yml'}
            if source and managed and not candidate.is_file():
                raise UpdateError('В релизе отсутствует обязательный Compose-файл; старый файл не подставляется.')
            # Only operator-local overrides may fall back to original files.
            args.extend(["-f", str(candidate if candidate.is_file() else original)])
        return args

    def _snapshot_args(self, snapshot: dict) -> list[str]:
        path = Path(snapshot["compose_path"]).resolve()
        if not path.is_relative_to(self.directory.resolve()) or not path.is_file():
            raise UpdateError("Снимок установки отсутствует или выходит за локальный каталог обновлений.")
        return ["compose", "--project-directory", str(self.root), "--project-name",
                snapshot["project_name"], "-f", str(path)]

    def _containers(self, project: str | None = None) -> list[dict]:
        args = ["ps", "-a", "-q", "--filter", "label=com.docker.compose.service=api"]
        if project:
            args = ["ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={project}"]
        ids = self._docker(*args).split()
        return self._json_docker("inspect", *ids) if ids else []

    def configure(self, *, compose_files=None, env_file=None, project_name=None,
                  context=None, url=None) -> dict:
        with installation_lock(self.directory):
            if self.status().get("phase") in {"quiescing", "restart_ready", "switching", "verifying", "rolling_back", "needs_recovery", "committed"}:
                raise UpdateError("Сначала завершите восстановление прерванного обновления.")
            existing = read_json(self.settings_path)
            url = url or existing.get('url') or 'http://127.0.0.1:8000'
            parsed = urlparse(url)
            if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                    or parsed.username or parsed.password or parsed.path not in {"", "/"}
                    or parsed.query or parsed.fragment):
                raise UpdateError("Проверка запуска должна использовать локальный HTTP-адрес API.")
            options = dict(existing, context=context or existing.get("context"), url=url.rstrip("/"))
            self._host(options)
            if not compose_files and existing.get("compose_files"):
                compose_files = existing["compose_files"]
                project_name = project_name or existing.get("project_name")
            if not compose_files:
                matches = []
                for container in self._containers():
                    labels = container["Config"].get("Labels") or {}
                    location = labels.get("com.docker.compose.project.working_dir", "")
                    if location and Path(location).resolve() == self.root:
                        matches.append(labels)
                projects = {labels["com.docker.compose.project"] for labels in matches}
                if len(projects) > 1:
                    raise UpdateError("В папке несколько Compose-проектов; явно выберите project-name.")
                labels = next((label for label in matches if not project_name or label["com.docker.compose.project"] == project_name), {})
                if labels:
                    compose_files = labels.get("com.docker.compose.project.config_files", "").split(",")
                    project_name = project_name or labels["com.docker.compose.project"]
                else:
                    compose_files = [str(self.root / "docker-compose.yml")]
                    if (self.root / "deploy/nas/.env.operator").is_file():
                        compose_files.append(str(self.root / "deploy/nas/docker-compose.operator.yml"))
                        env_file = env_file or str(self.root / "deploy/nas/.env.operator")
            files = []
            for name in compose_files:
                path = Path(name)
                path = (self.root / path).resolve() if not path.is_absolute() else path.resolve()
                if not path.is_relative_to(self.root) or not path.is_file():
                    raise UpdateError("Compose-файлы должны существовать внутри папки установки.")
                files.append(str(path))
            options.update(compose_files=files, project_name=project_name,
                           env_file=str((self.root / env_file).resolve()) if env_file else existing.get("env_file"))
            if not options["env_file"] and any(name.replace('\\', '/').endswith("deploy/nas/docker-compose.operator.yml") for name in files):
                if (self.root / "deploy/nas/.env.operator").is_file():
                    options["env_file"] = str(self.root / "deploy/nas/.env.operator")
                else:
                    raise UpdateError("Для NAS укажите существующий --env-file; пароли и режим по контейнеру не угадываются.")
            if options["env_file"] and not Path(options["env_file"]).is_file():
                raise UpdateError("Указанный .env не существует; шаблон вместо него не создаётся.")
            configuration = self._json_docker(*self._base(options), "config", "--format", "json")
            if "api" not in configuration.get("services", {}):
                raise UpdateError("Это storage-only NAS-проект, а не операторское приложение.")
            options.update(project_name=configuration["name"], context=self.context, schema_version=1)
            atomic_json(self.settings_path, options)
            return options

    def _options(self) -> dict:
        options = read_json(self.settings_path)
        if not options:
            raise UpdateError("Установка ещё не зарегистрирована: выполните configure один раз.")
        self._host(options)
        return options

    def _clean_sources(self) -> None:
        if self.commands.run(["git", "status", "--porcelain", "--untracked-files=normal"]):
            raise UpdateError("В исходниках есть локальные изменения. Они сохранены; обновление не применяется.")
        remote = self.commands.run(["git", "remote", "get-url", "origin"]).lower().rstrip("/")
        allowed = {f"https://github.com/{REPOSITORY.lower()}.git", f"https://github.com/{REPOSITORY.lower()}",
                   f"git@github.com:{REPOSITORY.lower()}.git"}
        if remote not in allowed:
            raise UpdateError("Origin не совпадает с доверенным репозиторием Printer's Companion.")

    def _stage(self, release: Release) -> Path:
        reference = f"refs/printer-companion/releases/{release.tag}"
        self.commands.run(["git", "fetch", "--no-tags", "origin", f"+refs/tags/{release.tag}:{reference}"])
        sha = self.commands.run(["git", "rev-parse", f"{reference}^{{commit}}"])
        if sha != release.sha:
            raise UpdateError("GitHub и Git расходятся по SHA релиза; установка запрещена.")
        source = self.directory / "releases" / f"{release.tag}-{sha[:12]}"
        private_directory(source.parent)
        if not source.exists():
            self.commands.run(["git", "worktree", "add", "--detach", str(source), sha])
        if self.commands.run(["git", "-C", str(source), "rev-parse", "HEAD"]) != sha:
            raise UpdateError("Сохранённая копия релиза имеет другой SHA.")
        if self.commands.run(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=normal"]):
            raise UpdateError("Сохранённая копия релиза изменена; автоматическая замена запрещена.")
        if (source / "VERSION").read_text(encoding="utf-8").strip() != release.version:
            raise UpdateError("VERSION в исходниках не соответствует тегу релиза.")
        return source

    def _running(self, project: str) -> dict[str, dict]:
        result = {}
        for container in self._containers(project):
            labels = container["Config"].get("Labels") or {}
            name = labels.get("com.docker.compose.service")
            if name in APPLICATIONS:
                location = labels.get("com.docker.compose.project.working_dir", "")
                if location and Path(location).resolve() != self.root:
                    raise UpdateError("Compose-проект принадлежит другой папке установки; он не изменялся.")
                if name in result:
                    raise UpdateError("Несколько контейнеров одного сервиса: автоматическое обновление запрещено.")
                result[name] = container
        return result

    def _snapshot(self, configuration: dict, path: Path, *, source: Path,
                  services: list[str], sha: str | None, version: str | None) -> dict:
        atomic_json(path, configuration)
        return {"compose_path": str(path), "project_name": configuration["name"],
                "source_path": str(source), "services": services, "sha": sha,
                "version": version, "context": self.context, "daemon_id": self.daemon_id}

    def _old_snapshot(self, options: dict, run_directory: Path, state: dict) -> tuple[dict, dict, dict]:
        current = state.get("current")
        if current:
            self._same_host(current)
            self._snapshot_args(current)
            configuration = read_json(Path(current["compose_path"]))
        else:
            configuration = self._json_docker(*self._base(options), "config", "--format", "json")
        running = self._running(options["project_name"])
        api = running.get("api", {})
        if not api.get("State", {}).get("Running"):
            raise UpdateError("API этой установки не запущен. Сначала запустите существующую сборку; нативные/dev-процессы не останавливаются.")
        if any(item.get("State", {}).get("Paused") for item in running.values()):
            raise UpdateError("В установке уже есть приостановленные сервисы; их состояние автоматически не меняется.")
        configuration = deepcopy(configuration)
        for name, container in running.items():
            if name not in configuration.get('services', {}):
                raise UpdateError('Работающий сервис отсутствует в сохранённой конфигурации; его состояние не менялось.')
            self._validate_live(container, configuration['services'][name], configuration)
            configuration["services"][name]["image"] = container["Image"]
            configuration["services"][name].pop("build", None)
        labels = api["Config"].get("Labels") or {}
        sha = labels.get("org.opencontainers.image.revision")
        version = labels.get("org.opencontainers.image.version")
        services = sorted(name for name, container in running.items() if container["State"]["Running"])
        old = self._snapshot(configuration, run_directory / "previous.compose.json",
                             source=Path(current["source_path"]) if current else self.root,
                             services=services, sha=sha, version=version)
        return old, configuration, running

    @staticmethod
    def _mount_source(value: str) -> str:
        value = value.replace('\\', '/')
        match = re.match(r'^/run/desktop/mnt/host/([a-zA-Z])/(.*)$', value)
        if match:
            value = match[1] + ':/' + match[2]
        if value.startswith('/host_mnt/'):
            value = value[len('/host_mnt'):]
        if re.match(r'^[a-zA-Z]:/', value):
            return value.rstrip('/').lower()
        return str(Path(value).resolve())

    @classmethod
    def _validate_live(cls, container: dict, service: dict, configuration: dict) -> None:
        declared = service.get('environment', {})
        actual = dict(item.split('=', 1) for item in container.get('Config', {}).get('Env', []) if '=' in item)
        # Compose's canonical JSON escapes every literal dollar as $$ so that
        # the snapshot can be fed back to Compose without re-interpolation.
        # The actual container environment contains the unescaped single $.
        if any((None if declared[key] is None else str(declared[key]).replace('$$', '$')) != actual.get(key) for key in CRITICAL_ENV if key in declared):
            raise UpdateError('Фактические настройки контейнера отличаются от сохранённых (хранилище или ПК). Автоматическая замена запрещена.')
        mounts = {item['Destination']: item for item in container.get('Mounts', [])}
        for volume in service.get('volumes', []):
            if not isinstance(volume, dict):
                raise UpdateError('Не подтверждена форма привязки данных работающей установки.')
            target = volume.get('target')
            actual_mount = mounts.get(target, {})
            if actual_mount.get('Type') != volume.get('type') or actual_mount.get('RW') != (not volume.get('read_only', False)):
                raise UpdateError('Фактическая привязка/доступ к данным отличается от журнала. Данные не переносились.')
            source = volume.get('source')
            if volume.get('type') == 'bind' and cls._mount_source(str(source).replace('$$', '$')) != cls._mount_source(actual_mount.get('Source', '')):
                raise UpdateError('Фактическая папка данных отличается от журнала. Данные не переносились.')
            if volume.get('type') == 'volume' and source:
                name = configuration.get('volumes', {}).get(source, {}).get('name') or configuration['name'] + '_' + source
                if actual_mount.get('Name') != name:
                    raise UpdateError('Фактический том отличается от журнала. Хранилище не заменялось.')

    def _same_host(self, snapshot: dict) -> None:
        if snapshot["context"] != self.context or snapshot["daemon_id"] != self.daemon_id:
            raise UpdateError("Docker context/движок изменился; нельзя восстанавливать другую установку.")

    def _prepare(self, release: Release, options: dict, state: dict) -> tuple[dict, dict]:
        source = self._stage(release)
        run_directory = self.directory / "runs" / uuid4().hex
        private_directory(run_directory)
        old, before, running = self._old_snapshot(options, run_directory, state)
        if old.get("version"):
            comparison = version_tuple(old["version"])
            if comparison > version_tuple(release.version):
                raise UpdateError("Последний релиз старее установленной версии; автоматический downgrade запрещён.")
            if comparison == version_tuple(release.version):
                if old.get("sha") != release.sha:
                    raise UpdateError("Одинаковая версия имеет другой SHA; это не подтверждённое обновление.")
                return old, old
        candidate = self._json_docker(*self._base(options, source), "config", "--format", "json")
        self._compatible_configuration(before, candidate)
        services = sorted(set(old["services"]) | REQUIRED)
        services = [name for name in services if name in candidate["services"]
                    and candidate["services"][name].get("deploy", {}).get("replicas", 1) != 0]
        if "api" not in services:
            raise UpdateError("У целевого релиза нет операторского API.")
        if set(old["services"]) - set(services):
            raise UpdateError("Релиз удаляет работающий сервис; требуется отдельное обслуживание установки.")
        self._pin_images(candidate, services, release)
        # Keep exactly the storage declarations of this installation. Updates
        # only pass the application allowlist to `up --no-deps`.
        for name, service in before["services"].items():
            if name not in APPLICATIONS:
                candidate["services"][name] = service
        target = self._snapshot(candidate, run_directory / "candidate.compose.json", source=source,
                                services=services, sha=release.sha, version=release.version)
        target["release"] = release.as_dict()
        self._check_schema(target)
        self._save(state, "prepared", current=old, candidate=target, frozen=[])
        return old, target

    def _pin_images(self, candidate: dict, services: list[str], release: Release, *, expected_images=None) -> None:
        for image in sorted({APPLICATIONS[name] for name in services}):
            tag = f"{IMAGE_REPOSITORY}:{image}-{release.tag}"
            if expected_images is not None:
                tag = expected_images.get(image, '')
                if not re.fullmatch(re.escape(IMAGE_REPOSITORY) + r'@sha256:[0-9a-f]{64}', tag):
                    raise UpdateError('В настольном пакете нет закреплённого digest необходимого контейнера.')
            self.progress(f"Проверяю образ {image} {release.tag}…")
            # Current published images are amd64. This is explicit on Apple Silicon,
            # not a fake ARM build; Docker Desktop may execute them via emulation.
            self._docker("pull", "--platform", "linux/amd64", tag, timeout=900)
            information = self._json_docker("image", "inspect", tag)[0]
            labels = information["Config"].get("Labels") or {}
            if (labels.get("org.opencontainers.image.revision") != release.sha
                    or labels.get("org.opencontainers.image.version") != release.version
                    or labels.get("io.printers-companion.source-state") != "clean"
                    or information.get("Os") != "linux" or information.get("Architecture") != "amd64"):
                raise UpdateError("Образ не соответствует опубликованному SHA/версии/платформе.")
            if expected_images is not None and labels.get('io.printers-companion.update-protocol') != '1':
                raise UpdateError('Контейнер не подтверждает безопасный протокол настольного обновления.')
            digests = [value for value in information.get("RepoDigests", [])
                       if re.fullmatch(re.escape(IMAGE_REPOSITORY) + r"@sha256:[0-9a-f]{64}", value)]
            if not digests:
                raise UpdateError("У скачанного образа нет подтверждённого registry digest.")
            if expected_images is not None and tag not in digests:
                raise UpdateError('Скачанный контейнер отличается от digest в настольном пакете.')
            for name in services:
                if APPLICATIONS[name] == image:
                    candidate["services"][name].update(image=tag if expected_images is not None else digests[0], platform="linux/amd64")
                    candidate["services"][name].pop("build", None)

    def _check_schema(self, target: dict) -> None:
        self.progress("Проверяю совместимость общей схемы БД (без миграции)…")
        try:
            self._docker(*self._snapshot_args(target), "run", "--rm", "--no-deps", "--pull", "never",
                         "--entrypoint", "python", "api", "-c", _SCHEMA_PROBE, timeout=30)
        except UpdateError:
            raise UpdateError("Новая версия не подтвердила БД/схему. Старые сервисы не менялись. Если нужна миграция NAS, выполните отдельное согласованное обслуживание из одного ПК.") from None

    @staticmethod
    def _compatible_configuration(before: dict, after: dict) -> None:
        if before["name"] != after["name"]:
            raise UpdateError("Релиз меняет имя Compose-проекта и может создать другие тома.")
        for key in CRITICAL_ENV:
            old = before["services"]["api"].get("environment", {}).get(key)
            new = after["services"]["api"].get("environment", {}).get(key)
            if old != new:
                raise UpdateError("Изменился адрес хранилища, секрет или идентификатор ПК; требуется отдельная настройка, не обновление.")
        if before.get("volumes", {}) != after.get("volumes", {}):
            raise UpdateError("Изменились определения томов; автоматический перенос данных запрещён.")
        for name in set(before["services"]) & set(after["services"]) & set(APPLICATIONS):
            if before["services"][name].get("volumes", []) != after["services"][name].get("volumes", []):
                raise UpdateError("Релиз меняет привязку данных сервиса; сначала требуется план миграции.")

    def _busy(self, api_id: str) -> None:
        try:
            raw = self._docker("exec", api_id, "python", "-c", _BUSY_PROBE, timeout=15)
            count = json.loads(raw)["running_jobs"]
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError("invalid count")
        except (UpdateError, ValueError, KeyError):
            raise UpdateError("Не удалось проверить активные задания этого ПК; переключение запрещено.") from None
        if count:
            raise UpdateBusy(f"Этот ПК выполняет задания ({count}). Ожидаю завершения расчётов.")

    def _unfreeze(self, state: dict) -> None:
        for container_id in list(state.get("frozen", [])):
            inspected = self._json_docker('inspect', container_id)
            if not inspected:
                raise UpdateError('Не найден приостановленный сервис. Журнал восстановления сохранён.')
            if inspected[0].get('State', {}).get('Paused'):
                self._docker("unpause", container_id, timeout=15)
            inspected = self._json_docker('inspect', container_id)
            if not inspected or inspected[0].get('State', {}).get('Paused'):
                raise UpdateError('Возобновление сервиса не подтверждено. Журнал восстановления сохранён.')
            self._save(state, state.get('phase', 'blocked'), frozen=[item for item in state['frozen'] if item != container_id])
        self._save(state, state.get("phase", "blocked"), frozen=[])

    def _gate(self, state: dict, action: str) -> None:
        token = state.get("maintenance_token")
        if not token:
            return
        api = self._running(state["current"]["project_name"]).get("api", {})
        if not api.get("State", {}).get("Running"):
            raise UpdateError("API недоступен для снятия локального режима обновления.")
        try:
            result = json.loads(self._docker("exec", api["Id"], "python", "-c", _GATE_PROGRAM, action, token, timeout=15))
            if result.get('owned') is not True and action == 'claim':
                self._save(state, 'prepared', maintenance_token=None)
                raise UpdateError('Режим обслуживания уже занят другим обновлением.')
        except (ValueError, AttributeError):
            raise UpdateError('Не подтверждён владелец режима обновления; журнал сохранён.') from None
        if action == "release":
            self._save(state, state["phase"], maintenance_token=None)

    def _abort_quiesce(self, state: dict, *, error: str | None = None) -> None:
        """Undo a rejected pre-switch operation, preserving failed cleanup.

        Do not mark the operation blocked or discard its candidate until every
        frozen consumer and our admission barrier have been released. If either
        cleanup fails, the previous journal phase remains recoverable.
        """
        self._unfreeze(state)
        self._gate(state, "release")
        self._save(state, "blocked", candidate=None, error=error)

    def _quiesce(self, state: dict, old: dict, running: dict) -> None:
        self._same_host(old)
        if set(old['services']) & {'scheduler', 'mcp'}:
            raise UpdateError('Работает дополнительный scheduler/MCP без безопасного drain. Сначала завершите его отдельно; операторская версия сохранена.')
        if not running.get('api', {}).get('State', {}).get('Running'):
            raise UpdateError('API недоступен для безопасного переключения.')
        self._busy(running["api"]["Id"])
        # Persist token before claiming it, so a process crash can recover it.
        self._save(state, "quiescing", frozen=[], maintenance_token=uuid4().hex)
        try:
            self._gate(state, "claim")
        except UpdateError:
            # Unknown commit: the marker may already be ours. Keep the token
            # until ownership-checked release confirms cleanup.
            raise UpdateError("Не удалось получить локальный режим обслуживания; переключение запрещено.") from None
        # Existing HTTP writes may have started before the barrier. Drain the
        # complete ASGI request, including upload and background publication.
        # Old backends without this protocol fail closed, not get killed.
        self.progress('Ожидаю завершения принятых загрузок и записей…')
        try:
            drained = json.loads(self._docker('exec', running['api']['Id'], 'python', '-c', _DRAIN_PROBE, timeout=65))
            if drained.get('drained') is not True:
                raise ValueError('not drained')
        except (UpdateError, ValueError, AttributeError):
            raise UpdateBusy('Принятые загрузки ещё не завершились или старая версия не поддерживает безопасное переключение. Работающая версия сохранена.') from None
        for name in old["services"]:
            if name != "api":
                container_id = running[name]["Id"]
                self._save(state, "quiescing", frozen=state["frozen"] + [container_id])
                self._docker("pause", container_id, timeout=15)
        self._busy(running["api"]["Id"])

    def _stop_frozen(self, state: dict) -> None:
        self._save(state, "switching")
        for container_id in state["frozen"]:
            self._docker("kill", "--signal=KILL", container_id, timeout=30)
        self._save(state, "switching", frozen=[])

    def _deploy(self, snapshot: dict, *, start_dependencies=False) -> None:
        self._same_host(snapshot)
        args = [*self._snapshot_args(snapshot), "up", "-d", "--no-build", "--pull", "never"]
        if not start_dependencies:
            args.append("--no-deps")
        self._docker(*args, *snapshot["services"], timeout=180)

    def _resume_containers(self, snapshot: dict) -> None:
        """Ordinary launch must not recreate a container doing a calculation.

        Compose up can recreate even the same image when a tag becomes a pinned
        ID or a compose label/hash changes. Start exact stopped containers; only
        create genuinely missing services with --no-deps. Switching a running
        service belongs exclusively to the drained update/rollback path.
        """
        self._same_host(snapshot)
        configuration = read_json(Path(snapshot['compose_path']))
        all_containers = self._containers(snapshot['project_name'])
        applications = self._running(snapshot['project_name'])
        for name in snapshot['services']:
            container = applications.get(name)
            if not container:
                continue
            expected = self._json_docker('image', 'inspect', configuration['services'][name]['image'])[0]
            if container.get('Image') != expected.get('Id') or container.get('State', {}).get('Paused'):
                raise UpdateError('Работающий/приостановленный сервис отличается от журнала. При запуске он не заменялся; требуется проверка установки.')
            self._validate_live(container, configuration['services'][name], configuration)
        # Start/create only declared local dependencies, never disabled NAS
        # storage. No dependency is restarted/reconfigured if it is already up.
        for name in ('postgres', 'minio', 'redis'):
            service = configuration.get('services', {}).get(name)
            if not service or service.get('deploy', {}).get('replicas') == 0:
                continue
            found = [item for item in all_containers if (item['Config'].get('Labels') or {}).get('com.docker.compose.service') == name]
            if len(found) > 1:
                raise UpdateError('Неоднозначные локальные зависимости; автоматический запуск запрещён.')
            if found:
                item = found[0]
                location = (item['Config'].get('Labels') or {}).get('com.docker.compose.project.working_dir')
                if not location or Path(location).resolve() != self.root or item.get('State', {}).get('Paused'):
                    raise UpdateError('Локальная зависимость принадлежит другой/приостановленной установке.')
                if not item['State']['Running']:
                    self._docker('start', item['Id'], timeout=60)
            elif name == 'redis':
                self._docker(*self._snapshot_args(snapshot), 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', name, timeout=60)
            else:
                raise UpdateError('Не найдено существующее локальное хранилище. Новая БД вместо него не создавалась.')
        stopped = [applications[name]['Id'] for name in snapshot['services'] if name in applications and not applications[name]['State']['Running']]
        if stopped:
            self._docker('start', *stopped, timeout=60)
        missing = set(snapshot['services']) - set(applications)
        if missing:
            self._docker(*self._snapshot_args(snapshot), 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', *sorted(missing), timeout=180)

    @staticmethod
    def _readiness(url: str, expected_sha: str | None) -> bool:
        try:
            with urlopen(url + "/health/ready", timeout=10) as response:
                data = json.load(response)
            checks = data.get("checks", {})
            required = ("database", "schema", "redis", "minio", "minio_buckets")
            return (data.get("status") == "ready" and all(checks.get(key) is True for key in required)
                    and (not expected_sha or data.get("build", {}).get("git_sha") == expected_sha))
        except (OSError, ValueError):
            return False

    def _verify(self, snapshot: dict, options: dict, timeout: int) -> None:
        deadline = time.monotonic() + timeout
        while True:
            containers = self._running(snapshot["project_name"])
            expected = read_json(Path(snapshot["compose_path"]))["services"]
            healthy = True
            for name in snapshot["services"]:
                container = containers.get(name, {})
                if not container.get("State", {}).get("Running") or container.get("State", {}).get("Paused"):
                    healthy = False
                    break
                # Inspect pinned digests/IDs, not mutable compose image aliases.
                image = self._json_docker("image", "inspect", expected[name]["image"])[0]
                if container.get("Image") != image.get("Id"):
                    healthy = False
                    break
            sha = snapshot.get("sha")
            sha = sha if sha and re.fullmatch(r"[0-9a-f]{40}", sha) else None
            if healthy and self.readiness(options["url"], sha):
                return
            if time.monotonic() >= deadline:
                raise UpdateError("Новая версия не подтвердила готовность/SHA/запуск сервисов. Это не проверка выполнения задач worker.")
            time.sleep(2)

    def _restore(self, state: dict, options: dict, timeout: int) -> None:
        old = state.get("current")
        if not old:
            raise UpdateError("Нет сохранённой установки для восстановления.")
        self._same_host(old)
        self._save(state, "rolling_back")
        self._unfreeze(state)
        try:
            extra = set((state.get("candidate") or {}).get("services", [])) - set(old["services"])
            if extra:
                self._docker(*self._snapshot_args(state["candidate"]), "stop", "--timeout", "30", *sorted(extra))
            self._deploy(old)
            self._verify(old, options, timeout)
            self._gate(state, "release")
        except BaseException:
            self._save(state, "needs_recovery", error="Восстановление не подтверждено; повторите rollback после устранения проблемы.")
            raise
        self._save(state, "rolled_back", candidate=None, error="Новая версия не применена; прежняя установка восстановлена.")

    def _recover(self, state: dict, options: dict, timeout: int) -> None:
        if state.get("phase") == "committed":
            self._same_host(state["current"])
            self._verify(state["current"], options, timeout)
            self._gate(state, "release")
            self._save(state, "completed")
            return
        if state.get("phase") in {"quiescing", "restart_ready"}:
            self._same_host(state["current"])
            self._unfreeze(state)
            self._gate(state, "release")
            self._save(state, "interrupted", candidate=None)
        elif state.get("phase") in {"switching", "verifying", "rolling_back", "needs_recovery"}:
            self.progress("Восстанавливаю прерванное переключение…")
            self._restore(state, options, timeout)

    def apply(self, *, version=None, timeout=180) -> dict:
        with installation_lock(self.directory):
            options = self._options()
            state = self.status()
            self._recover(state, options, timeout)
            self._clean_sources()
            self._save(state, "preparing", error=None)
            try:
                release = self.release_loader(version)
                old, target = self._prepare(release, options, state)
                if old is target:
                    self._verify(old, options, timeout)
                    self._save(state, "current", current=old, candidate=None)
                    return state
                running = self._running(options["project_name"])
                self._quiesce(state, old, running)
                self._clean_sources()
                # These consumers have no committed running compute jobs. KILL
                # while frozen avoids a claim between unpause and graceful stop.
                # Outbox transfers remain resumable via their durable receipts.
                self._stop_frozen(state)
                self._deploy(target)
                self._save(state, "verifying")
                self._verify(target, options, timeout)
                self._save(state, "committed", previous=old, current=target, candidate=None, error=None)
                self._gate(state, "release")
                self._save(state, "completed")
                self._notify(options, target)
                return state
            except BaseException as exc:
                if state.get("phase") == "committed":
                    # The verified version is already committed. Do not roll it
                    # back because barrier cleanup or notification failed.
                    raise
                if state.get("phase") in {"switching", "verifying"}:
                    self.progress("Запуск не подтверждён. Возвращаю прежнюю версию…")
                    self._restore(state, options, timeout)
                else:
                    self._abort_quiesce(state,
                                       error=str(exc) if isinstance(exc, UpdateError) else "Обновление прервано; текущая версия сохранена.")
                raise

    def rollback(self, *, timeout=180) -> dict:
        with installation_lock(self.directory):
            options = self._options()
            state = self.status()
            if state.get("phase") in {"quiescing", "restart_ready", "switching", "verifying", "rolling_back", "needs_recovery", "committed"}:
                self._recover(state, options, timeout)
                return state
            old = state.get("previous")
            if not old:
                raise UpdateError("Нет предыдущей подтверждённой версии для отката.")
            current = state["current"]
            running = self._running(current["project_name"])
            # Normal rollback is itself a journalled switch. Recovery target is
            # the currently running version until the old version is verified.
            self._same_host(old)
            self._docker(*self._snapshot_args(old), 'run', '--rm', '--no-deps', '--pull', 'never',
                         '--entrypoint', 'python', 'api', '-c', _DATABASE_PROBE, timeout=30)
            self._save(state, "prepared", candidate=old)
            try:
                self._quiesce(state, current, running)
                self._stop_frozen(state)
                extra = set(current['services']) - set(old['services'])
                if extra:
                    self._docker(*self._snapshot_args(current), 'stop', '--timeout', '30', *sorted(extra))
                self._deploy(old)
                self._verify(old, options, timeout)
            except BaseException:
                if state.get("phase") in {"switching", "verifying"}:
                    self._restore(state, options, timeout)
                else:
                    self._abort_quiesce(state)
                raise
            self._save(state, "committed", current=old, previous=current, candidate=None, error=None)
            self._gate(state, "release")
            self._save(state, "completed")
            self._notify(options, old)
            return state

    def launch(self, *, timeout=180) -> dict:
        options = read_json(self.settings_path)
        if not options:
            self.configure()
            options = read_json(self.settings_path)
        with installation_lock(self.directory):
            self._host(options)
            state = self.status()
            self._recover(state, options, timeout)
            if state.get("current"):
                self._resume_containers(state['current'])
                self._verify(state["current"], options, timeout)
            else:
                # First registration of an already-installed stack. Start exact
                # existing containers, not new mutable aliases from the checkout.
                running = self._running(options["project_name"])
                if "api" not in running:
                    raise UpdateError("Сначала выполните первоначальную установку проекта. Storage-only NAS не запускается обновлятором.")
                containers = self._containers(options["project_name"])
                configuration = self._json_docker(*self._base(options), 'config', '--format', 'json')
                eligible = {name for name, value in configuration.get('services', {}).items() if value.get('deploy', {}).get('replicas') != 0}
                for names in ({"postgres", "minio", "redis"} & eligible, REQUIRED & eligible):
                    stopped = [item["Id"] for item in containers
                               if (item["Config"].get("Labels") or {}).get("com.docker.compose.service") in names
                               and not item["State"].get("Running")]
                    if stopped:
                        self._docker("start", *stopped, timeout=60)
                run_directory = self.directory / "runs" / uuid4().hex
                private_directory(run_directory)
                old, _, _ = self._old_snapshot(options, run_directory, state)
                self._verify(old, options, timeout)
                self._save(state, "current", current=old)
        # Launch never silently downloads a new release. Updating is explicit.
        return state

    @staticmethod
    def _notify(options: dict, snapshot: dict) -> None:
        try:
            request = Request(options["url"] + "/admin/update/notify", method="POST",
                              data=json.dumps({"commit": snapshot.get("sha") or "",
                                               "message": f"Проверено обновление {snapshot.get('version') or ''}"}).encode(),
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=3):
                pass
        except OSError:
            pass  # Local durable state, not Redis notification, is authoritative.
