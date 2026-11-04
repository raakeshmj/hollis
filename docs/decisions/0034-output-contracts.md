# ADR-0034: Output contracts checked against the run's own log

- Status: Accepted (M8)
- Date: 2026-11-03

## Context

Specialists should return structured results that people and other agents can rely on. Asking a model for JSON gives structure but not truth: a model can report a file it never changed, a test it never ran, a number no query produced, or a citation it never saw. Research found that the reliable route to structure is an output tool with validated arguments, with errors fed back for another try. It needs no provider feature, so every protocol supports it. Model output is untrusted input, and child text never supplies evidence ([ADR-0030](0030-orchestration-on-durable-runs.md)).

## Decision

**A contract is a schema plus checks against recorded facts.** An agent with a `contract` gets a `submit_<contract>` tool, which is read-only and trusted. Its arguments are:
1. validated by the contract's schema. Malformed arguments are an invalid tool call, fed back like any other;
2. checked against `RunFacts`. These are built only from the run's log (finished invocations, their recorded arguments, outcomes and results, and the evidence the run received), plus the filesystem for whether a cited file and line exist.

The checks per contract:
- **change-report**: every listed file was changed by a successful patch or write in this run; every verification names a call of this run, with its recorded outcome, made after the last change.
- **code-review**: every finding points to an existing file and line the run was shown.
- **research-brief**: every citation is evidence the run received, or a delegated run that completed with its output accepted.
- **data-analysis**: every value appears in the recorded result of the query whose id it cites.
- **docs-change**: every file was written in this run and passed `docs_check` after its last change.
- **automation-log**: every step names a call with its real outcome; every acting call is logged; exactly the calls with unknown outcome are listed.

References use the call ids the model sees, never Hollis invocation ids.

**Problems go back to the model.** A rejected submission returns the problems (neutralized: no forged evidence handles), and the model can fix them and submit again within its budget. The accepted submission is recorded as a JSON part. The run's `output` (accepted, rejected or missing) is derived from the log, so it survives restarts and needs no new event type. Delegation reports carry the child's output and status.

**Accepted means the references were verified, not that the result is right.** The interfaces say so.

## Alternatives considered

- **Provider structured-output modes.** Not available on every protocol, and they validate shape only.
- **Validating the final answer's text.** Fragile parsing, and no channel for feedback.
- **Trusting the model's account of its own tool use.** That is exactly what the checks exist to replace.
- **A new event for outputs.** Unnecessary: the tool result is already durable and in the fold.

## Trade-offs

- Gains: detectable misreports cannot pass silently (the [evaluation](../architecture/specialist-evaluation.md) caught all of them), and with feedback they become corrected results; outputs compose across delegation.
- Costs:
  - extra tokens and a submission turn on every run;
  - errors of judgement pass the checks;
  - fixing a caught error can let an uncaught error in the same output through (measured: the silent-error rate rose with the fix rate).

## Consequences

- `hollis.specialists.contracts` holds the schemas and checks; `hollis.platform.outputs` holds the tool and the output view.
- `RunState.evidence()` is the single definition of evidence a run received (grounding uses it too).
- Tests cover each contract's checks (honest and misreported cases), the retry loop through the runner, delegation reports, and the evaluation's detection rates.
