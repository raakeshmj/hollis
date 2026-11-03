"""Specialized agents through the platform (M8): templates in a workspace, agents made from them,
customization, upgrades and export, overlays in runs and delegations, and every first-party
specialist's flow on the demo model, with its output checked against its run's log."""

import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import JsonValue

from hollis.platform.auth import Principal
from hollis.platform.errors import Conflict, InvalidRequest, NotFound
from hollis.platform.schemas import (
    AgentInput,
    AgentOverrides,
    ApprovalDecisionInput,
    CollectionInput,
    DeploymentInput,
    ExportInput,
    FromTemplateInput,
    RunPhase,
    RunSummary,
    StartRun,
    UpgradeInput,
)
from hollis.platform.services import Platform
from hollis.runtime.conversation import ToolCall
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.model import ModelRequest, ModelResponse
from hollis.runtime.tools import Decision, PolicyOverlayRule, ToolContext, ToolEffect, ToolError
from tests.support.orchestration import ScriptedAgentModel, answer, calls
from tests.support.platform import README

BILLING = "def tax(amount):\n    rate = 0.2\n    return amount * rate\n"
SALES = "region,amount\nnorth,10\nsouth,32.5\nnorth,7\n"


async def _output(platform: Platform, p: Principal, run: RunSummary) -> dict[str, Any]:
    """The run's accepted output, as plain data (fails the test if it was not accepted)."""
    output = (await platform.run(p, run.run_id)).output
    assert output is not None
    assert output.status == "accepted", output.problems
    assert output.data is not None
    return dict(output.data)


async def _wait(platform: Platform, p: Principal, run: RunSummary) -> RunSummary:
    return await platform.wait_run(p, run.run_id, timeout_s=10)


async def _run(platform: Platform, p: Principal, agent: str, task: str) -> RunSummary:
    return await _wait(platform, p, await platform.start_run(p, StartRun(input=task, agent=agent)))


async def _approve_all(platform: Platform, p: Principal, run: RunSummary) -> RunSummary:
    while run.phase is RunPhase.WAITING_APPROVAL:
        for approval in await platform.approvals(p):
            if approval.run_id == run.run_id:
                await platform.decide(
                    p, run.run_id, approval.invocation_id, ApprovalDecisionInput(approved=True)
                )
        run = await platform.wait_run(p, run.run_id, timeout_s=10)
    return run


async def _make(
    platform: Platform,
    admin: Principal,
    template: str,
    name: str | None = None,
    *,
    model: str = "demo",
    **overrides: object,
) -> None:
    await platform.create_agent_from_template(
        admin,
        FromTemplateInput(
            template=template,
            name=name or template,
            model=model,
            overrides=AgentOverrides.model_validate(overrides),
        ),
    )


# --- templates in a workspace ----------------------------------------------------------------


async def test_the_gallery_reports_readiness_from_the_workspace(
    platform: Platform, admin: Principal
) -> None:
    views = {t.id: t for t in await platform.templates(admin)}
    assert len(views) == 7
    assert views["code-reviewer"].ready, "hollis init configures file tools"
    assert views["data-analyst"].ready
    researcher = views["researcher"]
    assert not researcher.ready, "no knowledge collection yet"
    assert any(c.requirement == "knowledge" and c.hard and not c.ok for c in researcher.checks)
    engineer = {c.requirement: c for c in views["software-engineer"].checks}
    assert engineer["commands"].ok is False
    assert engineer["commands"].hard is False, "no commands is a warning, not a blocker"

    detail = await platform.template(admin, "software-engineer")
    assert detail.contract == "change-report"
    assert "apply_patch" in detail.tools
    demo = next(f for f in detail.fits if f.deployment == "demo")
    assert demo.ok, "the demo model declares tool calling and a 32k window"
    with pytest.raises(NotFound):
        await platform.template(admin, "astronaut")
    assert {c.id for c in await platform.contracts(admin)} >= {"change-report", "code-review"}
    capabilities = {c.id: c for c in await platform.capabilities(admin)}
    assert capabilities["code-reading"].available
    assert not capabilities["read-only"].tools, "a capability can be policy only"


async def test_an_agent_from_a_template_is_an_ordinary_agent_with_an_origin(
    platform: Platform, admin: Principal
) -> None:
    agent = await platform.create_agent_from_template(
        admin,
        FromTemplateInput(
            template="technical-writer",
            name="writer",
            model="demo",
            parameters={"docs_dir": "manual"},
        ),
    )
    assert agent.origin is not None
    assert (agent.origin.template, agent.origin.version) == ("technical-writer", "1.0.0")
    assert agent.origin.parameters == {"docs_dir": "manual"}
    assert agent.origin.overrides == ()
    assert agent.contract == "docs-change"
    assert "only under `manual/`" in agent.instructions
    rule = next(r for r in agent.policy if r.name == "docs-only")
    assert rule.outside["path"] == ("manual/*",)
    listed = {t.id: t for t in await platform.templates(admin)}
    assert listed["technical-writer"].agents == ("writer",)


