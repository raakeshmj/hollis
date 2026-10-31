"""A deterministic, scripted ModelProvider for tests and offline development.

It models a provider that deduplicates by idempotency key. The first request for an operation id
records that request and consumes the next scripted outcome. A repeated request with the same
operation id *and an identical request* returns the same outcome without consuming another.
Reusing an operation id for a different request raises `OperationIdReused`: an outcome is never
returned for a request it was not produced for.

Scripted `ModelCallError`s are replayed as-is for repeated identical requests, so a scripted
transient failure keeps failing for that operation. To script "fails, then succeeds", give each
attempt its own entry via `per_attempt=True`.

Metrics:
- `invocations`: physical calls to `generate`, including deduplicated retries and rejected reuse.
- `generations`: operations that produced a response (successful logical generations).
- `rejections`: scripted `ModelCallError` outcomes consumed (repeated deliveries not counted).
"""

from collections import Counter
from collections.abc import Iterable

from hollis.runtime.model import (
    ModelCallError,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
)

FAKE_CAPABILITIES = ProviderCapabilities(name="scripted-fake", deduplicates_requests=True)


class ScriptExhausted(AssertionError):
    """More logical operations were requested than were scripted."""


class OperationIdReused(ValueError):
    """An operation id was reused with a request that differs from the one first seen.

    Deliberately not a ModelCallError: this is a caller bug, not a provider failure, so the
    runtime must not record it as an ordinary attempt outcome.
    """

    def __init__(self, operation_id: str, original: ModelRequest, request: ModelRequest) -> None:
        self.operation_id = operation_id
        self.original = original
        self.request = request
        super().__init__(f"operation id {operation_id!r} reused for a different request")


def _same_call(a: ModelRequest, b: ModelRequest) -> bool:
    # The per-attempt timeout legitimately differs between attempts of one logical call.
    return a.model_copy(update={"timeout_s": 1.0}) == b.model_copy(update={"timeout_s": 1.0})


class ScriptedModelProvider:
    def __init__(
        self,
        script: Iterable[ModelResponse | ModelCallError],
        *,
        per_attempt: bool = False,
        capabilities: ProviderCapabilities = FAKE_CAPABILITIES,
    ) -> None:
        """`per_attempt=False` (default): one scripted outcome per operation id, deduplicated.
        `per_attempt=True`: failures consume a new entry on every call until a response is
        produced, after which that response is deduplicated."""
        self._script = list(script)
        self._per_attempt = per_attempt
        self._capabilities = capabilities
        self._requests: dict[str, ModelRequest] = {}
        self._outcomes: dict[str, ModelResponse | ModelCallError] = {}
        self._rejections = 0
        self.invocations_by_operation: Counter[str] = Counter()
        self.requests: list[ModelRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    @property
    def invocations(self) -> int:
        return self.invocations_by_operation.total()

    @property
    def generations(self) -> int:
        return sum(isinstance(o, ModelResponse) for o in self._outcomes.values())

    @property
    def rejections(self) -> int:
        return self._rejections

    async def generate(self, request: ModelRequest) -> ModelResponse:
        operation_id = request.operation_id
        self.invocations_by_operation[operation_id] += 1
        self.requests.append(request)
        original = self._requests.get(operation_id)
        if original is not None and not _same_call(request, original):
            raise OperationIdReused(operation_id, original, request)
        cached = self._outcomes.get(operation_id)
        if cached is None or (self._per_attempt and isinstance(cached, ModelCallError)):
            if not self._script:
                raise ScriptExhausted(f"no scripted outcome for {operation_id!r}")
            self._requests.setdefault(operation_id, request)
            cached = self._outcomes[operation_id] = self._script.pop(0)
            if isinstance(cached, ModelCallError):
                self._rejections += 1
        if isinstance(cached, ModelCallError):
            raise cached
        return cached
