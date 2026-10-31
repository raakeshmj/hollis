# ADR-0016: Tool execution isolation (no sandbox claimed)

- Status: Accepted (M3)
- Date: 2026-10-31

## Context

Tools range from trusted in-process functions to programs that process untrusted input. OWASP ASI05 (unexpected code execution) and the MCP security guidance both call for sandboxing untrusted execution. Calling an ordinary subprocess or container a "sandbox" would be a false security claim.

## Decision

`ToolDescriptor.isolation` states what separates execution from Hollis:
- **`none`:** in-process. All `@tool` functions and filesystem tools. Trusted extensions; *not* a sandbox. Synchronous functions run in a thread that can't be interrupted on timeout.
- **`process`:** a separate OS process. `CommandTool`, and MCP stdio servers (which are spawned by the SDK):
  - Same user, filesystem and network access as Hollis.
  - `CommandTool` adds: a fixed absolute program and an argv template (no shell; whole-element placeholders); refusal of leading-dash values unless allowed (option injection); a scrubbed environment; no stdin; its own process group killed on timeout or cancellation; an output cap that stops the process; POSIX `RLIMIT_CPU`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE` and `RLIMIT_CORE`.
  - **Not a sandbox.**
- **`sandbox`:** reserved for a backend whose isolation has been verified. **No M3 backend provides it.**

Other rules:
- There is no generic shell or arbitrary-code tool.
- Filesystem tools confine paths to a root: absolute, drive-qualified and NUL paths are refused, symlinks are resolved and final components not followed, and writes are atomic. They document their TOCTOU limitation.

**Options evaluated, all deferred to an execution-backend milestone:**
- Linux namespaces with seccomp (nsjail, bubblewrap).
- gVisor; Firecracker microVMs.
- Rootless containers with dropped capabilities, read-only rootfs and no network.
- macOS `sandbox-exec` (deprecated).
- Windows AppContainer.

Each needs per-platform validation: escape tests, network egress tests, resource exhaustion tests. That can't be done credibly on this branch.

## Alternatives considered

- **Ship a container backend and call it a sandbox:** isolation depends on runtime configuration Hollis can't verify here.
- **Run tools in-process only:** gives no resource controls or process boundary for external programs.

## Trade-offs

- Developers who need to run untrusted code must supply their own isolation today.
- The `process` level gives resource and failure containment, not confidentiality or integrity guarantees.

## Consequences

- **Evidence:** `tests/tools/test_command.py`:
  - Metacharacters stay literal; option injection is refused; the environment is scrubbed.
  - Timeout kills the whole process group (a grandchild is checked); the output cap stops the process; the file-size limit is enforced.
- `tests/tools/test_filesystem.py`: traversal, absolute and NUL paths, symlink escape, size and binary limits.
- Mutation checks cover path containment, the symlink guard and the option-injection guard.
