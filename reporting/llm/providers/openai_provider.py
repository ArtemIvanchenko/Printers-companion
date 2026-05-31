import asyncio
import logging
from pathlib import Path

import httpx

from core.config.settings import Settings
from reporting.llm.providers.base import BaseLLMProvider, LLMResult

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseLLMProvider):
    provider_name = "openai"

    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.llm_base_url.rstrip("/")
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key
        self.timeout = settings.llm_timeout_sec
        self.temperature = settings.llm_temperature
        self.top_p = settings.llm_top_p
        self.max_tokens = settings.llm_max_tokens
        prompt_path = Path(__file__).resolve().parent / "../prompts/session_report.md"
        self.system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""

    def status(self) -> dict:
        try:
            resp = httpx.get(
                f"{self.base_url}/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=5,
            )
            if resp.status_code == 200:
                models = [m["id"] for m in resp.json().get("data", [])]
                return {"provider": "openai", "available": True, "model": self.model, "models": models}
        except Exception as exc:
            pass
        return {"provider": "openai", "available": False, "model": self.model}

    async def generate_markdown(self, evidence_json: dict) -> LLMResult:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"Generate session report from evidence:\n```json\n{evidence_json}\n```"},
            ],
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
        try:
            # Explicit timeout with safety margin: actual timeout + 10s buffer
            actual_timeout = self.timeout + 10
            timeout_obj = httpx.Timeout(
                timeout=actual_timeout,
                connect=10.0,
                pool=30.0,
                read=self.timeout,
            )
            
            async with httpx.AsyncClient(timeout=timeout_obj) as client:
                resp = await asyncio.wait_for(
                    client.post(f"{self.base_url}/chat/completions", json=body, headers=headers),
                    timeout=actual_timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return LLMResult(
                    success=True,
                    content=content,
                    provider="openai",
                    model_name=self.model,
                    token_estimates={
                        "prompt": data.get("usage", {}).get("prompt_tokens", 0),
                        "completion": data.get("usage", {}).get("completion_tokens", 0),
                    },
                    response_metadata={"finish_reason": data["choices"][0].get("finish_reason", "")},
                )
        except asyncio.TimeoutError:
            error_msg = f"OpenAI request timed out after {self.timeout}s"
            logger.error(error_msg)
            return LLMResult(
                success=False,
                provider="openai",
                model_name=self.model,
                error=error_msg,
            )
        except Exception as exc:
            logger.error(f"OpenAI generation failed: {exc}")
            return LLMResult(
                success=False,
                provider="openai",
                model_name=self.model,
                error=str(exc),
            )