async def test_requirements_block_what_cannot_work(platform: Platform, admin: Principal) -> None:
    with pytest.raises(InvalidRequest, match="knowledge"):
        await _make(platform, admin, "researcher")
    with pytest.raises(InvalidRequest, match="delegates"):
        await _make(platform, admin, "coordinator")
    await platform.upsert_deployment(
        admin,
        DeploymentInput(
            deployment={
                "id": "no-tools",
                "protocol": "openai-chat-completions",
                "provider": "vllm",
                "model": "m",
                "locality": "local",
                "endpoint": {"base_url": "http://127.0.0.1:9/v1"},
            }
        ),
    )
    with pytest.raises(InvalidRequest, match="tool calling"):
        await _make(platform, admin, "code-reviewer", model="no-tools")
    with pytest.raises(InvalidRequest, match="unknown parameters"):
        await platform.create_agent_from_template(
            admin,
            FromTemplateInput(
                template="code-reviewer", name="r", model="demo", parameters={"x": "1"}
            ),
        )
    with pytest.raises(NotFound):
        await _make(platform, admin, "astronaut")


async def test_editing_marks_fields_customized_and_upgrades_keep_them(
    platform: Platform, admin: Principal
) -> None:
    templates = platform.ws.templates_dir
    templates.mkdir()
    v1 = (
        'id = "triage"\nversion = "1.0.0"\nname = "Triage"\ndescription = "Sorts issues."\n'
        'instructions = "Sort the {queue} queue."\ncapabilities = ["code-reading"]\n'
        'contract = "code-review"\n'
        '[[parameters]]\nname = "queue"\ndescription = "Queue"\ndefault = "inbox"\n'
    )
    (templates / "triage.toml").write_text(v1)
    await _make(platform, admin, "triage")
    agent = await platform.agent(admin, "triage")
    assert agent.origin is not None
    edited = await platform.update_agent(
        admin,
        "triage",
        AgentInput.model_validate(
            agent.model_dump(include=set(AgentInput.model_fields)) | {"description": "Mine."}
        ),
    )
    assert edited.origin is not None
    assert edited.origin.overrides == ("description",)

    preview = await platform.upgrade_preview(admin, "triage")
    assert preview.up_to_date

    (templates / "triage.toml").write_text(
        v1.replace('"1.0.0"', '"1.1.0"')
        .replace("Sort the {queue} queue.", "Sort the {queue} queue by severity.")
        .replace("Sorts issues.", "Sorts issues by severity.")
    )
    preview = await platform.upgrade_preview(admin, "triage")
    assert (preview.from_version, preview.to_version) == ("1.0.0", "1.1.0")
    assert not preview.up_to_date
    assert [c.field for c in preview.changes] == ["instructions"]
    assert preview.kept == ("description",)

    with pytest.raises(Conflict, match="changed since the preview"):
        await platform.upgrade_agent(admin, "triage", UpgradeInput(digest=preview.from_digest))
    upgraded = await platform.upgrade_agent(admin, "triage", UpgradeInput(digest=preview.to_digest))
    assert "by severity" in upgraded.instructions
    assert upgraded.description == "Mine.", "the customization survives"
    assert upgraded.origin is not None
    assert upgraded.origin.version == "1.1.0"

    reset = await platform.upgrade_agent(
        admin, "triage", UpgradeInput(digest=preview.to_digest, reset=("description",))
    )
    assert reset.description == "Sorts issues by severity."
    assert reset.origin is not None
    assert reset.origin.overrides == ()


async def test_export_writes_a_template_the_workspace_can_load(
    platform: Platform, admin: Principal
) -> None:
    await _make(
        platform,
        admin,
        "code-reviewer",
        "reviewer",
        instructions='Answer with JSON like {"file": "x"} and nothing else.',
    )
    exported = await platform.export_agent(admin, "reviewer", ExportInput(id="team-review"))
    assert exported.filename == "team-review.toml"
    platform.ws.templates_dir.mkdir()
    (platform.ws.templates_dir / exported.filename).write_text(exported.content)
    entry = platform.ws.catalog().entry("team-review")
    assert entry.source == "workspace:team-review.toml"
    assert entry.template.contract == "code-review"
    await _make(platform, admin, "team-review", "reviewer-two")
    copy = await platform.agent(admin, "reviewer-two")
    original = await platform.agent(admin, "reviewer")
    assert copy.instructions == original.instructions, "braces survive the round trip"
    assert copy.policy == original.policy
    assert copy.tools == original.tools
    with pytest.raises(InvalidRequest, match="first-party template id"):
        await platform.export_agent(admin, "reviewer", ExportInput(id="researcher"))


