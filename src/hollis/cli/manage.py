"""Management commands: agents, models, tools, MCP, knowledge, memory, tokens, principals and
configuration."""

import json
from pathlib import Path
from typing import Any

import click

from hollis.cli.context import State, json_option, pass_state
from hollis.cli.render import ago, short_id
from hollis.platform.schemas import AgentInput, PrincipalInput, Role
from hollis.runtime.knowledge import KnowledgeAccess, SourceTrust
from hollis.runtime.memory import MemoryAccess, MemoryKind


def register(cli: click.Group) -> None:
    for group in (
        agents,
        templates,
        skills,
        models,
        tools,
        mcp,
        knowledge,
        memory,
        tokens,
        principals,
        config,
    ):
        cli.add_command(group)


def _out(state: State, value: Any, render: Any = None) -> None:
    if state.console.json or render is None:
        state.console.data(value)
    else:
        render()


def _ok(console: Any) -> str:
    return str(console.style(console.glyph("✓", "+"), "green"))


def _complete_agents(ctx: click.Context, param: click.Parameter, incomplete: str) -> list[str]:
    """Shell completion for agent names (best effort: no output if the workspace can't open)."""
    try:
        from hollis.sdk import Hollis

        with Hollis.from_env() as client:
            return [a.name for a in client.agents.list() if a.name.startswith(incomplete)]
    except Exception:
        return []


# --- agents ---------------------------------------------------------------------------------------


@click.group()
def agents() -> None:
    """Agents: a model, instructions, tools and access, run by name."""


@agents.command("list")
@json_option
@pass_state
def agents_list(state: State) -> None:
    items = state.client().agents.list()
    console = state.console
    _out(
        state,
        items,
        lambda: console.table(
            ["name", "model", "tools", "memory", "knowledge", "updated"],
            [
                [
                    a.name,
                    a.model,
                    str(len(a.tools)),
                    ", ".join(a.memory.write or a.memory.read) if a.memory else "-",
                    ", ".join(a.knowledge.collections) if a.knowledge else "-",
                    ago(a.updated_at),
                ]
                for a in items
            ],
            flex=4,
            empty="No agents yet: `hollis init` creates a starter, or `hollis agents create`.",
        ),
    )


@agents.command("show")
@click.argument("name", shell_complete=_complete_agents)
@json_option
@pass_state
def agents_show(state: State, name: str) -> None:
    agent = state.client().agents.get(name)
    console = state.console

    def render() -> None:
        console.print(
            console.style(agent.name, "bold")
            + (f"  {console.style(agent.description, 'dim')}" if agent.description else "")
        )
        console.fields(
            [
                ("model", agent.model),
                ("tools", ", ".join(agent.tools) or "none"),
                (
                    "memory",
                    f"read {', '.join(agent.memory.read)} · "
                    f"write {', '.join(agent.memory.write) or 'none'}"
                    if agent.memory
                    else "none",
                ),
                (
                    "knowledge",
                    f"{', '.join(agent.knowledge.collections)}"
                    + (
                        f" (labels {', '.join(agent.knowledge.labels)})"
                        if agent.knowledge.labels
                        else ""
                    )
                    if agent.knowledge
                    else "none",
                ),
                (
                    "budget",
                    f"{agent.budget.max_output_tokens} output tokens/turn"
                    if agent.budget
                    else "workspace default",
                ),
                ("episodes", "recorded" if agent.record_episodes else "off"),
                ("contract", agent.contract or "none"),
                (
                    "policy",
                    "; ".join(
                        f"{r.decision.value} {r.name or r.tools}"
                        + (f" outside {', '.join(sorted(r.outside))}" if r.outside else "")
                        for r in agent.policy
                    )
                    or "workspace policy only",
                ),
                ("skills", ", ".join(agent.skills) or "none"),
                (
                    "template",
                    f"{agent.origin.template} {agent.origin.version}"
                    + (
                        f" (customized: {', '.join(agent.origin.overrides)})"
                        if agent.origin.overrides
                        else ""
                    )
                    if agent.origin
                    else "none",
                ),
                ("delegates", ", ".join(agent.delegates) or "none"),
                (
                    "delegation",
                    ", ".join(
                        f"{k.replace('_', ' ')} {v}"
                        for k, v in agent.delegation.model_dump(exclude_none=True).items()
                    )
                    or "workspace limits"
                    if agent.delegation
                    else "workspace limits",
                ),
                (
                    "version",
                    f"{agent.version} · {agent.updated_at:%Y-%m-%d %H:%M} · "
                    f"created by {agent.created_by}",
                ),
            ],
            indent=2,
        )
        if agent.instructions:
            console.print()
            console.print(console.style("  instructions", "dim"))
            for line in agent.instructions.splitlines():
                console.print(f"    {line}")

    _out(state, agent, render)


