# Research: Harness Engineering and the Development Process

Status: living note. Last reviewed 2026-10-31.

These notes cover how we develop Hollis with coding agents. Some findings also inform how Hollis's own runtime harness and evaluations will work. Verdicts: **now** means adopted in M0, **later** means revisit at the named milestone, and **no** means rejected.

## 1. OpenAI: "Harness engineering" (2026-02-11)

- Source: https://openai.com/index/harness-engineering/
- **Unverified.** The page returned HTTP 403 when fetched on 2026-10-31, so its content has not been reviewed. The principles below come from the M0 brief that cited it, not from the article. Fetch the article and confirm before citing it as evidence.
- Ideas attributed to it: the repository is the source of truth; agent instructions should be a map, not a manual; architecture invariants should be enforced mechanically rather than by prose.
- Relevance: high for how we develop Hollis. The same ideas also appear in the verified Anthropic sources below, so adopting them doesn't depend on this source.
- Verdict: **now**, based on the corroborating sources.

## 2. Google Developers: "The Anatomy of Harness Engineering" (2026-09-09)

- Source: https://developers.googleblog.com/en/the-anatomy-of-harness-engineering-how-to-evaluate-iterate-and-guard-ai-coding-agents/ (Taylor Mullen, Christian Gunderman). Fetched and verified.
- Key findings:
  - End-to-end benchmarks (for example, Terminal-Bench) say *whether* results changed. Behavioral evals say *why*. Use both.
  - Behavioral evals assert discrete actions, such as "called the search tool" or "ran the validator after editing a build file". They check tool calls, not prose. They are written like pytest tests and kept fast enough to run locally.
  - Dogfood first. Add evals once the agent does real work, mainly to catch regressions from prompt, tool-schema or model changes.
  - Loop: pick one recent concrete failure, then match assertion strictness to the task (strict tool milestones for simple tasks, outcome checks or LLM judges for complex ones), then track aggregate pass rates over batches instead of gating PRs on single noisy runs.
- Relevance to Hollis: this drives both our development process and the product's own evaluation design. A Hollis run must record its tool calls and decisions as structured events, or behavioral evals can't be written against it. That's one reason for event-recorded run state ([ADR-0005](../decisions/0005-event-recorded-run-state.md)).
- Verdicts:
  - Structured, assertable trajectories in the runtime: **now** as a design requirement, implemented in M1.
  - Behavioral eval suite: **later**, once an agent runs against a real provider (M2+).
  - Batch pass-rate tracking and end-to-end benchmarks: **later**.
  - Automated prompt-rewriting loops: **later**, and only once there's an eval suite worth optimizing against.

## 3. Anthropic: "Claude Code power user tips" (date not shown on page)

- Source: https://support.claude.com/en/articles/14554000-claude-code-power-user-tips. Fetched and verified. The page shows "updated today" and no fixed date.
- Key findings: giving the agent a way to verify its work is the top lever. Plan before building, and re-plan instead of patching mid-course. Keep a team-shared CLAUDE.md and add a rule after each recurring mistake. Turn workflows repeated daily into skills. Use hooks for formatting and deterministic checks. Check permission allowlists into `settings.json`. Use git worktrees for parallel sessions.
- Relevance: directly shapes [development.md](../development.md) and `CLAUDE.md`.
- Verdicts:
  - Verification-first, Plan Mode, shared CLAUDE.md: **now**.
  - "Add a rule after each mistake": **now**, but limited to recurring, non-obvious mistakes so CLAUDE.md stays short.
  - Skills: **later**, once a procedure has actually repeated.
  - Hooks and permission allowlists: **later**. The user chose no hooks in M0, and default permission prompts stay.
  - Parallel worktree sessions: **later**, once work streams are genuinely independent.

## 4. Anthropic: "Steering Claude Code: skills, hooks, rules, subagents and more" (2026-06-18)

- Source: https://claude.com/resources/articles/steering-claude-code-skills-hooks-rules-subagents-and-more (Michael Segner). Fetched and verified.
- Key findings:
  - Each mechanism trades context cost against reliability. Root CLAUDE.md loads every session, so keep it under 200 lines, give it an owner, and review it like code.
  - Path-scoped rules (`.claude/rules/` with `paths:`) and subdirectory CLAUDE.md files load only when relevant.
  - Skills load only their description until invoked, which suits procedures.
  - Subagents isolate noisy side work and return just a summary.
  - CLAUDE.md, rules and skills are followed *probabilistically*. Anything that must always or never happen belongs in hooks, permissions or managed settings.
  - Rule of thumb: facts go in CLAUDE.md, procedures in skills, file-specific constraints in rules, automation in hooks.
- Relevance: high. This is the basis for keeping CLAUDE.md as an index, and for the stance that critical invariants are enforced by tests and CI rather than prose.
- Verdicts:
  - Short root CLAUDE.md: **now**.
  - Enforcing invariants mechanically: **now**, via `make check` and CI. Import-boundary tests come in M1.
  - Path-scoped rules: **later**, once modules with distinct conventions exist (for example, adapters).
  - Subagents: **later**, and only for genuinely independent, noisy work.

## Synthesis: what Hollis adopts now

1. The repo holds requirements, architecture, ADRs, plans and research. `CLAUDE.md` is a map pointing to them.
2. Every task has acceptance criteria and a deterministic check. `make check` is the one gate, used locally, by agents and in CI.
3. Invariants that matter are enforced by tests or tooling, not by instructions alone.
4. Claude Code mechanisms (skills, rules, hooks, subagents) are added when dogfooding shows a repeated need, not in advance.
5. The Hollis runtime will record trajectories as structured events, so behavioral evaluation is possible once agents exist.
