# Research note: agent specialization (M8)

Read on 2026-11-01. This note covers what makes a specialized agent better than a general one, and how current systems package specializations. Figures are as reported by their authors.

## Findings

- **Fewer, better-chosen tools help, but tools alone are not the bottleneck.**
  - *How Many Tools Should an LLM Agent See?* ([arXiv 2605.24660](https://arxiv.org/abs/2605.24660)) found that a short tool list raised selection accuracy (one reported case: 87.1% to 93.1%), and that a fixed shortlist of five tools covered more ToolBench tasks than exposing all of them.
  - InfoMosaic-Bench ([arXiv 2510.02271](https://arxiv.org/abs/2510.02271)) found that domain-specific tools gave only marginal gains: the bottleneck is how tools are *used*, not whether they exist.
  - Implication: a specialization must bring procedure and verification along with its tools, and keep the tool set focused.
- **Structured results come from validated output tools.**
  - Agent frameworks ask the model to call an output tool whose arguments are validated against a schema, and feed validation errors back for another attempt within a retry budget (Pydantic AI's `ToolOutput`, [docs](https://ai.pydantic.dev/api/output/)).
  - This works with any tool-calling model, and needs no provider-specific structured-output feature.
- **Reusable instructions are converging on Agent Skills.** The [Agent Skills specification](https://agentskills.io/specification), an open format from Anthropic adopted by several agent products:
  - **Format:** a folder with `SKILL.md`, whose YAML frontmatter has a required `name` (≤ 64 lowercase characters, matching the folder) and `description` (≤ 1024), and optional `license`, `compatibility`, `metadata` and an experimental `allowed-tools`. The Markdown body holds the instructions, and `scripts/`, `references/` and `assets/` are optional.
  - **Loading:** skills load progressively. Names and descriptions are loaded at start (about 100 tokens each), the body on activation (under 5,000 tokens recommended), and resources on demand.
- **Specialist selection matters.** Routing each case to the right specialist yields large gains, and random routing often hurts (for example, specialist selection in medical imaging, cited in the tool-count literature). Delegation descriptions must say plainly what each specialist is for.

## Implications for Hollis

| Evidence | Design consequence |
|---|---|
| Focused tool sets help; tool *use* is the bottleneck | Templates select a small set of capability tools and pair them with procedures; capability tools return results that are easy to use correctly (line numbers, exact query results with ids, lint findings with locations) |
| Validated output tools are the reliable route to structure | Output contracts are submission tools with schema validation, plus validation against the run's own log: numbers come from queries that ran, files and lines exist, cited evidence was seen. Retries go through the normal tool loop |
| Agent Skills is the emerging format for reusable instructions | Workspaces can hold `skills/<name>/SKILL.md`; agents see names and descriptions and read bodies and references on demand. Hollis executes no skill scripts (there is no sandbox) and never treats `allowed-tools` as a grant (permissions never come from content) |
| Delegation needs clear specialist descriptions | Templates carry delegate-facing descriptions, and their typed results flow into M7 delegation reports |
| Specialization must be shown, not assumed | The evaluation separates what tools, contracts and instructions each contribute, against generalist baselines, and states its assumptions |

## Not adopted

- **A marketplace or "virtual employees."** Templates are versioned files that can be shared like code.
- **Executing skill scripts.** That would be code execution without a sandbox ([ADR-0016](../decisions/0016-execution-isolation.md)).
- **Separate engines per agent type.** Every specialist is an ordinary agent on the same runtime.
