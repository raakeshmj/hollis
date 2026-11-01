# Single source of truth for the check sequence. CI and CLAUDE.md both call `make check`.
.PHONY: sync fmt lint typecheck test check fix web-install web-check web-build web-api web-e2e

sync:
	uv sync --locked

fmt:
	uv run ruff format --check .

lint:
	uv run ruff check .

typecheck:
	uv run pyright

test:
	uv run pytest

check: fmt lint typecheck test

fix:
	uv run ruff format .
	uv run ruff check --fix .

# The web app (web/). Its build is committed under src/hollis/server/static, so Python users and
# `make check` never need Node. These targets need Node 22.12+ (and Chrome for web-e2e).
web-install:
	cd web && npm ci

web-check:
	cd web && npm run typecheck && npm run lint && npm test

web-build:
	cd web && npm run build

web-api:
	uv run python -m hollis.server.openapi > web/openapi.json
	cd web && npm run api

web-e2e:
	cd web && npx playwright test
