#!/usr/bin/env bash
# Run one OSWorld-V2 environment-path task (no model) through its own namespaced
# E2B relay. validate_parallel.sh is the coordinator; it owns the host fleet
# proxy and sets OSWORLD_TASK_SERVICE_PORTS for the worker's isolation mode.
# Relay start-up, watchdog and process-group teardown come from worker_lib.sh.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../runner/worker_lib.sh"  # shared with the benchmark path
FULL_MANIFEST="${VALIDATION_MANIFEST:-$V2ROOT/validation/full-manifest.json}"
RAW_DIR="${RAW_DIR:-$REPO_ROOT/out/osworld-v2-raw/full-suite}"

: "${TASK_ID:?TASK_ID required}"
: "${PORT_BASE:?PORT_BASE required}"
: "${OUTPUT:?OUTPUT required}"

if [[ ! "$TASK_ID" =~ ^[0-9]{3}$ ]]; then
    echo "TASK_ID must be three digits (got: '$TASK_ID')" >&2
    exit 2
fi
if [[ ! "$PORT_BASE" =~ ^[0-9]+$ ]]; then
    echo "PORT_BASE must be a non-negative integer (got: '$PORT_BASE')" >&2
    exit 2
fi
require_immutable_guest_template
require_campaign_id
resolve_e2b_api_key

if ! python3 "$RUNNER_DIR/preflight.py" \
    --osworld-root "$OSWORLD_ROOT" --tasks-dir "$TASKS_DIR" \
    --services-dir "$SERVICES_DIR" --manifest "$FULL_MANIFEST" --task-id "$TASK_ID"; then
    exit 2
fi

namespace_relay "$PORT_BASE"
export_fleet_wiring

WORKER_DIR="$RAW_DIR/task_${TASK_ID}"
WORKER_MANIFEST="$WORKER_DIR/manifest.json"
RELAY_LOG="$WORKER_DIR/relay.log"
mkdir -p "$WORKER_DIR" "$(dirname "$OUTPUT")"
python3 - "$FULL_MANIFEST" "$TASK_ID" "$WORKER_MANIFEST" <<'PY'
import json, sys
source, task_id, output = sys.argv[1:]
manifest = json.load(open(source))
selected = [item for item in manifest["tasks"] if item["id"] == task_id]
if len(selected) != 1:
    raise SystemExit(f"expected exactly one manifest item for {task_id}, found {len(selected)}")
manifest["tasks"] = selected
with open(output, "w") as stream:
    json.dump(manifest, stream, indent=2, sort_keys=True)
    stream.write("\n")
PY

# Never allow a failed worker to leave a stale receipt that the aggregate gate
# could mistake for this run.
rm -f "$OUTPUT" "${OUTPUT%.json}.jsonl"

install_worker_traps
if ! start_relay "$RELAY_LOG"; then
    echo "[task $TASK_ID] relay did not become ready on $CONTROL_PORT" >&2
    exit 1
fi

start_in_new_session "$OSWORLD_ROOT" "${WORKER_UV[@]}" python "$HERE/harness.py" \
    --osworld-root "$OSWORLD_ROOT" \
    --tasks-dir "$TASKS_DIR" \
    --manifest "$WORKER_MANIFEST" \
    --raw-dir "$WORKER_DIR" \
    --output "$OUTPUT" &
rollout_pid=$!
# The harness bounds each task itself (TASK_TIMEOUT_SECONDS); this outer
# deadline only catches a harness that never returns.
wait_for_rollout "$AGENT_TASK_TIMEOUT_SECONDS"
status=$?
echo "[task $TASK_ID] harness exit=${status}"
exit "$status"
