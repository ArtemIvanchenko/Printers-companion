import asyncio
import logging
from pathlib import Path
from typing import Any

import httpx

from core.config.settings import Settings, get_settings
from core.versioning.constants import LLM_PROMPT_VERSION
from reporting.llm.providers.base import BaseLLMProvider, LLMResult

logger = logging.getLogger(__name__)


class LMStudioProvider(BaseLLMProvider):
    provider_name = "lmstudio"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "session_report.md"
        self.system_prompt = prompt_path.read_text(encoding="utf-8")

    def status(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "base_url": self.settings.llm_base_url,
            "model": self.settings.llm_model,
            "configured": True,
        }

    async def health(self) -> dict[str, Any]:
        """Live reachability check against the currently configured base URL."""
        from reporting.llm.discovery import probe_lmstudio

        models = await probe_lmstudio(self.settings.llm_base_url, timeout=5.0)
        return {
            **self.status(),
            "reachable": models is not None,
            "models_loaded": models or [],
            "model_available": bool(models) and self.settings.llm_model in models,
        }

    async def generate_markdown(self, evidence_json: dict[str, Any]) -> LLMResult:
        url = f"{self.settings.llm_base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.settings.llm_model,
            "temperature": self.settings.llm_temperature,
            "top_p": self.settings.llm_top_p,
            "max_tokens": self.settings.llm_max_tokens,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": (
                        "Generate the requested Markdown narrative from this structured evidence JSON. "
                        "Preserve uncertainty and known unknowns.\n\n"
                        f"{evidence_json}"
                    ),
                },
            ],
        }
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        try:
            # Explicit timeout with safety margin: actual timeout + 10s buffer
            actual_timeout = self.settings.llm_timeout_sec + 10
            timeout = httpx.Timeout(
                timeout=actual_timeout,
                connect=10.0,  # 10s to establish connection
                pool=30.0,  # 30s for pool acquisition
                read=self.settings.llm_timeout_sec,  # LLM processing time
            )
            
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await asyncio.wait_for(
                    client.post(url, json=payload, headers=headers),
                    timeout=actual_timeout,
                )
                response.raise_for_status()
                data = response.json()
            content = data["choices"][0]["message"]["content"]
            return LLMResult(
                success=True,
                content=content,
                provider=self.provider_name,
                model_name=self.settings.llm_model,
                prompt_version=LLM_PROMPT_VERSION,
                token_estimates=data.get("usage", {}),
                response_metadata={"id": data.get("id"), "created": data.get("created")},
            )
        except asyncio.TimeoutError:
            error_msg = f"LLM request timed out after {self.settings.llm_timeout_sec}s"
            logger.error(error_msg, extra={"provider": self.provider_name})
            return LLMResult(
                success=False,
                provider=self.provider_name,
                model_name=self.settings.llm_model,
                prompt_version=LLM_PROMPT_VERSION,
                error=error_msg,
            )
        except Exception as exc:
            logger.error(f"LLM generation failed: {exc}", extra={"provider": self.provider_name})
            return LLMResult(
                success=False,
                provider=self.provider_name,
                model_name=self.settings.llm_model,
                prompt_version=LLM_PROMPT_VERSION,
                error=str(exc),
            )

