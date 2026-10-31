import pytest

from hollis.adapters.fake_provider import ScriptedModelProvider, ScriptExhausted
from hollis.runtime.model import ModelProvider, ModelProviderError, ModelRequest, ModelResponse


def _request(operation_id: str) -> ModelRequest:
    return ModelRequest(operation_id=operation_id, model="fake", input="hello")


async def test_returns_scripted_responses_in_order_per_new_operation() -> None:
    provider = ScriptedModelProvider([ModelResponse(content="a"), ModelResponse(content="b")])
    assert (await provider.generate(_request("op1"))).content == "a"
    assert (await provider.generate(_request("op2"))).content == "b"
    assert (provider.invocations, provider.generations) == (2, 2)


async def test_repeated_operation_id_is_deduplicated() -> None:
    provider = ScriptedModelProvider([ModelResponse(content="a"), ModelResponse(content="b")])
    first = await provider.generate(_request("op"))
    again = await provider.generate(_request("op"))
    assert first == again == ModelResponse(content="a")
    assert provider.invocations == 2
    assert provider.generations == 1
    assert provider.invocations_by_operation["op"] == 2


async def test_scripted_error_is_raised_and_deduplicated() -> None:
    error = ModelProviderError("rejected")
    provider = ScriptedModelProvider([error])
    for _ in range(2):
        with pytest.raises(ModelProviderError) as exc_info:
            await provider.generate(_request("op"))
        assert exc_info.value is error
    assert (provider.invocations, provider.generations) == (2, 1)


async def test_exhausted_script_fails_loudly() -> None:
    provider = ScriptedModelProvider([])
    with pytest.raises(ScriptExhausted):
        await provider.generate(_request("op"))
    assert (provider.invocations, provider.generations) == (1, 0)


def test_satisfies_the_model_provider_port() -> None:
    provider: ModelProvider = ScriptedModelProvider([])  # checked by pyright
    assert callable(provider.generate)
