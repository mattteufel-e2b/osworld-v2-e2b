#!/usr/bin/env bash
# Run one OSWorld-V2 environment-path task through its own namespaced E2B relay.
# validate_parallel.sh is the coordinator; it owns the host fleet proxy and sets
# OSWORLD_TASK_SERVICE_PORTS for the worker's isolation mode.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
V2ROOT="$(cd "$HERE/.." && pwd)"
REPO_ROOT="$V2ROOT"
OSWORLD_ROOT="${OSWORLD_ROOT:-$V2ROOT/OSWorld-V2}"
TASKS_DIR="${OSWORLD_TASKS_DIR:-$V2ROOT/tasks}"
SERVICES_DIR="${OSWORLD_SERVICES_DIR:-$V2ROOT/services}"
FULL_MANIFEST="${VALIDATION_MANIFEST:-$V2ROOT/validation/full-manifest.json}"
RAW_DIR="${RAW_DIR:-$REPO_ROOT/out/osworld-v2-raw/full-suite}"
UV="uv run --python 3.12 --with e2b==2.34.0 --with aiohttp==3.14.1"

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
if [[ ! "${GUEST_TEMPLATE:-}" =~ ^[a-z0-9-]+:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
    echo "GUEST_TEMPLATE must be an immutable name:build_id reference" >&2
    exit 2
fi
export GUEST_TEMPLATE
if [ -z "${OSWORLD_CAMPAIGN_ID:-}" ]; then echo "OSWORLD_CAMPAIGN_ID is required" >&2; exit 2; fi
export OSWORLD_CAMPAIGN_ID

if [ -z "${E2B_API_KEY:-}" ] && [ -f "$REPO_ROOT/.env.local" ]; then
    export E2B_API_KEY="$(grep '^E2B_API_KEY=' "$REPO_ROOT/.env.local" | cut -d= -f2)"
fi
if [ -z "${E2B_API_KEY:-}" ]; then echo "E2B_API_KEY is required" >&2; exit 2; fi

if ! python3 "$HERE/preflight.py" \
    --osworld-root "$OSWORLD_ROOT" --tasks-dir "$TASKS_DIR" \
    --services-dir "$SERVICES_DIR" --manifest "$FULL_MANIFEST" --task-id "$TASK_ID"; then
    exit 2
fi

export OSWORLD_RELAY_PORT_BASE="$PORT_BASE"
CONTROL_PORT=$((14999 + PORT_BASE))
export E2B_RELAY_CONTROL_URL="http://127.0.0.1:${CONTROL_PORT}"

read -r WEBSITE_HOST_SUFFIX GITLAB_URL < <(python3 - "$SERVICES_DIR/.runtime.json" <<'PY'
import json, sys
rt = json.load(open(sys.argv[1]))
print(rt["websites"]["public_host_suffix"], rt["gitlab"]["url"])
PY
)
export WEBSITE_HOST_SUFFIX GITLAB_URL
export GITLAB_PRIVATE_TOKEN="$(cat "$SERVICES_DIR/.gitlab-token")"
export OSWORLD_FILE_BASE_URL="$TASKS_DIR/assets"
export HOSTMAP_PROXY_SCRIPT="$SERVICES_DIR/hostmap_proxy.py"
export OSWORLD_FLEET_RULES="$SERVICES_DIR/.runtime.json"

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

relay_pid=""
cleanup() {
    curl -fsS -X POST "http://127.0.0.1:${CONTROL_PORT}/stop" >/dev/null 2>&1 || true
    if [ -n "$relay_pid" ]; then wait "$relay_pid" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

( cd "$OSWORLD_ROOT" && $UV --locked --extra full python e2b_relay.py ) 2>"$RELAY_LOG" &
relay_pid=$!
ready=0
for _ in $(seq 1 180); do
    if curl -fsS "http://127.0.0.1:${CONTROL_PORT}/health" >/dev/null 2>&1; then
        ready=1
        break
    fi
    if ! kill -0 "$relay_pid" 2>/dev/null; then break; fi
    sleep 2
done
if [ "$ready" -ne 1 ]; then
    echo "[task $TASK_ID] relay did not become ready on $CONTROL_PORT" >&2
    tail -40 "$RELAY_LOG" >&2
    exit 1
fi

( cd "$OSWORLD_ROOT" && $UV --locked --extra full python "$HERE/harness.py" \
    --osworld-root "$OSWORLD_ROOT" \
    --tasks-dir "$TASKS_DIR" \
    --manifest "$WORKER_MANIFEST" \
    --raw-dir "$WORKER_DIR" \
    --output "$OUTPUT" )