def _agent_options(command: Any) -> Any:
    options = [
        click.option("--model", "-m", help="Deployment id (see `hollis models list`)."),
        click.option("--description", help="One line on what the agent is for."),
        click.option(
            "--instructions", help="System instructions, or @file to read them from a file."
        ),
        click.option(
            "--tool",
            "tools",
            multiple=True,
            help="A tool it may use (repeatable; see `hollis tools list`).",
        ),
        click.option(
            "--memory",
            "memory_scopes",
            multiple=True,
            help="Memory scope it reads and writes, e.g. team:docs (repeatable).",
        ),
        click.option(
            "--memory-read",
            "memory_read",
            multiple=True,
            help="Memory scope it only reads (repeatable).",
        ),
        click.option(
            "--collection",
            "collections",
            multiple=True,
            help="Knowledge collection it searches (repeatable).",
        ),
        click.option(
            "--label",
            "labels",
            multiple=True,
            help="Knowledge access label, e.g. team:ops (repeatable).",
        ),
        click.option("--max-output-tokens", type=int, help="Output budget per model call."),
        click.option(
            "--delegate",
            "delegates",
            multiple=True,
            help="An agent it may delegate bounded tasks to (repeatable).",
        ),
        click.option(
            "--max-depth",
            type=int,
            help="Delegation depth limit for its runs.",
        ),
        click.option(
            "--token-budget",
            type=int,
            help="Tokens its run and everything it delegates may use together.",
        ),
        click.option(
            "--record-episodes/--no-record-episodes",
            default=None,
            help="Write an episode memory after each run.",
        ),
    ]
    for option in reversed(options):
        command = option(command)
    return command


def _agent_fields(**values: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key in ("model", "description"):
        if values.get(key) is not None:
            fields[key] = values[key]
    if values.get("instructions") is not None:
        text = values["instructions"]
        fields["instructions"] = (
            Path(text[1:]).read_text(encoding="utf-8") if text.startswith("@") else text
        )
    if values.get("tools"):
        fields["tools"] = tuple(values["tools"])
    scopes, read_only = (
        tuple(values.get("memory_scopes") or ()),
        tuple(values.get("memory_read") or ()),
    )
    if scopes or read_only:
        fields["memory"] = MemoryAccess(read=scopes + read_only, write=scopes)
    if values.get("collections"):
        fields["knowledge"] = KnowledgeAccess(
            collections=tuple(values["collections"]), labels=tuple(values.get("labels") or ())
        )
    if values.get("max_output_tokens") is not None:
        fields["budget"] = {"max_output_tokens": values["max_output_tokens"]}
    if values.get("record_episodes") is not None:
        fields["record_episodes"] = values["record_episodes"]
    if values.get("delegates"):
        fields["delegates"] = tuple(values["delegates"])
    limits = {
        key: values[key] for key in ("max_depth", "token_budget") if values.get(key) is not None
    }
    if limits:
        fields["delegation"] = limits
    return fields


@agents.command("create")
@click.argument("name")
@click.option("--template", "-t", help="Make it from a template (see `hollis templates list`).")
@click.option(
    "--param",
    "params",
    multiple=True,
    help="A template parameter, name=value (repeatable).",
)
@_agent_options
@json_option
@pass_state
def agents_create(
    state: State, name: str, template: str | None, params: tuple[str, ...], **values: Any
) -> None:
    """Create an agent (admin), from scratch or from a template.

    With --template, the other options override the template's fields or set the agent's own
    (knowledge, memory, delegates).

    \b
    Example:
      hollis agents create reviewer -m demo --tool fs_read_file --tool knowledge_search \\
        --collection docs --memory team:review --instructions @prompts/reviewer.md
      hollis agents create lead -m demo --delegate researcher --delegate reviewer \\
        --collection docs --token-budget 200000
      hollis agents create writer -m demo --template technical-writer --param docs_dir=manual
    """
    fields = _agent_fields(**values)
    if "model" not in fields:
        raise click.UsageError("--model is required")
    if template is not None:
        parameters: dict[str, str] = {}
        for item in params:
            key, sep, value = item.partition("=")
            if not sep:
                raise click.UsageError(f"--param {item!r}: use name=value")
            parameters[key] = value
        model = fields.pop("model")
        agent = state.client().agents.create_from_template(
            template, name, model, parameters=parameters, **fields
        )
    elif params:
        raise click.UsageError("--param needs --template")
    else:
        agent = state.client().agents.create(AgentInput.model_validate({"name": name, **fields}))
    _out(
        state,
        agent,
        lambda: state.console.print(
            f"Created agent {state.console.style(agent.name, 'bold')} (version {agent.version})."
        ),
    )


@agents.command("update")
@click.argument("name", shell_complete=_complete_agents)
@_agent_options
@json_option
@pass_state
def agents_update(state: State, name: str, **values: Any) -> None:
    """Change an agent (admin): only the options you give change."""
    fields = _agent_fields(**values)
    if not fields:
        raise click.UsageError("nothing to change")
    agent = state.client().agents.update(name, **fields)
    _out(
        state,
        agent,
        lambda: state.console.print(
            f"Updated agent {state.console.style(agent.name, 'bold')} to version {agent.version}."
        ),
    )


@agents.command("delete")
@click.argument("name", shell_complete=_complete_agents)
@click.option("--yes", is_flag=True, help="Do not ask for confirmation.")
@pass_state
def agents_delete(state: State, name: str, yes: bool) -> None:
    """Delete an agent (admin). Its past runs stay."""
    if not yes:
        click.confirm(f"Delete agent {name}?", abort=True)
    state.client().agents.delete(name)
    state.console.note(f"Deleted agent {name}.")


@agents.command("upgrade")
@click.argument("name", shell_complete=_complete_agents)
@click.option(
    "--reset",
    multiple=True,
    help="A customized field to take from the template again (repeatable).",
)
@click.option("--yes", is_flag=True, help="Apply without asking.")
@json_option
@pass_state
def agents_upgrade(state: State, name: str, reset: tuple[str, ...], yes: bool) -> None:
    """Upgrade an agent to its template's current version (admin), after a preview.

    Customized fields are kept unless --reset names them."""
    client = state.client()
    preview = client.agents.upgrade_preview(name)
    console = state.console
    if preview.up_to_date and not reset:
        _out(state, preview, lambda: console.note(f"{name} is up to date ({preview.to_version})."))
        return
    if not console.json:
        console.print(
            f"{console.style(name, 'bold')}: {preview.template} {preview.from_version} -> "
            f"{preview.to_version}"
        )
        for change in preview.changes:
            console.print(f"  changes {change.field}")
        if preview.kept:
            console.print(f"  keeps customized {', '.join(preview.kept)}")
        for check in preview.checks:
            if not check.ok:
                console.warn(f"{check.requirement}: {check.detail}")
    if not yes:
        click.confirm("Apply the upgrade?", abort=True)
    agent = client.agents.upgrade(name, digest=preview.to_digest, reset=reset)
    _out(
        state,
        agent,
        lambda: console.print(f"{_ok(console)} Upgraded {name} to version {agent.version}."),
    )


@agents.command("export")
@click.argument("name", shell_complete=_complete_agents)
@click.option("--id", "template_id", help="Template id (default: the agent's name).")
@click.option("--version", default="1.0.0", show_default=True)
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, writable=True),
    help="Write the template file here (default: print it).",
)
@pass_state
def agents_export(
    state: State, name: str, template_id: str | None, version: str, output: str | None
) -> None:
    """Write an agent as a template file (admin): share it by copying it into another
    workspace's templates directory."""
    exported = state.client().agents.export_template(name, id=template_id, version=version)
    if output is None:
        click.echo(exported.content, nl=False)
        return
    Path(output).write_text(exported.content, encoding="utf-8")
    state.console.note(f"Wrote template {exported.id} {exported.version} to {output}.")


