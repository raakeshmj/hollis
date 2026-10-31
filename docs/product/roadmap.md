# Roadmap

Each milestone ships something verifiable and ends at its exit criteria, not on a date. Later milestones are deliberately coarse and get re-planned once the previous one finishes. Detailed plans live in [`docs/plans/`](../plans/).

| Milestone | Goal | Exit criteria |
|---|---|---|
| **M0: Foundation** ✓ done | Repo, tooling, docs, decision records, dev process | `make check` passes locally and in CI. Product, architecture and ADRs are documented. No platform code. |
| **M1: Runtime core** ✓ done | Single-turn runtime: explicit state machine, versioned event log and fold, RunStore port (in-memory + SQLite), ModelProvider port with a scripted fake, resume from the log | Crash-boundary and SIGKILL recovery tests pass. Import-boundary tests enforce NFR-7. ADR-0004 and ADR-0005 accepted, ADR-0006 added. Plan: [m1-runtime-core.md](../plans/m1-runtime-core.md). |
| **M2: Universal model gateway** — implemented; live verification pending | Model gateway and deployment model (ADR-0011); protocol adapters for Anthropic Messages and OpenAI-compatible Chat Completions (ADR-0012); locality, routing policy and on-prem networking (ADR-0013); failure taxonomy, Hollis-owned retries, ambiguous-outcome policy, budgets (ADR-0007); credentials and redaction (ADR-0009); retention policy (ADR-0010). Plans: [m2-gateway.md](../plans/m2-gateway.md), [m2-anthropic.md](../plans/m2-anthropic.md). | **Met offline:** one runtime path runs local, private and external deployments over both protocols, configured by data; routing policy, capability validation and custom-CA TLS tested; secret-handling requirements 1–4 tested; the default suite needs no key or network. **Not yet met:** any live verification. The Anthropic live test and the conformance suite exist but haven't been run (no API key or local inference server here); behavioral evals need live access. |
| **M3: Tools and sandboxing** | Typed tool definitions, permission policy, sandboxed execution | Tool calls are validated, recorded and replay-safe. Policy denials are tested. A minimal coding-agent demo works. |
| **M4: Context and memory** | Context budgeting and summarization; memory/retrieval behind a storage port | Context-selection behavior is covered by behavioral evals. The retrieval backend is chosen by benchmark, recorded in an ADR. |
| **M5: Developer interfaces** | CLI and REST API on top of the SDK | CLI and API reach parity with SDK run operations. API contract tests pass. |
| **M6: Multi-agent coordination** | Several agents per task with scoped tools and context | Coordination patterns are recorded in trajectories and covered by evals. |

Deferred with no milestone: virtual workforce features, cloud deployment packaging beyond a container, model training.
