"""Run commands: `run`, `runs …`, `approvals …`, `artifacts …`."""

import asyncio
import contextlib
import json
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import click

from hollis.cli.context import EXIT_RUN, State, json_option, pass_state
from hollis.cli.render import Console, ago, duration, short_id, tokens
from hollis.platform.errors import InvalidRequest, NotFound
from hollis.platform.schemas import (
    Approval,
    EventView,
    OrchestrationView,
    OutputView,
    Plan,
    RunDetail,
    RunPhase,
    RunSummary,
)

if TYPE_CHECKING:
    from hollis.sdk import AsyncHollis, Hollis

_QUIET_EVENTS = {
    "run_created",
    "memory_recalled",
    "knowledge_retrieved",
    "context_assembled",
    "model_call_started",
    "tool_execution_started",
    "answer_grounded",
}
_FINAL_EVENTS = {"run_completed", "run_failed", "run_cancelled"}


def register(cli: click.Group) -> None:
    cli.add_command(run_command)
    cli.add_command(runs)
    cli.add_command(approvals)
    cli.add_command(artifacts)


# --- rendering ------------------------------------------------------------------------------------


def render_event(console: Console, event: EventView, *, verbose: bool) -> None:
    if event.type in _FINAL_EVENTS:
        return
    quiet = event.type in _QUIET_EVENTS
    if quiet and not verbose:
        return
    payload = event.payload
    glyph, style = console.glyph("▸", ">"), ""
    if quiet:
        glyph, style = console.glyph("·", "."), "dim"
    elif event.type == "tool_call_decided":
        decision = str(payload.get("decision", ""))
        if decision == "require_approval":
            glyph, style = console.glyph("◆", "!"), "brass"
        elif decision in ("deny", "invalid"):
            glyph, style = console.glyph("✕", "x"), "red"
    elif event.type == "tool_approval_resolved":
        approved = bool(payload.get("approved"))
        glyph = console.glyph("✓", "+") if approved else console.glyph("✕", "x")
        style = "green" if approved else "red"
    elif event.type == "tool_result_recorded":
        ok = payload.get("outcome") == "success"
        glyph, style = (
            (console.glyph("✓", "+"), "green") if ok else (console.glyph("✕", "x"), "red")
        )
    elif event.type == "model_call_failed":
        glyph, style = console.glyph("◆", "!"), "brass"
    text = console.style(event.summary, "dim") if quiet else event.summary
    console.print(f"  {console.style(glyph, style) if style else glyph} {text}")


def run_header(console: Console, summary: RunSummary) -> None:
    ring, _ = console.mark()
    who = f"agent {summary.agent}" if summary.agent else "ad hoc"
    console.print(
        f"{ring} {console.style('run ' + short_id(summary.run_id), 'bold')}  "
        + console.style(f"{who} · {summary.model}", "dim")
    )


def run_footer(console: Console, summary: RunSummary) -> None:
    usage = summary.usage
    parts = [
        console.phase(summary.phase),
        duration(summary.duration_s),
        f"{summary.turns} turn{'s' if summary.turns != 1 else ''}",
        f"{summary.tool_calls} tool call{'s' if summary.tool_calls != 1 else ''}",
        f"{tokens(usage.input_tokens)} in / {tokens(usage.output_tokens)} out",
    ]
    console.print("  ".join([parts[0], console.style(" · ".join(parts[1:]), "dim")]))
    if summary.error:
        console.print(f"  {console.style(summary.error, 'red')}")


def call_label(invocation_id: str) -> str:
    """`<run>/turn/0/call/1` as `turn 0 · call 1`."""
    parts = invocation_id.split("/")
    if len(parts) == 5 and parts[1] == "turn" and parts[3] == "call":
        return f"turn {parts[2]} · call {parts[4]}"
    return invocation_id


def show_approval(console: Console, approval: Approval) -> None:
    glyph = console.style(console.glyph("◆", "!"), "brass")
    console.print(
        f"  {glyph} {console.style(approval.tool, 'bold')} needs approval  "
        + console.style(approval.reason, "dim")
    )
    console.print(
        console.style(
            f"      {approval.effect} · {approval.trust} · {call_label(approval.invocation_id)}",
            "dim",
        )
    )
    arguments = approval.arguments
    if isinstance(arguments, dict):
        for key, value in arguments.items():
            if value is None:
                continue
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            lines = str(text).splitlines() or [""]
            shown = lines[:8]
            console.print(f"      {console.style(key, 'dim')}: {shown[0]}")
            for line in shown[1:]:
                console.print(f"      {' ' * len(key)}  {line}")
            if len(lines) > 8:
                console.print(console.style(f"      … {len(lines) - 8} more lines", "dim"))
    else:
        console.print(f"      {json.dumps(arguments, ensure_ascii=False)}")


