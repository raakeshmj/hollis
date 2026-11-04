# ADR-0035: Per-run policy overlays that can only tighten

- Status: Accepted (M8)
- Date: 2026-11-01

## Context

Tool policy was workspace-wide ([ADR-0015](0015-tool-definitions-policy-and-approval.md)). A specialist often needs *less* than the workspace allows: a reviewer that must not write, a technical writer that may write only under `docs/`, automation that needs approval for every change even where the workspace allows it. Delegated runs must never be able to do what their parent could not ([ADR-0031](0031-orchestration-authority-and-limits.md)). Recovery must decide the same way after a restart.

## Decision

**An overlay is a list of `PolicyOverlayRule`s recorded in `RunCreated` (schema v7; v6 logs upcast).** A rule has:
- a decision, `require_approval` or `deny`, never `allow`;
- a tool-name glob, optional effects;
- optional path scoping: `outside` maps an argument name to allowed globs, and the rule matches when any value of that argument is outside all of them or leaves the root with `..`.

The runner applies the strictest matching rule on top of the workspace policy's decision and never loosens it. The decision records the rule as `agent:<name>`.

**Agents carry overlays; delegations accumulate them.**
- A run of an agent gets the agent's rules.
- A delegated run gets every rule of its parent run, then its own agent's. Overlays therefore only accumulate down a tree.
- An effect rule on a parent also denies delegating to an agent that could act, because a delegate tool's effect is the strongest its delegation can reach.

**Path scoping is only as exact as the tools.** It matches what tools receive as arguments, so the write tools guarantee that the path named is the path written: no symbolic links in any component, no `..`, and `apply_patch` derives the `paths` argument from the patch itself ([ADR-0036](0036-capability-tools-and-protected-paths.md)). Rules with `outside` apply only to tools that have that argument.

## Alternatives considered

- **Per-agent full policies.** Could allow more than the workspace; overlays can't, by construction.
- **Restricting the tool list only.** Coarse: it can't express "only under `docs/`" or "approve every change".
- **Evaluating overlays outside the fold.** Recovery would decide differently from the original execution.

## Trade-offs

- Gains: specialists are narrower than the workspace by default, the narrowing is visible per decision, and it is inherited by everything delegated.
- Costs:
  - overlays are additive, so a child can't relax its parent's rule even when that would be safe;
  - path scoping depends on argument names.

## Consequences

- `PolicyOverlayRule` and `apply_overlay` live in `hollis.runtime.tools`.
- Agents and templates have `policy`; run details show the rules a run was held to.
- Tests: tighten-only, strictest-wins, path matching, recording and recovery, inheritance by delegated runs (including the denied delegation), and the specialists' overlays in real flows.
