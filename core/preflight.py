import logging
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


@dataclass
class PreflightReport:
    passed: bool = True
    checks: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def run_preflight(settings: "Settings", component: str = "api") -> PreflightReport:
    report = PreflightReport()

    check_environ(report, settings)
    check_secrets(report, settings)
    check_llm(report, settings)

    report.passed = len(report.errors) == 0
    return report


def check_environ(report: PreflightReport, settings: "Settings") -> None:
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


def check_secrets(report: PreflightReport, settings: "Settings") -> None:
    repo_root = Path(__file__).resolve().parents[1]
    secrets_path = repo_root / ".env.secrets"
    if not secrets_path.exists():
        report.warnings.append(
            ".env.secrets not found. Create it with TELEGRAM_BOT_TOKEN=<your-token>"
        )

    if settings.telegram_bot_token and not settings.telegram_bot_token_hash:
        report.warnings.append(
            "TELEGRAM_BOT_TOKEN is set but TELEGRAM_BOT_TOKEN_HASH is empty. "
            "Compute SHA256 of your token and add it to .env"
        )


def check_llm(report: PreflightReport, settings: "Settings") -> None:
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
