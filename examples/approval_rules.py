"""Decide pending approvals with your own rules, as a principal with the approve permission.

Approves file writes under notes/ and denies everything else, recording why. Run it against a
server (HOLLIS_URL and HOLLIS_TOKEN) or a workspace directory:

    uv run python examples/approval_rules.py [WORKSPACE_DIR]

Hollis still checks every decision: the token's principal must be allowed to approve, and the
decision applies only to the exact call it names.
"""

import sys

from hollis import Hollis
from hollis.platform.schemas import Approval


def verdict(approval: Approval) -> tuple[bool, str]:
    arguments = approval.arguments if isinstance(approval.arguments, dict) else {}
    path = str(arguments.get("path", ""))
    if approval.tool == "fs_write_file" and path.startswith("notes/") and ".." not in path:
        return True, "writes under notes/ are allowed by approval_rules.py"
    return False, "only writes under notes/ are approved automatically"


def main(workspace: str | None) -> None:
    with Hollis.from_env(workspace) as hollis:
        pending = hollis.approvals.list()
        if not pending:
            print("nothing is waiting for approval")
        for approval in pending:
            approved, reason = verdict(approval)
            decide = hollis.approvals.approve if approved else hollis.approvals.deny
            decide(approval, reason=reason)
            word = "approved" if approved else "denied"
            print(f"{word} {approval.tool} in run {approval.run_id}")
            # Embedded, the run continues in this process: wait before exiting. With a server,
            # it continues there either way.
            summary = hollis.runs.get(approval.run_id).wait(timeout_s=60)
            print(f"  run is now {summary.phase.value}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
