"""The template catalog, materialization and readiness (ADR-0033).

- **Catalog**: the first-party capabilities and templates shipped with Hollis (TOML under
  `data/`), plus templates a workspace keeps in its templates directory (shared by copying
  files; there is no marketplace). A workspace template may not reuse a first-party id.
- **Materialization**: a template, its capabilities and parameter values become the fields of an
  ordinary agent (`Draft`): description, instructions (the procedure, then each capability's
  guidance), tools (required ones, then optional ones the workspace has), the output contract,
  overlay rules, offered skills and budget.
- **Readiness**: what a template needs from the model and the workspace, checked against what
  is there. Hard mismatches block creating an agent; soft ones are warnings.
"""

import fnmatch
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import Field

from hollis.runtime.policy import RunBudget
from hollis.runtime.tools import PolicyOverlayRule
from hollis.specialists.model import (
    Capability,
    Frozen,
    Requirements,
    Template,
    digest,
    fill,
    load_toml,
    version_key,
)

DATA = Path(__file__).parent / "data"
TEMPLATE_FIELDS = ("description", "instructions", "tools", "contract", "policy", "skills", "budget")
"""Agent fields a template determines; an agent's other fields are always its own."""


class TemplateError(ValueError):
    """A template cannot be used as asked (unknown, bad parameters, missing tools)."""


@dataclass(frozen=True)
class Entry:
    template: Template
    capabilities: tuple[Capability, ...]
    source: str
    """builtin, or workspace:<file name>."""
    digest: str
    requires: Requirements


@dataclass
class Catalog:
    capabilities: dict[str, Capability] = field(default_factory=dict[str, Capability])
    entries: dict[str, Entry] = field(default_factory=dict[str, Entry])
    problems: list[str] = field(default_factory=list[str])
    """Files that could not be loaded, and why."""

    @classmethod
    def load(cls, workspace_templates: Path | None = None) -> "Catalog":
        catalog = cls()
        for path in sorted((DATA / "capabilities").glob("*.toml")):
            capability = load_toml(path, Capability)  # first-party data: errors are bugs
            catalog.capabilities[capability.id] = capability
        for path in sorted((DATA / "templates").glob("*.toml")):
            catalog._add(load_toml(path, Template), "builtin")
        if workspace_templates is not None and workspace_templates.is_dir():
            for path in sorted(workspace_templates.glob("*.toml")):
                if path.is_symlink():
                    catalog.problems.append(f"{path.name}: symbolic links are not loaded")
                    continue
                try:
                    template = load_toml(path, Template)
                except ValueError as exc:
                    catalog.problems.append(str(exc))
                    continue
                if template.id in catalog.entries:
                    catalog.problems.append(
                        f"{path.name}: template id {template.id!r} is already taken"
                    )
                    continue
                try:
                    catalog._add(template, f"workspace:{path.name}")
                except TemplateError as exc:
                    catalog.problems.append(f"{path.name}: {exc}")
        return catalog

    def _add(self, template: Template, source: str) -> None:
        unknown = [c for c in template.capabilities if c not in self.capabilities]
        if unknown:
            raise TemplateError(f"unknown capabilities: {', '.join(unknown)}")
        capabilities = tuple(self.capabilities[c] for c in template.capabilities)
        requires = template.requires
        for capability in capabilities:
            requires = requires.merge(capability.requires)
        self.entries[template.id] = Entry(
            template=template,
            capabilities=capabilities,
            source=source,
            digest=digest([template, *capabilities]),
            requires=requires,
        )

    def entry(self, template_id: str) -> Entry:
        entry = self.entries.get(template_id)
        if entry is None:
            raise TemplateError(f"no template {template_id!r}")
        return entry

    def templates(self) -> list[Entry]:
        return sorted(self.entries.values(), key=lambda e: (e.source != "builtin", e.template.id))


def newer(available: str, current: str) -> bool:
    return version_key(available) > version_key(current)


# --- materialization ----------------------------------------------------------------------------


class Draft(Frozen):
    """The agent fields a template determines (TEMPLATE_FIELDS)."""

    description: str
    instructions: str
    tools: tuple[str, ...]
    contract: str | None
    policy: tuple[PolicyOverlayRule, ...]
    skills: tuple[str, ...]
    budget: RunBudget | None
    missing: tuple[str, ...] = Field(default=(), exclude=True)
    """Required tools the workspace does not have (not part of the agent)."""


def parameter_values(template: Template, given: Mapping[str, str]) -> dict[str, str]:
    """Every parameter's value: given, else its default. TemplateError when one is unknown,
    missing, not a choice or not matching its pattern."""
    known = {p.name: p for p in template.parameters}
    unknown = sorted(set(given) - set(known))
    if unknown:
        raise TemplateError(f"unknown parameters: {', '.join(unknown)}")
    values: dict[str, str] = {}
    for name, parameter in known.items():
        value = given.get(name, parameter.default)
        if value is None:
            raise TemplateError(f"parameter {name!r} needs a value ({parameter.description})")
        if len(value) > 500 or "\x00" in value:
            raise TemplateError(f"parameter {name!r}: at most 500 characters, no NUL")
        if parameter.choices and value not in parameter.choices:
            raise TemplateError(f"parameter {name!r}: one of {', '.join(parameter.choices)}")
        if parameter.pattern is not None and not re.fullmatch(parameter.pattern, value):
            raise TemplateError(f"parameter {name!r}: must match {parameter.pattern}")
        values[name] = value
    return values


