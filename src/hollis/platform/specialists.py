"""Templates in a workspace (ADR-0033): views, creating agents from templates, customization,
upgrades and export. Services call these; authorization stays in `Platform`.

An agent made from a template is an ordinary agent that remembers its origin: the template id,
version and digest, its parameter values, and which template-determined fields were customized
(`overrides`). Editing such an agent marks every template field it changes as customized.
Upgrading re-materializes the newer template with the same parameters and keeps customized
fields, after a preview; it is refused if the template changed since the preview. Exporting
writes an agent's effective configuration as a template file.
"""

from typing import TYPE_CHECKING, Any, cast

from pydantic import JsonValue

from hollis.gateway.catalog import OpenAIEmbeddingsDeployment
from hollis.platform.config import dumps
from hollis.platform.errors import Conflict, InvalidRequest, NotFound
from hollis.platform.schemas import (
    Agent,
    AgentInput,
    AgentOrigin,
    CapabilityView,
    ContractView,
    DeploymentFit,
    ExportInput,
    FieldChange,
    FromTemplateInput,
    ParameterView,
    RequirementCheck,
    SkillsView,
    SkillView,
    TemplateDetail,
    TemplateExport,
    TemplateView,
    UpgradePreview,
)
from hollis.specialists import (
    CONTRACTS,
    TEMPLATE_FIELDS,
    Check,
    Draft,
    Entry,
    ModelFacts,
    Requirements,
    Template,
    TemplateError,
    WorkspaceFacts,
    load_skills,
    materialize,
    model_checks,
    parameter_values,
    workspace_checks,
)
from hollis.specialists.model import escape

if TYPE_CHECKING:
    from hollis.platform.workspace import Workspace


def _check_view(check: Check) -> RequirementCheck:
    return RequirementCheck(
        requirement=check.requirement, ok=check.ok, hard=check.hard, detail=check.detail
    )


async def workspace_facts(ws: "Workspace") -> WorkspaceFacts:
    collections = await ws.knowledge_store.collections()
    return WorkspaceFacts(
        tools=frozenset(ws.registry.names()),
        filesystem=ws.fs_root is not None,
        writable=ws.fs_writable,
        git="builtin:git" in ws.tool_sources.values(),
        commands=ws.configured_commands(),
        collections=tuple(c.name for c in collections),
        agents=tuple(ws.agents),
    )


def model_facts(ws: "Workspace") -> list[ModelFacts]:
    """Every text deployment an agent may use: the demo model and the allowed deployments."""
    facts: list[ModelFacts] = []
    demo = ws.resolver.demo
    if demo is not None:
        text = demo.descriptor.capabilities.text
        facts.append(
            ModelFacts(
                demo.descriptor.deployment,
                bool(text and text.tools),
                text.context_window_tokens if text else None,
            )
        )
    allowed = ws.config.routing.allowed_localities
    for deployment in ws.config.deployments:
        if isinstance(deployment, OpenAIEmbeddingsDeployment):
            continue
        if deployment.locality not in allowed:
            continue
        text = deployment.capabilities.text
        facts.append(
            ModelFacts(
                deployment.id,
                bool(text and text.tools),
                text.context_window_tokens if text else None,
            )
        )
    return facts


def _draft(ws: "Workspace", entry: Entry, values: dict[str, str]) -> Draft:
    return materialize(
        entry, values, available=set(ws.registry.names()), commands=ws.configured_commands()
    )


def _parameters(entry: Entry) -> tuple[ParameterView, ...]:
    return tuple(
        ParameterView(
            name=p.name,
            description=p.description,
            default=p.default,
            choices=p.choices,
            pattern=p.pattern,
        )
        for p in entry.template.parameters
    )


def _defaults(entry: Entry) -> dict[str, str]:
    """Parameter values for previews: defaults, and a visible placeholder for required ones."""
    return {
        p.name: p.default if p.default is not None else f"<{p.name}>"
        for p in entry.template.parameters
    }


def template_view(
    entry: Entry, facts: WorkspaceFacts, draft: Draft, agents: list[Agent]
) -> TemplateView:
    checks = [_check_view(c) for c in workspace_checks(entry, draft, facts)]
    template = entry.template
    return TemplateView(
        id=template.id,
        version=template.version,
        name=template.name,
        description=template.description,
        source=entry.source,
        digest=entry.digest,
        capabilities=template.capabilities,
        contract=template.contract,
        parameters=_parameters(entry),
        examples=tuple(e.task for e in template.examples),
        ready=not any(c.hard and not c.ok for c in checks),
        checks=tuple(checks),
        agents=tuple(
            a.name for a in agents if a.origin is not None and a.origin.template == template.id
        ),
    )