# --- templates and skills ---------------------------------------------------------------------


@click.group()
def templates() -> None:
    """Agent templates: versioned blueprints for specialized agents."""


@templates.command("list")
@json_option
@pass_state
def templates_list(state: State) -> None:
    items = state.client().templates.list()
    console = state.console
    _out(
        state,
        items,
        lambda: console.table(
            ["id", "version", "contract", "ready", "agents", "source"],
            [
                [
                    t.id,
                    t.version,
                    t.contract or "-",
                    "yes"
                    if t.ready
                    else "no: " + ", ".join(c.requirement for c in t.checks if c.hard and not c.ok),
                    ", ".join(t.agents) or "-",
                    t.source,
                ]
                for t in items
            ],
            flex=3,
        ),
    )


@templates.command("show")
@click.argument("template_id")
@json_option
@pass_state
def templates_show(state: State, template_id: str) -> None:
    template = state.client().templates.get(template_id)
    console = state.console

    def render() -> None:
        console.print(console.style(f"{template.name} ({template.id} {template.version})", "bold"))
        console.print(f"  {template.description}")
        console.fields(
            [
                ("contract", template.contract or "none"),
                ("capabilities", ", ".join(template.capabilities) or "none"),
                ("tools", ", ".join(template.tools) or "none"),
                (
                    "parameters",
                    ", ".join(
                        f"{p.name}={p.default!r}"
                        if p.default is not None
                        else f"{p.name} (required)"
                        for p in template.parameters
                    )
                    or "none",
                ),
                ("source", f"{template.source} · {template.digest}"),
            ],
            indent=2,
        )
        console.print()
        for check in template.checks:
            mark = (
                _ok(console) if check.ok else console.style("!" if not check.hard else "x", "red")
            )
            console.print(f"  {mark} {check.requirement}: {check.detail}")
        for fit in template.fits:
            mark = _ok(console) if fit.ok else console.style("x", "red")
            problems = [c.detail for c in fit.checks if not c.ok]
            console.print(
                f"  {mark} model {fit.deployment}"
                + (f": {'; '.join(problems)}" if problems else "")
            )
        if template.examples:
            console.print()
            console.print(console.style("  example tasks", "dim"))
            for example in template.examples:
                console.print(f"    {example}")

    _out(state, template, render)


@templates.command("contracts")
@json_option
@pass_state
def templates_contracts(state: State) -> None:
    """Output contracts and what Hollis checks for each."""
    items = state.client().templates.contracts()
    console = state.console

    def render() -> None:
        for contract in items:
            console.print(console.style(f"{contract.id}", "bold") + f"  {contract.description}")
            for rule in contract.rules:
                console.print(f"  - {rule}")

    _out(state, items, render)


