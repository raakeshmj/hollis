# Installation

Hollis needs Python 3.12 or newer. It is pre-alpha and not published to a package index yet: install it from a checkout. [uv](https://docs.astral.sh/uv/) is the supported tool.

## The `hollis` command

```bash
git clone <repository-url> hollis
cd hollis
uv tool install ".[server]"     # the CLI, the REST API and the web app
hollis --version
```

`uv tool install` puts `hollis` on your `PATH` in its own environment. Add extras as you need them:

| Extra | Adds | Needed for |
|---|---|---|
| `server` | FastAPI, uvicorn, httpx2 | `hollis serve` (REST API and web app), CLI commands while a server runs |
| `client` | httpx2 | `Hollis.connect(...)` and `hollis --url ...` against a server |
| `anthropic` | the Anthropic SDK | `anthropic-messages` deployments |
| `openai-compatible` | httpx2 | `openai-chat-completions` and `openai-embeddings` deployments |
| `mcp` | the MCP SDK | MCP tool servers |
| `pdf` | pypdf | PDF documents in knowledge collections |

For example: `uv tool install ".[server,openai-compatible,mcp,pdf]"`.

The web application is built into the package; you don't need Node to run it.

## As a library

In your project: `uv add "hollis[client] @ file:///path/to/hollis"` (or `pip install "/path/to/hollis[client]"`), then `from hollis import Hollis`. See the [SDK guide](sdk.md).

## For development

```bash
make sync     # all extras and development tools, from uv.lock
make check    # format check, lint, pyright strict, tests; no network or API key needed
```

Changing the web application needs Node 22.12 or newer (`make web-install`, then `make web-check`); see [docs/development.md](../development.md).

## Check the installation

```bash
hollis doctor
```

It reports the Python version, which extras are installed, and, inside a workspace, its configuration, server and health.
