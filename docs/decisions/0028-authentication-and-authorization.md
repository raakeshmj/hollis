# ADR-0028: Authentication and authorization

- Status: Accepted (M6)
- Date: 2026-11-01

## Context

Once Hollis listens on a socket, anyone who can reach it could run tools with the operator's privileges. Network services must not be unauthenticated by default; identifiers must not be enough to act on a resource; the web app must not approve beyond its user's authority; secrets must never be exposed (NFR-4, FR-18).

## Decision

**Principals** have a name, a role and grants:
- `admin` (everything), `operator` (runs agents, manages their own runs and memory they may write), `viewer` (reads what they may see).
- `can_approve`: may decide tool calls in runs they can see.
- Memory scopes (read, write) and knowledge collections and labels. Admins hold all grants.

**Authentication is always required** except `GET /v1/ping`:
- **API tokens** (`hx_…`, 256 random bits) for the SDK, CLI and scripts; stored as SHA-256 hashes, shown once, optionally expiring, revocable.
- **Single-use login tokens** (`hxl_…`, ten minutes) in sign-in links. The token travels in the URL fragment, which browsers never send to servers; the app removes it from the address bar and exchanges it once.
- **Browser sessions**: an HttpOnly, SameSite=Strict, path-wide cookie whose value is stored only as a hash. State-changing cookie requests must carry this server's `Origin`; no CORS headers are sent.
- **Embedded use** (SDK or CLI opening the workspace directly) acts as the local admin: a process that can open `.hollis/` already holds everything in it.

**Network exposure.** The server binds to `127.0.0.1` by default; other addresses print a warning that traffic is unencrypted. Requests whose `Host` is not loopback, the bind address or a configured `server.allowed_hosts` entry are refused (DNS rebinding). Every response carries a strict Content-Security-Policy (no inline or third-party scripts), `nosniff`, `frame-ancestors 'none'`, `no-referrer` and `Cache-Control: no-store` for the API.

**Authorization lives in the platform services**, so every interface inherits it:
- Runs are visible to their creator and admins, or to every principal with `server.run_visibility = "workspace"`; only the creator or an admin cancels or resumes.
- Agents are the unit of delegation: operators run agents admins define; ad-hoc runs (a model and tools named directly) are admin-only.
- A run's memory and knowledge access is the agent's configuration **intersected** with the caller's grants. A broadly configured agent never widens what a caller reaches.
- Approving needs `can_approve` **and** visibility of the run; the decision goes through `Runner.resolve_approval`, so the M3 binding (exact tool and arguments) and expiry checks apply, and the approver recorded is the authenticated principal.
- Deployment views report whether a credential's environment variable is set, never its value.
- Security-relevant actions are appended to the audit log, which refuses updates and deletes.

## Alternatives considered

- **Unauthenticated localhost:** other local users and websites (via DNS rebinding) could drive tools.
- **Passwords:** a credential database to protect, with no gain for local use; external identity providers are future work.
- **JWTs:** revocation needs server state anyway; opaque hashed tokens are simpler.

## Trade-offs

There is no TLS or external identity provider in M6; network exposure needs a TLS-terminating proxy. The `server.json` marker holds a live token while the server runs, owner-readable only and equivalent to access the file system already grants.

## Consequences

Tests: role and grant checks (mutation-checked), approval identity and visibility, cookie flags and cross-origin refusal, Host allowlist, single-use and expiring tokens, hashes at rest, and no token or session value in any database or response after real use.