# --- overlays in runs and delegations -----------------------------------------------------------


def _writes_once(path: str) -> ScriptedAgentModel:
    def behaviour(request: ModelRequest) -> ModelResponse:
        if not request.history:
            return calls(
                ToolCall(
                    call_id="w1", name="fs_write_file", arguments={"path": path, "content": "x"}
                )
            )
        return answer("done")

    return ScriptedAgentModel("writer-model", behaviour)


async def test_a_read_only_agent_cannot_write_even_where_the_workspace_would_ask(
    platform: Platform, admin: Principal
) -> None:
    platform.ws.register_provider(_writes_once("notes.md"))
    await _make(
        platform,
        admin,
        "code-reviewer",
        "reviewer",
        model="writer-model",
        tools=("code_read", "fs_write_file"),
    )
    run = await _run(platform, admin, "reviewer", "Review it.")
    assert run.phase is RunPhase.COMPLETED, "denied outright: no approval to wait for"
    detail = await platform.run(admin, run.run_id)
    call = detail.turns[0].tool_calls[0]
    assert call.decision == Decision.DENY.value
    assert call.rule == "agent:read-only"
    assert not (platform.ws.root / "notes.md").exists()
    assert any(rule.name == "read-only" for rule in detail.policy)


async def test_delegated_runs_inherit_their_parents_overlay(
    platform: Platform, admin: Principal
) -> None:
    """A coordinator cannot get done by delegating what its own overlay forbids it."""
    platform.ws.register_provider(_writes_once("loot.md"))

    def coordinate(request: ModelRequest) -> ModelResponse:
        if not request.history:
            return calls(
                ToolCall(call_id="d1", name="delegate_scribe", arguments={"objective": "write"})
            )
        return answer("done")

    platform.ws.register_provider(ScriptedAgentModel("lead-model", coordinate))
    await platform.create_agent(
        admin, AgentInput(name="scribe", model="writer-model", tools=("fs_write_file",))
    )

    async def lead(rule: PolicyOverlayRule) -> RunSummary:
        spec = AgentInput(name="lead", model="lead-model", delegates=("scribe",), policy=(rule,))
        if "lead" in platform.ws.agents:
            await platform.update_agent(admin, "lead", spec)
        else:
            await platform.create_agent(admin, spec)
        return await _run(platform, admin, "lead", "Get loot.md written.")

    # By effect: delegating to an agent that can write is itself a write, and is denied.
    run = await lead(
        PolicyOverlayRule(
            decision=Decision.DENY,
            effects=(ToolEffect.MUTATING, ToolEffect.DESTRUCTIVE),
            name="lead-read-only",
        )
    )
    detail = await platform.run(admin, run.run_id)
    delegation = detail.turns[0].tool_calls[0]
    assert (delegation.decision, delegation.rule) == ("deny", "agent:lead-read-only")
    assert len((await platform.run_tree(admin, run.run_id)).nodes) == 1, "no child was created"

    # By tool: the delegation runs (once approved: the child could write), and the child is
    # held to the parent's rule before its own: its write is denied, not sent for approval.
    run = await lead(
        PolicyOverlayRule(decision=Decision.DENY, tools="fs_write_file", name="no-file-writes")
    )
    run = await _approve_all(platform, admin, run)
    assert run.phase is RunPhase.COMPLETED
    tree = await platform.run_tree(admin, run.run_id)
    child = next(n for n in tree.nodes if n.run.agent == "scribe")
    detail = await platform.run(admin, child.run.run_id)
    assert [r.name for r in detail.policy] == ["no-file-writes"]
    write = detail.turns[0].tool_calls[0]
    assert (write.decision, write.rule) == ("deny", "agent:no-file-writes")
    assert not (platform.ws.root / "loot.md").exists()


# --- the first-party specialists on the demo model ---------------------------------------------


