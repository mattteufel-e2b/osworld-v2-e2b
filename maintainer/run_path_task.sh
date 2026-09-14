#!/usr/bin/env bash
# Run one OSWorld-V2 environment-path task (no model). validate_parallel.sh is
# the coordinator; it owns the host fleet proxy and sets
# OSWORLD_TASK_SERVICE_PORTS for the worker's isolation mode. The harness's
# E2BProvider owns its own in-process bridge; nothing else to start or stop.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../runner/common.sh"
FULL_MANIFEST="${VALIDATION_MANIFEST:-$V2ROOT/validation/full-manifest.json}"
RAW_DIR="${RAW_DIR:-$REPO_ROOT/out/osworld-v2-raw/full-suite}"

: "${TASK_ID:?TASK_ID required}"
: "${OUTPUT:?OUTPUT required}"

if [[ ! "$TASK_ID" =~ ^[0-9]{3}$ ]]; then
    echo "TASK_ID must be three digits (got: '$TASK_ID')" >&2
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
export_fleet_wiring

RAW_DIR="$(abspath "$RAW_DIR")"
OUTPUT="$(abspath "$OUTPUT")"
FULL_MANIFEST="$(abspath "$FULL_MANIFEST")"
WORKER_DIR="$RAW_DIR/task_${TASK_ID}"
WORKER_MANIFEST="$WORKER_DIR/manifest.json"
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

cd "$OSWORLD_ROOT" || exit 1
"${WORKER_UV[@]}" python "$HERE/harness.py" \
    --osworld-root "$OSWORLD_ROOT" \
    --tasks-dir "$TASKS_DIR" \
    --manifest "$WORKER_MANIFEST" \
    --raw-dir "$WORKER_DIR" \
    --output "$OUTPUT"
status=$?
echo "[task $TASK_ID] harness exit=${status}"
exit "$status"