@click.group()
def skills() -> None:
    """Agent Skills in this workspace (SKILL.md folders, offered to agents, never executed)."""


@skills.command("list")
@json_option
@pass_state
def skills_list(state: State) -> None:
    view = state.client().templates.skills()
    console = state.console

    def render() -> None:
        console.table(
            ["name", "description", "files"],
            [[s.name, s.description, str(len(s.files))] for s in view.skills],
            flex=1,
            empty="No skills: add folders with a SKILL.md under the skills directory.",
        )
        for problem in view.problems:
            console.warn(problem)

    _out(state, view, render)


# --- models ---------------------------------------------------------------------------------------


@click.group()
def models() -> None:
    """Model deployments: the built-in demo model and the ones you configure."""


@models.command("list")
@json_option
@pass_state
def models_list(state: State) -> None:
    items = state.client().models.list()
    console = state.console

    def status(view: Any) -> str:
        if view.status == "ready":
            return console.style(console.glyph("●", "+") + " ready", "green")
        return console.style(console.glyph("◆", "!") + " " + view.status.replace("_", " "), "brass")

    _out(
        state,
        items,
        lambda: console.table(
            ["id", "status", "protocol", "model", "where", "tasks"],
            [
                [
                    d.id + (console.style(" (built in)", "dim") if d.builtin else ""),
                    status(d),
                    d.protocol,
                    d.model,
                    d.locality,
                    ", ".join(d.tasks),
                ]
                for d in items
            ],
            flex=3,
        ),
    )


@models.command("show")
@click.argument("deployment")
@json_option
@pass_state
def models_show(state: State, deployment: str) -> None:
    view = state.client().models.get(deployment)
    console = state.console
    _out(
        state,
        view,
        lambda: console.fields(
            [
                ("id", view.id),
                ("status", view.status + (f": {view.status_detail}" if view.status_detail else "")),
                ("provider", view.provider),
                ("protocol", view.protocol),
                ("model", view.model),
                ("endpoint", view.endpoint or "in process"),
                ("locality", view.locality),
                (
                    "credential",
                    f"${view.credential.env} ({'set' if view.credential.present else 'not set'})"
                    if view.credential
                    else "none",
                ),
                ("tasks", ", ".join(view.tasks)),
                ("about", view.description),
            ]
        ),
    )


@models.command("add")
@click.argument("deployment")
@click.option(
    "--protocol",
    required=True,
    type=click.Choice(["openai-chat-completions", "anthropic-messages", "openai-embeddings"]),
)
@click.option("--model", "model_name", required=True, help="The provider's model name.")
@click.option("--base-url", required=True, help="API root, e.g. http://127.0.0.1:11434/v1.")
@click.option("--provider", help="A label for who serves it (default: from the protocol).")
@click.option(
    "--locality",
    type=click.Choice(["local", "private", "external"]),
    default="local",
    show_default=True,
)
@click.option(
    "--auth-env", help="Environment variable holding the API key (the value is never stored)."
)
@click.option("--auth-header", help="Send the key in this header instead of Authorization: Bearer.")
@click.option(
    "--tools/--no-tools", "tool_calling", default=False, help="Declare tool-calling support."
)
@click.option("--context-window", type=int, help="Context window in tokens.")
@click.option("--dimensions", type=int, help="Embedding dimensions (openai-embeddings).")
@json_option
@pass_state
def models_add(
    state: State,
    deployment: str,
    protocol: str,
    model_name: str,
    base_url: str,
    provider: str | None,
    locality: str,
    auth_env: str | None,
    auth_header: str | None,
    tool_calling: bool,
    context_window: int | None,
    dimensions: int | None,
) -> None:
    """Add or replace a deployment in hollis.toml (admin). Credentials stay in your environment."""
    auth: dict[str, Any] = {"type": "none"}
    if auth_env:
        auth = (
            {"type": "header", "header": auth_header, "env": auth_env}
            if auth_header
            else {"type": "bearer", "env": auth_env}
        )
    definition: dict[str, Any] = {
        "id": deployment,
        "protocol": protocol,
        "provider": provider
        or {"anthropic-messages": "anthropic"}.get(protocol, "openai-compatible"),
        "model": model_name,
        "locality": locality,
        "endpoint": {"base_url": base_url, "auth": auth},
    }
    if protocol == "openai-embeddings":
        if dimensions is None:
            raise click.UsageError("--dimensions is required for openai-embeddings")
        definition["capabilities"] = {
            "tasks": ["embedding"],
            "text": None,
            "embedding": {"dimensions": dimensions},
        }
    else:
        text: dict[str, Any] = {"tools": tool_calling}
        if context_window:
            text["context_window_tokens"] = context_window
        definition["capabilities"] = {"text": text}
    view = state.client().models.put(definition)
    console = state.console
    _out(
        state,
        view,
        lambda: console.print(
            f"Saved {console.style(view.id, 'bold')}: {view.status.replace('_', ' ')}"
            + (f" ({view.status_detail})" if view.status_detail else "")
        ),
    )


