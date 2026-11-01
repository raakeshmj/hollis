"""Per-run policy overlays (ADR-0035): recorded with the run, applied by the runner, and only
ever stricter than the workspace policy."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hollis.adapters.fake_provider import ScriptedModelProvider
from hollis.adapters.memory_store import InMemoryRunStore
from hollis.runtime.events import ToolOutcome, deserialize_event, serialize_event
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import Runner, RunSpec
from hollis.runtime.state import fold
from hollis.runtime.tools import (
    Decision,
    PolicyDecision,
    PolicyOverlayRule,
    PolicyRequest,
    ToolEffect,
    apply_overlay,
)
from hollis.tools.policy import RulePolicy, allow
from tests.support.events import build_log, created
from tests.support.tools import Catalog, FakeTool, answer, call, wants

BUDGET = RunBudget(max_output_tokens=256)
WRITE = FakeTool("fs_write_file", effect=ToolEffect.MUTATING, required=("path",)).descriptor.ref
READ = FakeTool("fs_read_file", required=("path",)).descriptor.ref
ALLOWED = PolicyDecision(decision=Decision.ALLOW, reason="workspace allows", rule="w")


def _request(tool: object = WRITE, **arguments: object) -> PolicyRequest:
    return PolicyRequest(run_id=uuid4(), invocation_id="i", tool=tool, arguments=arguments)  # type: ignore[arg-type]  # test helper


def test_an_overlay_can_only_tighten() -> None:
    deny_writes = PolicyOverlayRule(decision=Decision.DENY, effects=(ToolEffect.MUTATING,))
    assert apply_overlay(ALLOWED, [deny_writes], _request()).decision is Decision.DENY
    assert apply_overlay(ALLOWED, [deny_writes], _request(READ)).decision is Decision.ALLOW
    ask = PolicyOverlayRule(decision=Decision.REQUIRE_APPROVAL)
    denied = PolicyDecision(decision=Decision.DENY, reason="workspace denies", rule="w")
    assert apply_overlay(denied, [ask], _request()) == denied, "a looser rule changes nothing"
    with pytest.raises(ValidationError):
        PolicyOverlayRule(decision=Decision.ALLOW)  # type: ignore[arg-type]  # the point of the test


def test_the_strictest_matching_rule_wins_and_is_named() -> None:
    rules = [
        PolicyOverlayRule(decision=Decision.REQUIRE_APPROVAL, tools="fs_*", name="ask"),
        PolicyOverlayRule(decision=Decision.DENY, tools="fs_write_*", name="no-writes", reason="r"),
    ]
    verdict = apply_overlay(ALLOWED, rules, _request())
    assert (verdict.decision, verdict.rule, verdict.reason) == (
        Decision.DENY,
        "agent:no-writes",
        "r",
    )


@pytest.mark.parametrize(
    ("path", "denied"),
    [
        ("docs/guide.md", False),
        ("./docs/a/b.md", False),
        ("src/app.py", True),
        ("docs/../src/app.py", True),
        ("../outside.md", True),
        ("/etc/passwd", True),
    ],
)
def test_path_scoping_matches_values_outside_the_allowed_globs(path: str, denied: bool) -> None:
    rule = PolicyOverlayRule(decision=Decision.DENY, outside={"path": ("docs/**",)})
    verdict = apply_overlay(ALLOWED, [rule], _request(path=path))
    assert (verdict.decision is Decision.DENY) is denied


def test_list_arguments_match_when_any_value_is_outside() -> None:
    rule = PolicyOverlayRule(decision=Decision.DENY, outside={"paths": ("docs/**",)})
    assert apply_overlay(ALLOWED, [rule], _request(paths=["docs/a.md"])).decision is Decision.ALLOW
    assert (
        apply_overlay(ALLOWED, [rule], _request(paths=["docs/a.md", "setup.py"])).decision
        is Decision.DENY
    )


async def test_the_runner_records_and_applies_the_overlay() -> None:
    write = FakeTool("fs_write_file", effect=ToolEffect.MUTATING, required=("path",))
    provider = ScriptedModelProvider([wants(call("fs_write_file", path="src/x.py")), answer()])
    runner = Runner(
        InMemoryRunStore(),
        provider,
        tools=Catalog(write),
        policy=RulePolicy.of(allow()),  # the workspace allows everything
        sleep=_no_sleep,
    )
    rule = PolicyOverlayRule(
        decision=Decision.DENY, outside={"path": ("docs/**",)}, name="docs-only"
    )
    spec = RunSpec(
        model="fake", input="write", budget=BUDGET, tools=("fs_write_file",), policy=(rule,)
    )
    state = await runner.execute((await runner.create(spec)).run_id)
    [invocation] = state.turns[0].invocations
    assert invocation.decision is Decision.DENY
    assert invocation.rule == "agent:docs-only"
    assert invocation.outcome is ToolOutcome.DENIED
    assert write.calls == []
    assert state.policy == (rule,)


async def _no_sleep(_: float) -> None:
    return None


def test_run_created_v6_payloads_upcast_without_an_overlay() -> None:
    [event] = build_log(uuid4(), [created()])
    stored = json.loads(serialize_event(event))
    stored["schema_version"] = 6
    stored["payload"].pop("policy")
    assert fold([deserialize_event(json.dumps(stored))]).policy == ()
