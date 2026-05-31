import asyncio
import logging
from pathlib import Path

import httpx

from core.config.settings import Settings
from reporting.llm.providers.base import BaseLLMProvider, LLMResult

logger = logging.getLogger(__name__)


class OllamaProvider(BaseLLMProvider):
    provider_name = "ollama"

    def __init__(self, settings: Settings) -> None:
        self.base_url = "http://host.docker.internal:11434"
        self.model = settings.llm_model
        self.timeout = settings.llm_timeout_sec
        self.temperature = settings.llm_temperature
        self.top_p = settings.llm_top_p
        self.max_tokens = settings.llm_max_tokens
        prompt_path = Path(__file__).resolve().parent / "../prompts/session_report.md"
        self.system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() else ""

    def status(self) -> dict:
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            if resp.status_code == 200:
                models = [m["name"] for m in resp.json().get("models", [])]
                return {"provider": "ollama", "available": True, "model": self.model, "models": models}
        except Exception:
            pass
        return {"provider": "ollama", "available": False, "model": self.model}

    async def generate_markdown(self, evidence_json: dict) -> LLMResult:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"Generate session report from evidence:\n```json\n{evidence_json}\n```"},
            ],
            "options": {
                "temperature": self.temperature,
                "top_p": self.top_p,
                "num_predict": self.max_tokens,
            },
            "stream": False,
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
                    client.post(f"{self.base_url}/api/chat", json=body),
                    timeout=actual_timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["message"]["content"]
                return LLMResult(
                    success=True,
                    content=content,
                    provider="ollama",
                    model_name=self.model,
                    token_estimates={
                        "prompt": data.get("prompt_eval_count", 0),
                        "completion": data.get("eval_count", 0),
                    },
                    response_metadata={"done_reason": data.get("done_reason", "")},
                )
        except asyncio.TimeoutError:
            error_msg = f"Ollama request timed out after {self.timeout}s"
            logger.error(error_msg)
            return LLMResult(
                success=False,
                provider="ollama",
                model_name=self.model,
                error=error_msg,
            )
        except Exception as exc:
            logger.error(f"Ollama generation failed: {exc}")
            return LLMResult(
                success=False,
                provider="ollama",
                model_name=self.model,
                error=str(exc),
            )