# --- following a run ------------------------------------------------------------------------------


async def follow_until_rest(
    aio: "AsyncHollis",
    run_id: UUID,
    after: int,
    on_event: Callable[[EventView], None],
    timeout_s: float,
) -> tuple[RunSummary, int]:
    """Print events live while waiting for the run to come to rest (finished, waiting for
    approval or stopped). Returns its summary and the last event shown."""
    handle = await aio.runs.get(run_id)
    last = after

    async def consume() -> None:
        nonlocal last
        async for event in handle.stream(after=last):
            last = event.sequence
            on_event(event)

    stream = asyncio.create_task(consume())
    rest = asyncio.create_task(handle.wait(timeout_s))
    done, _ = await asyncio.wait({stream, rest}, return_when=asyncio.FIRST_COMPLETED)
    if stream in done:
        rest.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rest
        stream.result()
        summary = await handle.refresh()
    else:
        summary = rest.result()
        if summary.phase in (RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CANCELLED):
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(stream), timeout=5)
        stream.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stream
        for event in await handle.events(after=last):  # anything the stream had not delivered
            last = event.sequence
            on_event(event)
    return summary, last


def follow(
    state: State,
    client: "Hollis",
    run_id: UUID,
    *,
    approve: str,
    timeout_s: float,
    verbose: bool,
) -> RunSummary:
    """Follow a run to its end, asking for approvals at the terminal when allowed."""
    console = state.console
    last = 0
    interactive = approve == "ask" and sys.stdin.isatty() and not console.json
    while True:
        summary, last = client.run_coroutine(
            follow_until_rest(
                client.aio,
                run_id,
                last,
                (lambda e: render_event(console, e, verbose=verbose))
                if not console.json
                else (lambda e: None),
                timeout_s,
            )
        )
        if summary.phase is not RunPhase.WAITING_APPROVAL or approve == "stop" or console.json:
            return summary
        handle = client.runs.get(run_id)
        pending = handle.approvals()
        if not pending:
            return summary
        for approval in pending:
            show_approval(console, approval)
            if approve == "deny":
                handle.deny(approval.invocation_id, reason="denied by --approve deny")
                continue
            if not interactive:
                return summary
            me = client.whoami()
            if not me.can_approve:
                console.print(
                    console.style(f"      {me.name} cannot approve; ask someone who can.", "dim")
                )
                return summary
            if click.confirm("      Approve?", default=False):
                handle.approve(approval.invocation_id, reason="approved at the terminal")
            else:
                reason = click.prompt("      Reason (optional)", default="", show_default=False)
                handle.deny(approval.invocation_id, reason=reason or "denied at the terminal")
        # Every pending call is decided: follow the run again.


# --- hollis run -----------------------------------------------------------------------------------