@models.command("remove")
@click.argument("deployment")
@pass_state
def models_remove(state: State, deployment: str) -> None:
    """Remove a deployment from hollis.toml (admin)."""
    state.client().models.delete(deployment)
    state.console.note(f"Removed {deployment}.")


@models.command("probe")
@click.argument("deployment")
@json_option
@pass_state
def models_probe(state: State, deployment: str) -> int:
    """Send one tiny request to check a deployment works (a real call; it may cost money)."""
    result = state.client().models.probe(deployment)
    console = state.console
    if console.json:
        console.data(result)
    elif result.ok:
        latency = f" in {result.latency_ms:.0f} ms" if result.latency_ms is not None else ""
        console.print(f"{_ok(console)} {deployment} answered{latency}: {result.detail}")
    else:
        console.print(
            f"{console.style(console.glyph('✕', 'x'), 'red')} {deployment}: {result.detail}"
        )
    return 0 if result.ok else 1


# --- tools and MCP --------------------------------------------------------------------------------


@click.group()
def tools() -> None:
    """Tools agents can use, and what the policy decides for each."""


@tools.command("list")
@json_option
@pass_state
def tools_list(state: State) -> None:
    items = state.client().tools.list()
    console = state.console

    def policy(decision: str) -> str:
        styles = {"allow": "green", "require_approval": "brass", "deny": "red"}
        return console.style(decision.replace("_", " "), styles.get(decision, "dim"))

    _out(
        state,
        items,
        lambda: console.table(
            ["tool", "policy", "effect", "trust", "source", "description"],
            [
                [
                    t.name,
                    policy(t.policy),
                    t.effect,
                    t.trust,
                    t.source,
                    t.description.split("\n")[0],
                ]
                for t in items
            ],
            flex=5,
        ),
    )


@tools.command("activity")
@click.option("--limit", default=30, show_default=True)
@json_option
@pass_state
def tools_activity(state: State, limit: int) -> None:
    """Recent tool calls across the runs you can see."""
    items = state.client().tools.activity(limit)
    console = state.console
    _out(
        state,
        items,
        lambda: console.table(
            ["when", "tool", "decision", "outcome", "approver", "run"],
            [
                [
                    ago(a.at),
                    a.tool,
                    (a.decision or "-").replace("_", " "),
                    a.outcome or "-",
                    a.approver or "-",
                    short_id(a.run_id),
                ]
                for a in items
            ],
            empty="No tool calls yet.",
        ),
    )


@click.group()
def mcp() -> None:
    """MCP servers configured in hollis.toml."""


@mcp.command("list")
@json_option
@pass_state
def mcp_list(state: State) -> None:
    items = state.client().tools.mcp_servers()
    console = state.console
    _out(
        state,
        items,
        lambda: console.table(
            ["server", "status", "transport", "target", "tools"],
            [
                [
                    s.name,
                    s.status.replace("_", " ") + (f": {s.error}" if s.error else ""),
                    s.transport,
                    s.target,
                    ", ".join(s.tools) or ", ".join(s.allowlist),
                ]
                for s in items
            ],
            flex=4,
            empty="No MCP servers configured (see docs/reference/configuration.md).",
        ),
    )


@mcp.command("connect")
@click.argument("server")
@json_option
@pass_state
def mcp_connect(state: State, server: str) -> int:
    """Connect to a server now and list its tools (admin)."""
    view = state.client().tools.connect_mcp(server)
    console = state.console
    if console.json:
        console.data(view)
    elif view.status == "connected":
        console.print(f"{_ok(console)} {server}: {', '.join(view.tools) or 'no allowed tools'}")
    else:
        console.print(f"{console.style(console.glyph('✕', 'x'), 'red')} {server}: {view.error}")
    return 0 if view.status == "connected" else 1


# --- knowledge ------------------------------------------------------------------------------------


@click.group()
def knowledge() -> None:
    """Document collections agents search, with citations."""


@knowledge.command("collections")
@json_option
@pass_state
def knowledge_collections(state: State) -> None:
    items = state.client().knowledge.collections()
    console = state.console
    _out(
        state,
        items,
        lambda: console.table(
            ["collection", "sources", "chunks", "embedding", "description"],
            [
                [c.name, str(c.sources), str(c.chunks), c.embedding or "none", c.description]
                for c in items
            ],
            flex=4,
            empty="No collections. Create one with `hollis knowledge create NAME`.",
        ),
    )


@knowledge.command("create")
@click.argument("name")
@click.option("--description", default="")
@click.option("--no-embeddings", is_flag=True, help="Lexical search only.")
@pass_state
def knowledge_create(state: State, name: str, description: str, no_embeddings: bool) -> None:
    """Create a collection (admin)."""
    state.client().knowledge.create_collection(
        name, description=description, embedded=not no_embeddings
    )
    state.console.note(f"Created collection {name}.")


