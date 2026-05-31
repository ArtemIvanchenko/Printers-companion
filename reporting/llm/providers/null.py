from typing import Any

from reporting.llm.providers.base import BaseLLMProvider, LLMResult


class NullLLMProvider(BaseLLMProvider):
    provider_name = "null"

    def status(self) -> dict[str, Any]:
        return {"provider": self.provider_name, "available": True, "mode": "disabled"}

    async def generate_markdown(self, evidence_json: dict[str, Any]) -> LLMResult:
        return LLMResult(
            success=False,
            provider=self.provider_name,
            error="LLM disabled; deterministic report should be used.",
        )

