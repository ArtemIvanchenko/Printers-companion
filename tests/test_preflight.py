"""Preflight checks: credentials and database URL validation."""
import pytest

from core.config.settings import Settings
from core.preflight import check_environ, check_database_url, PreflightReport


def _report_for(app_env: str) -> PreflightReport:
    settings = Settings(
        app_env=app_env,
        agent_api_token="change-me-agent-token",
        api_service_token="change-me-service-token",
        minio_root_password="change-me-minio",
        compute_node_id="operator-test",
        llm_provider="null",  # skip network discovery
    )
    report = PreflightReport()
    check_environ(report, settings)
    return report


def test_default_tokens_are_errors_in_production() -> None:
    report = _report_for("production")
    assert report.errors, "default credentials must be errors in production"
    # All three default secrets should be flagged.
    joined = " ".join(report.errors)
    assert "AGENT_API_TOKEN" in joined
    assert "API_SERVICE_TOKEN" in joined
    assert "MINIO_ROOT_PASSWORD" in joined


def test_default_tokens_are_warnings_in_local() -> None:
    report = _report_for("local")
    assert not report.errors, "local must not be blocked by default credentials"
    assert report.warnings


def test_unique_tokens_pass_in_production() -> None:
    settings = Settings(
        app_env="production",
        agent_api_token="a-real-unique-agent-token",
        api_service_token="a-real-unique-service-token",
        minio_root_password="a-real-unique-minio-password",
        minio_root_user="operator-storage-user",
        compute_node_id="operator-test",
        llm_provider="null",
    )
    report = PreflightReport()
    check_environ(report, settings)
    assert not report.errors


def test_bare_postgresql_url_is_error() -> None:
    """postgresql:// requires psycopg2 which is not installed — must be caught early."""
    settings = Settings(
        app_env="local",
        database_url="postgresql://printer_logs:change-me@postgres:5432/printer_logs",
        llm_provider="null",
    )
    report = PreflightReport()
    check_database_url(report, settings)
    assert report.errors
    assert "postgresql+psycopg://" in report.errors[0]


def test_correct_psycopg3_url_passes() -> None:
    settings = Settings(
        app_env="local",
        database_url="postgresql+psycopg://printer_logs:change-me@postgres:5432/printer_logs",
        llm_provider="null",
    )
    report = PreflightReport()
    check_database_url(report, settings)
    assert not report.errors


def test_legacy_owner_id_is_reserved_for_migration_rows() -> None:
    with pytest.raises(ValueError, match="legacy-unassigned"):
        Settings(app_env="test", compute_node_id="legacy-unassigned")


class TestRemoteBackendCredentials:
    """Default passwords are a local convenience only while the backend IS local.

    The NAS migration (deploy/nas/README.md) changes DATABASE_URL and
    MINIO_ENDPOINT and nothing else — APP_ENV stays "local" — so a check keyed
    on APP_ENV never fires at the moment the credentials start guarding a
    network service.
    """

    def _settings(self, **overrides):
        from core.config.settings import Settings

        base = dict(
            app_env="local",
            agent_api_token="unique-agent",
            api_service_token="unique-service",
            compute_node_id="operator-test",
            llm_provider="null",
        )
        return Settings(**{**base, **overrides})

    def test_local_compose_defaults_are_fine(self):
        from core.preflight import run_preflight

        report = run_preflight(self._settings(
            database_url="postgresql+psycopg://printer_logs:change-me@postgres:5432/printer_logs",
            minio_endpoint="minio:9000",
            minio_root_password="change-me-minio",
        ))
        assert report.passed, report.errors

    def test_remote_database_with_placeholder_password_is_an_error(self):
        from core.preflight import run_preflight

        report = run_preflight(self._settings(
            database_url="postgresql+psycopg://printer_logs:change-me@100.64.1.5:5433/printer_logs",
        ))
        assert not report.passed
        assert any("100.64.1.5" in e for e in report.errors)

    def test_remote_database_with_a_real_password_passes(self):
        from core.preflight import run_preflight

        report = run_preflight(self._settings(
            database_url="postgresql+psycopg://printer_logs:s3cret-xyz@100.64.1.5:5433/printer_logs",
            minio_endpoint="100.64.1.5:9000",
            minio_root_user="nas-user",
            minio_root_password="nas-password",
        ))
        assert report.passed, report.errors

    def test_remote_minio_with_default_credentials_is_an_error(self):
        from core.preflight import run_preflight

        report = run_preflight(self._settings(minio_endpoint="100.64.1.5:9000"))
        assert not report.passed
        assert any("MINIO_ROOT_USER" in e or "MINIO_ROOT_PASSWORD" in e for e in report.errors)

    def test_remote_minio_over_plain_http_warns(self):
        from core.preflight import run_preflight

        report = run_preflight(self._settings(
            minio_endpoint="100.64.1.5:9000",
            minio_root_user="nas-user",
            minio_root_password="nas-password",
            minio_secure=False,
        ))
        assert report.passed, report.errors
        assert any("MINIO_SECURE=false" in w for w in report.warnings)

    def test_uppercase_example_placeholders_are_rejected(self):
        from core.preflight import run_preflight

        report = run_preflight(self._settings(
            app_env="production",
            database_url="postgresql+psycopg://printer_logs:CHANGE_ME@nas:5433/printer_logs",
            minio_endpoint="nas.example:9000",
            minio_root_user="CHANGE_ME",
            minio_root_password="CHANGE_ME",
        ))
        assert not report.passed
        assert any("CHANGE_ME" in error for error in report.errors)
