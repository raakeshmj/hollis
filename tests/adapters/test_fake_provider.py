import pytest

from hollis.adapters.fake_provider import (
    FAKE_CAPABILITIES,
    OperationIdReused,
    ScriptedModelProvider,
    ScriptExhausted,
)
from hollis.runtime.model import (
    FailureKind,
    ModelCallError,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)


def _request(operation_id: str, *, text: str = "hello", timeout_s: float = 30.0) -> ModelRequest:
    return ModelRequest(
        operation_id=operation_id,
        model="fake",
        input=text,
        max_output_tokens=64,
        timeout_s=timeout_s,
    )


def _metrics(provider: ScriptedModelProvider) -> tuple[int, int, int]:
    return provider.invocations, provider.generations, provider.rejections


def _error(kind: FailureKind = FailureKind.INVALID_REQUEST) -> ModelCallError:
    return ModelCallError(kind, "rejected")


async def test_returns_scripted_responses_in_order_per_new_operation() -> None:
    provider = ScriptedModelProvider([ModelResponse(content="a"), ModelResponse(content="b")])
    assert (await provider.generate(_request("op1"))).content == "a"
    assert (await provider.generate(_request("op2"))).content == "b"
    assert _metrics(provider) == (2, 2, 0)


async def test_identical_retry_is_deduplicated() -> None:
    provider = ScriptedModelProvider([ModelResponse(content="a"), ModelResponse(content="b")])
    first = await provider.generate(_request("op"))
    again = await provider.generate(_request("op"))
    assert first == again == ModelResponse(content="a")
    assert _metrics(provider) == (2, 1, 0)
    assert provider.invocations_by_operation["op"] == 2


async def test_retry_with_a_shorter_attempt_timeout_is_the_same_call() -> None:
    provider = ScriptedModelProvider([ModelResponse(content="a")])
    await provider.generate(_request("op", timeout_s=30))
    assert await provider.generate(_request("op", timeout_s=5)) == ModelResponse(content="a")


@pytest.mark.parametrize(
    "changed",
    [
        _request("op", text="something else"),
        _request("op").model_copy(update={"model": "other-model"}),
        _request("op").model_copy(update={"instructions": "new"}),
        _request("op").model_copy(update={"max_output_tokens": 1}),
    ],
    ids=["input", "model", "instructions", "max_output_tokens"],
)
async def test_reusing_operation_id_for_a_different_request_is_rejected(
    changed: ModelRequest,
) -> None:
    provider = ScriptedModelProvider([ModelResponse(content="a"), ModelResponse(content="b")])
    original = _request("op")
    await provider.generate(original)

    with pytest.raises(OperationIdReused) as exc_info:
        await provider.generate(changed)

    assert exc_info.value.original == original
    assert exc_info.value.request == changed
    assert _metrics(provider) == (2, 1, 0)  # counted as an invocation, nothing generated
    assert await provider.generate(original) == ModelResponse(content="a")
    assert (await provider.generate(_request("op2"))).content == "b"


def test_operation_id_reuse_is_not_a_model_call_error() -> None:
    assert not issubclass(OperationIdReused, ModelCallError)


async def test_scripted_error_is_deduplicated_and_counted_once() -> None:
    error = _error()
    provider = ScriptedModelProvider([error, ModelResponse(content="ok")])
    for _ in range(2):
        with pytest.raises(ModelCallError) as exc_info:
            await provider.generate(_request("op"))
        assert exc_info.value is error
    assert _metrics(provider) == (2, 0, 1)

    await provider.generate(_request("op2"))
    assert _metrics(provider) == (3, 1, 1)


async def test_per_attempt_mode_consumes_an_entry_per_failed_attempt() -> None:
    provider = ScriptedModelProvider(
        [
            _error(FailureKind.OVERLOADED),
            _error(FailureKind.OVERLOADED),
            ModelResponse(content="ok"),
        ],
        per_attempt=True,
    )
    for _ in range(2):
        with pytest.raises(ModelCallError):
            await provider.generate(_request("op"))
    assert await provider.generate(_request("op")) == ModelResponse(content="ok")
    assert await provider.generate(_request("op")) == ModelResponse(content="ok")  # deduplicated
    assert _metrics(provider) == (4, 1, 2)


async def test_mismatched_retry_of_a_rejected_operation_is_rejected() -> None:
    provider = ScriptedModelProvider([_error()])
    with pytest.raises(ModelCallError):
        await provider.generate(_request("op"))
    with pytest.raises(OperationIdReused):
        await provider.generate(_request("op", text="changed"))
    assert _metrics(provider) == (2, 0, 1)


async def test_exhausted_script_fails_loudly() -> None:
    provider = ScriptedModelProvider([])
    with pytest.raises(ScriptExhausted):
        await provider.generate(_request("op"))
    assert _metrics(provider) == (1, 0, 0)


def test_declares_that_it_deduplicates() -> None:
    provider: ModelProvider = ScriptedModelProvider([])  # checked by pyright
    assert provider.capabilities == FAKE_CAPABILITIES
    assert provider.capabilities.deduplicates_requests
