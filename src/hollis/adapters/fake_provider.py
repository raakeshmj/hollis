"""A deterministic, scripted ModelProvider for tests and offline development.

It models a provider that deduplicates by idempotency key: the first request for an
operation id consumes the next scripted outcome (a logical generation); repeated requests with
the same operation id return that same outcome without generating again.
"""

from collections import Counter
from collections.abc import Iterable

from hollis.runtime.model import ModelProviderError, ModelRequest, ModelResponse


class ScriptExhausted(AssertionError):
    """More logical generations were requested than were scripted."""


class ScriptedModelProvider:
    def __init__(self, script: Iterable[ModelResponse | ModelProviderError]) -> None:
        self._script = list(script)
        self._outcomes: dict[str, ModelResponse | ModelProviderError] = {}
        self.invocations_by_operation: Counter[str] = Counter()

    @property
    def invocations(self) -> int:
        """Physical calls to `generate`, including deduplicated retries."""
        return self.invocations_by_operation.total()

    @property
    def generations(self) -> int:
        """Logical generations: distinct operation ids that consumed a scripted outcome."""
        return len(self._outcomes)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.invocations_by_operation[request.operation_id] += 1
        if request.operation_id not in self._outcomes:
            if not self._script:
                raise ScriptExhausted(f"no scripted outcome for {request.operation_id!r}")
            self._outcomes[request.operation_id] = self._script.pop(0)
        outcome = self._outcomes[request.operation_id]
        if isinstance(outcome, ModelProviderError):
            raise outcome
        return outcome