async def test_code_reviewer_flow(platform: Platform, admin: Principal) -> None:
    (platform.ws.root / "billing.py").write_text(BILLING)
    await _make(platform, admin, "code-reviewer")
    run = await _run(platform, admin, "code-reviewer", "Review billing.py please.")
    detail = await platform.run(admin, run.run_id)
    assert run.phase is RunPhase.COMPLETED
    assert "submit_code_review" in detail.tools
    assert detail.output is not None
    assert detail.output.status == "accepted"
    assert detail.output.data is not None
    assert detail.output.data["findings"] == []
    assert detail.output.data["scope"] == ["billing.py"]
    assert detail.answer is not None
    assert "accepted" in detail.answer


async def test_software_engineer_flow_with_approval_and_a_configured_check(
    tmp_path: Path,
) -> None:
    from hollis.platform.config import initialize

    initialize(tmp_path, "eng")
    config = tmp_path / "hollis.toml"
    config.write_text(
        config.read_text()
        + f'\n[[tools.commands]]\nname = "run_checks"\ndescription = "Run the checks."\n'
        f'argv = ["{sys.executable}", "-c", "print(\'2 passed\')"]\neffect = "read_only"\n'
    )
    (tmp_path / "billing.py").write_text(BILLING)
    platform = await Platform.open(tmp_path)
    try:
        admin = await platform.local_admin()
        await _make(platform, admin, "software-engineer", "engineer")
        agent = await platform.agent(admin, "engineer")
        assert "run_checks" in agent.tools
        run = await _run(platform, admin, "engineer", 'Replace "0.2" with "0.25" in billing.py.')
        assert run.phase is RunPhase.WAITING_APPROVAL, "the patch waits for a person"
        run = await _approve_all(platform, admin, run)
        assert run.phase is RunPhase.COMPLETED
        assert "rate = 0.25" in (tmp_path / "billing.py").read_text()
        data = await _output(platform, admin, run)
        assert data["changes"] == [{"file": "billing.py", "summary": "replaced '0.2' with '0.25'"}]
        [verification] = data["verifications"]
        assert (verification["command"], verification["outcome"]) == ("run_checks", "passed")
    finally:
        await platform.close()


async def test_data_analyst_flow(platform: Platform, admin: Principal) -> None:
    (platform.ws.root / "sales.csv").write_text(SALES)
    await _make(platform, admin, "data-analyst")
    run = await _run(platform, admin, "data-analyst", "How many sales are recorded?")
    data = await _output(platform, admin, run)
    assert data["findings"][0]["value"] == 3


async def test_researcher_flow(platform: Platform, admin: Principal) -> None:
    await platform.create_collection(admin, CollectionInput(name="docs"))
    await platform.ingest(admin, "docs", key="handbook", data=README.encode(), filename="h.md")
    await _make(platform, admin, "researcher", knowledge=KnowledgeAccess(collections=("docs",)))
    run = await _run(platform, admin, "researcher", "What is the deploy command?")
    data = await _output(platform, admin, run)
    assert data["findings"][0]["citations"][0].startswith("C-")


async def test_technical_writer_writes_docs_but_nothing_else(
    platform: Platform, admin: Principal
) -> None:
    await _make(platform, admin, "technical-writer", "writer")
    run = await _run(platform, admin, "writer", "Write docs/guide.md: # Guide\n\nHow to deploy.")
    run = await _approve_all(platform, admin, run)
    output = (await platform.run(admin, run.run_id)).output
    assert output is not None
    assert output.status == "accepted", output.problems
    assert (platform.ws.root / "docs" / "guide.md").read_text().startswith("# Guide")

    outside = await _run(platform, admin, "writer", "Write notes.md: # Notes")
    assert outside.phase is RunPhase.COMPLETED, "denied by the overlay, never asked"
    detail = await platform.run(admin, outside.run_id)
    assert detail.turns[0].tool_calls[0].rule == "agent:docs-only"
    assert not (platform.ws.root / "notes.md").exists()
    assert detail.output is not None
    assert detail.output.status == "missing"


async def test_automation_flow_logs_its_steps(tmp_path: Path) -> None:
    from hollis.platform.config import initialize

    initialize(tmp_path, "ops")
    config = tmp_path / "hollis.toml"
    config.write_text(
        config.read_text()
        + f'\n[[tools.commands]]\nname = "rotate_logs"\ndescription = "Rotate logs."\n'
        f'argv = ["{sys.executable}", "-c", "print(\'rotated\')"]\n'
    )
    platform = await Platform.open(tmp_path)
    try:
        admin = await platform.local_admin()
        await _make(platform, admin, "automation", "ops")
        run = await _run(platform, admin, "ops", "Rotate the logs then confirm")
        assert run.phase is RunPhase.WAITING_APPROVAL, "every change is approved"
        run = await _approve_all(platform, admin, run)
        data = await _output(platform, admin, run)
        assert data["steps"][0]["outcome"] == "succeeded"
    finally:
        await platform.close()


