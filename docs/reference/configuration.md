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
[tools.filesystem]        # root-scoped file tools: fs_list_dir, fs_read_file, fs_write_file
root = "."                # relative to the workspace
writable = true           # false offers only the read tools

[tools]
memory_forget = false     # offer the destructive memory_forget tool to agents

[[tools.mcp]]             # an MCP server, connected when a run needs it
name = "files"
transport = { type = "stdio", command = "/usr/local/bin/mcp-files", args = ["--root", "."] }
tools = { read = { effect = "read_only" }, write = { effect = "mutating" } }   # the allowlist
```

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

## `[knowledge]`

| Key | Default | Meaning |
|---|---|---|
| `embedder` | `"local-hashing"` | `local-hashing` (built in: surface similarity, not meaning) or the id of an `openai-embeddings` deployment |
| `sync_roots` | `["."]` | Workspace directories `knowledge sync` may read |

Changing the embedder of existing collections needs a migration ([ADR-0022](../decisions/0022-embedding-task.md)).
