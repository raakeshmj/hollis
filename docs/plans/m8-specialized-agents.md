# M8 plan: specialized agents and the agent ecosystem

Branch `m8-specialized-agents`, from `m7-orchestration`. Owned end to end. No separate approval step.

**Status: implemented and verified offline** (see the [roadmap](../product/roadmap.md) row and the [evaluation](../architecture/specialist-evaluation.md)). Changes from the plan below, found while building it:
- Overlays live in `hollis.runtime.tools` (next to the policy types), not `hollis.runtime.policy`.
- Contracts reference the call ids the model sees, not invocation ids, which models never see.
- A research brief may cite a delegated run only if it completed *with its output accepted*.
- File tools gained protected paths and link-free writes ([ADR-0036](../decisions/0036-capability-tools-and-protected-paths.md)); the skills and templates directories are protected because they become instructions.
- Templates are TOML under `hollis/specialists/data/`; workspace templates live in `agent-templates/` (`[specialists]` configures both directories).
- ADRs 0033–0036 record the decisions.

## Starting point

M7 gives durable runs, tools with policy and approvals, context, memory, knowledge, delegation and four interfaces. An "agent" today is a model, instructions, a list of tool names and access grants. Nothing in that makes an agent good at a kind of work:
- the tools are generic (read, list and write files; search knowledge);
- nothing checks what an agent produces;
- policy is workspace-wide only;
- there is no way to package, reuse, version or share a configuration.

The research ([docs/research/specialization.md](../research/specialization.md)) points the same way:
- **Tools:** fewer, well-chosen tools help models choose. Domain tools alone add little unless tool *use* improves.
- **Structured results:** the reliable way to get them is an output tool whose arguments are validated, with errors fed back for another try.
- **Reusable instructions** are converging on the open Agent Skills format (SKILL.md, loaded progressively).

## Architecture

A specialization is assembled from platform features, each usable without templates:

| Piece | What it is | Where |
|---|---|---|
| **Capability tools** | Real abilities: `code_search`, `code_outline`, `code_read` (line-numbered), `apply_patch` (atomic, reconcilable); `data_list`, `data_describe`, `data_query` (read-only SQL over workspace tables, exact results with query ids); `docs_check`; operator-configured commands (`[[tools.commands]]`); read-only git | `hollis.tools.builtin` |
| **Output contracts** | A `submit_<contract>` tool whose arguments are validated by schema and then **against the run's own log**. Retries happen through the normal tool loop. The accepted result is the run's structured output, shown in views and delegation reports | `hollis.specialists.contracts` (validators), `hollis.platform.outputs` (tool) |
| **Policy overlays** | Per-agent rules (`require_approval`, `deny`, optional path scoping) that can only tighten the workspace policy. Recorded in `RunCreated` v7, applied by the runner, inherited by delegated runs | `hollis.runtime.policy`, runner |
| **Capabilities** | Named, versioned bundles: tools, guidance, overlay rules, model and workspace requirements | `hollis.specialists` (TOML data) |
| **Templates** | Versioned blueprints: description, capabilities, instructions (procedure), output contract, requirements, budgets, parameters, example tasks. Materialized into an ordinary agent | `hollis.specialists` (TOML data) |
| **Agent Skills** | `skills/<name>/SKILL.md` folders in a workspace, offered by name and description and read on demand (`skill_read`). Scripts are never executed; `allowed-tools` grants nothing | `hollis.specialists.skills` |

**Agents stay agents.** A template-made agent is an ordinary agent record: the same runtime, delegation and interfaces. It also remembers its origin (template id, version and digest), its parameters and its overrides:
- **Customizing** edits overrides.
- **Upgrading** re-materializes the newer template and re-applies the overrides, with a preview.
- **Exporting** writes an agent as a template file to share. Sharing means copying files; there is no marketplace.

**Explicit requirements.** Templates declare model needs (tool calling, minimum context) and workspace needs (filesystem, writable files, commands, collections). Hollis reports readiness and per-deployment fit, and blocks hard mismatches. Templates name no vendors.

## First-party specialists

| Template | Does | Contract (validated against the run log) |
|---|---|---|
| `software-engineer` | investigates code, changes it with patches (approval), runs configured checks | `change-report`: each change was applied in this run; each verification names a command it actually ran, with its real outcome |
| `code-reviewer` | read-only review of files or the working diff | `code-review`: every finding points to an existing file and line; read-only by overlay |
| `researcher` | investigates knowledge collections, keeps contradictions | `research-brief`: every citation is evidence this run had; no claim cites fabricated evidence |
| `data-analyst` | answers from workspace tables with exact queries | `data-analysis`: every finding names a query this run executed whose result contains the reported value |
| `technical-writer` | documents code accurately; writes only under `docs/` (overlay) | `docs-change`: every listed file was written in this run, and `docs_check` passed on it afterwards |
| `automation` | carries out operational procedures with configured commands and files, plan first | `automation-log`: every step names an invocation of this run, with its real outcome; unknown side effects are listed |
| `coordinator` | delegates to specialists (M7), combines their typed results | `research-brief` |

## Integration

- **Delegation (M7):**
  - Specialists are ordinary delegates.
  - Delegation reports carry the child's validated output and contract status.
  - A delegated run's policy is its parent's overlay plus its own agent's (only narrower).
- **Interfaces:**
  - **SDK:** `templates`, `agents.create_from_template`, `customize`, `upgrade`, `export_template`, `run.output()`.
  - **CLI:** `hollis templates …`, `agents create --template`, `agents upgrade`, `skills list`.
  - **REST:** `/v1/templates`, `/v1/capabilities`, from-template creation, upgrade, export; `output` on run details.
  - **Web:** a template gallery, create-from-template, the agent's origin and overrides, and the run's structured output.
- **Demo model:** runs every specialist's flow offline, with honest, minimal results.

## Evaluation

The question: does specialization improve results, and what does it cost? Run on the real platform, offline, with deterministic simulated models (not language models). Conditions:
- generalist: generic tools and instructions, no contract;
- generalist with the specialist's tools;
- the specialist.

Two kinds of measurement:
- **Assumption-free:** tokens, calls, time, permission scoping, whether validators catch errors.
- **Under explicit, identical error models:** correctness and evidence quality, with sensitivity analysis.

There is also an opt-in live evaluation with a real model, generalist against specialist on the same tasks, not run here. Specialists are also evaluated as M7 delegates.

## Commits

1. docs: plan and research note.
2. feat(runtime): agent policy overlays (`RunCreated` v7).
3. feat(tools): code, data and docs tools; configured commands; git.
4. feat(specialists): capabilities, templates, contracts, Agent Skills, materialization and overrides.
5. feat(platform): template-made agents, output contracts, readiness, upgrades, delegation reports, demo flows.
6. feat(server,sdk,cli): templates in every interface.
7. feat(web): template gallery, create-from-template, outputs.
8. test(evals): specialist evaluation.
9. docs: ADRs, guides, references, examples.
