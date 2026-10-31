from pathlib import Path

import pytest
from pydantic import ValidationError

from hollis.adapters.config import OpenAIChatOptions
from hollis.gateway import AnthropicMessagesDeployment, Catalog, OpenAIChatDeployment
from hollis.runtime.model import Locality, UsageReporting

TOML = """
[routing]
allowed_localities = ["local", "private"]

[[deployments]]
id = "local-qwen"
protocol = "openai-chat-completions"
provider = "vllm"
model = "Qwen/Qwen3-8B"
locality = "local"
[deployments.endpoint]
base_url = "http://127.0.0.1:8000/v1"

[[deployments]]
id = "corp-llama"
protocol = "openai-chat-completions"
provider = "llama-cpp"
model = "llama-3.3-70b"
locality = "private"
[deployments.endpoint]
base_url = "https://inference.corp.internal/v1"
auth = { type = "bearer", env = "CORP_INFERENCE_TOKEN" }
tls = { ca_bundle = "/etc/ssl/corp-ca.pem" }
[deployments.capabilities.text]
max_output_tokens = 4096

[[deployments]]
id = "claude"
protocol = "anthropic-messages"
provider = "anthropic"
model = "claude-opus-5-5"
locality = "external"
[deployments.endpoint]
base_url = "https://api.anthropic.com"
auth = { type = "bearer", env = "ANTHROPIC_API_KEY" }
[deployments.capabilities.text]
reasoning_effort = ["low", "medium", "high", "xhigh", "max"]
[deployments.options]
effort = "low"
quota_on_429_without_retry_after = true
"""


def _catalog(tmp_path: Path, text: str = TOML) -> Catalog:
    path = tmp_path / "hollis.toml"
    path.write_text(text)
    return Catalog.from_toml(path)


def test_toml_catalog_loads_every_deployment_kind(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    assert catalog.routing.allowed_localities == {Locality.LOCAL, Locality.PRIVATE}
    local, corp, claude = catalog.deployments
    assert isinstance(local, OpenAIChatDeployment)
    assert isinstance(claude, AnthropicMessagesDeployment)
    assert corp.capabilities.text is not None
    assert corp.capabilities.text.max_output_tokens == 4096
    assert claude.options.effort == "low"


def test_descriptor_exposes_identity_without_credentials(tmp_path: Path) -> None:
    corp = _catalog(tmp_path).deployments[1]
    descriptor = corp.descriptor()
    assert descriptor.ref.model_dump() == {
        "deployment": "corp-llama",
        "provider": "llama-cpp",
        "protocol": "openai-chat-completions",
        "model": "llama-3.3-70b",
        "endpoint": "https://inference.corp.internal",
        "locality": Locality.PRIVATE,
        "deduplicates_requests": False,
    }
    assert "CORP_INFERENCE_TOKEN" not in descriptor.model_dump_json()


def _openai(**overrides: object) -> OpenAIChatDeployment:
    data: dict[str, object] = {
        "id": "m",
        "provider": "vllm",
        "model": "x",
        "locality": "local",
        "endpoint": {"base_url": "http://localhost:8000/v1"},
    }
    return OpenAIChatDeployment.model_validate(data | overrides)


def test_presets_fill_omitted_options_only() -> None:
    openai = _openai(
        provider="openai", locality="external", endpoint={"base_url": "https://api.openai.com/v1"}
    )
    assert (openai.options.token_limit_param, openai.options.system_role) == (
        "max_completion_tokens",
        "developer",
    )
    explicit = _openai(
        provider="openai",
        locality="external",
        endpoint={"base_url": "https://api.openai.com/v1"},
        options={"token_limit_param": "max_tokens"},
    )
    assert explicit.options == OpenAIChatOptions(token_limit_param="max_tokens")
    ollama_messages = AnthropicMessagesDeployment.model_validate(
        {
            "id": "o",
            "provider": "ollama",
            "model": "qwen3",
            "locality": "local",
            "endpoint": {"base_url": "http://localhost:11434"},
        }
    )
    assert ollama_messages.capabilities.text is not None
    assert ollama_messages.capabilities.text.usage_reporting is UsageReporting.APPROXIMATE


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"endpoint": {"base_url": "https://inference.corp.internal/v1"}}, "requires a loopback"),
        (
            {"locality": "private", "endpoint": {"base_url": "https://api.openai.com/v1"}},
            "external service",
        ),
        ({"options": {"reasoning_effort": "high"}}, "not declared"),
        ({"endpoint": {"base_url": "http://u:p@localhost/v1"}}, "embed credentials"),
    ],
)
def test_invalid_deployments_are_rejected_at_load(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _openai(**overrides)


def test_declared_effort_is_accepted() -> None:
    deployment = _openai(
        capabilities={"text": {"reasoning_effort": ["low", "high"]}},
        options={"reasoning_effort": "high"},
    )
    assert deployment.options.reasoning_effort == "high"


def test_duplicate_ids_and_unknown_protocols_are_rejected() -> None:
    entry = {
        "id": "m",
        "protocol": "openai-chat-completions",
        "provider": "vllm",
        "model": "x",
        "locality": "local",
        "endpoint": {"base_url": "http://localhost:1/v1"},
    }
    with pytest.raises(ValidationError, match="duplicate"):
        Catalog.from_mapping({"deployments": [entry, entry]})
    with pytest.raises(ValidationError):
        Catalog.from_mapping({"deployments": [entry | {"protocol": "grpc-magic"}]})