@knowledge.command("drop")
@click.argument("name")
@click.option("--yes", is_flag=True)
@pass_state
def knowledge_drop(state: State, name: str, yes: bool) -> None:
    """Delete a collection and its documents from the index (admin)."""
    if not yes:
        click.confirm(f"Delete collection {name} and all its documents?", abort=True)
    state.client().knowledge.delete_collection(name)
    state.console.note(f"Deleted collection {name}.")


@knowledge.command("add")
@click.argument("collection")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--key", help="Store under this key (one file only; default: the path as given).")
@click.option("--label", "labels", multiple=True, help="Access label, e.g. team:ops (repeatable).")
@click.option(
    "--trust",
    type=click.Choice([t.value for t in SourceTrust]),
    default="internal",
    show_default=True,
)
@json_option
@pass_state
def knowledge_add(
    state: State,
    collection: str,
    files: tuple[str, ...],
    key: str | None,
    labels: tuple[str, ...],
    trust: str,
) -> int:
    """Add or update documents (read from this machine, sent to the workspace)."""
    if key and len(files) > 1:
        raise click.UsageError("--key works with one file")
    client = state.client()
    results: list[Any] = []
    failed = 0
    for file in files:
        name = key or Path(file).as_posix().removeprefix("./")
        result = client.knowledge.ingest_file(
            collection, file, key=name, labels=labels, trust=SourceTrust(trust)
        )
        results.append(result)
        if not state.console.json:
            action = result.action
            mark = (
                state.console.glyph("✓", "+")
                if action in ("created", "updated", "unchanged")
                else state.console.glyph("✕", "x")
            )
            count = result.source.chunks if result.source else 0
            chunks = f" · {count} chunk{'s' if count != 1 else ''}" if result.source else ""
            state.console.print(
                f"  {mark} {name}  "
                + state.console.style(
                    action + chunks + (f" ({result.reason})" if result.reason else ""), "dim"
                )
            )
        failed += result.action not in ("created", "updated", "unchanged")
    if state.console.json:
        state.console.data(results)
    return 1 if failed else 0


@knowledge.command("sync")
@click.argument("collection")
@click.argument("path")
@click.option("--label", "labels", multiple=True)
@click.option(
    "--trust",
    type=click.Choice([t.value for t in SourceTrust]),
    default="internal",
    show_default=True,
)
@json_option
@pass_state
def knowledge_sync(
    state: State, collection: str, path: str, labels: tuple[str, ...], trust: str
) -> None:
    """Mirror a workspace directory into a collection (within knowledge.sync_roots; admin)."""
    report = state.client().knowledge.sync(
        collection, path, labels=labels, trust=SourceTrust(trust)
    )
    console = state.console
    _out(
        state,
        report,
        lambda: console.print(
            f"created {len(report.created)} · updated {len(report.updated)} · "
            f"unchanged {len(report.unchanged)} · deleted {len(report.deleted)} · "
            f"rejected {len(report.rejected)}"
        ),
    )


@knowledge.command("sources")
@click.argument("collection")
@json_option
@pass_state
def knowledge_sources(state: State, collection: str) -> None:
    items = state.client().knowledge.sources(collection)
    console = state.console
    _out(
        state,
        items,
        lambda: console.table(
            ["key", "title", "version", "chunks", "labels", "ingested", "id"],
            [
                [
                    s.key,
                    s.title,
                    str(s.version),
                    str(s.chunks),
                    ", ".join(s.labels) or "-",
                    ago(s.ingested_at),
                    s.source_id,
                ]
                for s in items
            ],
            flex=1,
            empty="No documents in this collection.",
        ),
    )


@knowledge.command("remove")
@click.argument("collection")
@click.argument("key")
@pass_state
def knowledge_remove(state: State, collection: str, key: str) -> None:
    """Remove a document from the index (admin)."""
    state.client().knowledge.delete(collection, key)
    state.console.note(f"Removed {key} from {collection}.")


@knowledge.command("label")
@click.argument("collection")
@click.argument("key")
@click.argument("labels", nargs=-1)
@json_option
@pass_state
def knowledge_label(state: State, collection: str, key: str, labels: tuple[str, ...]) -> None:
    """Replace a document's access labels (none: visible to everyone with the collection)."""
    view = state.client().knowledge.relabel(collection, key, labels)
    _out(
        state, view, lambda: state.console.print(f"{key}: {', '.join(view.labels) or 'no labels'}")
    )


@knowledge.command("search")
@click.argument("query", nargs=-1, required=True)
@click.option("--collection", "collections", multiple=True)
@click.option("--limit", default=5, show_default=True)
@json_option
@pass_state
def knowledge_search(
    state: State, query: tuple[str, ...], collections: tuple[str, ...], limit: int
) -> None:
    """Search the collections you can read, as an agent would."""
    result = state.client().knowledge.search(" ".join(query), collections=collections, limit=limit)
    console = state.console

    def render() -> None:
        if not result.hits:
            console.print(console.style("No passages found.", "dim"))
        for hit in result.hits:
            p = hit.passage
            console.print(
                console.style(p.key, "brass", "bold")
                + f" {console.style(hit.source_key, 'bold')}  {console.style(p.location, 'dim')}"
            )
            for line in p.text.strip().splitlines()[:4]:
                console.print(f"   {line[:160]}")
            console.print()
        for note in result.notes:
            console.note(note)

    _out(state, result, render)


