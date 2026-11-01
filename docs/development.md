# Development Process

This applies to everyone working on the repo: humans and coding agents.

## Setup and checks

```bash
make sync        # uv sync --locked: install exact locked deps (incl. dev group)
make check       # format check, lint, pyright strict, pytest (the merge gate, same as CI)
make fix         # apply ruff formatting and autofixes
uv run pytest tests/test_cli.py::test_version_flag_prints_version_and_exits_zero   # single test
```

The web app (`web/`) has its own toolchain, needed only to change it (Node 22.12+):

```bash
make web-install   # npm ci
make web-check     # TypeScript, ESLint (strict accessibility rules), Vitest
make web-build     # writes the committed build to src/hollis/server/static
make web-api       # after API changes: regenerate web/openapi.json and the TypeScript types
make web-e2e       # Playwright journeys in your installed Chrome against a real `hollis serve`
```

Changes to the interface get a visual review in both themes and at phone width before they're committed.

Dependencies are added with `uv add <pkg>` (runtime) or `uv add --dev <pkg>` (dev). Both update `uv.lock`, which is committed. Every new dependency needs a one-line justification in the commit message. A dependency that shapes architecture needs an ADR.

## Workflow

1. **Plan proportionally.**
   - Changes that are complex, high-impact or cross-module start in Plan Mode and get a written plan (`docs/plans/` for milestone-sized work).
   - Small bounded changes go straight to implementation.
   - Re-plan when an approach fails instead of patching around it.
2. **Acceptance criteria first.** Each task states how completion will be verified: which tests, which commands, and what output.
3. **Small, verified commits.**
   - Each commit is one coherent change that passes `make check`.
   - Review the diff before committing.
   - Use conventional prefixes: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`, `ci:`.
   - Work on branches, not `master`.
4. **Report honestly.** State which checks were run and their actual results. Never report a check as passing without running it.

## Decisions

- Read [architecture/overview.md](architecture/overview.md) and the relevant ADRs before changing architecture.
- Write an ADR ([template](decisions/0000-template.md)) when a decision is load-bearing and costly to reverse: a new boundary, a persistence or format choice, a dependency that shapes architecture, or a security model.
- Don't write one for routine implementation choices.
- To change an accepted decision, write a new ADR that supersedes it.
- Update the overview's "Decided vs open" table when a decision lands.

## Enforcing invariants

If a rule matters, encode it as a test, lint rule or CI step. Use prose instructions only for judgment calls. When a rule is broken repeatedly, the fix is to automate it, not to add more instructions.

## Claude Code configuration policy

M0 has only the root `CLAUDE.md` (an index, kept under ~100 lines; hard limit 200). Add other mechanisms only when dogfooding shows a concrete, repeated need:

| Mechanism | Add when |
|---|---|
| Path-scoped rule (`.claude/rules/`) | A module has conventions that don't apply elsewhere (for example, adapters) |
| Skill (`.claude/skills/`) | A multi-step procedure has been performed by hand more than a couple of times |
| Hook (`.claude/settings.json`) | Something must happen *every* time and keeps being missed (for example, formatting) |
| Subagent (`.claude/agents/`) | Side work is genuinely independent and produces noisy output (for example, a broad audit) |

Normal permission prompts stay on. Don't use permission-bypass modes in this repo.
