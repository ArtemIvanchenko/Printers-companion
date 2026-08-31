from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from core.config.settings import Settings

logger = logging.getLogger(__name__)

_PLACEHOLDER_MARKERS = (
    "change-me",
    "change_me",
    "changeme",
    "replace-me",
    "replace_me",
    "minioadmin",
)


def _looks_like_placeholder(value: object) -> bool:
    normalized = str(value or "").strip().lower()
    return not normalized or any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


@dataclass
class PreflightReport:
    passed: bool = True
    checks: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_preflight(settings: Settings, component: str = "api") -> PreflightReport:
    """Validate configuration before a component starts.

    ``component`` names the caller (api / watcher / …) in log lines; every check
    currently applies to all of them.
    """
    report = PreflightReport()

    check_environ(report, settings)
    check_database_url(report, settings)
    check_remote_backends(report, settings)
    check_llm(report, settings)

    report.passed = len(report.errors) == 0
    return report


def check_environ(report: PreflightReport, settings: Settings) -> None:
    defaults = {
        "AGENT_API_TOKEN": "change-me-agent-token",
        "API_SERVICE_TOKEN": "change-me-service-token",
        "MINIO_ROOT_PASSWORD": "change-me-minio",
    }
    # Only "local" and "test" are treated as safe dev environments; anything else
    # (production, prod, staging, …) must not boot with default credentials.
    is_production = settings.app_env not in ("local", "test")
    for name, default in defaults.items():
        actual = getattr(settings, name.lower(), None)
        if actual == default:
            msg = (
                f"{name} is still set to the default '{default}'. "
                "Set a unique value in .env for production."
            )
            if is_production:
                report.errors.append(msg)
            else:
                report.warnings.append(msg)

    # Example files deliberately contain conspicuous placeholders. Check them
    # case-insensitively as production templates often use uppercase CHANGE_ME,
    # which exact comparisons against the built-in lowercase defaults miss.
    sensitive_values = {
        "DATABASE_URL": settings.database_url,
        "MINIO_ROOT_USER": settings.minio_root_user,
        "MINIO_ROOT_PASSWORD": settings.minio_root_password,
        "AGENT_API_TOKEN": settings.agent_api_token,
        "API_SERVICE_TOKEN": settings.api_service_token,
    }
    for name, value in sensitive_values.items():
        if not _looks_like_placeholder(value):
            continue
        msg = f"{name} is empty or still contains a CHANGE_ME/default placeholder."
        if is_production:
            if msg not in report.errors:
                report.errors.append(msg)
        elif msg not in report.warnings:
            report.warnings.append(msg)

    # Job ownership is persisted in the shared database.  Reusing the shipped
    # placeholder on several PCs would make them one logical worker and allow
    # either PC to execute the other's calculations.
    default_node_id = "local-operator"
    remote_data = (
        (_is_remote(_host_of(settings.database_url)) and not settings.database_url.startswith("sqlite"))
        or _is_remote(_host_of(settings.minio_endpoint))
    )
    if settings.compute_node_id == default_node_id:
        msg = (
            "COMPUTE_NODE_ID is still 'local-operator'. Set a stable, unique value "
            "for this operator PC (for example operator-01); it is the durable "
            "owner of locally executed calculations."
        )
        if is_production or remote_data:
            report.errors.append(msg)
        else:
            report.warnings.append(msg)


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "minio", "redis", ""}

_DEFAULT_DB_PASSWORDS = ("change-me",)
_DEFAULT_MINIO = {"minioadmin": "minio_root_user", "change-me-minio": "minio_root_password"}


def _host_of(url: str) -> str:
    """Host part of a URL or ``host:port`` pair, lowercased."""
    from urllib.parse import urlparse

    parsed = urlparse(url if "//" in url else f"//{url}")
    return (parsed.hostname or "").lower()


def _is_remote(host: str) -> bool:
    """True when the host is something other than this machine or a compose peer.

    Compose service names resolve inside the private bridge network, so they are
    no more exposed than localhost.
    """
    return host not in _LOCAL_HOSTS


def check_remote_backends(report: PreflightReport, settings: Settings) -> None:
    """Refuse default credentials once PostgreSQL or MinIO is off this machine.

    check_environ only escalates to an error when APP_ENV says "production", but
    the shipped default is "local".  An operator stack connected to the NAS sets
    production, but this host-based check remains deliberately independent of that label.
    When DATABASE_URL or MINIO_ENDPOINT becomes remote, placeholder passwords stop being a local
    convenience and start guarding a service reachable from the whole tailnet,
    so the check keys on where the backend actually lives, not on a label.
    """
    db_host = _host_of(settings.database_url)
    if _is_remote(db_host) and not settings.database_url.startswith("sqlite"):
        if any(pw in settings.database_url.lower() for pw in _DEFAULT_DB_PASSWORDS) or _looks_like_placeholder(settings.database_url):
            report.errors.append(
                f"DATABASE_URL points at a remote host ({db_host}) but still carries the "
                "placeholder password 'change-me'. Set a real password on both the server "
                "and in .env before exposing the database beyond this machine."
            )

    minio_host = _host_of(settings.minio_endpoint)
    if _is_remote(minio_host):
        for default, field in _DEFAULT_MINIO.items():
            if (
                getattr(settings, field, None) == default
                or _looks_like_placeholder(getattr(settings, field, None))
            ):
                report.errors.append(
                    f"MINIO_ENDPOINT points at a remote host ({minio_host}) but "
                    f"{field.upper()} is still the default '{default}'. Set real "
                    "credentials before exposing object storage beyond this machine."
                )
        if not settings.minio_secure:
            report.warnings.append(
                f"MinIO at {minio_host} is reached over plain HTTP (MINIO_SECURE=false). "
                "Acceptable only while the link is itself encrypted (e.g. a Tailscale "
                "tunnel); otherwise credentials and files travel in the clear."
            )


def check_database_url(report: PreflightReport, settings: Settings) -> None:
    """Catch the psycopg2→psycopg3 migration trap.

    SQLAlchemy picks the driver from the URL scheme:
      postgresql://   → tries psycopg2 (not installed → ModuleNotFoundError at import)
      postgresql+psycopg:// → psycopg v3 (the installed driver)

    This produces a clear error instead of a cryptic traceback buried in uvicorn startup.
    """
    url = settings.database_url
    if url.startswith("postgresql://") or url.startswith("postgres://"):
        report.errors.append(
            f"DATABASE_URL uses the bare 'postgresql://' scheme which requires psycopg2 "
            f"(not installed). Change it to 'postgresql+psycopg://' in your .env file.\n"
            f"  Current:  {url[:60]}{'...' if len(url) > 60 else ''}\n"
            f"  Fix:      {url.replace('postgresql://', 'postgresql+psycopg://', 1).replace('postgres://', 'postgresql+psycopg://', 1)[:60]}"
        )


def check_llm(report: PreflightReport, settings: Settings) -> None:
    if settings.llm_provider in ("null", "none", ""):
        return
    try:
        url = settings.llm_base_url.rstrip("/") + "/models"
        resp = httpx.get(url, headers={"User-Agent": "printer-log-analytics/1.0"}, timeout=5)
        resp.raise_for_status()
        report.checks["llm_reachable"] = True
    except Exception as exc:
        report.checks["llm_reachable"] = False
        report.warnings.append(
            f"LLM endpoint {settings.llm_base_url} not reachable: {exc}"
        )


def exit_on_failure(report: PreflightReport) -> None:
    if report.errors:
        for err in report.errors:
            logger.error("PREFLIGHT FAIL: %s", err)
        sys.exit(1)
