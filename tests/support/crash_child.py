"""Child process for the SIGKILL recovery test.

Usage: python crash_child.py <db-path>

Runs a SQLite-backed run. Right after ModelResponseRecorded commits it prints
`RECORDED <run_id>` and waits to be killed before it can append RunCompleted.
"""

import asyncio
import sys
from collections.abc import Sequence
from uuid import UUID

from hollis.adapters.fake_provider import ScriptedModelProvider
from hollis.adapters.sqlite_store import SqliteRunStore
from hollis.runtime.events import EventEnvelope, EventType
from hollis.runtime.model import ModelResponse
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import Runner, RunSpec

CHILD_RESPONSE = ModelResponse(content="generated in the child process")


class PauseAfterResponse:
    def __init__(self, inner: SqliteRunStore) -> None:
        self._inner = inner

    async def append(
        self, run_id: UUID, expected_version: int, events: Sequence[EventEnvelope]
    ) -> None:
        await self._inner.append(run_id, expected_version, events)
        if any(e.event_type is EventType.MODEL_RESPONSE_RECORDED for e in events):
            print(f"RECORDED {run_id}", flush=True)
            await asyncio.sleep(60)  # the parent kills us here

    async def read(self, run_id: UUID) -> list[EventEnvelope]:
        return await self._inner.read(run_id)


async def main(db_path: str) -> None:
    store = await SqliteRunStore.open(db_path)
    runner = Runner(PauseAfterResponse(store), ScriptedModelProvider([CHILD_RESPONSE]))
    run = await runner.create(
        RunSpec(model="fake", input="hello", budget=RunBudget(max_output_tokens=64))
    )
    await runner.execute(run.run_id)
    sys.exit("unreachable: child should have been killed")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
