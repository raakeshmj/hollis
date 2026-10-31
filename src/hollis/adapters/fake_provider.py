"""A deterministic, scripted ModelProvider for tests and offline development.

It models a provider that deduplicates by idempotency key. The first request for an operation id
records that request and consumes the next scripted outcome. A repeated request with the same
operation id *and an identical request* returns the same outcome without consuming another.
Reusing an operation id for a different request raises `OperationIdReused`: an outcome is never
returned for a request it was not produced for.

Metrics:
- `invocations`: physical calls to `generate`, including deduplicated retries and rejected reuse.
- `generations`: operations that produced a response (successful logical generations).
- `rejections`: operations definitively rejected with `ModelProviderError`, producing no response.
"""

from collections import Counter
from collections.abc import Iterable

from hollis.runtime.model import ModelProviderError, ModelRequest, ModelResponse


class ScriptExhausted(AssertionError):
    """More logical operations were requested than were scripted."""


class OperationIdReused(ValueError):
    """An operation id was reused with a request that differs from the one first seen.

    Deliberately not a ModelProviderError: this is a caller bug, not a provider rejection, so the
    runtime must not record it as an ordinary run failure.
    """

    def __init__(self, operation_id: str, original: ModelRequest, request: ModelRequest) -> None:
        self.operation_id = operation_id
        self.original = original
        self.request = request
        super().__init__(f"operation id {operation_id!r} reused for a different request")


class ScriptedModelProvider:
    def __init__(self, script: Iterable[ModelResponse | ModelProviderError]) -> None:
        self._script = list(script)
        self._requests: dict[str, ModelRequest] = {}
        self._outcomes: dict[str, ModelResponse | ModelProviderError] = {}
        self.invocations_by_operation: Counter[str] = Counter()

    @property
    def invocations(self) -> int:
        return self.invocations_by_operation.total()

    @property
    def generations(self) -> int:
        return sum(isinstance(o, ModelResponse) for o in self._outcomes.values())

    @property
    def rejections(self) -> int:
        return sum(isinstance(o, ModelProviderError) for o in self._outcomes.values())

    async def generate(self, request: ModelRequest) -> ModelResponse:
        operation_id = request.operation_id
        self.invocations_by_operation[operation_id] += 1
        original = self._requests.get(operation_id)
        if original is None:
            if not self._script:
                raise ScriptExhausted(f"no scripted outcome for {operation_id!r}")
            self._requests[operation_id] = request  # ModelRequest is frozen
            self._outcomes[operation_id] = self._script.pop(0)
        elif request != original:
            raise OperationIdReused(operation_id, original, request)
        outcome = self._outcomes[operation_id]
        if isinstance(outcome, ModelProviderError):
            raise outcome
        return outcome
