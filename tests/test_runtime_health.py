"""Readiness is read-only dependency evidence, never a synthetic worker heartbeat."""
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from core.config.settings import Settings
from core import runtime_health
from core.versioning.provenance import build_manifest, build_provenance


@pytest.fixture
def health_settings(tmp_path):
    return Settings(_env_file=None, app_env="test", database_url=f"sqlite:///{tmp_path / 'health.sqlite'}",
                    redis_url="redis://127.0.0.1:1/0", minio_endpoint="127.0.0.1:2",
                    llm_provider="null")


def ready_probes(monkeypatch):
    monkeypatch.setattr(runtime_health, "_database_checks", lambda s: {"database": True, "schema": True})
    monkeypatch.setattr(runtime_health, "_redis_ready", lambda s: True)
    monkeypatch.setattr(runtime_health, "_object_store_checks", lambda s: {"minio": True, "minio_buckets": True})


def test_ready_explicitly_leaves_workers_and_writes_unverified(monkeypatch, health_settings):
    ready_probes(monkeypatch)
    report = runtime_health.check_api_readiness(health_settings)
    assert report["status"] == "ready"
    assert report["scope"] == "api_dependencies"
    assert all(value["status"] == "unknown" for value in report["capabilities"].values())
    assert report["build"] == build_manifest()


@pytest.mark.parametrize("failed", ["database", "schema", "redis", "minio", "minio_buckets"])
def test_each_dependency_is_required(monkeypatch, health_settings, failed):
    ready_probes(monkeypatch)
    if failed in {"database", "schema"}:
        monkeypatch.setattr(runtime_health, "_database_checks", lambda s: {
            "database": failed != "database", "schema": failed != "schema",
        })
    elif failed == "redis":
        monkeypatch.setattr(runtime_health, "_redis_ready", lambda s: False)
    else:
        monkeypatch.setattr(runtime_health, "_object_store_checks", lambda s: {
            "minio": failed != "minio", "minio_buckets": failed != "minio_buckets",
        })
    report = runtime_health.check_api_readiness(health_settings)
    assert report["status"] == "not_ready"
    assert report["checks"][failed] is False


def test_probe_failures_do_not_leak_secrets(monkeypatch, health_settings, caplog):
    def fail(settings):
        raise RuntimeError("sensitive-password-in-driver-exception")
    for name in ("_database_checks", "_redis_ready", "_object_store_checks"):
        monkeypatch.setattr(runtime_health, name, fail)
    report = runtime_health.check_api_readiness(health_settings)
    assert not any(report["checks"].values())
    assert "sensitive-password" not in str(report) + caplog.text


def test_absent_sqlite_is_not_created(health_settings, tmp_path):
    assert runtime_health._database_checks(health_settings) == {"database": False, "schema": False}
    assert not (tmp_path / "health.sqlite").exists()


def test_readiness_never_migrates_an_old_schema(health_settings):
    engine = create_engine(health_settings.database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('old-revision')"))
    try:
        assert runtime_health._database_checks(health_settings) == {"database": True, "schema": False}
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "old-revision"
    finally:
        engine.dispose()


def test_installed_head_is_read_only_ready(health_settings):
    config = runtime_health.Config(str(runtime_health._ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(runtime_health._ROOT / "migrations"))
    heads = runtime_health.ScriptDirectory.from_config(config).get_heads()
    engine = create_engine(health_settings.database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        for head in heads:
            connection.execute(text("INSERT INTO alembic_version VALUES (:head)"), {"head": head})
    engine.dispose()
    assert runtime_health._database_checks(health_settings) == {"database": True, "schema": True}


def test_object_store_probe_does_not_create_or_write_buckets(monkeypatch, health_settings):
    expected = {health_settings.minio_bucket_raw, health_settings.minio_bucket_reports,
                health_settings.minio_bucket_stls, health_settings.minio_bucket_magics,
                health_settings.minio_bucket_photos, health_settings.minio_bucket_docs}
    class ReadOnlyClient:
        def list_buckets(self):
            return [SimpleNamespace(name=name) for name in expected]
    monkeypatch.setattr(runtime_health, "Minio", lambda *a, **k: ReadOnlyClient())
    assert runtime_health._object_store_checks(health_settings) == {"minio": True, "minio_buckets": True}
    expected.remove(health_settings.minio_bucket_reports)
    assert runtime_health._object_store_checks(health_settings) == {"minio": True, "minio_buckets": False}


def test_api_liveness_and_readiness_have_distinct_contracts(monkeypatch):
    from api.main import app
    monkeypatch.setattr("api.main.check_api_readiness", lambda s: {
        "status": "not_ready", "scope": "api_dependencies", "checks": {"database": False},
    })
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.get("/health").json()["build"] == build_manifest()
    assert client.get("/health/ready").status_code == 503


def test_manifest_matches_analytical_provenance_and_marks_dirty_source(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "a" * 40)
    monkeypatch.setenv("SOURCE_STATE", "dirty")
    monkeypatch.setenv("BUILD_DATE", "2026-09-23T12:00:00Z")
    manifest = build_manifest()
    assert manifest["git_sha"] == "a" * 40
    assert manifest["source_state"] == "dirty"
    assert manifest == build_provenance("test")["build"]
    assert manifest["build_id"] == build_manifest()["build_id"]
    monkeypatch.setenv("SOURCE_STATE", "clean")
    assert manifest["build_id"] != build_manifest()["build_id"]


def test_unknown_build_is_not_presented_as_reproducible(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT", "unknown")
    monkeypatch.delenv("SOURCE_STATE", raising=False)
    monkeypatch.setenv("BUILD_DATE", "unknown")
    result = build_manifest()
    assert result["git_sha"] is None
    assert result["source_state"] == "unknown"
    assert result["built_at"] is None
