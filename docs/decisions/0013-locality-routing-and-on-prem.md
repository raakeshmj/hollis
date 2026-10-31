# ADR-0013: Locality, routing policy and on-premises networking

- Status: Accepted (M2)
- Date: 2026-10-31

## Context

On-premises and air-gapped operation is a core Hollis requirement. Operators must be able to prove that run data stays inside their infrastructure. That means: no silent cloud fallback, no environment-driven redirection, no telemetry to third parties, and support for private CAs and proxies.

## Decision

**Locality.**
- Every deployment declares `local` (loopback), `private` (operator network: private cloud, enterprise, on-prem) or `external` (third-party service).
- At load, `local` requires a loopback endpoint, and well-known third-party hosts (`api.anthropic.com`, `api.openai.com`, `ollama.com`) must be `external`.
- Each run records the deployment's locality and endpoint origin in `RunCreated`.

**Routing policy.**
- `RoutingPolicy.allowed_localities`, for example `{local, private}` for data that must not leave operator infrastructure.
- It is enforced at resolution, which happens in `Runner.create` before anything is persisted, and again on every `execute`.
- There is no fallback path: a denied or failing deployment is never substituted.

**No implicit routing.**
- Every client gets an explicit base URL and explicit auth.
- Proxy and certificate environment variables are ignored (`trust_env=False`). A proxy is used only when configured, and redirects are not followed.
- SDK environment fallbacks are neutralized and tested with hostile values.

**TLS.**
- Verification is on by default. A custom CA bundle and client certificates (mutual TLS) are supported.
- `verify = false` must be explicit and is logged.

**No external telemetry.**
- The SDK's OpenTelemetry is disabled. Hollis calls no Hollis-operated services.
- Keyless local endpoints send no credential.

## Alternatives considered

- **Inferring locality from DNS or IP ranges:** unreliable for private networks and split-horizon DNS. Explicit declaration, plus checks of what can be verified, is honest.
- **Honouring `HTTP(S)_PROXY`:** convenient, but environment changes would then alter where traffic goes. Proxies are explicit per endpoint instead.

## Trade-offs

- `private` can't be verified, beyond excluding known external hosts. A misdeclared private endpoint is an operator error that Hollis can only partly catch.
- Operators who rely on environment proxies must configure them per endpoint.

## Consequences

- **Evidence:**
  - `tests/gateway/test_gateway_runtime.py`: routing denial before persistence; requests reach only the resolved endpoint.
  - `tests/network/test_loopback.py`: custom-CA TLS success, and handshake failure without the CA.
  - Hostile-environment adapter tests.
  - The full suite passes under a sandbox profile that denies all non-loopback outbound traffic.
- **Not in M2:** a distributed control plane, an installer, egress auditing beyond the event log, and proxy credentials (proxy URLs with userinfo are rejected).
