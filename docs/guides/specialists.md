# Specialized agents

A specialist is an ordinary Hollis agent made from a **template**. The template gives it four things:
- a small set of capability tools;
- a procedure;
- policy rules that can only make it *more* restricted than the workspace;
- an **output contract**: the structured result Hollis checks against what the run actually did.

Runs, approvals, delegation and the interfaces work exactly as for any agent ([ADR-0033](../decisions/0033-specialization-templates-and-skills.md)).

**Specialization is not free, and it is not a correctness guarantee.** In Hollis's [evaluation](../architecture/specialist-evaluation.md) (a simulated agent, not a language model), specialists:
- used 2.3–4.2× the tokens of a generalist;
- caught every error a contract can detect, turning silent errors into visible failures, and into correct results when the agent acted on the feedback;
- let errors of judgement through, in every condition.

## Templates

| Template | Does | Output contract (checked against the run's log) |
|---|---|---|
| `software-engineer` | investigates code, changes it with patches (each needs approval), runs the configured checks | `change-report`: every changed file was changed in this run; every verification names a command it ran, with its real outcome, after the last change |
| `code-reviewer` | reviews files or the working diff; read-only by policy | `code-review`: every finding points to an existing file and a line the run was shown |
| `researcher` | investigates knowledge collections and keeps contradictions | `research-brief`: every citation is evidence the run received (or a completed delegation whose output was accepted) |
| `data-analyst` | answers from tabular files with exact, read-only SQL | `data-analysis`: every value appears in the result of the query whose id it cites |
| `technical-writer` | documents code; may write only under `docs/` (a parameter) | `docs-change`: every file was written in this run and passed `docs_check` after its last change |
| `automation` | runs a procedure with configured commands, one approved step at a time | `automation-log`: every step names a call with its real outcome; calls with unknown outcome are listed |
| `coordinator` | delegates to specialists and combines their checked results | `research-brief` |

```bash
hollis templates list                    # each template, and whether this workspace can run it
hollis templates show software-engineer  # procedure, requirements, per-model fit, example tasks
hollis agents create engineer -m local-qwen --template software-engineer \
  --param conventions="Use type hints."
hollis run -a engineer "Fix the off-by-one in paginate() and run the tests"
hollis runs show RUN                     # the checked output, then the answer
```

```python
from hollis import Hollis

with Hollis.open() as hollis:
    hollis.agents.create_from_template("data-analyst", "analyst", "local-qwen")
    run = hollis.runs.start("Which region led Q3 revenue?", agent="analyst")
    run.wait()
    output = run.output()  # status: accepted | rejected | missing
    print(output.status, output.data)
```

In the web app: **Templates** shows the gallery. A template's page shows what it needs and which models fit, with a form to create an agent.

### Requirements

Templates declare what they need from the model and the workspace:
- **model:** tool calling, a minimum context window;
- **workspace:** file tools, writes, configured commands, git, collections, delegates.

Hard mismatches block creation, with the reason: no tool calling, a declared context window that is too small, missing tools, no collection for a researcher, no delegates for a coordinator. Soft ones are warnings, for example no configured commands, which leaves the engineer unable to verify. Templates never name a vendor.

## What a contract does, and does not, check

When an agent has a contract, its runs get a `submit_<contract>` tool and the contract's rules in their instructions.
- **Bad arguments:** a submission with malformed arguments is refused like any invalid tool call.
- **Mismatch with the log:** a submission that doesn't match the run's log is rejected with the problems listed, and the model can fix them and submit again within its budget.
- **Accepted:** the accepted submission is the run's **output**. It appears in run details, in the CLI and web app, and in delegation reports to the agent that delegated.

**Accepted means the references were verified:** the files, lines, queries, commands and sources the result names. **It does not mean the result is right.** Errors a contract cannot see:
- a wrong conclusion drawn from real numbers;
- a review finding on a real line that is not actually a defect;
- a made-up statement with a real citation.

Treat the judgement in an accepted output as the agent's.

## Policy overlays

An agent's `policy` holds rules that only tighten the workspace policy ([ADR-0035](../decisions/0035-policy-overlays.md)):
- `require_approval` or `deny`;
- a tool glob and effects;
- optionally `outside`: allowed path globs for an argument.

The code reviewer denies every call that could change something. The technical writer denies writes outside `{docs_dir}/*`. Automation requires approval for every change, even where the workspace would allow it.

**Delegated runs inherit their parent's rules before their own**, so a delegation can never do what the parent could not. A read-only coordinator is even denied delegating to an agent that could write.

## Customizing, upgrading, sharing

- **Customize:** edit the agent like any other, in the web app, `hollis agents update` or `agents.customize`. Template fields you change (description, instructions, tools, contract, policy, skills, budget) are marked customized.
- **Upgrade:** when the template's version or content changes, `hollis agents upgrade NAME` (or the agent's page) previews what changes and what your customizations keep, then applies it. It is refused if the template changed again since the preview. `--reset FIELD` takes a customized field from the template again.
- **Share:** `hollis agents export NAME -o team-review.toml` writes the agent as a template. Copy the file into another workspace's `agent-templates/` and it appears in that workspace's gallery. There is no marketplace; a template is a file you can review like code. First-party template ids cannot be reused.

## Agent Skills

Put skills in `skills/<name>/SKILL.md` folders ([agentskills.io](https://agentskills.io/specification) format) and offer them to an agent (`skills` in its definition, or the checkboxes in the web app). The agent:
- sees each offered skill's name and description in its instructions;
- reads a skill's body, and its `references/` files, with `skill_read`.

`hollis skills list` shows the skills and any folder that could not be loaded.
- Hollis **never executes** a skill's scripts (there is no verified sandbox); `skill_read` shows them as text.
- `allowed-tools` grants nothing.
- Which skills an agent is offered is guidance, not access control: `skill_read` can read any skill in the workspace.

## The capability tools

With `[tools.filesystem]` configured, every workspace has:
- **code tools:** `code_search`, `code_outline`, `code_read`, and `apply_patch` when writable;
- **data tools:** `data_list`, `data_describe`, `data_query`;
- **docs tools:** `docs_check`.

Optionally, `[tools.git]` adds read-only `git_status`, `git_diff` and `git_log`, and `[[tools.commands]]` adds operator-configured commands such as tests, linters and procedures ([configuration](../reference/configuration.md#tools)).

What these tools protect:
- File tools never touch `.git`, `.hollis`, `hollis.toml` or the skills and templates directories, and never write through links.
- Git refuses repositories whose configuration would make it run programs.
- SQL can only read.
- Commands run as the Hollis user, with process isolation. **That is not a sandbox** ([ADR-0036](../decisions/0036-capability-tools-and-protected-paths.md)).

## Specialists as delegates

Specialists are ordinary delegates ([orchestration](orchestration.md)). Their reports carry their output and its status. A coordinator's research brief may cite a delegated run only if that run completed with its output accepted.

**A delegated run gets at most what the delegating run may reach.** So a coordinator needs the knowledge access its researchers will need:

```bash
hollis agents create lead -m local-qwen --template coordinator \
  --delegate researcher --delegate analyst --collection docs
```

## The demo model

On the built-in demo model (rules, not a language model), every template runs a short, fixed procedure and submits a minimal, honest result:
- a review with no findings;
- a change only for `replace "a" with "b" in <path>`;
- a brief quoting the top passages;
- a row count;
- a docs page only for `write <path>.md: <text>`;
- one configured command.

Use it to try the flows. Its outputs say nothing about what a real model would do.