@click.command("run")
@click.argument("task", nargs=-1, required=True)
@click.option("--agent", "-a", help="Agent to run (default: the workspace's default agent).")
@click.option("--model", "-m", help="Run a model directly instead of an agent (admins).")
@click.option(
    "--tool", "tools", multiple=True, help="With --model: a tool the run may use (repeatable)."
)
@click.option("--instructions", help="With --model: system instructions (or @file).")
@click.option(
    "--plan",
    "plan_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Run a fixed plan of delegations (TOML or JSON file) on the workflow coordinator.",
)
@click.option(
    "--session", "-s", help="Conversation name: runs in a session share a private memory scope."
)
@click.option(
    "--approve",
    type=click.Choice(["ask", "deny", "stop"]),
    default="ask",
    show_default=True,
    help="Tool calls needing approval: ask at the terminal, deny them, or stop (leave waiting).",
)
@click.option("--detach", is_flag=True, help="Start the run and return at once (needs a server).")
@click.option(
    "--timeout", "timeout_s", type=float, default=600, show_default=True, help="Seconds to wait."
)
@click.option("--verbose", "-v", is_flag=True, help="Show every event.")
@json_option
@pass_state
def run_command(
    state: State,
    task: tuple[str, ...],
    agent: str | None,
    model: str | None,
    tools: tuple[str, ...],
    instructions: str | None,
    plan_file: Path | None,
    session: str | None,
    approve: str,
    detach: bool,
    timeout_s: float,
    verbose: bool,
) -> int:
    """Run an agent on TASK and follow it to the end.

    \b
    Examples:
      hollis run "summarize docs/handbook.md"
      hollis run -a reviewer --session pr-42 "what changed since yesterday?"
      hollis run --approve stop "write notes.txt: done"   # leave approvals for later
      hollis run -a lead "@researcher find X and @analyst check Y"   # demo model delegates
      hollis run --plan release.toml "Prepare the release brief"
    """
    client = state.client()
    if detach and state.embedded:
        raise click.UsageError(
            "--detach needs a server: an embedded run stops when this command exits. "
            "Start `hollis serve` in this workspace (commands then use it automatically)."
        )
    text = " ".join(task)
    handle = client.runs.start(
        text,
        agent=agent,
        model=model,
        tools=tools,
        instructions=_text_or_file(instructions),
        session=session,
        plan=read_plan(plan_file) if plan_file is not None else None,
    )
    console = state.console
    if detach:
        if console.json:
            console.data(handle.summary)
        else:
            console.print(str(handle.id))
        return 0
    if not console.json:
        run_header(console, handle.summary)
    try:
        summary = follow(
            state, client, handle.id, approve=approve, timeout_s=timeout_s, verbose=verbose
        )
    except KeyboardInterrupt:
        console.note(
            "\nStopped following. A server keeps running it; otherwise resume it with "
            f"`hollis runs resume {handle.id}`."
        )
        return 130
    detail = client.runs.get(handle.id).detail()
    if console.json:
        console.data(detail)
        return EXIT_RUN[detail.summary.phase]
    run_footer(console, summary)
    if summary.phase is RunPhase.WAITING_APPROVAL:
        console.print(
            console.style(
                "  Waiting for approval: hollis approvals list · "
                f"hollis approvals approve {short_id(handle.id)}",
                "dim",
            )
        )
    render_output(console, detail.output)
    if detail.answer:
        console.print()
        console.print(detail.answer)
    return EXIT_RUN[summary.phase]


def read_plan(path: Path) -> Plan:
    """A plan from TOML (`[[steps]]` tables) or JSON."""
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix == ".json":
            return Plan.model_validate_json(text)
        return Plan.model_validate(tomllib.loads(text))
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        raise click.BadParameter(f"{path}: {exc}", param_hint="--plan") from exc


def _text_or_file(value: str | None) -> str | None:
    if value is None or not value.startswith("@"):
        return value
    with open(value[1:], encoding="utf-8") as handle:
        return handle.read()


# --- hollis runs ----------------------------------------------------------------------------------


@click.group()
def runs() -> None:
    """List, inspect, follow, cancel and resume runs."""


