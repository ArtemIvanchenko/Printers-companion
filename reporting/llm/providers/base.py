from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LLMResult:
    success: bool
    content: str = ""
    provider: str = "null"
    model_name: str = ""
    prompt_version: str = "session-report-0.1.0"
    token_estimates: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    response_metadata: dict[str, Any] = field(default_factory=dict)


class BaseLLMProvider(ABC):
    provider_name: str = "base"

    @abstractmethod
    def status(self) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    async def generate_markdown(self, evidence_json: dict[str, Any]) -> LLMResult:
        raise NotImplementedError

