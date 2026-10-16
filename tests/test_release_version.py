"""Version bumps use isolated files and fake Git/Docker, never a real release."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
VERSION_FILES = (
    "VERSION", "pyproject.toml", "desktop/package.json", "desktop/package-lock.json",
)


@pytest.fixture
def release_versions(tmp_path):
    repo = tmp_path / "source"
    (repo / "desktop").mkdir(parents=True)
    shutil.copy2(ROOT / "release.sh", repo / "release.sh")
    (repo / "VERSION").write_text("1.0.0\n")
    (repo / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\n\n[project]\n'
        'version = "1.0.0"  # static metadata\n\n'
        '[tool.example]\nversion = "9.0.0"\n',
    )
    (repo / "desktop/package.json").write_text('{"version": "1.0.0"}\n')
    (repo / "desktop/package-lock.json").write_text(
        '{"version": "1.0.0", "packages": {"": {"version": "1.0.0"}, '
        '"node_modules/dependency": {"version": "9.0.0"}}}\n',
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    command_log = tmp_path / "commands.jsonl"
    for name in ("git", "docker"):
        binary = binaries / name
        binary.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['TEST_COMMAND_LOG'], 'a') as target:
    target.write(json.dumps({'tool': name, 'args': args}) + '\\n')
if name == 'git':
    if args[0] == 'status':
        if os.environ.get('TEST_DIRTY') == '1': print(' M pending.txt')
    elif args[0] == 'rev-parse':
        if '--verify' in args:
            if os.environ.get('TEST_EXISTING_TAG') != '1': sys.exit(1)
        print('b' * 40)
    elif args[0] == 'diff':
        sys.exit(0 if os.environ.get('TEST_UNCHANGED') == '1' else 1)
''')
        binary.chmod(0o755)
    env = dict(os.environ, PATH=f"{binaries}:{os.environ['PATH']}",
               TEST_COMMAND_LOG=str(command_log))
    return repo, env, command_log


def _run_release(fixture, bump="patch", extra=()):
    repo, env, _ = fixture
    return subprocess.run(
        ["bash", "release.sh", bump, "--no-push", "--no-flash", *extra],
        cwd=repo, env=env, capture_output=True, text=True, timeout=10,
    )


def _versions(repo):
    project = tomllib.loads((repo / "pyproject.toml").read_text())
    package = json.loads((repo / "desktop/package.json").read_text())
    lock = json.loads((repo / "desktop/package-lock.json").read_text())
    return [(repo / "VERSION").read_text().strip(), project["project"]["version"],
            package["version"], lock["version"], lock["packages"][""]["version"]]


@pytest.mark.parametrize("bump,expected", [("patch", "1.0.1"), ("2.3.4", "2.3.4")])
def test_release_bumps_every_package_version_before_staging(release_versions, bump, expected):
    repo, _, log = release_versions
    result = _run_release(release_versions, bump)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _versions(repo) == [expected] * 5
    assert tomllib.loads((repo / "pyproject.toml").read_text())["tool"]["example"]["version"] == "9.0.0"
    lock = json.loads((repo / "desktop/package-lock.json").read_text())
    assert lock["packages"]["node_modules/dependency"]["version"] == "9.0.0"
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    staged = next(item["args"] for item in commands
                  if item["tool"] == "git" and item["args"][0] == "add")
    assert staged == ["add", *VERSION_FILES]


@pytest.mark.parametrize("source", ["project", "desktop", "lock", "lock_package"])
def test_release_refuses_version_drift_before_changing_files(release_versions, source):
    repo, _, log = release_versions
    if source == "project":
        path = repo / "pyproject.toml"
        path.write_text(path.read_text().replace('version = "1.0.0"', 'version = "0.9.0"'))
    else:
        path = repo / ("desktop/package.json" if source == "desktop" else "desktop/package-lock.json")
        value = json.loads(path.read_text())
        target = value["packages"][""] if source == "lock_package" else value
        target["version"] = "0.9.0"
        path.write_text(json.dumps(value))
    before = {name: (repo / name).read_bytes() for name in VERSION_FILES}

    result = _run_release(release_versions)

    assert result.returncode != 0
    assert "VERSION" in result.stderr
    assert {name: (repo / name).read_bytes() for name in VERSION_FILES} == before
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(item["tool"] == "git" and item["args"][0] in {"status", "rev-parse"}
               for item in commands)


@pytest.mark.parametrize("gate", ["TEST_DIRTY", "TEST_EXISTING_TAG"])
def test_release_keeps_existing_clean_source_and_immutable_tag_gates(release_versions, gate):
    repo, env, log = release_versions
    env[gate] = "1"
    before = {name: (repo / name).read_bytes() for name in VERSION_FILES}
    result = _run_release(release_versions)
    assert result.returncode != 0
    assert {name: (repo / name).read_bytes() for name in VERSION_FILES} == before
    assert all(json.loads(line)["tool"] == "git" for line in log.read_text().splitlines())


def test_release_dry_run_changes_no_metadata_and_calls_no_tools(release_versions):
    repo, _, log = release_versions
    before = {name: (repo / name).read_bytes() for name in VERSION_FILES}
    result = _run_release(release_versions, extra=("--dry-run",))
    assert result.returncode == 0, result.stdout + result.stderr
    assert {name: (repo / name).read_bytes() for name in VERSION_FILES} == before
    assert not log.exists()


def test_release_same_version_keeps_metadata_format_and_skips_version_commit(release_versions):
    repo, env, log = release_versions
    env["TEST_UNCHANGED"] = "1"
    before = {name: (repo / name).read_bytes() for name in VERSION_FILES}
    result = _run_release(release_versions, bump="1.0.0")
    assert result.returncode == 0, result.stdout + result.stderr
    assert {name: (repo / name).read_bytes() for name in VERSION_FILES} == before
    assert all(json.loads(line)["args"][0] != "commit" for line in log.read_text().splitlines())
