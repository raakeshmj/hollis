import pytest

from hollis.adapters.fake_provider import OperationIdReused, ScriptedModelProvider, ScriptExhausted
from hollis.runtime.model import ModelProvider, ModelProviderError, ModelRequest, ModelResponse


def _request(operation_id: str, *, text: str = "hello") -> ModelRequest:
    return ModelRequest(operation_id=operation_id, model="fake", input=text)


def _metrics(provider: ScriptedModelProvider) -> tuple[int, int, int]:
    return provider.invocations, provider.generations, provider.rejections


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


@pytest.mark.parametrize(
    "changed",
    [
        ModelRequest(operation_id="op", model="fake", input="something else"),
        ModelRequest(operation_id="op", model="other-model", input="hello"),
        ModelRequest(operation_id="op", model="fake", input="hello", instructions="new"),
    ],
    ids=["input", "model", "instructions"],
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
    # The original request still deduplicates, and the unused script entry is untouched.
    assert await provider.generate(original) == ModelResponse(content="a")
    assert (await provider.generate(_request("op2"))).content == "b"


async def test_operation_id_reuse_is_not_a_provider_rejection() -> None:
    assert not issubclass(OperationIdReused, ModelProviderError)


async def test_rejection_is_counted_separately_from_generations() -> None:
    error = ModelProviderError("rejected")
    provider = ScriptedModelProvider([error, ModelResponse(content="ok")])
    for _ in range(2):
        with pytest.raises(ModelProviderError) as exc_info:
            await provider.generate(_request("op"))
        assert exc_info.value is error
    assert _metrics(provider) == (2, 0, 1)

    await provider.generate(_request("op2"))
    assert _metrics(provider) == (3, 1, 1)


async def test_mismatched_retry_of_a_rejected_operation_is_rejected() -> None:
    provider = ScriptedModelProvider([ModelProviderError("rejected")])
    with pytest.raises(ModelProviderError):
        await provider.generate(_request("op"))
    with pytest.raises(OperationIdReused):
        await provider.generate(_request("op", text="changed"))
    assert _metrics(provider) == (2, 0, 1)


async def test_exhausted_script_fails_loudly() -> None:
    provider = ScriptedModelProvider([])
    with pytest.raises(ScriptExhausted):
        await provider.generate(_request("op"))
    assert _metrics(provider) == (1, 0, 0)


def test_satisfies_the_model_provider_port() -> None:
    provider: ModelProvider = ScriptedModelProvider([])  # checked by pyright
    assert callable(provider.generate)