async def template_views(ws: "Workspace", agents: list[Agent]) -> list[TemplateView]:
    facts = await workspace_facts(ws)
    return [
        template_view(entry, facts, _draft(ws, entry, _defaults(entry)), agents)
        for entry in ws.catalog().templates()
    ]


async def template_detail(ws: "Workspace", template_id: str, agents: list[Agent]) -> TemplateDetail:
    try:
        entry = ws.catalog().entry(template_id)
    except TemplateError as exc:
        raise NotFound(str(exc)) from exc
    facts = await workspace_facts(ws)
    draft = _draft(ws, entry, _defaults(entry))
    view = template_view(entry, facts, draft, agents)
    fits = [
        DeploymentFit(
            deployment=model.deployment,
            ok=all(c.ok or not c.hard for c in checks),
            checks=tuple(_check_view(c) for c in checks),
        )
        for model in model_facts(ws)
        for checks in [model_checks(entry, model)]
    ]
    return TemplateDetail(
        **view.model_dump(),
        instructions=draft.instructions,
        tools=draft.tools,
        policy=draft.policy,
        budget=draft.budget,
        skills=draft.skills,
        requires=cast(dict[str, JsonValue], entry.requires.model_dump(mode="json")),
        fits=tuple(fits),
    )


def capability_views(ws: "Workspace") -> list[CapabilityView]:
    available = set(ws.registry.names())
    return [
        CapabilityView(
            id=c.id,
            version=c.version,
            name=c.name,
            description=c.description,
            tools=c.tools,
            optional_tools=c.optional_tools,
            configured_commands=c.configured_commands,
            guidance=c.guidance.strip(),
            policy=c.policy,
            available=all(t in available for t in c.tools),
        )
        for c in sorted(ws.catalog().capabilities.values(), key=lambda c: c.id)
    ]


def contract_views() -> list[ContractView]:
    return [
        ContractView(
            id=c.id,
            version=c.version,
            name=c.name,
            description=c.description,
            rules=c.rules,
            tool=c.tool_name,
            input_schema=cast(dict[str, JsonValue], c.schema.model_json_schema()),
        )
        for c in CONTRACTS.values()
    ]


def skills_view(ws: "Workspace") -> SkillsView:
    skills, problems = load_skills(ws.skills_dir)
    return SkillsView(
        directory=ws.config.specialists.skills_dir if ws.skills_dir.is_dir() else None,
        skills=tuple(SkillView(**s.model_dump()) for s in skills),
        problems=tuple(problems),
    )


# --- creating, customizing, upgrading, exporting ------------------------------------------------


def _hard_failures(checks: list[Check]) -> list[str]:
    return [f"{c.requirement}: {c.detail}" for c in checks if c.hard and not c.ok]


async def from_template(ws: "Workspace", spec: FromTemplateInput) -> tuple[AgentInput, AgentOrigin]:
    """The agent a template makes, or InvalidRequest/NotFound saying why it can't."""
    try:
        entry = ws.catalog().entry(spec.template)
    except TemplateError as exc:
        raise NotFound(str(exc)) from exc
    try:
        values = parameter_values(entry.template, spec.parameters)
    except TemplateError as exc:
        raise InvalidRequest(str(exc)) from exc
    draft = _draft(ws, entry, values)
    failures = _hard_failures(workspace_checks(entry, draft, await workspace_facts(ws)))
    model = next((m for m in model_facts(ws) if m.deployment == spec.model), None)
    if model is not None:
        failures += _hard_failures(model_checks(entry, model))
    if failures:
        raise InvalidRequest(
            f"the {entry.template.id} template cannot be used here: " + "; ".join(failures)
        )
    fields: dict[str, Any] = {f: getattr(draft, f) for f in TEMPLATE_FIELDS}
    given = spec.overrides.model_dump(exclude_unset=True)
    fields |= {k: getattr(spec.overrides, k) for k in given}
    agent = AgentInput(name=spec.name, model=spec.model, **fields)
    needs = entry.requires.workspace
    if needs.collections and (agent.knowledge is None or not agent.knowledge.collections):
        raise InvalidRequest(
            f"the {entry.template.id} template needs knowledge access: give overrides.knowledge "
            "with at least one collection"
        )
    if len(agent.delegates) < needs.delegates:
        raise InvalidRequest(
            f"the {entry.template.id} template needs at least {needs.delegates} delegates: "
            "give overrides.delegates"
        )
    origin = AgentOrigin(
        template=entry.template.id,
        version=entry.template.version,
        digest=entry.digest,
        parameters=values,
        overrides=tuple(f for f in TEMPLATE_FIELDS if f in given),
    )
    return agent, origin


