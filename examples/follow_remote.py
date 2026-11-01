"""Start a run on a Hollis server and follow its events live (asynchronous SDK).

    export HOLLIS_URL=http://127.0.0.1:7878 HOLLIS_TOKEN=hx_...
    uv run python examples/follow_remote.py "list the files"

The run executes in the server: interrupt this script and the run keeps going. If the
connection drops, the stream resumes after the last event it saw.
"""

import asyncio
import os
import sys

from hollis import AsyncHollis


async def main(task: str) -> None:
    async with AsyncHollis.connect(os.environ["HOLLIS_URL"], os.environ["HOLLIS_TOKEN"]) as hollis:
        run = await hollis.runs.start(task)
        print(f"run {run.id}")
        async for event in run.stream():
            print(f"{event.sequence:>3}  {event.type:<26} {event.summary}")
            if (
                event.type == "tool_call_decided"
                and event.payload.get("decision") == "require_approval"
            ):
                print(
                    "     waiting for a decision: web app, `hollis approvals` or approval_rules.py"
                )
        print((await run.detail()).answer)


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "list the files"))