# --- memory ---------------------------------------------------------------------------------------


@click.group()
def memory() -> None:
    """Governed long-term memory: list, remember, retract, forget."""


@memory.command("list")
@click.option("--scope")
@click.option("--kind", type=click.Choice([k.value for k in MemoryKind]))
@click.option("--status")
@click.option("--query", "-q")
@click.option("--limit", default=50, show_default=True)
@json_option
@pass_state
def memory_list(
    state: State,
    scope: str | None,
    kind: str | None,
    status: str | None,
    query: str | None,
    limit: int,
) -> None:
    items = state.client().memory.list(
        scope=scope,
        kind=MemoryKind(kind) if kind else None,
        status=status,
        query=query,
        limit=limit,
    )
    console = state.console
    _out(
        state,
        items,
        lambda: console.table(
            ["id", "scope", "kind", "status", "content", "recorded"],
            [
                [m.memory_id[:12], m.scope, m.kind.value, m.status, m.content, ago(m.recorded_at)]
                for m in items
            ],
            flex=4,
            empty="No memories you can see.",
        ),
    )


@memory.command("show")
@click.argument("memory_id")
@json_option
@pass_state
def memory_show(state: State, memory_id: str) -> None:
    detail = state.client().memory.get(memory_id)
    console = state.console
    m = detail.memory

    def render() -> None:
        console.print(m.content)
        console.print()
        console.fields(
            [
                ("id", m.memory_id),
                ("scope", m.scope),
                (
                    "kind",
                    m.kind.value
                    + (f" · subject {m.subject}" if m.subject else "")
                    + (f" · key {m.key}" if m.key else ""),
                ),
                ("status", m.status + (f" ({m.status_reason})" if m.status_reason else "")),
                ("source", m.source + (" (external)" if m.external else "")),
                ("certainty", m.certainty.value),
                (
                    "recorded",
                    f"{m.recorded_at:%Y-%m-%d %H:%M}"
                    + (f" · valid until {m.valid_until:%Y-%m-%d}" if m.valid_until else ""),
                ),
                ("recalled", f"{m.recall_count} times"),
            ]
        )
        if detail.history:
            console.print()
            for change in detail.history:
                console.print(
                    console.style(
                        f"  {change.at:%Y-%m-%d %H:%M}  {change.action}  {change.actor}  "
                        f"{change.detail}",
                        "dim",
                    )
                )

    _out(state, detail, render)


@memory.command("remember")
@click.argument("content", nargs=-1, required=True)
@click.option("--scope", required=True, help="e.g. team:docs, project:acme")
@click.option(
    "--kind", type=click.Choice([k.value for k in MemoryKind]), default="fact", show_default=True
)
@click.option("--subject")
@click.option("--key", help="Slot key: a new value supersedes the old one.")
@json_option
@pass_state
def memory_remember(
    state: State,
    content: tuple[str, ...],
    scope: str,
    kind: str,
    subject: str | None,
    key: str | None,
) -> None:
    """Write a memory through the memory policy (as you, confirmed)."""
    result = state.client().memory.remember(
        " ".join(content), scope=scope, kind=MemoryKind(kind), subject=subject, key=key
    )
    _out(
        state,
        result,
        lambda: state.console.print(
            f"{result.action}"
            + (f": {result.memory.memory_id}" if result.memory else "")
            + (f" ({result.reason})" if result.reason else "")
        ),
    )


@memory.command("retract")
@click.argument("memory_id")
@click.option("--reason", required=True)
@pass_state
def memory_retract(state: State, memory_id: str, reason: str) -> None:
    """Stop recalling a memory, keeping it for history."""
    state.client().memory.retract(memory_id, reason=reason)
    state.console.note(f"Retracted {memory_id}.")


@memory.command("forget")
@click.argument("memory_id")
@click.option("--reason", required=True)
@click.option("--yes", is_flag=True)
@pass_state
def memory_forget(state: State, memory_id: str, reason: str, yes: bool) -> None:
    """Delete a memory's content from the memory store. Copies in run logs remain."""
    if not yes:
        click.confirm(
            "Delete this memory's content? (Run event logs keep their copies.)", abort=True
        )
    state.client().memory.forget(memory_id, reason=reason)
    state.console.note(f"Forgot {memory_id}. Run event logs that recalled it still contain it.")


@memory.command("scopes")
@json_option
@pass_state
def memory_scopes(state: State) -> None:
    scopes = state.client().memory.scopes()
    _out(
        state,
        scopes,
        lambda: (
            [state.console.print(s) for s in scopes]
            or state.console.print(state.console.style("No scopes.", "dim"))
        ),
    )


@memory.command("maintain")
@json_option
@pass_state
def memory_maintain(state: State) -> None:
    """Expire memories past their validity (admin)."""
    report = state.client().memory.maintain()
    _out(
        state,
        report,
        lambda: state.console.print(f"expired {report.expired} · deleted {report.deleted}"),
    )