def materialize(
    entry: Entry,
    values: Mapping[str, str],
    *,
    available: Collection[str],
    commands: Sequence[str] = (),
) -> Draft:
    """The agent fields `entry` gives with these parameter values in a workspace that has the
    `available` tools and these configured `commands`."""
    template = entry.template
    values = dict(values)
    required: list[str] = []
    optional: list[str] = []
    for capability in entry.capabilities:
        required += capability.tools
        for pattern in capability.optional_tools:
            optional += sorted(t for t in available if fnmatch.fnmatchcase(t, pattern))
        if capability.configured_commands:
            optional += commands
    required += template.tools
    tools = tuple(dict.fromkeys([*required, *(t for t in optional if t in available)]))
    sections = [fill(template.instructions, values).strip()]
    sections += [
        f"## {c.name}\n\n{c.guidance.strip()}" for c in entry.capabilities if c.guidance.strip()
    ]
    policy = [rule for capability in entry.capabilities for rule in capability.policy]
    policy += [_fill_rule(rule, values) for rule in template.policy]
    return Draft(
        description=fill(template.description, values),
        instructions="\n\n".join(sections),
        tools=tools,
        contract=template.contract,
        policy=tuple(policy),
        skills=template.skills,
        budget=template.budget,
        missing=tuple(t for t in dict.fromkeys(required) if t not in available),
    )


def _fill_rule(rule: PolicyOverlayRule, values: Mapping[str, str]) -> PolicyOverlayRule:
    return rule.model_copy(
        update={
            "reason": fill(rule.reason, dict(values)),
            "outside": {
                argument: tuple(fill(g, dict(values)) for g in globs)
                for argument, globs in rule.outside.items()
            },
        }
    )


# --- readiness ----------------------------------------------------------------------------------


class Check(Frozen):
    requirement: str
    ok: bool
    hard: bool
    """A failed hard check blocks creating an agent; a soft one is a warning."""
    detail: str


@dataclass(frozen=True)
class WorkspaceFacts:
    tools: frozenset[str]
    filesystem: bool
    writable: bool
    git: bool
    commands: tuple[str, ...]
    collections: tuple[str, ...]
    agents: tuple[str, ...]


@dataclass(frozen=True)
class ModelFacts:
    deployment: str
    tools: bool
    context_window_tokens: int | None


def workspace_checks(entry: Entry, draft: Draft, facts: WorkspaceFacts) -> list[Check]:
    needs = entry.requires.workspace
    checks: list[Check] = []
    if needs.filesystem:
        checks.append(
            Check(
                requirement="filesystem",
                ok=facts.filesystem,
                hard=True,
                detail="file tools are configured ([tools.filesystem])"
                if facts.filesystem
                else "configure [tools.filesystem] in hollis.toml",
            )
        )
    if needs.writable:
        checks.append(
            Check(
                requirement="writable files",
                ok=facts.writable,
                hard=True,
                detail="writes are enabled (each still needs approval under the default policy)"
                if facts.writable
                else "set writable = true under [tools.filesystem]",
            )
        )
    if needs.commands:
        checks.append(
            Check(
                requirement="commands",
                ok=bool(facts.commands),
                hard=False,
                detail=f"configured commands: {', '.join(facts.commands)}"
                if facts.commands
                else "no [[tools.commands]]: the agent cannot run checks or procedures",
            )
        )
    if needs.git:
        checks.append(
            Check(
                requirement="git",
                ok=facts.git,
                hard=False,
                detail="read-only git tools are configured"
                if facts.git
                else "no [tools.git]: the agent cannot see the working diff",
            )
        )
    if needs.collections:
        checks.append(
            Check(
                requirement="knowledge",
                ok=bool(facts.collections),
                hard=True,
                detail=f"collections: {', '.join(facts.collections)}"
                if facts.collections
                else "create a knowledge collection first",
            )
        )
    if needs.delegates:
        checks.append(
            Check(
                requirement="delegates",
                ok=len(facts.agents) >= needs.delegates,
                hard=True,
                detail=f"{len(facts.agents)} agents could be delegated to"
                f" (at least {needs.delegates} needed)",
            )
        )
    checks += [
        Check(requirement=f"tool {tool}", ok=False, hard=True, detail="not in this workspace")
        for tool in draft.missing
    ]
    return checks


def model_checks(entry: Entry, model: ModelFacts) -> list[Check]:
    needs = entry.requires.model
    checks: list[Check] = []
    if needs.tool_calling:
        checks.append(
            Check(
                requirement="tool calling",
                ok=model.tools,
                hard=True,
                detail=f"{model.deployment} declares tool calling"
                if model.tools
                else f"{model.deployment} does not declare tool calling",
            )
        )
    if needs.min_context_tokens is not None:
        window = model.context_window_tokens
        checks.append(
            Check(
                requirement="context window",
                ok=window is not None and window >= needs.min_context_tokens,
                hard=window is not None,
                detail=f"{model.deployment} does not declare its context window "
                f"(at least {needs.min_context_tokens} tokens needed)"
                if window is None
                else f"{window} tokens declared, {needs.min_context_tokens} needed",
            )
        )
    return checks