async def test_coordinator_combines_checked_results_of_specialists(
    platform: Platform, admin: Principal
) -> None:
    (platform.ws.root / "sales.csv").write_text(SALES)
    await platform.create_collection(admin, CollectionInput(name="docs"))
    await platform.ingest(admin, "docs", key="handbook", data=README.encode(), filename="h.md")
    await _make(platform, admin, "researcher", knowledge=KnowledgeAccess(collections=("docs",)))
    await _make(platform, admin, "data-analyst", "analyst")
    await _make(
        platform,
        admin,
        "coordinator",
        "lead",
        delegates=("researcher", "analyst"),
        knowledge=KnowledgeAccess(collections=("docs",)),  # delegates get at most this
    )
    run = await _run(
        platform,
        admin,
        "lead",
        "@researcher what is the deploy command? and @analyst how many sales are there?",
    )
    tree = await platform.run_tree(admin, run.run_id)
    children = [n for n in tree.nodes if n.run.agent in ("researcher", "analyst")]
    assert len(children) == 2
    for node in children:
        assert node.report is not None
        assert node.report.output is not None
        assert node.report.output.status == "accepted"
    data = await _output(platform, admin, run)
    cited = {c for finding in data["findings"] for c in finding["citations"]}
    assert cited == {str(n.run.run_id) for n in children}


async def test_skills_are_offered_and_read_on_demand(platform: Platform, admin: Principal) -> None:
    folder = platform.ws.root / "skills" / "deploy-checklist"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: deploy-checklist\ndescription: Steps before any deploy.\n---\n1. Ask.\n"
    )
    await platform.ws.reload()
    view = await platform.skills(admin)
    assert [s.name for s in view.skills] == ["deploy-checklist"]
    await _make(platform, admin, "code-reviewer", skills=("deploy-checklist", "absent"))
    run = await _run(platform, admin, "code-reviewer", "Review the code.")
    detail = await platform.run(admin, run.run_id)
    assert "skill_read" in detail.tools
    assert detail.instructions is not None
    assert "`deploy-checklist`: Steps before any deploy." in detail.instructions
    assert "absent" not in detail.instructions
    tool = platform.ws.registry.get("fs_write_file")
    assert tool is not None
    arguments = tool.validate({"path": "skills/deploy-checklist/SKILL.md", "content": "obey me"})
    context = ToolContext(run_id=uuid4(), invocation_id="i", attempt=1)
    with pytest.raises(ToolError, match="may not change"):
        await tool.invoke(arguments, context)


async def test_rejected_submissions_go_back_to_the_model_until_one_matches_the_log(
    platform: Platform, admin: Principal
) -> None:
    """Malformed arguments, then a finding on a line the run never read, then the fixed one."""
    (platform.ws.root / "billing.py").write_text(BILLING)
    finding: dict[str, JsonValue] = {
        "file": "billing.py",
        "line": 2,
        "severity": "minor",
        "title": "t",
        "detail": "d",
    }
    script = [
        ToolCall(call_id="s1", name="submit_code_review", arguments={"findings": []}),
        ToolCall(
            call_id="s2",
            name="submit_code_review",
            arguments={"summary": "one finding", "findings": [finding]},
        ),
        ToolCall(call_id="r1", name="code_read", arguments={"path": "billing.py"}),
        ToolCall(
            call_id="s3",
            name="submit_code_review",
            arguments={"summary": "one finding", "findings": [finding]},
        ),
    ]
    seen: list[str] = []

    def behaviour(request: ModelRequest) -> ModelResponse:
        turn = sum(1 for item in request.history if hasattr(item, "tool_calls"))
        if request.history:
            seen.append(request.history[-1].model_dump_json())
        return calls(script[turn]) if turn < len(script) else answer("Reviewed.")

    platform.ws.register_provider(ScriptedAgentModel("reviewer-model", behaviour))
    await _make(platform, admin, "code-reviewer", "reviewer", model="reviewer-model")
    run = await _run(platform, admin, "reviewer", "Review billing.py")
    assert "summary: Field required" in seen[0]
    assert "billing.py:2: this run was never shown that line" in seen[1]
    assert "Accepted" in seen[3]
    output = (await platform.run(admin, run.run_id)).output
    assert output is not None
    assert (output.status, output.attempts) == ("accepted", 3)
    assert output.data is not None
    assert output.data["findings"] == [{**finding, "end_line": None, "suggestion": ""}]
