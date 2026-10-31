# Model compatibility matrix

**Status key:**
- **documented**: the vendor's docs describe support. Hollis presets and fixtures are based on them. Not verified against a running server.
- **verified**: the conformance suite (`tests/conformance`) passed against a live server. The server version and model are recorded below.

**As of 2026-10-31, nothing is verified.** No live endpoint was available during M2. Offline tests use mock transports and a loopback server that speaks each protocol's documented shape; they don't establish compatibility with any product.

## OpenAI Chat Completions (`openai-chat-completions`)

| Server | Status | Preset (`provider`) | Documented differences that matter |
|---|---|---|---|
| OpenAI | documented | `openai`: `max_completion_tokens`, `developer` role | `max_tokens` deprecated; OpenAI recommends the Responses API for new work (a native adapter would use it) |
| vLLM | documented | `vllm`: `max_tokens` | extras via body (`top_k`, `structured_outputs`); `user` ignored; whether unknown params are ignored or rejected is undocumented; `--api-key` doesn't protect `/invocations` |
| Ollama | documented | `ollama`: `max_tokens` | no `max_completion_tokens`, `tool_choice`, `n` or logprobs; `reasoning_effort` mapped per model; key ignored locally |
| llama.cpp | documented | `llama-cpp`: `max_tokens` | reasoning in `reasoning_content` depending on `--reasoning-format`; 503 while loading (classified unavailable, retryable); may ignore `model` |
| LM Studio | documented | `lm-studio`: `max_tokens` | parameter support documented on subpages not yet reviewed |
| SGLang | documented | `sglang`: `max_tokens` | extras such as `chat_template_kwargs`; usage object undocumented |

## Anthropic Messages (`anthropic-messages`)

| Server | Status | Preset | Documented differences that matter |
|---|---|---|---|
| Anthropic API | documented (offline adapter tests only) | `anthropic`: spend-cap 429 rule | thinking always on for current models; effort is model-dependent |
| vLLM `/v1/messages` | documented (endpoint listed) | none | no dedicated documentation page found |
| Ollama `/v1/messages` | documented | `ollama`: usage `approximate` | subset only; no `tool_choice`, `count_tokens`, caching; `budget_tokens` not enforced |
| llama.cpp | documented ("Messages-compatible") | none | route details not confirmed |
| LM Studio | listed in navigation | none | not reviewed |

## Recording a verification

Run the live conformance suite (see `tests/conformance/test_conformance.py`) and add a row:

| Date | Server and version | Model | Protocol | Result | Notes |
|---|---|---|---|---|---|
| — | — | — | — | — | No runs recorded yet. |
