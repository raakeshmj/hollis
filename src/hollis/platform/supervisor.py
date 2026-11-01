"""Background run execution and live observation (ADR-0027).

- `RunSupervisor` drives runs as asyncio tasks in the platform's process: starting a run returns
  immediately, and execution does not depend on any HTTP connection or browser staying open.
  Shutting down cancels the tasks; every run stays resumable from its event log, and `recover`
  restarts runs that were interrupted (not those waiting for approval).
- `EventBus` is a runner observer: it records each run's latest version and wakes anyone waiting
  for new events (Server-Sent Events, SDK `wait`, CLI `follow`).

One process, one supervisor: the single-writer assumption of the stores (ADR-0004) is unchanged.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from hollis.runtime.events import EventEnvelope
from hollis.runtime.runner import Runner
from hollis.runtime.state import RunState

log = logging.getLogger("hollis.platform")


class EventBus:
    def __init__(self) -> None:
        self._versions: dict[UUID, int] = {}
        self._condition = asyncio.Condition()

    async def publish(self, event: EventEnvelope, state: RunState) -> None:
        async with self._condition:
            self._versions[event.run_id] = event.sequence
            self._condition.notify_all()

    async def notify(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    def version(self, run_id: UUID) -> int:
        """The latest event sequence published for a run in this process (0 if none)."""
        return self._versions.get(run_id, 0)

    async def wait_until(self, predicate: Callable[[], bool], timeout_s: float) -> bool:
        """Wait until `predicate` holds, re-checking at every publish or notify (or until
        `timeout_s` passes). Returns the predicate's final value."""
        async with self._condition:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._condition.wait_for(predicate), timeout_s)
            return predicate()

    async def wait(self, run_id: UUID, after: int, timeout_s: float) -> bool:
        """Wait until the run has an event after `after` (or `timeout_s` passes). True if it has."""
        return await self.wait_until(lambda: self.version(run_id) > after, timeout_s)


type ErrorHandler = Callable[[UUID, str], Awaitable[None]]


class RunSupervisor:
    def __init__(
        self,
        runner: Callable[[], Runner],
        bus: EventBus,
        *,
        on_error: ErrorHandler | None = None,
        prepare: Callable[[UUID], Awaitable[None]] | None = None,
    ) -> None:
        """`runner` returns the current runner (configuration reloads replace it). `prepare` runs
        before each execution (e.g. connecting MCP servers a run needs)."""
        self._runner = runner
        self._bus = bus
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._on_error = on_error
        self._prepare = prepare
        self._closing = False

    def active(self, run_id: UUID) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    @property
    def active_count(self) -> int:
        return sum(1 for task in self._tasks.values() if not task.done())

    def schedule(self, run_id: UUID) -> bool:
        """Start driving a run unless it is already being driven. Returns True if started."""
        if self._closing or self.active(run_id):
            return False
        self._tasks[run_id] = asyncio.create_task(self._drive(run_id), name=f"hollis-run-{run_id}")
        return True

    async def wait_idle(self, run_id: UUID) -> None:
        task = self._tasks.get(run_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)

    async def cancel(self, run_id: UUID, *, by: str, reason: str) -> RunState:
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        state = await self._runner().cancel(run_id, by=by, reason=reason)
        await self._bus.notify()
        return state

    async def shutdown(self) -> None:
        self._closing = True
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    async def _drive(self, run_id: UUID) -> None:
        try:
            if self._prepare is not None:
                await self._prepare(run_id)
            await self._runner().execute(run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # surfaced on the run; the log keeps the run resumable
            message = f"{type(exc).__name__}: {exc}"[:500]
            log.warning("run %s stopped: %s", run_id, message)
            if self._on_error is not None:
                with contextlib.suppress(Exception):
                    await self._on_error(run_id, message)
        finally:
            self._tasks.pop(run_id, None)
            await self._bus.notify()
