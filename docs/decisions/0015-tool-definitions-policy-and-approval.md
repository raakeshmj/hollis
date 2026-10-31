# ADR-0015: Tool definitions, registry, policy and approval

- Status: Accepted (M3)
- Date: 2026-10-31

## Context

Developers need a pleasant way to add tools without learning the runtime. Operators need deterministic, least-privilege authorization. OWASP's agentic top 10 (2026) lists tool misuse (ASI02), identity and privilege abuse (ASI03) and human–agent trust exploitation (ASI09). A tool's description is model input, so it must never grant permission.

## Decision

**Definitions (`hollis.tools.tool`, `FunctionTool`):**
- One Pydantic model is derived from a typed signature. It is both the JSON Schema given to the model and the validator for the model's arguments; unknown arguments are rejected. One contract, no drift.
- The return annotation validates output. Structured results become JSON parts.
- The docstring summary is the description. Definition errors (missing annotations or docstring, `*args`) fail at decoration time.
- Validation errors never echo input values.
- Behaviour that matters for security and recovery is declared by the registering code: `effect` (read-only, mutating, destructive), `idempotent` (defaults to true only for read-only tools), an optional `reconcile` hook, `timeout_s`, `version`.
- The schema digest (name, version, description, schema) is recorded per run. Resuming against a changed tool is refused.
- Names follow `^[A-Za-z0-9_-]{1,64}$`, the intersection of the provider rules.

**Registry (`ToolRegistry`):**
- An explicit, operator-assembled catalog. Nothing is discovered or loaded dynamically.
- A run can call only the tools named in `RunSpec.tools`.

**Policy (`ToolPolicy`):**
- Decides `allow`, `require_approval` or `deny` from runtime-controlled facts only: the registered tool's identity, declared effect and trust; the execution environment label; and deterministic predicates over validated arguments (resource scoping, e.g. a path prefix).
- `RulePolicy` applies first-match rules over tool globs, effects, trust and environments.
- `DefaultToolPolicy` is the fail-safe fallback: trusted read-only tools are allowed, everything else requires approval.
- Before policy runs: calls naming tools outside the run, with unparseable or off-schema arguments, or over limits, are `invalid`. Calls whose arguments contained a registered credential are denied (the credential guard).

**Approval (`Approver` protocol, `Runner.resolve_approval`):**
- An `ApprovalRequest` shows the tool, version, effect, trust, idempotency, the policy's reason, the exact arguments (`describe()` renders them), an expiry, and a binding digest.
- A decision is accepted only with the matching binding. The fold re-verifies it.
- Without an approver, approval-required calls are denied, never silently approved.
- An approver may defer (return None). The run suspends with `state.awaiting_approval`, and a later `resolve_approval` records the decision.
- Expired requests are denied.
- Restarts cannot bypass any of this: approval state lives only in events.

## Alternatives considered

- **Separate hand-written JSON Schemas:** they drift from validation.
- **Model-declared or description-derived risk:** that's exactly the injection surface.
- **Allow-by-default policy:** violates least privilege.
- **Approval binding by invocation id only:** an approval could then outlive a change to the arguments.

## Trade-offs

- Developers must classify effects honestly. A mislabeled mutating tool marked read-only would be allowed by the default policy. Rules can still restrict it, and the risk is documented.
- Approval prompts can still be socially engineered (ASI09). Hollis shows exact arguments and effects; judgement remains human.
- Hollis doesn't authenticate approvers. `resolve_approval` is an operator API, and the host application must protect it.

## Consequences

- **Evidence:** `tests/tools/`, `tests/runtime/test_tool_loop.py` (policy and approval), `tests/security/` (descriptions cannot grant permission; approvals survive restarts and can't be replayed).
- **Future:** an approval UI, approver identity integration, and policy re-evaluation on resume.
