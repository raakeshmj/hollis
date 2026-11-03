"""Capabilities and templates: the data a specialization is made of (ADR-0033).

A **capability** is a named, versioned bundle: tools (required and optional), guidance for the
model, policy overlay rules, and requirements on the model and the workspace. A **template** is a
versioned blueprint for an agent: a delegate-facing description, the capabilities it uses, a
procedure (instructions), an output contract, its own overlay rules, budgets, parameters and
example tasks. Both are plain TOML data; neither names a vendor.

A template is *materialized* into an ordinary agent (`materialize.py`); nothing at run time
knows a template exists.
"""

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hollis.runtime.policy import RunBudget
from hollis.runtime.tools import PolicyOverlayRule

ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"
VERSION_PATTERN = r"^\d{1,4}\.\d{1,4}\.\d{1,4}$"
PARAMETER_PATTERN = r"^[a-z][a-z0-9_]{0,30}$"
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,30})\}")


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


class ModelRequirements(Frozen):
    tool_calling: bool = True
    """The deployment must declare tool calling (every specialist acts through tools)."""
    min_context_tokens: int | None = Field(default=None, ge=1)
    """A smaller declared context window is a hard mismatch; an undeclared one a warning."""


class WorkspaceRequirements(Frozen):
    filesystem: bool = False
    """File, code, data or docs tools: `[tools.filesystem]` must be configured."""
    writable: bool = False
    """...and writable."""
    commands: bool = False
    """At least one `[[tools.commands]]` (soft: the agent works without, but can't verify)."""
    git: bool = False
    """`[tools.git]` (soft: optional tools)."""
    collections: bool = False
    """The agent must be given knowledge access to at least one collection."""
    delegates: int = Field(default=0, ge=0, le=16)
    """The agent must be given at least this many delegates."""


class Requirements(Frozen):
    model: ModelRequirements = ModelRequirements()
    workspace: WorkspaceRequirements = WorkspaceRequirements()

    def merge(self, other: "Requirements") -> "Requirements":
        """The stricter of both."""
        a, b = self.model, other.model
        contexts = [c for c in (a.min_context_tokens, b.min_context_tokens) if c is not None]
        x, y = self.workspace, other.workspace
        return Requirements(
            model=ModelRequirements(
                tool_calling=a.tool_calling or b.tool_calling,
                min_context_tokens=max(contexts) if contexts else None,
            ),
            workspace=WorkspaceRequirements(
                filesystem=x.filesystem or y.filesystem or x.writable or y.writable,
                writable=x.writable or y.writable,
                commands=x.commands or y.commands,
                git=x.git or y.git,
                collections=x.collections or y.collections,
                delegates=max(x.delegates, y.delegates),
            ),
        )


class Capability(Frozen):
    id: str = Field(pattern=ID_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    tools: tuple[str, ...] = Field(default=(), max_length=32)
    """Required: the capability is not available without every one of them."""
    optional_tools: tuple[str, ...] = Field(default=(), max_length=32)
    """Included when the workspace has them (names or globs, e.g. git_*)."""
    configured_commands: bool = False
    """Include every operator-configured command (`[[tools.commands]]`)."""
    guidance: str = Field(default="", max_length=4000)
    policy: tuple[PolicyOverlayRule, ...] = Field(default=(), max_length=16)
    requires: Requirements = Requirements()


class TemplateParameter(Frozen):
    name: str = Field(pattern=PARAMETER_PATTERN)
    description: str = Field(min_length=1, max_length=300)
    default: str | None = Field(default=None, max_length=500)
    """None: a value is required."""
    choices: tuple[str, ...] = Field(default=(), max_length=20)
    pattern: str | None = Field(default=None, max_length=200)
    """A regular expression the whole value must match."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.pattern is not None:
            re.compile(self.pattern)
        if self.choices and self.default is not None and self.default not in self.choices:
            raise ValueError(f"parameter {self.name}: the default is not one of the choices")
        return self


class Example(Frozen):
    task: str = Field(min_length=1, max_length=1000)
    notes: str = Field(default="", max_length=500)


class Template(Frozen):
    id: str = Field(pattern=ID_PATTERN)
    version: str = Field(pattern=VERSION_PATTERN)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    """What the agent is for, written for whoever delegates to it."""
    capabilities: tuple[str, ...] = Field(default=(), max_length=16)
    tools: tuple[str, ...] = Field(default=(), max_length=32)
    """Further required tools, besides the capabilities'."""
    instructions: str = Field(min_length=1, max_length=20_000)
    """The procedure. `{name}` is replaced with a parameter's value."""
    contract: str | None = Field(default=None, pattern=ID_PATTERN)
    """The output contract runs of the agent submit their result under."""
    policy: tuple[PolicyOverlayRule, ...] = Field(default=(), max_length=16)
    """Overlay rules; `{name}` in their path globs and reasons is replaced too."""
    skills: tuple[str, ...] = Field(default=(), max_length=32)
    """Workspace Agent Skills offered to the agent by name (if the workspace has them)."""
    budget: RunBudget | None = None
    parameters: tuple[TemplateParameter, ...] = Field(default=(), max_length=16)
    examples: tuple[Example, ...] = Field(default=(), max_length=10)
    requires: Requirements = Requirements()

    @model_validator(mode="after")
    def _check(self) -> Self:
        names = [p.name for p in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError("parameter names must be unique")
        used = set(_PLACEHOLDER.findall(self.instructions))
        for rule in self.policy:
            used |= set(_PLACEHOLDER.findall(rule.reason))
            for globs in rule.outside.values():
                used |= {n for g in globs for n in _PLACEHOLDER.findall(g)}
        undeclared = used - set(names)
        if undeclared:
            raise ValueError(f"placeholders without a parameter: {sorted(undeclared)}")
        return self


def digest(data: BaseModel | list[BaseModel]) -> str:
    """A content digest: what a template materializes from, so an agent can tell whether the
    template it came from changed, even at the same version."""
    payload: Any = (
        [d.model_dump(mode="json") for d in data]
        if isinstance(data, list)
        else data.model_dump(mode="json")
    )
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()[:16]


def load_toml[T: BaseModel](path: Path, model: type[T]) -> T:
    """Parse one TOML file as `model`. ValueError (with the path) when it is not valid."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        return model.model_validate(data)
    except (OSError, tomllib.TOMLDecodeError, ValueError) as exc:
        raise ValueError(f"{path.name}: {exc}") from exc


def fill(text: str, values: dict[str, str]) -> str:
    """Replace `{name}` placeholders with parameter values; other braces stay as they are."""
    return _PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)
