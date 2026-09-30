"""Exercise release/launcher scripts with isolated Git and fake Docker only."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def executable(path, source):
    path.write_text(source)
    path.chmod(0o755)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def release_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy2(ROOT / "release.sh", repo / "release.sh")
    (repo / "VERSION").write_text("1.0.0\n")
    (repo / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Release test")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "tag.gpgsign", "false")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "docker.log"
    executable(binaries / "docker", '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_DOCKER_LOG"\n')
    env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "TEST_DOCKER_LOG": str(log)}
    return repo, env, log


def test_release_images_reference_version_commit_not_its_parent(release_repo):
    repo, env, log = release_repo
    before = git(repo, "rev-parse", "HEAD")
    subprocess.run(["bash", "release.sh", "patch", "--no-push", "--no-flash"], cwd=repo,
                   env=env, check=True, capture_output=True, text=True)
    after = git(repo, "rev-parse", "HEAD")
    assert after != before
    assert git(repo, "rev-parse", "v1.0.1^{commit}") == after
    builds = [line for line in log.read_text().splitlines() if "--build-arg GIT_COMMIT=" in line]
    assert len(builds) == 5
    assert all(f"GIT_COMMIT={after}" in line and "APP_VERSION=1.0.1" in line
               and "SOURCE_STATE=clean" in line for line in builds)


@pytest.mark.parametrize("conflict", ["dirty", "existing_tag"])
def test_release_refuses_ambiguous_sources_without_changing_them(release_repo, conflict):
    repo, env, log = release_repo
    if conflict == "dirty":
        (repo / "pending.txt").write_text("operator work")
    else:
        git(repo, "tag", "v1.0.1")
    before = git(repo, "rev-parse", "HEAD")
    result = subprocess.run(["bash", "release.sh", "patch", "--no-push", "--no-flash"],
                            cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert git(repo, "rev-parse", "HEAD") == before
    assert (repo / "VERSION").read_text() == "1.0.0\n"
    assert not log.exists()


@pytest.mark.parametrize("first_run", [True, False])
@pytest.mark.parametrize("running_revision", ["b" * 40, "stale-build"])
def test_mac_launcher_sets_identity_before_every_build(tmp_path, first_run, running_revision):
    launcher = tmp_path / "launch.command"
    shutil.copy2(ROOT / "deploy/launchers/Запустить.command", launcher)
    source = tmp_path / "source"
    source.mkdir()
    (source / "VERSION").write_text("1.2.3\n")
    (source / ".env.example").write_text("# no secrets\n")
    if not first_run:
        shutil.copytree(source, tmp_path / "printers-companion")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    executable(binaries / "git", '''#!/usr/bin/env python3
import os, shutil, sys
if sys.argv[1] == 'clone': shutil.copytree(os.environ['TEST_SOURCE'], sys.argv[-1])
elif 'rev-parse' in sys.argv: print('b' * 40)
''')
    executable(binaries / "docker", '''#!/usr/bin/env python3
import json, os, sys
if 'printenv' in sys.argv: print(os.environ['TEST_API_REVISION'])
if 'build' in sys.argv or 'up' in sys.argv:
    with open(os.environ['TEST_DOCKER_LOG'], 'a') as target:
        target.write(json.dumps({'args': sys.argv[1:], 'identity': {key: os.environ.get(key) for key in ['APP_VERSION', 'GIT_COMMIT', 'BUILD_DATE', 'SOURCE_STATE']}}) + '\\n')
''')
    for binary in ("open", "curl"):
        executable(binaries / binary, "#!/bin/sh\nexit 0\n")
    log = tmp_path / "builds.jsonl"
    env = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}",
           "TEST_SOURCE": str(source), "TEST_DOCKER_LOG": str(log), "TEST_API_REVISION": running_revision}
    result = subprocess.run(["bash", str(launcher)], env=env, cwd=tmp_path, input="\n", text=True,
                            capture_output=True, timeout=20)
    if running_revision == "stale-build":
        assert result.returncode != 0
        assert "не соответствует" in result.stdout
    else:
        assert result.returncode == 0, result.stdout + result.stderr
    runs = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(runs) == 2
    assert "build" in runs[0]["args"] and "up" in runs[1]["args"]
    assert runs[0]["identity"] == runs[1]["identity"]
    assert runs[0]["identity"]["GIT_COMMIT"] == "b" * 40
    assert runs[0]["identity"]["APP_VERSION"] == "1.2.3"
    assert runs[0]["identity"]["SOURCE_STATE"] == "clean"
    assert runs[0]["identity"]["BUILD_DATE"].endswith("Z")


def test_compose_uses_same_identity_for_all_application_images():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    for name in ("api", "worker", "nas-sync", "estimator", "watcher", "scheduler", "mcp"):
        assert compose["services"][name]["build"]["args"] == compose["x-build-identity"]


def test_local_compose_uses_available_minio_registry_and_dependency_health():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert compose["services"]["minio"]["image"] == "quay.io/minio/minio:RELEASE.2024-05-10T01-41-38Z"
    assert "/health/ready" in " ".join(compose["services"]["api"]["healthcheck"]["test"])


@pytest.mark.parametrize("name", ["api", "worker", "scheduler", "watcher", "mcp"])
def test_service_images_embed_the_manifest_inputs(name):
    source = (ROOT / f"Dockerfile.{name}").read_text()
    for key in ("APP_VERSION", "GIT_COMMIT", "BUILD_DATE", "SOURCE_STATE"):
        assert f"ARG {key}=" in source
        assert f"{key}=${key}" in source
    assert 'org.opencontainers.image.source="https://github.com/ArtemIvanchenko/Printers-companion"' in source


def test_release_image_publication_requires_matching_version_and_successful_ci():
    workflow = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"]["push"]["tags"] == ["v*"]
    assert workflow["permissions"] == {"contents": "read", "actions": "read", "packages": "write"}
    steps = workflow["jobs"]["publish"]["steps"]
    gate = next(step["run"] for step in steps if step.get("name", "").startswith("Require"))
    assert 'test "$GITHUB_REF_NAME" = "v$VERSION"' in gate
    assert '--commit "$GITHUB_SHA"' in gate
    assert '.status == "completed" and .conclusion == "success"' in gate
    assert "grep -qx success" in gate


def test_release_builds_all_images_before_updating_mutable_aliases():
    workflow = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["publish"]["steps"]
    build_index = next(i for i, step in enumerate(steps) if step.get("name", "").startswith("Build all"))
    publish_index = next(i for i, step in enumerate(steps) if step.get("name", "").startswith("Publish versioned"))
    assert build_index < publish_index
    build = steps[build_index]["run"]
    assert "for service in api worker scheduler watcher mcp" in build
    assert '--build-arg "GIT_COMMIT=$GITHUB_SHA"' in build
    assert "org.opencontainers.image.revision" in build
    publish = steps[publish_index]["run"]
    assert publish.index('docker push "$IMAGE_REPO:$service-v$APP_VERSION"') < publish.index('docker push "$IMAGE_REPO:$service"')
