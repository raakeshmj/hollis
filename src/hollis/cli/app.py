"""The `hollis` command line.

Every command goes through the Python SDK (embedded workspace or server), so the CLI renders
what the platform computes and decides nothing itself. Global options come before the command:
`hollis --json runs list`; most commands also accept `--json` themselves.
"""

import importlib.util
import os
import sys
import webbrowser
from collections.abc import Sequence
from pathlib import Path

import click

from hollis import __version__
from hollis.cli.context import (
    State,
    json_option,
    pass_state,
    read_server_file,
    retire_stale_marker,
    running_server,
    server_file,
    write_server_file,
)
from hollis.cli.render import Console, ago, short_id
from hollis.platform.config import CONFIG_FILE, ConfigError, initialize, load_config
from hollis.platform.errors import PlatformError

HINTS = {
    "unauthenticated": "Set HOLLIS_TOKEN to a valid API token (`hollis tokens create`).",
    "unavailable": "Is the server running? Check the URL, or run `hollis serve`.",
}

EPILOG = """\b
Start here:
  hollis init               create a workspace (demo model, starter agent)
  hollis run "list files"   run the default agent
  hollis serve              the web app and REST API at http://127.0.0.1:7878

Docs: docs/guides/quickstart.md · Reference: docs/reference/cli.md"""


class HollisGroup(click.Group):
    """Lists commands in the order they were added (most used first)."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return list(self.commands)


@click.group(cls=HollisGroup, invoke_without_command=True, epilog=EPILOG)
@click.option(
    "--workspace",
    "-w",
    envvar="HOLLIS_WORKSPACE",
    type=click.Path(file_okay=False),
    help="Workspace directory (default: the nearest with a hollis.toml).",
)
@click.option(
    "--url", envvar="HOLLIS_URL", help="Use a Hollis server instead of a local workspace."
)
@click.option(
    "--token",
    envvar="HOLLIS_TOKEN",
    help="API token for --url (prefer the HOLLIS_TOKEN variable: it stays out of shell history).",
)
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON.")
@click.option("--no-color", is_flag=True, help="No colors (also: NO_COLOR=1).")
@click.version_option(
    __version__, "--version", "-V", prog_name="hollis", message="%(prog)s %(version)s"
)
@click.pass_context
def cli(
    ctx: click.Context,
    workspace: str | None,
    url: str | None,
    token: str | None,
    json_output: bool,
    no_color: bool,
) -> None:
    """Hollis: build, run and supervise agents on your own machine."""
    console = Console.detect(json_output=json_output, no_color=no_color)
    state = State(console=console, workspace=workspace, url=url, token=token)
    ctx.obj = state
    ctx.call_on_close(state.close)
    if ctx.invoked_subcommand is None:
        banner(console)
        console.print()
        console.print(ctx.get_help())


def banner(console: Console, detail: str = "") -> None:
    ring, path = console.mark()
    console.print(f" {ring}")
    title = console.style("hollis", "bold") + console.style(f" {__version__}", "dim")
    console.print(f" {path}  {title}" + (f"  {console.style(detail, 'dim')}" if detail else ""))


def run(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return its exit code (errors are printed, not raised)."""
    args = list(argv) if argv is not None else sys.argv[1:]
    try:
        result = cli.main(args=args, prog_name="hollis", standalone_mode=False)
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.exceptions.Abort:
        click.echo("Aborted.", err=True)
        return 130
    except PlatformError as exc:
        console = Console.detect(json_output="--json" in args, no_color="--no-color" in args)
        console.error(exc.code, exc.detail, HINTS.get(exc.code), status=exc.status)
        return exc.exit_code
    except ConfigError as exc:
        click.echo(f"error: {exc}", err=True)
        return 2
    except KeyboardInterrupt:
        click.echo("Interrupted.", err=True)
        return 130
    return result if isinstance(result, int) else 0


