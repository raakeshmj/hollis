# ADR-0036: Capability tools, protected paths and read-only git

- Status: Accepted (M8)
- Date: 2026-11-03

## Context

Specialists need real abilities:
- code search, outlines and numbered reads, and all-or-nothing patches;
- exact queries over tabular files, and documentation checks;
- operator-configured commands, and read-only git.

Each is a new route to side effects. A file write could change repository metadata (git hooks and configuration run programs), Hollis's own data, or the workspace configuration. A link could make a write land somewhere other than where the approver or an overlay thought. A "read-only" git call can run programs through repository configuration. SQL can do more than read. There is still no verified sandbox ([ADR-0016](0016-execution-isolation.md)).

## Decision

**Protected paths, enforced in the shared path guard.**
- File, code, data and docs tools never read or write `.git` or `.hollis` at any depth. The check is case-insensitive, Unicode-folded, Windows-aware, and applies after following links too.
- No file tool writes `hollis.toml` (or its backup) at any depth.
- Writes never pass through a symbolic link in any component and may not contain `..`.
- The workspace adds the skills and templates directories (they become instructions) to the paths writes may not touch.

**Data tools run one SELECT in an in-memory SQLite database holding copies of the named files.**
- An authorizer allows reading and functions only. It denies `fts3_tokenizer`, `fts5` and `load_extension`; extensions cannot be loaded.
- Values are capped at 1 MB, a progress handler stops long queries, and results are capped in rows and characters.
- Query ids come from the invocation, so contracts can cite them.

**Read-only git, closed against program execution.**
- The root must be a repository, and git does not look above it.
- Arguments: values never start with `-`, revisions match a pattern that can't either, and they follow `--end-of-options`/`--`.
- Hooks, fsmonitor, pager and signature checks are off, and system and global configuration are not read.
- No external diff, textconv or submodule recursion.
- Before every call, the effective repository configuration (includes followed) is listed, and the call is refused if it defines filter drivers or `core.worktree`, which no flag can switch off.
- The repository's own configuration still applies, so these tools are for repositories whose `.git/config` you trust; agents cannot write it.

**Configured commands** (`[[tools.commands]]`) run with fixed argv, no shell, and anchored parameter patterns. They get a scrubbed environment (only named variables inherited), resource limits and timeouts, and are declared mutating by default. They may not shadow a tool or use a platform-generated prefix.

**None of this is a sandbox.** Every process runs as the Hollis user, with isolation `process`, as the tools declare.

## Alternatives considered

- **A generic shell tool.** Unbounded; operators configure fixed commands instead.
- **Running git without repository configuration.** Git always reads it. Refusing the dangerous keys is the honest option.
- **Allowing reads of `.git`.** Remote URLs can hold credentials, and the git tools cover the legitimate need.

## Trade-offs

- Gains: specialists can do real work, and each new route to side effects is closed or declared.
- Costs:
  - repositories that use filter drivers (for example a local Git LFS install) can't use the git tools;
  - writes through links are refused even when harmless;
  - large data files are loaded into memory per query.

## Consequences

Adversarial tests plant programs in repository configuration (external diff, textconv, fsmonitor, hooks, filter drivers including via `include.path`, `core.worktree`) and check they never run; removing each defence fails a test. Further tests cover protected paths in every case variant and through links, writes through linked directories, the SQL surface, configured-command patterns and environment, and name collisions.

The previously reported `git_diff` option injection was not reachable through the tool, because the command tool already refused values starting with `-`. The revision pattern now refuses them as a second layer.
