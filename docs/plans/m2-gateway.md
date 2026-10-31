# M2 Plan (revised): Universal Model Gateway

Status: approved 2026-10-31. Supersedes the provider scope of [m2-anthropic.md](m2-anthropic.md); everything else in that plan (failure taxonomy, Hollis-owned retries, ambiguous-outcome policy, budgets, redaction) is retained.

## Why

Hollis must run models across providers, API protocols and deployment environments, including fully self-hosted and air-gapped inference. The first M2 cut was Anthropic-centric. It had two concrete defects:
- **Silent endpoint redirection.** SDK clients built without an explicit `base_url` read `ANTHROPIC_BASE_URL`, and `ANTHROPIC_CUSTOM_HEADERS` is merged into every request.
- **Identity by provider name only.** One provider per `Runner`, identified by name, can't express "same protocol, different endpoint", or detect that a deployment changed between a crash and a resume.

## Concept separation

| Concept | Representation |
|---|---|
| Model identity | Logical deployment id that runs reference (`local-qwen`). |
| Provider | Informational label (`anthropic`, `vllm`, `ollama`, …). It never selects code. |
| Protocol | `anthropic-messages` or `openai-chat-completions`. This selects the adapter. |
| Deployment | A catalog entry: protocol, provider, endpoint, wire model id, locality, capabilities, protocol options. |
| Endpoint | `base_url`, auth (`none`, `bearer` or `header`, with env-var references only), TLS, explicit proxy, non-secret headers. |
| Capabilities | Declared per deployment and validated before persistence. Task families get separate ports. |

## Components

- **`hollis.runtime`.** Adds `DeploymentDescriptor`/`DeploymentRef`, `ModelCapabilities`, the `ModelResolver` port, and capability validation. `RunCreated` records the deployment reference. Resume refuses a changed deployment.
- **`hollis.gateway`.** Catalog (Python or TOML), routing policy by locality, credential references resolved into a shared `Redactor`, and lazy, cached adapter construction. No routing intelligence, fallback or load balancing.
- **`hollis.adapters.endpoint`** (pure configuration) and **`hollis.adapters._http`** (`httpx2` client factory, transport classification, retry-after).
- **Protocol adapters:**
  - `anthropic-messages`: the existing adapter, generalized. Explicit endpoint, auth and version; capability-gated effort; SDK environment fallbacks neutralized.
  - `openai-chat-completions`: new, on raw `httpx2`. Configurable token-limit field, system role, effort and `extra_body`.

## Decisions

- **Own adapter layer, no multi-provider library.** LiteLLM 1.106 has 23 core dependencies, pins `openai<3`, and has its own retries and fallback. any-llm 1.41 pins `anthropic<1`.
- **Raw `httpx2` for the OpenAI-compatible protocol.** Exact wire control, keyless operation, no environment reads. `httpx2` is the same stack the official SDKs use.
- **`hollis.toml` support** via stdlib `tomllib`.
- **`trustme`** (dev) for a real loopback TLS test with a custom CA.

## Acceptance criteria

- `make check` passes offline, with no keys and with hostile SDK environment variables set.
- One runtime code path runs Anthropic-protocol and OpenAI-compatible deployments, local and cloud, configured by data alone.
- Unsupported capabilities are rejected before persistence, never dropped.
- External deployments are refused under a local/private routing policy. There's no fallback path.
- Custom-CA TLS works on loopback, and fails without the CA.
- Credentials never appear in events, SQLite bytes, logs or descriptors.
- Recovery guarantees hold through the gateway, and a changed deployment is refused on resume.
- The compatibility matrix distinguishes documented from verified; no claims without a live conformance run.
