"""Function tools, registry and rule policy driving a real multi-turn run."""

from hollis.adapters.fake_provider import ScriptedModelProvider
from hollis.runtime.events import ToolOutcome
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import Runner, RunSpec
from hollis.runtime.state import RunStatus
from hollis.runtime.store import RunStore
from hollis.runtime.tools import ToolEffect
from hollis.tools import RulePolicy, ToolRegistry, deny, tool
from tests.support.tools import answer, call, results, wants


async def test_decorated_tools_in_a_run(store: RunStore) -> None:
    registry = ToolRegistry()
    deleted: list[str] = []

    @registry.add
    @tool
    async def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    @registry.add
    @tool(effect=ToolEffect.DESTRUCTIVE)
    async def drop_table(name: str) -> str:
        """Drop a database table."""
        deleted.append(name)
        return "dropped"

    provider = ScriptedModelProvider(
        [
            wants(call("add", "c1", a=2, b="3"), call("drop_table", "c2", name="users")),
            answer("2 + 3 = 5"),
        ]
    )
    runner = Runner(
        store, provider, tools=registry, policy=RulePolicy.of(deny("drop_*", name="no-drops"))
    )
    run = await runner.create(
        RunSpec(
            model="fake",
            input="add",
            tools=("add", "drop_table"),
            budget=RunBudget(max_output_tokens=64),
        )
    )
    final = await runner.execute(run.run_id)

    assert final.status is RunStatus.COMPLETED
    assert [i.outcome for i in final.turns[0].invocations] == [
        ToolOutcome.SUCCESS,
        ToolOutcome.DENIED,
    ]
    assert final.turns[0].invocations[0].arguments == {"a": 2, "b": 3}  # coerced by the schema
    assert deleted == []
    returned = results(provider.requests[1])
    assert returned[0].content == "5"
    # A clear, non-negotiable denial the model can't mistake for a transient error.
    assert returned[1].content == "tool call denied by policy: rule no-drops matched"
    assert final.turns[0].invocations[1].rule == "no-drops"
