# Running a shared server

Hollis is local-first: one process owns a workspace, on one machine. This guide covers running that process for a small team.

## On your own machine (the default)

```bash
hollis serve
```

- Listens on `127.0.0.1:7878`: reachable only from this machine.
- Authentication is always required, even on loopback.
- `Ctrl-C` stops the server. Runs in progress stop safely; the next start resumes them. Runs waiting for approval stay waiting.

## For a team

1. **Create principals and tokens** for people and services (as an admin):

   ```bash
   hollis principals create olive --role operator --can-approve --memory team:ops --collection docs
   hollis tokens create --principal olive --label laptop --expires-days 90
   ```

   Roles: `viewer` reads, `operator` runs agents and manages their own runs, `admin` does everything. Grants limit which memory scopes and knowledge collections someone reaches. A run's access is the agent's configuration intersected with its caller's grants. Hand tokens over through a secret manager; they are shown once.

2. **Put TLS in front.** Hollis does not terminate TLS. Run it on loopback and put a reverse proxy (Caddy, nginx, a corporate gateway) in front, for example with Caddy:

   ```
   hollis.example.internal {
       reverse_proxy 127.0.0.1:7878
   }
   ```

   Configure the public host name so Hollis accepts it:

   ```toml
   [server]
   host = "127.0.0.1"
   allowed_hosts = ["hollis.example.internal"]
   ```

   Behind TLS, browsers keep the session cookie on HTTPS.

3. **Or listen on the network directly** (unencrypted; only on networks you trust):

   ```bash
   hollis serve --host 0.0.0.0
   ```

   Hollis prints a warning. Requests must use a host name it accepts (loopback, the bind address, or `server.allowed_hosts`), which defeats DNS-rebinding attacks.

4. **Decide who sees runs.** By default a run is visible to its creator and admins. `server.run_visibility = "workspace"` shares reading with everyone; only the creator or an admin can cancel or resume.

## Running as a service

Any process supervisor works. A systemd unit, for example:

```ini
[Unit]
Description=Hollis
After=network.target

[Service]
WorkingDirectory=/srv/hollis/acme
ExecStart=/home/hollis/.local/bin/hollis serve
Environment=OPENAI_API_KEY=...        # credentials your deployments reference
KillSignal=SIGINT
Restart=on-failure
User=hollis

[Install]
WantedBy=multi-user.target
```

## Backups and data

Everything is in the workspace directory: `hollis.toml` and `.hollis/*.db` (SQLite). Back up the directory while the server is stopped, or use SQLite's online backup. Databases are not encrypted at rest; protect the directory with file permissions.

While the server runs, `.hollis/server.json` (owner-readable only) holds its address and a token that CLI commands in the workspace use to reach it. The token is revoked when the server stops.

## Limits to know

- One server per workspace, one machine. There is no clustering or high availability.
- No external identity provider, password login or TLS in Hollis itself.
- Tools run with the server's privileges. There is no sandbox ([ADR-0016](../decisions/0016-execution-isolation.md)): keep writes behind approval, and don't give agents tools you wouldn't run yourself.
