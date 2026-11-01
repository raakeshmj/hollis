#!/usr/bin/env bash
# The CLI in scripts: JSON output and exit codes. Needs jq.
#   cd my-workspace && bash path/to/examples/scripting.sh
set -euo pipefail

# `--approve stop` leaves a run waiting and exits 12; that is expected here.
detail=$(hollis --json run --approve stop "write notes/today.txt: shipped" || true)
run_id=$(jq -r .summary.run_id <<<"$detail")
echo "run $run_id"

set +e
hollis runs wait "$run_id" >/dev/null
status=$?
set -e

case $status in
  0) echo "completed" ;;
  12)
    echo "waiting for approval:"
    hollis --json approvals list | jq -r '.[] | "  \(.tool) \(.arguments | tostring)"'
    ;;
  *) echo "run ended with exit code $status" ;;
esac
