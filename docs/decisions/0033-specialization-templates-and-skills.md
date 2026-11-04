# ADR-0033: Specialization as templates over ordinary agents, with Agent Skills

- Status: Accepted (M8)
- Date: 2026-11-03

## Context

Before M8 an agent was a model, instructions, a list of tool names and access grants. Nothing made it good at a kind of work, and nothing could be packaged, versioned, reused or shared. Research ([specialization.md](../research/specialization.md)) found:
- focused tool sets help, but tool *use* is the bottleneck;
- reusable instructions are converging on the open Agent Skills format.

The core must not assume coding tasks, and every specialist must keep the runtime's guarantees: durable runs, policy, approvals, delegation and recovery.

## Decision

**A specialist is an ordinary agent, assembled from data.** `hollis.specialists` defines:
- **capabilities**: named, versioned bundles of required and optional tools, configured commands, guidance, overlay rules ([ADR-0035](0035-policy-overlays.md)) and requirements;
- **templates**: versioned blueprints with a delegate-facing description, capabilities, a procedure, an output contract ([ADR-0034](0034-output-contracts.md)), overlay rules, offered skills, a budget, parameters and example tasks.

Both are TOML. Seven first-party templates ship with Hollis; a workspace may add its own under `agent-templates/` but may not take a first-party id. Templates name no vendor.

**Materialization, not a runtime concept.** Creating an agent from a template materializes it into the ordinary agent fields:
- the procedure, then each capability's guidance;
- the required tools, then the optional tools and configured commands the workspace has;
- the contract, the overlay rules (parameters filled into path globs) and the skills.

The agent records its origin: template id, version, a content digest over the template and its capabilities, the parameter values, and which template fields were customized. Nothing at run time knows a template exists.
- **Customize** is editing the agent; changed template fields are marked customized.
- **Upgrade** re-materializes the template's current version, keeps the customized fields (unless reset), and needs the previewed digest; it is refused if the template changed since the preview.
- **Export** writes the agent's effective configuration as a template file. Sharing is copying files; there is no marketplace.

**Requirements are explicit.** Templates declare model needs (tool calling, minimum context window) and workspace needs (file tools, writes, commands, git, collections, delegates). Hollis reports readiness and per-deployment fit. Hard mismatches block creation; soft ones are warnings.

**Agent Skills.** `skills/<name>/SKILL.md` folders in the open format (the spec's YAML subset is parsed without a new dependency). Agents are offered skills by name and description and read them with `skill_read`. Hollis never executes skill scripts (no verified sandbox, [ADR-0016](0016-execution-isolation.md)) and never treats `allowed-tools` as a grant. Which skills an agent is offered is guidance, not access control.

## Alternatives considered

- **A separate engine or agent type per specialist.** It would fork the runtime and every guarantee built on it.
- **Templates as live references** (an agent that follows its template). Silent behaviour changes; runs already record their instructions and tool identities, so explicit upgrades with previews fit the model.
- **A marketplace or registry.** Out of scope, and a supply-chain surface. Files can be reviewed like code.
- **Executing skill scripts.** That is code execution without a sandbox.

## Trade-offs

- Gains: specialists inherit every existing guarantee and interface, and stay inspectable and editable.
- Costs:
  - Template text is copied into agents, so upgrades are a deliberate step.
  - A capability's tools exist only when the workspace configures them, and readiness has to say so.
  - Specialists cost more tokens per run ([evaluation](../architecture/specialist-evaluation.md)).

## Consequences

- Agents gain `policy`, `contract`, `skills` and `origin`; stored agents from earlier versions load unchanged.
- File tools never write the skills or templates directories, because they become agents' instructions ([ADR-0036](0036-capability-tools-and-protected-paths.md)).
- Enforced by tests:
  - `tests/specialists`: catalog, materialization, readiness, skills;
  - `tests/platform/test_specialists.py`: creation, customization, upgrade, export, every first-party flow on the demo model;
  - the interface tests;
  - a layering test (specialists never import the platform).
