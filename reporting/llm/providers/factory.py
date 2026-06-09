import logging
from typing import Optional

from core.config.settings import Settings, get_settings
from reporting.llm.circuit_breaker import CircuitBreaker, CircuitBreakerError
from reporting.llm.providers.base import BaseLLMProvider, LLMResult
from reporting.llm.providers.lmstudio import LMStudioProvider
from reporting.llm.providers.null import NullLLMProvider
from reporting.llm.providers.openai_provider import OpenAIProvider
from reporting.llm.providers.ollama_provider import OllamaProvider

logger = logging.getLogger(__name__)


class LLMRouter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.providers: dict[str, BaseLLMProvider] = {}
        self.circuit_breakers: dict[str, CircuitBreaker] = {}
        self.current_index = 0
        self._initialize_providers()

    def _initialize_providers(self) -> None:
        """Initialize providers based on router mode and provider order."""
        provider_map = {
            "lmstudio": LMStudioProvider(self.settings),
            "openai": OpenAIProvider(self.settings),
            "ollama": OllamaProvider(self.settings),
            "null": NullLLMProvider(),
        }

        if self.settings.llm_router_mode == "single":
            provider_name = self.settings.llm_provider
            self.providers[provider_name] = provider_map[provider_name]
            self.circuit_breakers[provider_name] = CircuitBreaker(
                failure_threshold=self.settings.llm_circuit_breaker_threshold,
                recovery_timeout=float(self.settings.llm_circuit_breaker_timeout),
            )
        else:
            for name in self.settings.llm_providers_order.split(","):
                name = name.strip()
                if name in provider_map:
                    self.providers[name] = provider_map[name]
                    self.circuit_breakers[name] = CircuitBreaker(
                        failure_threshold=self.settings.llm_circuit_breaker_threshold,
                        recovery_timeout=float(self.settings.llm_circuit_breaker_timeout),
                    )

    async def generate_markdown(self, evidence_json: dict) -> LLMResult:
        """Generate markdown with failover and circuit breaker support."""
        providers_to_try = self._get_providers_in_order()

        last_error: Optional[Exception] = None

        for provider_name in providers_to_try:
            provider = self.providers[provider_name]
            breaker = self.circuit_breakers[provider_name]

            try:
                # Providers don't raise on failure — they return
                # LLMResult(success=False). Translate that into an exception so
                # the circuit breaker actually counts the failure and the loop
                # fails over to the next provider (otherwise both failover and
                # the breaker are dead — the first provider's failed result is
                # returned immediately).
                async def _call(p=provider) -> LLMResult:
                    r = await p.generate_markdown(evidence_json)
                    if not getattr(r, "success", False):
                        raise RuntimeError(r.error or "provider returned unsuccessful result")
                    return r

                result = await breaker.async_call(_call)
                logger.info(f"LLM generation succeeded with {provider_name}")
                return result
            except CircuitBreakerError:
                logger.warning(f"Circuit breaker OPEN for {provider_name}, skipping")
                last_error = CircuitBreakerError(f"{provider_name} circuit breaker is open")
                continue
            except Exception as exc:
                logger.warning(f"LLM generation failed with {provider_name}: {exc}")
                last_error = exc
                if not self.settings.llm_fallback_on_failure:
                    raise

        result = LLMResult(
            success=False,
            error=f"All LLM providers failed. Last error: {last_error}",
            provider="fallback",
        )
        return result

    def _get_providers_in_order(self) -> list[str]:
        """Get provider order based on router mode."""
        if self.settings.llm_router_mode == "single":
            return list(self.providers.keys())
        elif self.settings.llm_router_mode == "round_robin":
            keys = list(self.providers.keys())
            self.current_index = (self.current_index + 1) % len(keys)
            return [keys[self.current_index]] + [k for k in keys if k != keys[self.current_index]]
        else:  # priority
            # Strip whitespace and keep only registered providers, so a config
            # like "lmstudio, openai" (space after comma) can't KeyError.
            return [
                n.strip()
                for n in self.settings.llm_providers_order.split(",")
                if n.strip() in self.providers
            ]

    def get_status(self) -> dict:
        """Get status of all providers and their circuit breakers."""
        status = {}
        for name, provider in self.providers.items():
            breaker = self.circuit_breakers[name]
            status[name] = {**provider.status(), "circuit_breaker": breaker.metrics}
        return status


_router_instance: Optional[LLMRouter] = None


def get_llm_router() -> LLMRouter:
    global _router_instance
    if _router_instance is None:
        _router_instance = LLMRouter(get_settings())
    return _router_instance


def get_llm_provider() -> BaseLLMProvider:
    """For backward compatibility - returns the primary provider."""
    settings = get_settings()
    if settings.llm_provider == "openai":
        return OpenAIProvider(settings)
    elif settings.llm_provider == "ollama":
        return OllamaProvider(settings)
    elif settings.llm_provider == "lmstudio":
        return LMStudioProvider(settings)
    return NullLLMProvider()
