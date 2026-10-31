# Single source of truth for the check sequence. CI and CLAUDE.md both call `make check`.
.PHONY: sync fmt lint typecheck test check fix

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