def customized(current: Agent, spec: AgentInput) -> AgentOrigin | None:
    """The origin after an edit: every template field the edit changes becomes customized."""
    if current.origin is None:
        return None
    changed = {f for f in TEMPLATE_FIELDS if getattr(spec, f) != getattr(current, f)}
    overrides = [f for f in TEMPLATE_FIELDS if f in changed or f in current.origin.overrides]
    return current.origin.model_copy(update={"overrides": tuple(overrides)})


async def upgrade_preview(
    ws: "Workspace", agent: Agent, reset: tuple[str, ...] = ()
) -> tuple[UpgradePreview, AgentInput, AgentOrigin]:
    origin = agent.origin
    if origin is None:
        raise InvalidRequest(f"agent {agent.name!r} was not made from a template")
    unknown = sorted(set(reset) - set(TEMPLATE_FIELDS))
    if unknown:
        raise InvalidRequest(f"reset: not template fields: {', '.join(unknown)}")
    try:
        entry = ws.catalog().entry(origin.template)
    except TemplateError as exc:
        raise NotFound(f"template {origin.template!r} is no longer available") from exc
    known = {p.name for p in entry.template.parameters}
    try:
        values = parameter_values(
            entry.template, {k: v for k, v in origin.parameters.items() if k in known}
        )
    except TemplateError as exc:
        raise InvalidRequest(f"the new template version needs parameters: {exc}") from exc
    draft = _draft(ws, entry, values)
    kept = tuple(f for f in origin.overrides if f not in reset)
    fields = {f: getattr(agent, f) if f in kept else getattr(draft, f) for f in TEMPLATE_FIELDS}
    result = AgentInput(**(agent.model_dump(include=set(AgentInput.model_fields)) | fields))
    changes = tuple(
        FieldChange(
            field=f,
            before=cast(JsonValue, _plain(getattr(agent, f))),
            after=cast(JsonValue, _plain(getattr(result, f))),
        )
        for f in TEMPLATE_FIELDS
        if getattr(agent, f) != getattr(result, f)
    )
    checks = workspace_checks(entry, draft, await workspace_facts(ws))
    preview = UpgradePreview(
        agent=agent.name,
        template=entry.template.id,
        from_version=origin.version,
        to_version=entry.template.version,
        from_digest=origin.digest,
        to_digest=entry.digest,
        up_to_date=entry.digest == origin.digest and not changes,
        changes=changes,
        kept=kept,
        checks=tuple(_check_view(c) for c in checks),
    )
    new_origin = origin.model_copy(
        update={
            "version": entry.template.version,
            "digest": entry.digest,
            "parameters": values,
            "overrides": kept,
        }
    )
    return preview, result, new_origin


def check_upgrade(preview: UpgradePreview, digest: str) -> None:
    if preview.to_digest != digest:
        raise Conflict(
            f"the {preview.template} template changed since the preview "
            f"(now {preview.to_digest}); preview the upgrade again"
        )
    failures = [f"{c.requirement}: {c.detail}" for c in preview.checks if c.hard and not c.ok]
    if failures:
        raise InvalidRequest("the upgraded agent could not run here: " + "; ".join(failures))


def _plain(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_plain(v) for v in cast(tuple[Any, ...], value)]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def export(ws: "Workspace", agent: Agent, spec: ExportInput) -> TemplateExport:
    template_id = spec.id or agent.name
    catalog = ws.catalog()
    if catalog.entries.get(template_id, None) is not None and (
        catalog.entries[template_id].source == "builtin"
    ):
        raise InvalidRequest(f"{template_id!r} is a first-party template id; choose another id")
    requires = Requirements()
    if agent.origin is not None and agent.origin.template in catalog.entries:
        requires = catalog.entries[agent.origin.template].requires
    template = Template(
        id=template_id,
        version=spec.version,
        name=agent.name.replace("-", " ").capitalize(),
        description=agent.description.strip() or f"Exported from the {agent.name} agent.",
        tools=agent.tools,
        instructions=escape(agent.instructions.strip() or "Help with the task."),
        contract=agent.contract,
        policy=tuple(
            rule.model_copy(
                update={
                    "reason": escape(rule.reason),
                    "outside": {k: tuple(escape(g) for g in v) for k, v in rule.outside.items()},
                }
            )
            for rule in agent.policy
        ),
        skills=agent.skills,
        budget=agent.budget,
        requires=requires,
    )
    header = (
        f"# Hollis agent template, exported from agent {agent.name!r}.\n"
        "# Put this file in a workspace's templates directory to create agents from it.\n"
    )
    body = dumps(template.model_dump(mode="json", exclude_defaults=True))
    return TemplateExport(
        id=template.id,
        version=template.version,
        filename=f"{template.id}.toml",
        content=header + body,
    )
