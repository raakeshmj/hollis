# CLI reference

```
hollis [--workspace PATH] [--url URL] [--token TOKEN] [--json] [--no-color] COMMAND
```

Commands go through the Python SDK, so they behave exactly like the SDK and the REST API.

**Which workspace or server.** `--url` (or `HOLLIS_URL`, with `HOLLIS_TOKEN`) targets a server as the token's principal. Otherwise the workspace is `--workspace`, `HOLLIS_WORKSPACE`, or the nearest directory with a `hollis.toml`. If `hollis serve` is running for it, commands go through that server automatically; otherwise the workspace is opened in the command's process, as its local admin. Prefer `HOLLIS_TOKEN` to `--token`: it stays out of shell history.

**Output.** Tables and live progress for people; `--json` (globally, or after any command) for scripts, with `runs follow --json` printing JSON Lines. Notes and errors go to stderr. Colors appear only on a terminal and never with `NO_COLOR` set; `FORCE_COLOR` forces them. Glyphs fall back to ASCII when the terminal can't encode Unicode, or with `HOLLIS_ASCII=1` (the mark becomes `o` over `` `-# ``).

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success; for run commands, the run completed |
| 1 | Unexpected error, or a check failed (`doctor`, `models probe`, `mcp connect`) |
| 2 | Invalid request or usage |
| 3 | Not authenticated, or not allowed |
| 4 | Not found (or not visible to you) |
| 5 | Conflict |
| 6 | Unavailable (server unreachable, credential not set) |
| 10 | The run failed |
| 11 | The run was cancelled |
| 12 | The run is waiting for approval |
| 13 | The run is interrupted (nothing is driving it) |
| 14 | The run was still running when the command stopped waiting |
| 130 | Interrupted with Ctrl-C |

With `--json`, errors print `{"error": {"code": …, "detail": …, "status": …}}` on stderr.

## Workspace and server

| Command | |
|---|---|
| `init [PATH] [--name] [--bare]` | Create `hollis.toml` and `.hollis/`, a starter agent `assistant` and a `docs` collection (`--bare`: configuration only). Safe to rerun |
| `status` | Health, you, models, agents, runs waiting for you, recent runs |
| `doctor` | Check Python, installed extras, the workspace, a running server and health; exit 1 when something needs fixing |
| `serve [--host] [--port] [--open] [--log-level]` | Serve the web app and REST API. Prints a single-use sign-in link. Loopback by default; other addresses print a warning |
| `login-link [--open]` | A fresh single-use sign-in link for the running server |
| `whoami` · `audit [--limit]` | Your principal and grants · the audit log (admin) |
| `config show` · `config path` · `config check` | The effective configuration · its path · validate `hollis.toml` |
| `completion bash\|zsh\|fish` | Shell completion: `eval "$(hollis completion zsh)"` |

## Runs

| Command | |
|---|---|
| `run TASK… [-a AGENT] [-s SESSION] [--approve ask\|deny\|stop] [--detach] [--timeout] [-v]` | Start a run and follow it live. `--approve ask` (default) asks at a terminal; `deny` denies held calls; `stop` leaves the run waiting. `--detach` returns at once (needs a server: an embedded run stops when the command exits). Admins can run a model directly: `--model ID --tool NAME… --instructions TEXT\|@file`. `--plan FILE` runs a fixed plan of delegations (TOML `[[steps]]` or JSON) |
| `runs list [--phase] [--agent] [--roots] [--limit]` | Recent runs, delegated runs indented; `--roots` leaves them out |
| `runs tree RUN` | The orchestration a run belongs to: who delegated what, outcomes, citation checks, usage, refusals, disagreements |
| `runs show RUN` | Turns, tool calls, decisions, the checked output (for agents with a contract), the answer. `RUN` may be a unique id prefix |
| `runs events RUN [--after N]` | The event log |
| `runs follow RUN [--approve …]` | Follow live (JSON Lines with `--json`) |
| `runs wait RUN [--timeout]` | Wait until it finishes or needs approval; the exit code says which |
| `runs cancel RUN [--reason]` | Stop for good; calls in progress are recorded as interrupted |
| `runs resume RUN [--no-wait]` | Continue an interrupted run from its log |
| `approvals list` | Pending tool calls you can see |
| `approvals approve RUN [INVOCATION] [--reason] [--no-wait]` | Approve the only pending call, or one by id or call index; then follow the run |
| `approvals deny RUN [INVOCATION] [--reason] [--no-wait]` | Deny; the agent is told and continues |
| `artifacts show RUN ARTIFACT [--offset] [--length]` | Read a stored large tool result |

