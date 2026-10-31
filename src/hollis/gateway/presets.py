"""Conservative per-provider defaults, applied only when a deployment omits the field.

Sourced from each project's documentation as of 2026-10-31. They are starting points, not
compatibility claims: see docs/architecture/model-compatibility.md for what has been verified.
"""

from typing import Any

# protocol -> provider label -> default `options`
OPTIONS: dict[str, dict[str, dict[str, Any]]] = {
    "openai-chat-completions": {
        # OpenAI deprecated max_tokens in favour of max_completion_tokens; o1+ use `developer`.
        "openai": {"token_limit_param": "max_completion_tokens", "system_role": "developer"},
        # Documented to accept max_tokens (Ollama lists no max_completion_tokens).
        "vllm": {"token_limit_param": "max_tokens"},
        "ollama": {"token_limit_param": "max_tokens"},
        "llama-cpp": {"token_limit_param": "max_tokens"},
        "lm-studio": {"token_limit_param": "max_tokens"},
        "sglang": {"token_limit_param": "max_tokens"},
    },
    "anthropic-messages": {
        # Anthropic documents that a spend-cap 429 carries no retry-after.
        "anthropic": {"quota_on_429_without_retry_after": True},
    },
}

# protocol -> provider label -> default `capabilities`
CAPABILITIES: dict[str, dict[str, dict[str, Any]]] = {
    "anthropic-messages": {
        # Anthropic's API documents tool use for current models.
        "anthropic": {"text": {"tools": True}},
        # Ollama documents Messages-compatible token counts as approximations. Tool support
        # depends on the served model, so it is not declared here.
        "ollama": {"text": {"usage_reporting": "approximate"}},
    },
    "openai-chat-completions": {
        # OpenAI documents function tools for Chat Completions. Self-hosted servers support tools
        # only for some models and parsers, so operators declare it per deployment.
        "openai": {"text": {"tools": True}},
    },
}


def apply(data: dict[str, Any]) -> dict[str, Any]:
    protocol = data.get("protocol")
    provider = data.get("provider")
    if not isinstance(protocol, str) or not isinstance(provider, str):
        return data
    result = dict(data)
    for field, table in (("options", OPTIONS), ("capabilities", CAPABILITIES)):
        preset = table.get(protocol, {}).get(provider)
        if preset is not None and field not in result:
            result[field] = preset
    return result
