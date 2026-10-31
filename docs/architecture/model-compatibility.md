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

## Tool calling (M3)

Tool calling is implemented for both protocols and declared (`tools=True`) only in the `anthropic` and `openai` presets. Other presets leave it off until a live run shows the server's tool-call behaviour; set it explicitly on a deployment to opt in.

| Protocol | Status | Notes |
|---|---|---|
| `anthropic-messages` | documented (offline fixtures) | Assistant content blocks, including thinking signatures, are echoed back unchanged after tool use. |
| `openai-chat-completions` | documented (offline fixtures) | Arguments arrive as JSON strings; unparseable arguments become `invalid` results, not crashes. Legacy `function_call` is rejected as an invalid response. |

`tests/live/test_tool_calling_live.py` runs one tool round trip against Anthropic's API when enabled. It has not been run.

## Context windows and token counting (M4)

Presets declare no `context_window_tokens`: windows depend on the model, not the server. Set it per deployment. Without it, the context engine applies only a configured `max_input_tokens` or `max_history_turns`, and records that the window is unknown.

| Protocol | Exact token counting | Status |
|---|---|---|
| `anthropic-messages` | `count_tokens` (`TokenCounter` port), used near the budget limit | documented (offline mock-transport tests only) |
| `openai-chat-completions` | none: no standard endpoint; heuristic estimate calibrated from reported usage | n/a |

## Embeddings (`openai-embeddings`, M5)

| Server | Status | Notes |
|---|---|---|
| OpenAI | documented | `dimensions` supported by `text-embedding-3-*` (set `send_dimensions`) |
| Ollama | documented | `/v1/embeddings`; models such as `nomic-embed-text` expect `search_query: ` / `search_document: ` prefixes |
| vLLM, llama.cpp, LM Studio, TEI, Infinity | documented | OpenAI-compatible embeddings endpoints; dimensions must be declared per deployment |

Nothing is verified. The local `HashingEmbedder` needs no server. It captures surface similarity only.

## Recording a verification

Run the live conformance suite (see `tests/conformance/test_conformance.py`) and add a row:

| Date | Server and version | Model | Protocol | Result | Notes |
|---|---|---|---|---|---|
| — | — | — | — | — | No runs recorded yet. |
