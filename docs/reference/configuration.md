# Configuration reference: `hollis.toml`

The workspace configuration. It is safe to commit: credentials appear only as environment-variable names. Changes made through the CLI, SDK, API or web app are validated before they are saved; the previous file is kept as `hollis.toml.bak` (comments in a hand-edited file survive only in the backup). Unknown keys are errors. `hollis config check` validates the file.

## `[workspace]`

| Key | Default | Meaning |
|---|---|---|
| `name` | `"hollis"` | Display name |

## `[server]`

| Key | Default | Meaning |
|---|---|---|
| `host` | `"127.0.0.1"` | Bind address for `hollis serve`. Loopback by default |
| `port` | `7878` | |
| `session_hours` | `12` | Browser session lifetime |
| `run_visibility` | `"owner"` | `owner`: a run is visible to its creator and admins. `workspace`: to every principal (control stays with the creator and admins) |
| `allowed_hosts` | `[]` | Host names accepted besides loopback and the bind address (behind a proxy). `"*"` accepts any; use only when a proxy validates `Host` |

Authentication cannot be turned off.

## `[runtime]`

| Key | Default | Meaning |
|---|---|---|
| `environment` | `"local"` | A label policy rules can match, e.g. `production` |
| `max_output_tokens` | `1024` | Output budget per model call for agents without their own |
| `default_agent` | none (`hollis init` sets `"assistant"`) | The agent a run uses when it names none |

## `demo`

`demo = true` (default) offers the built-in demo model as deployment `demo`: deterministic rules, not a language model. Agents that use it fail to start runs when it is off.

The deployment id `workflow` is always reserved for the built-in plan coordinator (deterministic code, not a model; [ADR-0030](../decisions/0030-orchestration-on-durable-runs.md)). Agents cannot use it; plans run on it.

## `[[deployments]]`

Model deployments, the gateway catalog ([ADR-0011](../decisions/0011-model-gateway-and-deployments.md)). One table per deployment:

```toml
[[deployments]]
id = "local-qwen"
protocol = "openai-chat-completions"     # or anthropic-messages, openai-embeddings
provider = "ollama"                      # a label
model = "qwen3:8b"                       # the provider's model name
locality = "local"                       # local | private | external
endpoint = { base_url = "http://127.0.0.1:11434/v1" }
capabilities = { text = { tools = true, context_window_tokens = 32768 } }

[[deployments]]
id = "gpt"
protocol = "openai-chat-completions"
provider = "openai"
model = "gpt-4.1"
locality = "external"
endpoint = { base_url = "https://api.openai.com/v1", auth = { type = "bearer", env = "OPENAI_API_KEY" } }
```

`auth` is `{ type = "none" }` (default), `{ type = "bearer", env = "VAR" }` or `{ type = "header", header = "x-api-key", env = "VAR" }`. TLS (`tls = { ca_bundle = "…", client_cert = "…", client_key = "…" }`), explicit proxies and extra headers are described in [ADR-0013](../decisions/0013-locality-routing-and-on-prem.md). `hollis models list` shows whether each deployment is ready.

## `[routing]`

`allowed_localities = ["local", "private"]` confines runs to those deployments. Default: all.

## `[tools]`

```toml
[tools.filesystem]        # root-scoped file tools: fs_*, code_*, data_*, docs_check (and apply_patch)
root = "."                # relative to the workspace
writable = true           # false offers only the read tools (no fs_write_file, no apply_patch)

[tools]
memory_forget = false     # offer the destructive memory_forget tool to agents

[tools.git]               # read-only git_status, git_diff, git_log for the filesystem root
binary = "/usr/bin/git"   # absolute path; the root must itself be a repository

[[tools.commands]]        # a fixed command an agent may run: no shell, not a sandbox
name = "run_tests"
description = "Run the unit tests."
argv = ["/usr/bin/python3", "-m", "pytest", "-q", "{path}"]
parameters = { path = { description = "Test path", default = "tests", pattern = "[A-Za-z0-9_./-]+" } }
effect = "read_only"      # read_only | mutating (default) | destructive
idempotent = false
timeout_s = 120
cwd = "."                 # relative to the workspace
env = { PYTHONDONTWRITEBYTECODE = "1" }   # set for the command (PATH and LANG are fixed)
inherit_env = []          # names of Hollis's own variables to pass on (they can appear in output)

[[tools.mcp]]             # an MCP server, connected when a run needs it
name = "files"
transport = { type = "stdio", command = "/usr/local/bin/mcp-files", args = ["--root", "."] }
tools = { read = { effect = "read_only" }, write = { effect = "mutating" } }   # the allowlist
```