## Building

| Command | |
|---|---|
| `agents list` · `show NAME` · `delete NAME [--yes]` | |
| `agents create NAME -m MODEL [--tool T]… [--memory SCOPE]… [--memory-read SCOPE]… [--collection C]… [--label L]… [--instructions TEXT\|@file] [--description] [--max-output-tokens N] [--record-episodes] [--delegate AGENT]… [--max-depth N] [--token-budget N]` | Admin. `--delegate` lets it hand tasks to another agent ([orchestration](../guides/orchestration.md)) |
| `agents update NAME [same options]` | Only the options given change; each change is a new version. On an agent made from a template, changed template fields are marked customized |
| `agents create NAME -m MODEL --template ID [--param NAME=VALUE]… [other options]` | Admin. An ordinary agent made from a template ([specialists](../guides/specialists.md)); the other options override the template's fields or set the agent's own (knowledge, memory, delegates). Hard requirement failures refuse with the reason |
| `agents upgrade NAME [--reset FIELD]… [--yes]` | Admin. Preview what the template's current version changes and keeps, confirm, apply (refused if the template changed since the preview) |
| `agents export NAME [--id ID] [--version V] [-o FILE]` | Admin. The agent as a template file to copy into another workspace's `agent-templates/` |
| `templates list` · `templates show ID` · `templates contracts` | Templates with readiness in this workspace · a template's procedure, requirements, per-model fit and example tasks · the output contracts and what Hollis checks for each |
| `skills list` | Agent Skills in this workspace (`skills/<name>/SKILL.md`), and folders that could not be loaded |
| `models list` · `show ID` · `remove ID` | |
| `models add ID --protocol P --model NAME --base-url URL [--locality] [--auth-env VAR] [--auth-header H] [--tools] [--context-window N] [--dimensions N]` | Admin. Stores the variable's name, never the key |
| `models probe ID` | One tiny real request (may cost money) |
| `tools list` · `tools activity` | Tools with the policy's decision · recent tool calls |
| `mcp list` · `mcp connect NAME` | MCP servers · connect now (admin) |
| `knowledge collections` · `create NAME` · `drop NAME` | |
| `knowledge add COLLECTION FILE… [--key] [--label L]… [--trust]` | Read files on this machine and ingest them (admin) |
| `knowledge sync COLLECTION PATH [--label]…` | Mirror a workspace directory within `knowledge.sync_roots` (admin) |
| `knowledge sources COLLECTION` · `remove COLLECTION KEY` · `label COLLECTION KEY LABEL…` | |
| `knowledge search QUERY… [--collection C]… [--limit]` | Search as an agent would, with your access |
| `memory list [--scope] [--kind] [--status] [-q]` · `show ID` · `scopes` | |
| `memory remember TEXT… --scope S [--kind] [--subject] [--key]` | Through the memory policy, as you |
| `memory retract ID --reason` · `memory forget ID --reason [--yes]` | Stop recalling · delete the content (run logs keep copies) |
| `memory maintain` | Expire memories past their validity (admin) |
| `tokens list` · `tokens create [--principal] [--label] [--expires-days]` · `tokens revoke ID` | The token value is printed once |
| `principals list` · `principals create NAME [--role] [--can-approve] [--memory S]… [--collection C]… [--label L]…` | Admin |

## Scripting example

```bash
export HOLLIS_URL=http://127.0.0.1:7878 HOLLIS_TOKEN=hx_...
run=$(hollis run --detach --json "summarize release.md" | jq -r .run_id)
hollis runs wait "$run"; status=$?
if [ "$status" -eq 12 ]; then hollis --json approvals list | jq '.[] | {tool, arguments}'; fi
hollis --json runs show "$run" | jq -r .answer
```
