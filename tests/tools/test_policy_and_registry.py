from datetime import UTC, datetime
from uuid import uuid4

import pytest

from hollis.runtime.tools import (
    ApprovalDecision,
    ApprovalRequest,
    Decision,
    PolicyRequest,
    ToolEffect,
    ToolTrust,
)
from hollis.tools import (
    CallbackApprover,
    DuplicateTool,
    Rule,
    RulePolicy,
    ToolRegistry,
    allow,
    deny,
    describe,
    require_approval,
    tool,
)
from tests.support.tools import FakeTool


def _request(
    name: str = "fs_read",
    effect: ToolEffect = ToolEffect.READ_ONLY,
    trust: ToolTrust = ToolTrust.TRUSTED,
    env: str | None = None,
    **arguments: str,
) -> PolicyRequest:
    ref = FakeTool(name, effect=effect, trust=trust).descriptor.ref
    return PolicyRequest(
        run_id=uuid4(), invocation_id="i", tool=ref, arguments=dict(arguments), environment=env
    )


def test_first_matching_rule_decides_and_names_itself() -> None:
    policy = RulePolicy.of(
        deny("fs_*", effects=[ToolEffect.DESTRUCTIVE], name="no-fs-delete", reason="never delete"),
        allow("fs_*", name="fs"),
    )
    destructive = policy.decide(_request("fs_delete", ToolEffect.DESTRUCTIVE))
    assert (destructive.decision, destructive.rule, destructive.reason) == (
        Decision.DENY,
        "no-fs-delete",
        "never delete",
    )
    assert policy.decide(_request("fs_read")).decision is Decision.ALLOW


def test_unmatched_requests_fall_back_to_the_fail_safe_default() -> None:
    policy = RulePolicy.of(allow("fs_*"))
    assert (
        policy.decide(_request("http_get", ToolEffect.MUTATING)).decision
        is Decision.REQUIRE_APPROVAL
    )
    assert policy.decide(_request("http_get")).decision is Decision.ALLOW  # trusted read-only
    assert (
        policy.decide(_request("ext", trust=ToolTrust.EXTERNAL)).decision
        is Decision.REQUIRE_APPROVAL
    )


def test_environment_trust_and_argument_scoped_rules() -> None:
    policy = RulePolicy.of(
        deny(environments=["prod"], effects=[ToolEffect.MUTATING], name="prod-readonly"),
        allow(
            "fs_write",
            when=lambda r: str(r.arguments.get("path", "")).startswith("scratch/"),
            name="scratch",
        ),
        require_approval("fs_write", name="other-paths"),
        deny(trust=[ToolTrust.EXTERNAL], name="no-external"),
    )
    assert (
        policy.decide(_request("fs_write", ToolEffect.MUTATING, env="prod")).rule == "prod-readonly"
    )
    assert (
        policy.decide(_request("fs_write", ToolEffect.MUTATING, path="scratch/a")).decision
        is Decision.ALLOW
    )
    assert (
        policy.decide(_request("fs_write", ToolEffect.MUTATING, path="etc/passwd")).decision
        is Decision.REQUIRE_APPROVAL
    )
    assert policy.decide(_request("mcp_x", trust=ToolTrust.EXTERNAL)).rule == "no-external"


def test_a_rule_cannot_decide_invalid() -> None:
    with pytest.raises(ValueError, match="INVALID"):
        Rule(decision=Decision.INVALID)


def test_registry_is_explicit_and_rejects_duplicates() -> None:
    registry = ToolRegistry()

    @registry.add
    @tool
    async def ping() -> str:
        """Ping."""
        return "pong"

    assert registry.names() == ["ping"]
    assert registry.get("ping") is ping
    assert registry.get("missing") is None
    with pytest.raises(DuplicateTool):
        registry.add(FakeTool("ping"))
    registry.remove("ping")
    assert len(registry) == 0


async def test_callback_approver_and_description() -> None:
    request = ApprovalRequest(
        run_id=uuid4(),
        invocation_id="i",
        tool=FakeTool("pay", effect=ToolEffect.MUTATING).descriptor.ref,
        arguments={"amount": 5},
        reason="mutating tool requires approval",
        binding="sha256:b",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    text = describe(request)
    assert "pay (version 1)" in text
    assert "amount = 5" in text
    assert "mutating" in text

    async def decide(r: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(binding=r.binding, approved=True, approver="cli")

    for approver in (CallbackApprover(decide), CallbackApprover(lambda r: None)):
        await approver.review(request)
    assert (await CallbackApprover(decide).review(request)) is not None
    assert (await CallbackApprover(lambda r: None).review(request)) is None
