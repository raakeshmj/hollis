import { useApprovals } from "../api/hooks";
import { ApprovalItem } from "../components/runs";
import { Async, Empty, PageHead } from "../components/ui";
import { canApprove, useMe } from "../lib/session";

export function Approvals() {
  const approvals = useApprovals();
  const me = useMe();
  return (
    <>
      <PageHead
        title="Approvals"
        description={
          canApprove(me)
            ? "Tool calls the policy holds for a person. Each decision is recorded with your name and applies only to that exact call; a run waits until someone decides or the request expires."
            : "Tool calls waiting in runs you can see. Someone with the approve permission decides them."
        }
      />
      <Async
        query={approvals}
        empty={(items) =>
          items.length === 0 ? (
            <Empty title="Nothing is waiting">When a run calls a tool that needs approval, it appears here.</Empty>
          ) : null
        }
      >
        {(items) => (
          <div>
            {items.map((approval) => (
              <ApprovalItem key={approval.binding} approval={approval} />
            ))}
          </div>
        )}
      </Async>
    </>
  );
}