# --- workspace ------------------------------------------------------------------------------------


@cli.command()
@click.argument("path", required=False, type=click.Path(file_okay=False))
@click.option("--name", help="Workspace name (default: the directory name).")
@click.option(
    "--bare", is_flag=True, help="Only the configuration; no starter agent or collection."
)
@pass_state
def init(state: State, path: str | None, name: str | None, bare: bool) -> None:
    """Create a workspace: hollis.toml, the .hollis/ data directory and a starter agent."""
    root = Path(path or ".").resolve()
    created = initialize(root, name)
    console = state.console
    if not created:
        console.note(f"{root / CONFIG_FILE} already exists; leaving it as it is.")
    if not bare:
        state.workspace = str(root)
        agent = state.client().agents.starter()
        if created:
            console.note(f"Created agent {agent.name} and collection docs.")
    if console.json:
        console.data({"workspace": str(root), "created": created})
        return
    banner(console, str(root))
    console.print()
    if created:
        console.print(f"  {console.style('Workspace ready.', 'bold')}  Next:")
        console.print()
        steps = [
            ('hollis run "list the files here"', "run the starter agent (demo model)"),
            ("hollis knowledge add docs README.md", "give it documents to search"),
            ("hollis serve", "open the web app"),
        ]
        width = max(len(cmd) for cmd, _ in steps)
        for cmd, why in steps:
            console.print(f"    {cmd.ljust(width)}   {console.style(why, 'dim')}")
        console.print()
        console.print(
            console.style(
                "  The demo model is deterministic rules, not a language model. Add a real one\n"
                "  with `hollis models add` and point the agent at it.",
                "dim",
            )
        )


@cli.command()
@json_option
@pass_state
def status(state: State) -> None:
    """Workspace overview: models, agents, runs waiting for you."""
    client = state.client()
    health = client.health()
    me = client.whoami()
    agents = client.agents.list()
    recent = client.runs.list(limit=5)
    console = state.console
    if console.json:
        console.data(
            {
                "health": health,
                "principal": me,
                "agents": [a.name for a in agents],
                "recent_runs": recent.items,
                "via_server": state.via_server,
            }
        )
        return
    mode = (
        f"via {state.via_server}"
        if state.via_server
        else ("server " + client.location if state.url else "embedded")
    )
    banner(console, f"{health.workspace} · {mode}")
    console.print()
    ok = (
        console.style(console.glyph("●", "+"), "green")
        if health.status == "ok"
        else console.style(console.glyph("◆", "!"), "brass")
    )
    console.fields(
        [
            ("health", f"{ok} {health.status}"),
            ("you", f"{me.name} ({me.role.value}{', can approve' if me.can_approve else ''})"),
            (
                "models",
                f"{health.deployments} deployments"
                + (
                    f", {health.deployment_problems} need attention"
                    if health.deployment_problems
                    else ""
                )
                + (" · demo model on" if health.demo_model else ""),
            ),
            (
                "agents",
                ", ".join(a.name for a in agents)
                or console.style("none: run `hollis init`", "dim"),
            ),
            (
                "runs",
                f"{health.active_runs} running · {health.pending_approvals} waiting for approval",
            ),
        ],
        indent=2,
    )
    for problem in health.problems:
        console.print(f"  {console.style(console.glyph('◆', '!'), 'brass')} {problem}")
    if recent.items:
        console.print()
        console.table(
            ["run", "phase", "agent", "input", "updated"],
            [
                [
                    short_id(r.run_id),
                    console.phase(r.phase),
                    r.agent or "-",
                    r.input_preview,
                    ago(r.updated_at),
                ]
                for r in recent.items
            ],
            flex=3,
        )