# --- tokens and principals ------------------------------------------------------------------------


@click.group()
def tokens() -> None:
    """API tokens for the SDK, CLI and REST API."""


@tokens.command("list")
@json_option
@pass_state
def tokens_list(state: State) -> None:
    items = state.client().tokens.list()
    console = state.console
    _out(
        state,
        items,
        lambda: console.table(
            ["id", "principal", "label", "created", "last used", "expires"],
            [
                [
                    t.token_id,
                    t.principal,
                    t.label or "-",
                    ago(t.created_at),
                    ago(t.last_used_at) or "never",
                    f"{t.expires_at:%Y-%m-%d}" if t.expires_at else "never",
                ]
                for t in items
            ],
            empty="No API tokens.",
        ),
    )


@tokens.command("create")
@click.option("--principal", help="For another principal (admins).")
@click.option("--label", default="", help="What it is for, e.g. ci or laptop.")
@click.option("--expires-days", type=float, help="Expire after this many days.")
@json_option
@pass_state
def tokens_create(
    state: State, principal: str | None, label: str, expires_days: float | None
) -> None:
    """Create a token. The value is shown once; store it in a secret manager or HOLLIS_TOKEN."""
    token = state.client().tokens.create(principal, label=label, expires_in_days=expires_days)
    console = state.console
    if console.json:
        console.data(token)
        return
    console.print(token.token)
    console.note(f"Token {token.token_id} for {token.principal}. It is not shown again.")


@tokens.command("revoke")
@click.argument("token_id")
@pass_state
def tokens_revoke(state: State, token_id: str) -> None:
    state.client().tokens.revoke(token_id)
    state.console.note(f"Revoked {token_id}.")


@click.group()
def principals() -> None:
    """People and services that can sign in (admin)."""


@principals.command("list")
@json_option
@pass_state
def principals_list(state: State) -> None:
    items = state.client().principals.list()
    console = state.console
    _out(
        state,
        items,
        lambda: console.table(
            ["name", "role", "approve", "memory", "knowledge"],
            [
                [
                    p.name,
                    p.role.value,
                    "yes" if p.can_approve else "no",
                    "all"
                    if p.role is Role.ADMIN
                    else (", ".join(p.memory.read) if p.memory else "-"),
                    "all"
                    if p.role is Role.ADMIN
                    else (", ".join(p.knowledge.collections) if p.knowledge else "-"),
                ]
                for p in items
            ],
        ),
    )


@principals.command("create")
@click.argument("name")
@click.option(
    "--role", type=click.Choice([r.value for r in Role]), default="operator", show_default=True
)
@click.option("--can-approve", is_flag=True, help="May approve tool calls in runs they can see.")
@click.option(
    "--memory",
    "memory_scopes",
    multiple=True,
    help="Memory scope they read and write (repeatable).",
)
@click.option("--memory-read", multiple=True, help="Memory scope they only read (repeatable).")
@click.option(
    "--collection",
    "collections",
    multiple=True,
    help="Knowledge collection they read (repeatable).",
)
@click.option("--label", "labels", multiple=True, help="Knowledge access label (repeatable).")
@json_option
@pass_state
def principals_create(
    state: State,
    name: str,
    role: str,
    can_approve: bool,
    memory_scopes: tuple[str, ...],
    memory_read: tuple[str, ...],
    collections: tuple[str, ...],
    labels: tuple[str, ...],
) -> None:
    """Create a principal; then `hollis tokens create --principal NAME` gives them a token."""
    spec = PrincipalInput(
        name=name,
        role=Role(role),
        can_approve=can_approve,
        memory=MemoryAccess(read=memory_scopes + memory_read, write=memory_scopes)
        if memory_scopes or memory_read
        else None,
        knowledge=KnowledgeAccess(collections=collections, labels=labels) if collections else None,
    )
    view = state.client().principals.create(spec)
    _out(
        state,
        view,
        lambda: state.console.print(
            f"Created {view.role.value} {state.console.style(view.name, 'bold')}."
        ),
    )


# --- config ---------------------------------------------------------------------------------------


@click.group()
def config() -> None:
    """The workspace configuration (hollis.toml)."""


@config.command("show")
@json_option
@pass_state
def config_show(state: State) -> None:
    """The effective configuration (credentials appear only as variable names)."""
    settings = state.client().settings()
    if state.console.json:
        state.console.data(settings.config)
        return
    state.console.note(settings.config_path)
    state.console.print(json.dumps(settings.config, indent=2))


@config.command("path")
@pass_state
def config_path(state: State) -> None:
    state.console.print(str(state.root() / "hollis.toml"))


@config.command("check")
@pass_state
def config_check(state: State) -> None:
    """Validate hollis.toml without opening the workspace."""
    from hollis.platform.config import load_config

    loaded = load_config(state.root())
    state.console.print(
        f"{_ok(state.console)} valid: {len(loaded.deployments)} deployments, "
        f"{len(loaded.policy.rules)} policy rules, {len(loaded.tools.mcp)} MCP servers"
    )
