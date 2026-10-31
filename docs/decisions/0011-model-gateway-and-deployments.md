# ADR-0011: Model gateway and the deployment model

- Status: Accepted (M2)
- Date: 2026-10-31

## Context

Hollis must run models across providers, API protocols and deployment environments: cloud APIs, private cloud, enterprise gateways, localhost and air-gapped on-premises inference. The first M2 cut had one provider per `Runner`, identified by name. It couldn't express "same protocol, different endpoint". It couldn't detect that a run's endpoint changed between a crash and a resume. It also let SDK environment variables redirect traffic.

The same protocol is served by many implementations. For example, OpenAI Chat Completions is served by OpenAI, vLLM, Ollama, llama.cpp, LM Studio and SGLang. And one provider can expose several protocols, with capabilities that vary per model.

## Decision

**Separate the concepts:**

| Concept | Representation | Selects code? |
|---|---|---|
| Model identity | Logical deployment id that runs reference | — |
| Provider | Label (`anthropic`, `vllm`, …); selects presets only | no |
| Protocol | `anthropic-messages`, `openai-chat-completions` | **yes**: the adapter |
| Deployment | Catalog entry: protocol, provider, endpoint, wire model, locality, capabilities, options | — |
| Endpoint | Base URL, auth by env-var reference, TLS, explicit proxy, non-secret headers | — |
| Capabilities | `ModelCapabilities`: task families plus per-family features | validated |

**Runtime (provider-neutral):**
- `DeploymentRef` is the identity recorded in `RunCreated`: deployment id, provider, protocol, wire model, endpoint origin, locality, `deduplicates_requests`.
- `DeploymentDescriptor` is the ref plus capabilities.
- `ModelProvider` exposes `descriptor` and `generate`. It is the text-generation port; other task families will get their own ports, not a larger request class.
- `ModelResolver.resolve(model) -> ModelProvider` is a runtime port. `Runner.create` resolves the model, validates capabilities (`CapabilityMismatch`) and records the ref before persisting.
- `Runner.execute` re-resolves and refuses `DeploymentChanged` if any identity field differs.

**Gateway (`hollis.gateway`):**
- `Catalog`: deployments from Python or `hollis.toml`. Validated at load: URL safety, loopback for `local`, known external hosts can't be `local`/`private`, declared effort only, unique ids.
- `RoutingPolicy`: allowed localities.
- `ModelGateway` implements `ModelResolver`:
  - Applies routing policy (`RoutingDenied`).
  - Resolves credentials through the credential boundary and registers them with the shared `Redactor` before any request.
  - Imports adapter modules lazily and caches one provider per deployment.
- **There is no model routing, fallback or load balancing.** A model id resolves to exactly one deployment, or fails.

**Split of responsibilities:**
- Runtime: retries, timeouts, budgets, the ambiguous policy, the event log (ADR-0007).
- Gateway: resolution, policy, credentials.
- Adapters: wire translation and failure classification (ADR-0012).

## Alternatives considered

- **Keep one provider per Runner:** can't host more than one deployment, and has no deployment-change detection.
- **A gateway inside the runtime:** couples the event-sourced core to configuration and adapters.
- **One adapter per server product:** N implementations of one protocol, multiplying maintenance.
- **A universal request type covering every modality:** becomes a lowest-common-denominator class that silently drops fields. Per-family ports are used instead.

## Trade-offs

- Capabilities are declared, not discovered, so a wrong declaration is caught only by conformance runs or by provider errors.
- Re-resolving on resume means config edits can block resuming a run. That is deliberate, to protect auditability.

## Consequences

- `RunCreated` now records where data was sent. It was changed in place before release, as allowed by ADR-0007's baseline reset.
- New task families (embeddings, rerank, image, audio) need a port and adapters, but no gateway redesign.
- **Evidence:** `tests/gateway/`, `tests/runtime/test_runner.py` (capabilities, resolution, deployment change), `tests/recovery/test_gateway_recovery.py`.
