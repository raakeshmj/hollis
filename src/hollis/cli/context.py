"""CLI state: output settings and the SDK client every command uses.

Which client (all with the same API):
1. `--url` / HOLLIS_URL (+ `--token` / HOLLIS_TOKEN): a Hollis server, as the token's principal.
2. Otherwise the workspace (`--workspace`, HOLLIS_WORKSPACE, or the nearest hollis.toml). If
   `hollis serve` is running for it, commands go through that server (so one process owns the
   stores, ADR-0004); otherwise the workspace is opened in this process.
"""

import json
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from hollis.cli.render import Console
from hollis.platform.config import DATA_DIR, find_workspace
from hollis.platform.errors import NotFound
from hollis.platform.schemas import RunPhase

if TYPE_CHECKING:
    from hollis.platform.services import Platform
    from hollis.sdk import Hollis

SERVER_FILE = "server.json"

# Exit codes (docs/reference/cli.md). Platform errors use PlatformError.exit_code (2-6).
EXIT_RUN = {
    RunPhase.COMPLETED: 0,
    RunPhase.FAILED: 10,
    RunPhase.CANCELLED: 11,
    RunPhase.WAITING_APPROVAL: 12,
    RunPhase.INTERRUPTED: 13,
    RunPhase.QUEUED: 13,
    RunPhase.RUNNING: 14,  # still running when the command stopped waiting
}


@dataclass
class RunningServer:
    url: str
    token: str
    pid: int


@dataclass
class State:
    console: Console
    workspace: str | None
    url: str | None
    token: str | None
    _client: "Hollis | None" = None
    via_server: str | None = None

    def root(self) -> Path:
        root = Path(self.workspace).expanduser() if self.workspace else find_workspace()
        if root is None or not (root / "hollis.toml").is_file():
            raise NotFound("no Hollis workspace here: run `hollis init`, or pass --workspace")
        return root.resolve()

    def client(self) -> "Hollis":
        if self._client is not None:
            return self._client
        from hollis.sdk import Hollis

        if self.url:
            if not self.token:
                raise click.UsageError("--url needs a token: set HOLLIS_TOKEN (or pass --token)")
            self._client = Hollis.connect(self.url, self.token)
        else:
            root = self.root()
            server = running_server(root)
            if server is not None:
                self.via_server = server.url
                self._client = Hollis.connect(server.url, server.token)
            else:
                self._client = Hollis.open(root)
        return self._client

    @property
    def embedded(self) -> bool:
        return self._client is not None and self.url is None and self.via_server is None

    def close(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            client.close()


pass_state = click.make_pass_decorator(State)


def json_option[F: Callable[..., Any]](command: F) -> F:
    """`--json` on a command (the global `hollis --json ...` works too)."""

    def enable(ctx: click.Context, param: click.Parameter, value: bool) -> None:
        state = ctx.find_object(State)
        if value and state is not None:
            state.console.json = True
            state.console.color = False

    return click.option(
        "--json",
        "json_output",
        is_flag=True,
        expose_value=False,
        callback=enable,
        help="Print machine-readable JSON.",
    )(command)


# --- the local server marker ----------------------------------------------------------------------


def server_file(root: Path) -> Path:
    return root / DATA_DIR / SERVER_FILE


def write_server_file(root: Path, *, url: str, token: str, token_id: str) -> None:
    """Written by `hollis serve`: lets CLI commands in this workspace use the server. The token
    grants nothing beyond what reading `.hollis/` already does, and the file is owner-only."""
    path = server_file(root)
    body = {
        "url": url,
        "pid": os.getpid(),
        "token": token,
        "token_id": token_id,
        "started_at": datetime.now(UTC).isoformat(),
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(body, handle)


async def retire_stale_marker(platform: "Platform", root: Path) -> bool:
    """Revoke the token a previous server left in its marker (it stopped without cleaning up,
    for example after a crash). Returns True if a token was revoked."""
    marker = read_server_file(root)
    token_id = marker.get("token_id") if marker else None
    if not isinstance(token_id, str):
        return False
    admin = await platform.local_admin()
    try:
        await platform.revoke_token(admin, token_id)
    except NotFound:
        return False
    return True


def read_server_file(root: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(server_file(root).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None  # pyright: ignore[reportUnknownVariableType]  # parsed JSON


def running_server(root: Path) -> RunningServer | None:
    data = read_server_file(root)
    if data is None:
        return None
    try:
        url, token, pid = str(data["url"]), str(data["token"]), int(data["pid"])
    except (KeyError, TypeError, ValueError):
        return None
    if not _alive(pid) or not _answers(url):
        return None
    return RunningServer(url=url, token=token, pid=pid)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _answers(url: str) -> bool:
    if not url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        return False  # the marker only ever points at this machine
    try:
        with urllib.request.urlopen(f"{url}/v1/ping", timeout=1.5) as response:  # noqa: S310  # loopback URL checked above
            return response.status == 200
    except OSError:
        return False
