import hashlib
import hmac
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# Candidate base URLs probed when auto-discovering a local LM Studio (OpenAI-compatible)
# server. Ordered from most-likely (Docker host gateway) to localhost fallbacks.
LMSTUDIO_CANDIDATE_URLS = (
    "http://host.docker.internal:1234/v1",
    "http://172.17.0.1:1234/v1",
    "http://localhost:1234/v1",
    "http://127.0.0.1:1234/v1",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", ".env.secrets"), env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    log_level: str = "INFO"

    database_url: str = "sqlite:///./printer_logs.db"
    redis_url: str = "redis://localhost:6379/0"

    minio_endpoint: str = "localhost:9000"
    minio_root_user: str = "minioadmin"
    minio_root_password: str = "change-me-minio"
    minio_bucket_raw: str = "raw-logs"
    minio_bucket_reports: str = "reports"
    minio_secure: bool = False

    raw_logs_host_path: str = r"C:\PrinterLogs"
    raw_logs_container_path: str = "/mnt/raw_logs"
    incoming_path: str = "/mnt/raw_logs"
    watch_mode: Literal["filesystem_events", "polling"] = "filesystem_events"
    require_operator_import_confirmation: bool = True
    file_stability_seconds: int = 60
    file_stability_retry_seconds: int = 30
    file_stability_max_retries: int = 10  # Max 10 retries = ~5 minutes with 30s intervals
    import_confirmation_timeout_hours: int = 24

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    internal_api_url: str = "http://api:8000"
    worker_concurrency: int = 2

    llm_provider: Literal["lmstudio", "openai", "ollama", "null"] = "lmstudio"
    llm_base_url: str = "http://host.docker.internal:1234/v1"
    llm_model: str = "qwen3.6"
    llm_api_key: str = "lm-studio"
    llm_timeout_sec: int = 300
    llm_temperature: float = 0.1
    llm_top_p: float = 0.9
    llm_max_tokens: int = 8192
    llm_router_mode: Literal["priority", "round_robin", "single"] = "single"
    llm_providers_order: str = "lmstudio,openai,ollama"
    llm_fallback_on_failure: bool = True
    llm_circuit_breaker_threshold: int = 3
    llm_circuit_breaker_timeout: int = 30

    daily_review_cron: str = "0 20 * * *"
    historical_reanalysis_cron: str = "0 2 * * *"
    historical_reanalysis_window_days: int = 90
    historical_reanalysis_max_iterations: int = 10

    telegram_bot_token: str = Field(default="", repr=False)
    telegram_bot_token_hash: str = Field(default="", repr=False)
    telegram_default_chat_id: str = ""
    telegram_proxy_url: str = Field(default="", repr=False)
    voice_transcription_enabled: bool = True
    voice_transcription_provider: Literal["faster_whisper", "null"] = "faster_whisper"
    voice_transcription_model: str = "small"
    voice_transcription_language: str = "ru"
    voice_transcription_device: str = "cpu"
    voice_transcription_compute_type: str = "int8"
    voice_transcription_model_cache: str = "/models/stt"

    agent_api_token: str = Field(default="change-me-agent-token", repr=False)
    api_service_token: str = Field(default="change-me-service-token", repr=False)
    cors_origins: str = "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000"
    rate_limit_chat_rpm: int = 20
    rate_limit_agent_rpm: int = 30
    log_retention_days: int = 90

    @model_validator(mode="after")
    def _warn_default_tokens(self):
        if self.app_env == "test":
            return self
        defaults = {
            "AGENT_API_TOKEN": ("change-me-agent-token", self.agent_api_token),
            "API_SERVICE_TOKEN": ("change-me-service-token", self.api_service_token),
        }
        import warnings
        for name, (default, actual) in defaults.items():
            if actual == default:
                warnings.warn(f"{name} is still set to default '{default}'. Set a unique value in .env for production.")
        return self

    @model_validator(mode="after")
    def _validate_telegram_token(self):
        if self.telegram_bot_token and self.telegram_bot_token_hash:
            actual = hashlib.sha256(self.telegram_bot_token.encode()).hexdigest()
            # Use constant-time comparison to prevent timing attacks
            if not hmac.compare_digest(actual, self.telegram_bot_token_hash):
                raise ValueError(
                    "TELEGRAM_BOT_TOKEN does not match TELEGRAM_BOT_TOKEN_HASH. "
                    f"Expected hash: {self.telegram_bot_token_hash}, "
                    f"got: {actual}"
                )
        return self

    @model_validator(mode="after")
    def _discover_llm(self):
        # Skip blocking network discovery in tests: probing candidate URLs at
        # settings-construction time makes imports slow and non-deterministic.
        if self.llm_provider == "null" or self.app_env == "test":
            return self
        import json
        import urllib.request

        empty_server: str | None = None
        for url in LMSTUDIO_CANDIDATE_URLS:
            try:
                req = urllib.request.Request(
                    f"{url.rstrip('/')}/models",
                    method="GET",
                    headers={"User-Agent": "printer-log-analytics/1.0"},
                )
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status != 200:
                        continue
                    body = json.loads(resp.read().decode("utf-8"))
            except Exception:
                continue
            models = [m.get("id") for m in body.get("data", []) if m.get("id")]
            if not models:
                # Server is up but no model is loaded — remember it but keep looking
                # for a candidate that actually has a usable model.
                empty_server = empty_server or url
                continue
            self.llm_base_url = url
            if self.llm_model not in models:
                self.llm_model = models[0]
            return self
        if empty_server:
            self.llm_base_url = empty_server
        return self

    @property
    def repo_root(self) -> Path:
        return Path(__file__).resolve().parents[2]


@lru_cache
def get_settings() -> Settings:
    return Settings()