@cli.command()
@json_option
@pass_state
def doctor(state: State) -> None:
    """Check the installation and the workspace, and explain what to fix."""
    console = state.console
    checks: list[tuple[str, bool, str]] = []
    checks.append(("python", sys.version_info >= (3, 12), sys.version.split()[0]))
    for extra, module, purpose in (
        ("server", "fastapi", "hollis serve"),
        ("client", "httpx2", "talking to a server"),
        ("mcp", "mcp", "MCP tool servers"),
        ("pdf", "pypdf", "PDF documents"),
        ("anthropic", "anthropic", "Anthropic models"),
    ):
        present = importlib.util.find_spec(module) is not None
        checks.append(
            (
                f"extra {extra}",
                True,
                ("installed" if present else f"not installed (needed for {purpose})"),
            )
        )
    try:
        root = state.root()
        load_config(root)
        checks.append(("workspace", True, str(root)))
    except (PlatformError, ConfigError) as exc:
        checks.append(("workspace", False, str(exc)))
        root = None
    if root is not None:
        marker = read_server_file(root)
        server = running_server(root)
        if server is not None:
            checks.append(("server", True, f"running at {server.url}"))
        elif marker is not None:
            checks.append(
                ("server", True, f"stale marker {server_file(root)} (server not running)")
            )
        health = state.client().health()
        checks.append(("health", health.status == "ok", health.status))
        for problem in health.problems:
            checks.append(("problem", False, problem))
        if health.demo_model:
            checks.append(
                ("demo model", True, "enabled: deterministic rules, not a language model")
            )
    failed = [c for c in checks if not c[1]]
    if console.json:
        console.data(
            {
                "ok": not failed,
                "checks": [{"check": n, "ok": ok, "detail": d} for n, ok, d in checks],
            }
        )
    else:
        width = max(len(n) for n, _, _ in checks)
        for name, ok, detail in checks:
            mark = (
                console.style(console.glyph("✓", "+"), "green")
                if ok
                else console.style(console.glyph("✕", "x"), "red")
            )
            console.print(f"  {mark} {name.ljust(width)}  {detail}")
    if failed:
        raise click.exceptions.Exit(1)


@cli.command()
@json_option
@pass_state
def whoami(state: State) -> None:
    """The principal you act as."""
    me = state.client().whoami()
    if state.console.json:
        state.console.data(me)
        return
    state.console.fields(
        [
            ("name", me.name),
            ("role", me.role.value),
            ("approve", "yes" if me.can_approve else "no"),
            (
                "memory",
                ", ".join(me.memory.read)
                if me.memory
                else ("all scopes" if me.role.value == "admin" else "none"),
            ),
            (
                "knowledge",
                ", ".join(me.knowledge.collections)
                if me.knowledge
                else ("all collections" if me.role.value == "admin" else "none"),
            ),
        ]
    )


@cli.command()
@click.option("--limit", default=50, show_default=True)
@json_option
@pass_state
def audit(state: State, limit: int) -> None:
    """The workspace audit log (admin)."""
    entries = state.client().audit(limit)
    if state.console.json:
        state.console.data(entries)
        return
    state.console.table(
        ["at", "who", "action", "target", "detail"],
        [
            [e["at"][:19].replace("T", " "), e["principal"], e["action"], e["target"], e["detail"]]
            for e in entries
        ],
        flex=4,
        empty="No audit entries.",
    )


# --- server ---------------------------------------------------------------------------------------


