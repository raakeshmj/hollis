# Research note: multi-agent orchestration (M7)

Read on 2026-11-01. This note covers what the evidence says about when multiple agents help, and what that implies for Hollis. Sources are linked; figures are as reported by their authors.

## Findings

- **Multi-agent systems fail often, and mostly for structural reasons.** MAST (Cemri et al., *Why Do Multi-Agent LLM Systems Fail?*, NeurIPS 2025 D&B, [arXiv 2503.13657](https://arxiv.org/abs/2503.13657)):
  - Analysed seven open multi-agent frameworks over 200+ tasks and found 14 failure modes in three groups: system design and specification (~42%), inter-agent misalignment (~37%), and task verification (~21%).
  - Gains over single-agent baselines were often small.
  - The authors conclude that fixes need structural redesign, not better prompts.
- **Parallelizable work benefits; sequential work suffers.** Kim et al., *Towards a Science of Scaling Agent Systems* (Google Research, DeepMind and MIT, [arXiv 2512.08296](https://arxiv.org/abs/2512.08296), [blog](https://research.google/blog/towards-a-science-of-scaling-agent-systems-when-and-why-agent-systems-work/)):
  - Across 180 configurations, coordination improved parallelizable tasks substantially and degraded sequential ones by 39–70%.
  - There is a "coordination tax": global context must be compressed into messages.
  - Returns diminish once a single agent is already strong.
  - Independent agents amplified errors about 17×; centralized coordination contained them.
- **Orchestrator–worker works for breadth, at a token cost.** Anthropic, [*How we built our multi-agent research system*](https://www.anthropic.com/engineering/multi-agent-research-system):
  - A lead agent delegating to parallel subagents beat a single agent by about 90% on breadth-first research.
  - Token usage explained most of the variance, and the system used many times the tokens of chat.
  - Vague delegation was a primary failure; it was fixed with explicit objectives, boundaries and output formats.
  - Tasks needing shared context or tight dependencies were a poor fit.
  - Restarting failed agents from scratch was costly, and ordinary logging was not enough to debug them.
- **Interoperability: A2A 1.0.** The [Agent2Agent protocol](https://a2a-protocol.org/latest/specification/), an open specification hosted by the Linux Foundation:
  - Agents are described by an Agent Card (`/.well-known/agent-card.json`).
  - Work is exchanged as Tasks with messages, parts and artifacts, through operations such as `SendMessage`, `GetTask`, `CancelTask` and `SubscribeToTask`.
  - Task states are submitted, working, input-required, auth-required, completed, failed, canceled and rejected.
  - Servers must authenticate, and must not reveal resources a caller can't access.

## Implications for Hollis

| Evidence | Design consequence |
|---|---|
| Specification failures dominate | Delegations are structured assignments with an objective, context, expected output and explicit inputs, not free-form messages. Children get a fixed report protocol. |
| Verification failures; error amplification without a checker | Reports separate what Hollis observed (outcome, usage, side effects, grounding) from what the child claims. Evidence crosses runs only as recorded, verifiable blocks. Disagreements are detected and kept, not averaged away. The reviewer pattern is first class in plans. |
| Sequential tasks get worse; there's a coordination tax | Single agent stays the default. Dependent steps pass prior results by reference (no lossy re-typing). The evaluation reports where multi-agent loses. |
| Parallel breadth wins | Independent read-only delegations in one turn run concurrently, using the runtime's existing parallel tool execution. |
| Restarting failed agents is costly; debugging is hard | Children are durable runs with deterministic ids: recovery reattaches instead of restarting. The whole tree is inspectable from event logs. |
| Token cost dominates | Tree-wide token budgets, per-run token limits, children and depth limits; usage is shown per agent and per tree. |
| A2A is the interoperability standard | The internal model maps cleanly to A2A (Task↔run, Message↔assignment, Artifact↔report), but internal orchestration does not depend on the protocol. See ADR-0032. |

## Not adopted

- **Group chat or blackboard architectures** (agents reading and writing a shared conversation). They are the architecture most associated with the misalignment failures above, and they make permissions, budgets and provenance hard to bound.
- **Debate and voting as defaults.** Plans can ask several agents the same question and compare their answers. Hollis keeps disagreement visible and routes it to a reviewer, instead of picking a majority silently.
- **A separate agent framework.** Every coordination pattern is built from runs, tools and the event log that already exist.