What the built-in capability tools do and refuse ([ADR-0036](../decisions/0036-capability-tools-and-protected-paths.md)):
- **File, code, data and docs tools** never read or write `.git` or `.hollis` (any letter case, also through links), and never write `hollis.toml` at any depth, the skills directory or the templates directory. Writes never pass through a symbolic link and may not contain `..`.
- **`data_query`** runs one SELECT over in-memory copies of the named CSV/TSV/JSON/JSONL files: no writes, PRAGMA, ATTACH or extensions; values up to 1 MB; 5 s per query; 200 rows shown.
- **Configured commands:** `{name}` argv elements are filled with whole parameter values, which never start with `-` and must match the whole `pattern`. A command may not take a built-in tool's name or a `delegate_`, `submit_`, `mcp_` or `skill_` prefix.
- **Git tools** refuse to run in a repository whose configuration (includes followed) defines filter drivers or `core.worktree`, and never run hooks, fsmonitor, external diff or textconv drivers. They read the repository's own configuration: use them for repositories whose `.git/config` you trust.

Commands and git run as the Hollis user, with its access: isolation `process`, not a sandbox.

Only allowlisted MCP tools are offered. Each declares its effect (`read_only`, `mutating`, `destructive`; default `destructive`, the fail-safe), and optionally `idempotent`, `timeout_s` and a definition `pin`. Hollis never takes these from the server's own annotations. The other transport is `{ type = "streamable_http", endpoint = { base_url = "…" } }`. Built-in artifact, knowledge and memory tools are always present. See [ADR-0017](../decisions/0017-mcp-integration.md).

## `[[policy.rules]]`

Deterministic tool policy, first match wins ([ADR-0015](../decisions/0015-tool-definitions-policy-and-approval.md)):

```toml
[[policy.rules]]
decision = "allow"                 # allow | require_approval | deny
tools = "knowledge_*"              # glob over tool names
effects = []                       # e.g. ["read_only"]; empty: any
trust = []                         # e.g. ["trusted"]; empty: any
environments = []                  # e.g. ["production"]; empty: any
delegated = true                   # optional: true matches only runs another agent delegated
name = "knowledge-read"
reason = "read-only knowledge tools"
```

Without a matching rule: trusted read-only tools run; everything else waits for approval. Delegate tools (`delegate_<agent>`) declare the strongest effect the delegated agent can reach, so delegating to a read-only agent runs and delegating to one that can change things waits for approval.

## `[orchestration]`

Limits on agents delegating to agents ([ADR-0031](../decisions/0031-orchestration-authority-and-limits.md), [guide](../guides/orchestration.md)). An agent's own `delegation` limits can only narrow these.

| Key | Default | Meaning |
|---|---|---|
| `max_depth` | `2` | Delegation levels below the root (0–5) |
| `max_children` | `8` | Delegations per run |
| `max_runs` | `24` | Runs per orchestration, the root included |
| `token_budget` | none | Provider-reported tokens for the whole orchestration. Delegated work stops at 75% of it; calls in flight reserve their output budget and estimated input |
| `wait_s` | `900` | How long a run waits for one delegate (at most 3500; also capped by its deadline) |

## `[specialists]`

Where a workspace keeps Agent Skills and its own agent templates ([ADR-0033](../decisions/0033-specialization-templates-and-skills.md), [guide](../guides/specialists.md)). Both are relative to the workspace; file tools never write them.

| Key | Default | Meaning |
|---|---|---|
| `skills_dir` | `"skills"` | `<name>/SKILL.md` folders offered to agents by name and read with `skill_read`; scripts are never executed |
| `templates_dir` | `"agent-templates"` | `*.toml` templates (for example exported with `hollis agents export`); first-party ids cannot be reused |

## `[knowledge]`

| Key | Default | Meaning |
|---|---|---|
| `embedder` | `"local-hashing"` | `local-hashing` (built in: surface similarity, not meaning) or the id of an `openai-embeddings` deployment |
| `sync_roots` | `["."]` | Workspace directories `knowledge sync` may read |

Changing the embedder of existing collections needs a migration ([ADR-0022](../decisions/0022-embedding-task.md)).