@cli.command()
@click.option("--host", help="Address to listen on (default from hollis.toml: 127.0.0.1).")
@click.option("--port", type=int, help="Port (default from hollis.toml: 7878).")
@click.option("--open", "open_browser", is_flag=True, help="Open the web app (signed in).")
@click.option(
    "--log-level",
    default="warning",
    show_default=True,
    type=click.Choice(["critical", "error", "warning", "info", "debug"]),
)
@pass_state
def serve(
    state: State, host: str | None, port: int | None, open_browser: bool, log_level: str
) -> None:
    """Serve the web app and REST API for this workspace."""
    try:
        import uvicorn

        from hollis.server import create_app
        from hollis.server.app import allowed_hosts
    except ImportError as exc:
        raise click.ClickException(
            "the server needs the server extra: pip install 'hollis[server]'"
        ) from exc
    from hollis.platform.schemas import TokenInput
    from hollis.platform.services import Platform

    root = state.root()
    existing = running_server(root)
    if existing is not None:
        raise click.ClickException(
            f"a server is already running for this workspace at {existing.url}"
        )
    config = load_config(root)
    bind = host or config.server.host
    listen = port or config.server.port
    local = bind in ("127.0.0.1", "localhost", "::1")
    url = f"http://{'127.0.0.1' if not local else bind.replace('::1', '[::1]')}:{listen}"
    console = state.console
    cli_token: dict[str, str] = {}

    async def ready(platform: Platform) -> None:
        await retire_stale_marker(platform, root)  # a previous server that did not stop cleanly
        admin = await platform.local_admin()
        login = await platform.login_token(admin.name)
        token = await platform.create_token(
            admin, TokenInput(principal=admin.name, label="local CLI (this server)")
        )
        cli_token["id"] = token.token_id
        write_server_file(root, url=url, token=token.token, token_id=token.token_id)
        health = await platform.health(admin)
        banner(console, f"{health.workspace} · {root}")
        console.print()
        link = f"{url}/login#token={login}"
        console.fields(
            [
                ("web", url),
                ("sign in", link + console.style("  (single use, 10 minutes)", "dim")),
                ("api", f"{url}/v1" + console.style("  (OpenAPI: /v1/openapi.json)", "dim")),
                (
                    "models",
                    f"{health.deployments} deployments"
                    + (" · demo model on (not a language model)" if health.demo_model else ""),
                ),
            ],
            indent=2,
        )
        console.print()
        if not local:
            console.warn(
                f"listening on {bind}: reachable from the network. Authentication is required, but "
                "traffic is not encrypted; put TLS in front (docs/guides/deployment.md)."
            )
        console.note(
            "  Ctrl-C stops the server. Runs in progress stop safely and resume on the next start."
        )
        console.out.flush()
        if open_browser:
            webbrowser.open(link)

    async def stopping(platform: Platform) -> None:
        if "id" in cli_token:
            admin = await platform.local_admin()
            await platform.revoke_token(admin, cli_token["id"])
        marker = read_server_file(root)
        if marker is not None and marker.get("pid") == os.getpid():
            server_file(root).unlink(missing_ok=True)

    app = create_app(
        root=root,
        on_ready=ready,
        on_stop=stopping,
        hosts=allowed_hosts(bind, config.server.allowed_hosts),
    )
    uvicorn.run(app, host=bind, port=listen, log_level=log_level, ws="none", server_header=False)


@cli.command("login-link")
@click.option("--open", "open_browser", is_flag=True, help="Open it in the browser.")
@pass_state
def login_link(state: State, open_browser: bool) -> None:
    """A fresh single-use sign-in link for the web app of the running server."""
    root = state.root()
    server = running_server(root)
    if server is None:
        raise click.ClickException(
            "no server is running for this workspace: start one with `hollis serve`"
        )
    link = f"{server.url}/login#token={state.client().login_token()}"
    state.console.print(link)
    state.console.note("Single use, valid for 10 minutes; it signs in as you.")
    if open_browser:
        webbrowser.open(link)


@cli.command()
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion(shell: str) -> None:
    """Print a shell completion script, e.g. `eval "$(hollis completion zsh)"`."""
    from click.shell_completion import get_completion_class

    completer = get_completion_class(shell)
    if completer is None:  # pragma: no cover - the choice above lists supported shells
        raise click.ClickException(f"unsupported shell {shell}")
    click.echo(completer(cli, {}, "hollis", "_HOLLIS_COMPLETE").source())


def register() -> None:
    from hollis.cli import manage, runs

    runs.register(cli)
    manage.register(cli)


register()
