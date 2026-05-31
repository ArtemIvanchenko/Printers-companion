import time
from enum import Enum


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._metrics = {"total": 0, "success": 0, "failure": 0, "rejected": 0}

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def call(self, fn, *args, **kwargs):
        self._metrics["total"] += 1
        if self.state == CircuitState.OPEN:
            self._metrics["rejected"] += 1
            raise CircuitBreakerError("Circuit breaker is OPEN")

        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure()
            raise

    async def async_call(self, fn, *args, **kwargs):
        self._metrics["total"] += 1
        if self.state == CircuitState.OPEN:
            self._metrics["rejected"] += 1
            raise CircuitBreakerError("Circuit breaker is OPEN")

        try:
            result = await fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as exc:
            self._on_failure()
            raise

    def _on_success(self) -> None:
        self._metrics["success"] += 1
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.CLOSED
            self._failure_count = 0

    def _on_failure(self) -> None:
        self._metrics["failure"] += 1
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN

    def reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0

    @property
    def metrics(self) -> dict:
        return {**self._metrics, "state": self._state.value}


class CircuitBreakerError(Exception):
    pass
