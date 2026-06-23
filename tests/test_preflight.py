"""Preflight checks: credentials and database URL validation."""
from core.config.settings import Settings
from core.preflight import check_environ, check_database_url, PreflightReport


def _report_for(app_env: str) -> PreflightReport:
    settings = Settings(
        app_env=app_env,
        agent_api_token="change-me-agent-token",
        api_service_token="change-me-service-token",
        minio_root_password="change-me-minio",
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