def _resolve(client: "Hollis", run: str) -> UUID:
    """A run id, or a unique prefix of one among recent runs."""
    try:
        return UUID(run)
    except ValueError:
        pass
    matches = [r.run_id for r in client.runs.list(limit=200).items if str(r.run_id).startswith(run)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise NotFound(f"no run matching {run!r}")
    raise InvalidRequest(f"{run!r} matches {len(matches)} runs; give more characters")


@runs.command("list")
@click.option("--phase", type=click.Choice([p.value for p in RunPhase]))
@click.option("--agent")
@click.option("--roots", is_flag=True, help="Leave out runs another run delegated.")
@click.option("--limit", default=20, show_default=True)
@json_option
@pass_state
def runs_list(state: State, phase: str | None, agent: str | None, roots: bool, limit: int) -> None:
    """Recent runs, newest first."""
    page = state.client().runs.list(
        phase=RunPhase(phase) if phase else None, agent=agent, roots=roots, limit=limit
    )
    console = state.console
    if console.json:
        console.data(page)
        return
    console.table(
        ["run", "phase", "agent", "input", "tools", "updated"],
        [
            [
                ("  " * r.depth) + short_id(r.run_id),
                console.phase(r.phase),
                (r.agent or ("plan" if r.model == "workflow" else "-"))
                + (f" +{r.children}" if r.children else ""),
                r.objective or r.input_preview,
                str(r.tool_calls),
                ago(r.updated_at),
            ]
            for r in page.items
        ],
        flex=3,
        empty='No runs yet. Start one with `hollis run "..."`.',
    )
    if page.total > len(page.items):
        console.note(f"{len(page.items)} of {page.total} runs; use --limit for more.")


@runs.command("show")
@click.argument("run")
@json_option
@pass_state
def runs_show(state: State, run: str) -> None:
    """A run in detail: turns, tool calls, approvals, the answer."""
    client = state.client()
    detail = client.runs.get(_resolve(client, run)).detail()
    if state.console.json:
        state.console.data(detail)
        return
    show_detail(state.console, detail)


def show_detail(console: Console, detail: RunDetail) -> None:
    s = detail.summary
    run_header(console, s)
    console.fields(
        [
            ("phase", console.phase(s.phase)),
            (
                "created",
                f"{s.created_at:%Y-%m-%d %H:%M:%S} by {s.created_by or '?'}"
                + (f" · session {s.session}" if s.session else ""),
            ),
            ("input", detail.input if len(detail.input) < 300 else detail.input[:300] + "…"),
            (
                "delegated by",
                " > ".join(
                    f"{step.agent or 'run'} {short_id(step.run_id)}" for step in detail.lineage
                ),
            ),
            (
                "delegated",
                f"{s.children} runs (hollis runs tree {short_id(s.run_id)})" if s.children else "",
            ),
            ("model", f"{s.model} ({s.provider}, {s.protocol})"),
            ("tools", ", ".join(detail.tools) or "none"),
            ("memory", ", ".join(detail.memory_access.read) if detail.memory_access else ""),
            (
                "knowledge",
                ", ".join(detail.knowledge_access.collections) if detail.knowledge_access else "",
            ),
            (
                "usage",
                f"{tokens(s.usage.input_tokens)} in / {tokens(s.usage.output_tokens)} out · "
                f"{duration(s.duration_s)}",
            ),
        ],
        indent=2,
    )
    for turn in detail.turns:
        console.print()
        call = turn.model_call
        stop = f" · {call.stop_reason}" if call.stop_reason else ""
        console.print(
            f"  {console.style(f'turn {turn.index}', 'bold')}{console.style(stop, 'dim')}"
        )
        for attempt in call.attempts:
            if attempt.outcome == "failed":
                console.print(
                    console.style(
                        f"    attempt {attempt.attempt} failed: {attempt.failure_kind} "
                        f"{attempt.message or ''}",
                        "brass",
                    )
                )
        for tool in turn.tool_calls:
            approval = ""
            if tool.approved is not None:
                approval = f" · {'approved' if tool.approved else 'denied'} by {tool.approver}"
            console.print(
                f"    {console.style(tool.tool, 'bold')} "
                + console.style(tool.status + approval, "dim")
            )
            if tool.result:
                preview = tool.result.strip().splitlines()[:3]
                for line in preview:
                    console.print(console.style(f"      {line[:160]}", "dim"))
    if detail.failure:
        console.print()
        console.print(
            console.style(f"  failed ({detail.failure.reason}): {detail.failure.message}", "red")
        )
    if detail.cancellation:
        console.print()
        console.print(
            console.style(
                f"  cancelled by {detail.cancellation.by}: "
                f"{detail.cancellation.reason or 'no reason given'}",
                "dim",
            )
        )
    render_output(console, detail.output)
    if detail.answer:
        console.print()
        console.print(detail.answer)


def render_output(console: Console, output: OutputView | None) -> None:
    """A run's structured output under its contract, and whether Hollis's checks accepted it."""
    if output is None:
        return
    console.print()
    if output.status == "accepted":
        console.print(
            console.style(f"  output ({output.contract}): accepted", "green")
            + console.style(" (checked against this run's log)", "dim")
        )
        text = json.dumps(output.data, indent=2, ensure_ascii=False)
        for line in text.splitlines()[:60]:
            console.print(f"    {line}")
    elif output.status == "rejected":
        console.print(
            console.style(
                f"  output ({output.contract}): rejected after {output.attempts} attempts", "red"
            )
        )
        for problem in output.problems[:10]:
            console.print(console.style(f"    - {problem}", "dim"))
    else:
        console.print(console.style(f"  output ({output.contract}): none submitted", "dim"))


@runs.command("tree")
@click.argument("run")
@json_option
@pass_state
def runs_tree(state: State, run: str) -> None:
    """The orchestration a run belongs to: who delegated what, outcomes, usage, refusals."""
    client = state.client()
    tree = client.runs.get(_resolve(client, run)).tree()
    if state.console.json:
        state.console.data(tree)
        return
    show_tree(state.console, tree)


def show_tree(console: Console, tree: OrchestrationView) -> None:
    t = tree.totals
    console.print(
        console.style(f"orchestration {short_id(tree.root_run_id)}", "bold")
        + console.style(
            f"  {t.runs} runs · depth {t.max_depth} · "
            f"{tokens(t.input_tokens + t.output_tokens)} tokens "
            f"(coordination {tokens(t.coordinator_tokens)}) · "
            f"{duration(t.wall_s)} wall, {duration(t.agent_s)} agent time",
            "dim",
        )
    )
    for node in tree.nodes:
        s = node.run
        indent = "  " + "   " * s.depth + ("└─ " if s.depth else "")
        name = s.agent or ("plan" if s.model == "workflow" else "run")
        parts = [
            console.phase(s.phase),
            f"{tokens(s.usage.input_tokens + s.usage.output_tokens)} tok",
        ]
        report = node.report
        if report is not None and report.verification is not None and report.verification.claims:
            v = report.verification
            parts.append(f"{v.supported}/{v.claims} supported")
        if report is not None and report.side_effects_unknown:
            parts.append("unknown side effects: " + ", ".join(report.side_effects_unknown))
        if node.waiting_on:
            parts.append(f"waiting on {node.waiting_on}")
        line = f"{indent}{console.style(name, 'bold')} {short_id(s.run_id)}  " + " · ".join(parts)
        console.print(line)
        if s.objective:
            console.print(console.style(f"{indent}   {s.objective[:120]}", "dim"))
        if report is not None and report.outcome != "completed" and report.reason:
            console.print(
                console.style(f"{indent}   {report.outcome}: {report.reason[:160]}", "brass")
            )
        if node.narrowed:
            console.print(
                console.style(f"{indent}   not granted: {', '.join(node.narrowed)}", "dim")
            )
    for refusal in tree.refusals:
        console.print(
            console.style(
                f"  refused: {refusal.agent} (from {short_id(refusal.parent_run_id)}): "
                f"{refusal.reason}",
                "brass",
            )
        )
    for item in tree.disagreements:
        answers = "; ".join(item.answers)
        console.print(console.style(f"  disagreement on {item.objective!r}: {answers}", "brass"))
    if tree.hidden:
        console.note(f"{tree.hidden} runs in this orchestration are not visible to you.")


@runs.command("events")
@click.argument("run")
@click.option(
    "--after", default=0, show_default=True, help="Only events after this sequence number."
)
@json_option
@pass_state
def runs_events(state: State, run: str, after: int) -> None:
    """The run's event log, one line per event."""
    client = state.client()
    events = client.runs.get(_resolve(client, run)).events(after)
    console = state.console
    if console.json:
        console.data(events)
        return
    for event in events:
        sequence = console.style(str(event.sequence).rjust(4), "dim")
        kind = console.style(event.type.ljust(24), "dim")
        console.print(f"{sequence}  {event.occurred_at:%H:%M:%S}  {kind}  {event.summary}")


@runs.command("follow")
@click.argument("run")
@click.option(
    "--approve", type=click.Choice(["ask", "deny", "stop"]), default="ask", show_default=True
)
@click.option("--timeout", "timeout_s", type=float, default=600, show_default=True)
@click.option("--verbose", "-v", is_flag=True)
@json_option
@pass_state
def runs_follow(state: State, run: str, approve: str, timeout_s: float, verbose: bool) -> int:
    """Follow a run live (JSON Lines with --json)."""
    client = state.client()
    run_id = _resolve(client, run)
    console = state.console
    if console.json:
        for event in client.runs.get(run_id).stream():
            console.line(event)
        summary = client.runs.get(run_id).summary
        console.line({"end": summary})
        return EXIT_RUN[summary.phase]
    run_header(console, client.runs.get(run_id).summary)
    summary = follow(state, client, run_id, approve=approve, timeout_s=timeout_s, verbose=verbose)
    run_footer(console, summary)
    return EXIT_RUN[summary.phase]


@runs.command("wait")
@click.argument("run")
@click.option("--timeout", "timeout_s", type=float, default=600, show_default=True)
@json_option
@pass_state
def runs_wait(state: State, run: str, timeout_s: float) -> int:
    """Wait until a run finishes or needs approval; the exit code tells which."""
    client = state.client()
    summary = client.runs.get(_resolve(client, run)).wait(timeout_s)
    if state.console.json:
        state.console.data(summary)
    else:
        run_footer(state.console, summary)
    return EXIT_RUN[summary.phase]


@runs.command("cancel")
@click.argument("run")
@click.option("--reason", default="", help="Recorded with the cancellation.")
@json_option
@pass_state
def runs_cancel(state: State, run: str, reason: str) -> None:
    """Stop a run for good. Tool calls in progress are recorded as interrupted."""
    client = state.client()
    summary = client.runs.get(_resolve(client, run)).cancel(reason)
    _summary_out(state, summary)


@runs.command("resume")
@click.argument("run")
@click.option("--no-wait", is_flag=True, help="Return at once (needs a server).")
@json_option
@pass_state
def runs_resume(state: State, run: str, no_wait: bool) -> int:
    """Resume an interrupted run from its event log."""
    client = state.client()
    handle = client.runs.get(_resolve(client, run))
    handle.resume()
    summary = handle.summary if no_wait and not state.embedded else handle.wait()
    _summary_out(state, summary)
    return EXIT_RUN[summary.phase]


def _summary_out(state: State, summary: RunSummary) -> None:
    if state.console.json:
        state.console.data(summary)
    else:
        run_footer(state.console, summary)


# --- hollis approvals -----------------------------------------------------------------------------


@click.group()
def approvals() -> None:
    """Tool calls waiting for a decision."""


@approvals.command("list")
@json_option
@pass_state
def approvals_list(state: State) -> None:
    """Pending approvals you can see."""
    pending = state.client().approvals.list()
    console = state.console
    if console.json:
        console.data(pending)
        return
    if not pending:
        console.print(console.style("Nothing is waiting for approval.", "dim"))
        return
    for approval in pending:
        run = console.style("run " + short_id(approval.run_id), "bold")
        context = console.style(f"{approval.agent or 'ad hoc'}  {approval.run_input[:80]}", "dim")
        console.print(f"{run}  {context}")
        show_approval(console, approval)
        console.print()
    console.note("Decide with: hollis approvals approve RUN [INVOCATION]  (or deny)")


def _decide(
    state: State,
    run: str,
    invocation: str | None,
    *,
    approved: bool,
    reason: str | None,
    wait: bool,
) -> int:
    client = state.client()
    handle = client.runs.get(_resolve(client, run))
    if invocation is not None and "/" not in invocation:
        matches = [
            a.invocation_id
            for a in handle.approvals()
            if a.invocation_id.endswith(f"/call/{invocation}")
        ]
        invocation = matches[0] if len(matches) == 1 else invocation
    if approved:
        handle.approve(invocation, reason=reason)
    else:
        handle.deny(invocation, reason=reason)
    summary = handle.wait() if wait or state.embedded else handle.summary
    _summary_out(state, summary)
    return EXIT_RUN[summary.phase]


@approvals.command("approve")
@click.argument("run")
@click.argument("invocation", required=False)
@click.option("--reason", help="Recorded with your decision.")
@click.option(
    "--no-wait", is_flag=True, help="Do not wait for the run to continue (needs a server)."
)
@json_option
@pass_state
def approvals_approve(
    state: State, run: str, invocation: str | None, reason: str | None, no_wait: bool
) -> int:
    """Approve a pending tool call (the only one, or INVOCATION: an id or call index)."""
    return _decide(state, run, invocation, approved=True, reason=reason, wait=not no_wait)


@approvals.command("deny")
@click.argument("run")
@click.argument("invocation", required=False)
@click.option("--reason", help="Recorded with your decision.")
@click.option(
    "--no-wait", is_flag=True, help="Do not wait for the run to continue (needs a server)."
)
@json_option
@pass_state
def approvals_deny(
    state: State, run: str, invocation: str | None, reason: str | None, no_wait: bool
) -> int:
    """Deny a pending tool call; the agent is told and continues without it."""
    return _decide(state, run, invocation, approved=False, reason=reason, wait=not no_wait)


# --- hollis artifacts -----------------------------------------------------------------------------


@click.group()
def artifacts() -> None:
    """Large tool results stored outside the conversation."""


@artifacts.command("show")
@click.argument("run")
@click.argument("artifact")
@click.option("--offset", default=0, show_default=True)
@click.option("--length", default=20_000, show_default=True)
@json_option
@pass_state
def artifacts_show(state: State, run: str, artifact: str, offset: int, length: int) -> None:
    """Print (part of) an artifact a run stored."""
    client = state.client()
    view = client.runs.get(_resolve(client, run)).artifact(artifact, offset=offset, length=length)
    if state.console.json:
        state.console.data(view)
        return
    state.console.print(view.text)
    if view.truncated:
        state.console.note(
            f"… {view.chars - offset - len(view.text)} more characters; "
            f"use --offset {offset + len(view.text)}"
        )
